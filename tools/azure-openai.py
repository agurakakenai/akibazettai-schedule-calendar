"""Shared production Luna configuration and bounded structured-output transport.

Callers own task-specific prompts/schemas, provenance, budgets and durable state.
"""
import http.client
import json
import re
import urllib.error
import urllib.request


MODEL_NAME = 'gpt-5.6-luna'
MODEL_VERSION = '2026-07-09'
DEPLOYMENT = 'gpt-5.6-luna'
TIMEOUT = 30
MAX_RESPONSE_BYTES = 24000


class AzureFailure(Exception):
    def __init__(self, reason, status=None, retry_at=None):
        super().__init__(reason)
        self.reason, self.status, self.retry_at = reason, status, retry_at

    def facts(self):
        value = {'reason': self.reason}
        if self.status is not None:
            value['httpStatus'] = self.status
        if self.retry_at is not None:
            value['retryAt'] = self.retry_at
        return value


def strict_json(raw):
    def pairs(items):
        value = {}
        for key, child in items:
            if key in value:
                raise ValueError('duplicate_key')
            value[key] = child
        return value

    def invalid(_):
        raise ValueError('invalid_constant')

    return json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise AzureFailure('azure_http_error')


class AzureOpenAI:
    def __init__(self, environment, *, on_http_failure, opener=None):
        endpoint = environment.get('AZURE_OPENAI_ENDPOINT', '').rstrip('/')
        deployment = environment.get('AZURE_OPENAI_DEPLOYMENT', DEPLOYMENT)
        key = environment.get('AZURE_OPENAI_API_KEY', '')
        if (not re.fullmatch(r'https://[a-z0-9-]+\.openai\.azure\.com', endpoint)
                or deployment != DEPLOYMENT or not key or re.search(r'[\r\n]', key)):
            raise ValueError('invalid_azure_configuration')
        self.url = endpoint + '/openai/v1/chat/completions'
        self.deployment, self._key = deployment, key
        self.identity = {'provider': 'azure_openai', 'endpoint': endpoint,
                         'deployment': deployment, 'model': MODEL_NAME, 'modelVersion': MODEL_VERSION}
        self.on_http_failure = on_http_failure
        self.opener = opener or urllib.request.build_opener(NoRedirect())

    def fail_http(self, status, retry_after):
        self.on_http_failure(status, retry_after)
        raise AzureFailure('azure_http_error', status)

    def structured(self, messages, schema, *, name, max_completion_tokens):
        payload = {
            'model': self.deployment, 'reasoning_effort': 'none',
            'max_completion_tokens': max_completion_tokens, 'messages': messages,
            'response_format': {'type': 'json_schema', 'json_schema': {
                'name': name, 'strict': True, 'schema': schema}},
        }
        request = urllib.request.Request(
            self.url, data=json.dumps(payload).encode('utf-8'),
            headers={'Content-Type': 'application/json', 'api-key': self._key})
        try:
            with self.opener.open(request, timeout=TIMEOUT) as response:
                if response.getcode() != 200:
                    self.fail_http(response.getcode(), response.headers.get('Retry-After'))
                raw = response.read(MAX_RESPONSE_BYTES + 1)
                if len(raw) > MAX_RESPONSE_BYTES:
                    raise AzureFailure('azure_invalid_output')
        except urllib.error.HTTPError as exc:
            status, retry = exc.code, exc.headers.get('Retry-After') if exc.headers else None
            exc.close()
            self.fail_http(status, retry)
        except TimeoutError:
            raise AzureFailure('azure_timeout') from None
        except (OSError, http.client.HTTPException):
            raise AzureFailure('azure_network_error') from None
        try:
            envelope = strict_json(raw)
            choices = envelope['choices']
            if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
                raise ValueError
            model, version = self.identity['model'], self.identity['modelVersion']
            if envelope.get('model') not in (model, model + '-' + version):
                raise AzureFailure('azure_model_mismatch')
            choice = choices[0]
            message = choice['message']
            if not isinstance(message, dict):
                raise ValueError
            if message.get('refusal'):
                raise AzureFailure('azure_refused')
            if (choice['finish_reason'] != 'stop' or message.get('tool_calls')
                    or message.get('function_call') or not isinstance(message['content'], str)):
                raise ValueError
            return strict_json(message['content'])
        except (KeyError, TypeError, ValueError, UnicodeError, RecursionError):
            raise AzureFailure('azure_invalid_output') from None
