"""Shared production Luna configuration and bounded structured-output transport.

Callers own task-specific prompts/schemas, provenance, budgets and durable state.
"""
import http.client
import hashlib
import json
import re
import urllib.error
import urllib.request


MODEL_NAME = 'gpt-5.6-luna'
MODEL_VERSION = '2026-07-09'
DEPLOYMENT = 'gpt-5.6-luna'
TIMEOUT = 30
MAX_RESPONSE_BYTES = 24000
MAX_INPUT_TOKENS = 922000


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


def request_payload(messages, schema, *, name, max_completion_tokens):
    return {'model': DEPLOYMENT, 'reasoning_effort': 'none',
            'max_completion_tokens': max_completion_tokens, 'messages': messages,
            'response_format': {'type': 'json_schema', 'json_schema': {
                'name': name, 'strict': True, 'schema': schema}}}


def request_budget(messages, schema, *, name, max_completion_tokens):
    if type(max_completion_tokens) is not int or not 1 <= max_completion_tokens <= 128000:
        raise AzureFailure('azure_input_limit')
    images = False
    if not isinstance(messages, list):
        raise AzureFailure('azure_input_limit')
    for message in messages:
        if not isinstance(message, dict) or not isinstance(message.get('content'), (str, list)):
            raise AzureFailure('azure_input_limit')
        if isinstance(message['content'], list):
            for part in message['content']:
                if not isinstance(part, dict) or part.get('type') not in ('text', 'image_url'):
                    raise AzureFailure('azure_input_limit')
                images = images or part['type'] == 'image_url'
    raw = json.dumps(request_payload(messages, schema, name=name,
                                     max_completion_tokens=max_completion_tokens)).encode('utf-8')
    # Escaped full wire text bounds byte-tokenization, including the schema and
    # metadata, plus the existing conservative 512-token framing reserve.
    bound = MAX_INPUT_TOKENS if images else len(raw) + 512
    if bound > MAX_INPUT_TOKENS:
        raise AzureFailure('azure_input_limit')
    return {'payloadHash': hashlib.sha256(raw).hexdigest(), 'inputCeiling': bound,
            'outputCeiling': max_completion_tokens, 'imageInput': images}


class AzureOpenAI:
    def __init__(self, environment, *, on_http_failure, opener=None, usage=None):
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
        self.usage = usage
        self.opener = opener or urllib.request.build_opener(NoRedirect())

    def fail_http(self, status, retry_after):
        self.on_http_failure(status, retry_after)
        raise AzureFailure('azure_http_error', status)

    def structured(self, messages, schema, *, name, max_completion_tokens):
        payload = request_payload(messages, schema, name=name, max_completion_tokens=max_completion_tokens)
        budget = request_budget(messages, schema, name=name, max_completion_tokens=max_completion_tokens)
        request = urllib.request.Request(
            self.url, data=json.dumps(payload).encode('utf-8'),
            headers={'Content-Type': 'application/json', 'api-key': self._key})
        if self.usage is None:
            raise AzureFailure('azure_budget_exhausted')
        self.money_call('authorize_http', self.identity, budget)
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
            self.money_call('record_usage', envelope)
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

    def money_call(self, method, *args):
        try:
            return getattr(self.usage, method)(*args)
        except self.usage.failure_type as exc:
            raise AzureFailure(exc.reason, exc.status, exc.retry_at) from None
