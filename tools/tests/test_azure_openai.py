"""Offline shared production model/transport checks; no live requests."""
import importlib.util
import json
from pathlib import Path
import unittest
from unittest import mock
import urllib.request

spec = importlib.util.spec_from_file_location('shared_azure_test', Path(__file__).parents[1] / 'azure-openai.py')
azure = importlib.util.module_from_spec(spec)
spec.loader.exec_module(azure)
ENV = {'AZURE_OPENAI_ENDPOINT': 'https://offline.openai.azure.com/',
       'AZURE_OPENAI_API_KEY': 'OFFLINE_KEY'}


class TransportTests(unittest.TestCase):
    def setUp(self):
        patch = mock.patch.object(urllib.request.OpenerDirector, 'open',
                                  side_effect=AssertionError('live network forbidden'))
        patch.start()
        self.addCleanup(patch.stop)
        self.opener = mock.Mock()
        self.failures = mock.Mock()
        self.client = azure.AzureOpenAI(ENV, on_http_failure=self.failures, opener=self.opener)

    def reply(self, model='gpt-5.6-luna-2026-07-09', **extra):
        response = mock.MagicMock()
        response.getcode.return_value = 200
        response.__enter__.return_value = response
        response.read.return_value = json.dumps({
            'model': model, 'choices': [{'finish_reason': 'stop',
                                        'message': {'content': '{"ok":true}', **extra}}]}).encode()
        self.opener.open.return_value = response
        return response

    def test_shared_stable_luna_default_and_no_other_deployment(self):
        self.assertEqual(self.client.deployment, 'gpt-5.6-luna')
        for deployment in ('', 'gpt-5.4-nano', 'gpt-5.4-mini-compare', 'gpt-5.6-luna-compare', 'anything'):
            with self.subTest(deployment=deployment), self.assertRaisesRegex(ValueError, 'invalid_azure_configuration'):
                azure.AzureOpenAI({**ENV, 'AZURE_OPENAI_DEPLOYMENT': deployment}, on_http_failure=self.failures)
        for endpoint in ('http://offline.openai.azure.com', 'https://example.com', ENV['AZURE_OPENAI_ENDPOINT'] + 'path'):
            with self.assertRaises(ValueError):
                azure.AzureOpenAI({**ENV, 'AZURE_OPENAI_ENDPOINT': endpoint}, on_http_failure=self.failures)

    def test_task_specific_messages_and_schema_are_not_reinterpreted(self):
        self.reply()
        messages = [{'role': 'system', 'content': 'task-owned prompt'}, {'role': 'user', 'content': 'untrusted input'}]
        schema = {'type': 'object', 'properties': {'ok': {'type': 'boolean'}},
                  'required': ['ok'], 'additionalProperties': False}
        self.assertEqual(self.client.structured(messages, schema, name='task_output', max_completion_tokens=1200),
                         {'ok': True})
        request = self.opener.open.call_args.args[0]
        body = json.loads(request.data)
        self.assertEqual(request.full_url, 'https://offline.openai.azure.com/openai/v1/chat/completions')
        self.assertEqual(body, {
            'model': 'gpt-5.6-luna', 'reasoning_effort': 'none', 'max_completion_tokens': 1200,
            'messages': messages, 'response_format': {
                'type': 'json_schema', 'json_schema': {'name': 'task_output', 'strict': True, 'schema': schema}}})
        self.assertEqual(self.opener.open.call_args.kwargs['timeout'], 30)
        self.assertNotIn('OFFLINE_KEY', json.dumps(body))
        self.assertNotIn('OFFLINE_KEY', json.dumps(self.client.identity))

    def test_wrong_or_missing_reported_model_never_enters_the_cache_as_luna(self):
        for model in ('gpt-5.4-nano', 'gpt-5.6-luna-2026-08-01', None):
            self.reply(model)
            with self.subTest(model=model), self.assertRaisesRegex(azure.AzureFailure, 'azure_model_mismatch'):
                self.client.structured([], {}, name='task', max_completion_tokens=1)

    def test_http_failure_callback_cannot_silently_return_success(self):
        response = self.reply()
        response.getcode.return_value = 403
        response.headers = {'Retry-After': '60'}
        with self.assertRaisesRegex(azure.AzureFailure, 'azure_http_error'):
            self.client.structured([], {}, name='task', max_completion_tokens=1)
        self.failures.assert_called_once_with(403, '60')
        response.read.assert_not_called()

    def test_refusal_tools_timeout_and_size_remain_bounded(self):
        self.reply(refusal='refused')
        with self.assertRaisesRegex(azure.AzureFailure, 'azure_refused'):
            self.client.structured([], {}, name='task', max_completion_tokens=1)
        self.reply(tool_calls=[{}])
        with self.assertRaisesRegex(azure.AzureFailure, 'azure_invalid_output'):
            self.client.structured([], {}, name='task', max_completion_tokens=1)
        response = self.reply()
        response.read.return_value = b'x' * (azure.MAX_RESPONSE_BYTES + 1)
        with self.assertRaisesRegex(azure.AzureFailure, 'azure_invalid_output'):
            self.client.structured([], {}, name='task', max_completion_tokens=1)
        self.opener.open.side_effect = TimeoutError()
        with self.assertRaisesRegex(azure.AzureFailure, 'azure_timeout'):
            self.client.structured([], {}, name='task', max_completion_tokens=1)

    def test_redirect_is_not_followed(self):
        with self.assertRaises(azure.AzureFailure):
            azure.NoRedirect().redirect_request(None, None, 302, '', {}, 'https://example.com')


if __name__ == '__main__':
    unittest.main()
