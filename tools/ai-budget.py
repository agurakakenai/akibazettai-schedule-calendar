"""Integer JPY application-budget ceilings, not a guarantee of Azure invoices.

The ledger remains ai-usage.json. Retail JPY prices are pre-tax reference prices;
the expensive context/cache tier is deliberately retained.
"""
import datetime as dt
import email.utils
from decimal import Decimal
import hashlib
import json
import os
import re
import subprocess
import urllib.error
import urllib.request


LIMIT = 1_000_000_000  # micro-JPY; 1,000 JPY
JST = dt.timezone(dt.timedelta(hours=9))
PRICE_VERSION = 'azure-retail-jpy-2026-09-13-write4'
PRICE_SOURCE = 'https://prices.azure.com/api/retail/prices'
MODEL_SOURCE = 'https://learn.microsoft.com/en-us/azure/foundry/foundry-models/concepts/models-sold-directly-by-azure'
# JPY / million tokens: uncached input, cached read, additional cache write, output.
# Luna uses the more expensive LongCo tier and additive write charging.
PRICES = {
    ('gpt-5.6-luna', '2026-07-09'): ('63.728', '6.3728', '79.66', '286.776'),
    ('gpt-5.4-nano', '2026-03-17'): ('31.864', '3.1864', '0', '199.15'),
    ('gpt-5.4-mini', '2026-03-17'): ('119.49', '11.949', '0', '716.94'),
}
INPUT_MAX = 922_000
OUTPUT_MAX = 128_000
MAX_CACHE_WRITES = 4
RESOURCE_ID = ('/subscriptions/eb1d1a6f-a4b6-4c6f-885d-5ef15ed3bb64'
               '/resourceGroups/rg-akibazettai-ai/providers/Microsoft.CognitiveServices'
               '/accounts/aoai-akibazettai-nano')
HASH = re.compile(r'[0-9a-f]{64}\Z')


def require(condition):
    if not condition:
        raise ValueError('invalid_ai_money')


def keys(value, required):
    require(isinstance(value, dict) and set(value) == set(required))


def integer(value, minimum=0):
    require(type(value) is int and value >= minimum)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True,
                                     separators=(',', ':')).encode()).hexdigest()


def month(now):
    require(isinstance(now, dt.datetime) and now.tzinfo is not None)
    return now.astimezone(JST).strftime('%Y-%m')


def empty():
    return {'schemaVersion': 1, 'currency': 'JPY', 'limitMicroJPY': LIMIT,
            'priceVersion': PRICE_VERSION, 'opening': {},
            'billing': {'attemptedAt': None, 'lastSuccess': None, 'failure': None, 'retryAt': None,
                        'highWater': {}}}


def cost(model, version, prompt, cached, written, output):
    for value in (prompt, cached, written, output):
        integer(value)
    require(cached <= prompt and written <= MAX_CACHE_WRITES * prompt and (model, version) in PRICES)
    rates = [int(Decimal(value) * 1_000_000) for value in PRICES[model, version]]
    numerator = ((prompt - cached) * rates[0] + cached * rates[1]
                 + written * rates[2] + output * rates[3])
    denominator = 1_000_000
    return (numerator + denominator - 1) // denominator


def ceiling(model, version, input_tokens, output_tokens):
    integer(input_tokens, 1)
    integer(output_tokens, 1)
    return cost(model, version, input_tokens, 0, input_tokens * MAX_CACHE_WRITES, output_tokens)


def validate_request(value):
    keys(value, ('payloadHash', 'inputCeiling', 'outputCeiling', 'imageInput'))
    require(isinstance(value['payloadHash'], str) and HASH.fullmatch(value['payloadHash']))
    integer(value['inputCeiling'], 1)
    integer(value['outputCeiling'], 1)
    require(value['inputCeiling'] <= INPUT_MAX and value['outputCeiling'] <= OUTPUT_MAX
            and type(value['imageInput']) is bool
            and (not value['imageInput'] or value['inputCeiling'] == INPUT_MAX))


def reservation(identity, request):
    validate_request(request)
    model, version = identity['model'], identity['modelVersion']
    return {**request, 'priceVersion': PRICE_VERSION, 'model': model, 'modelVersion': version,
            'reservedMicroJPY': ceiling(model, version, request['inputCeiling'], request['outputCeiling']),
            'usage': None, 'chargedMicroJPY': None, 'usageStatus': 'reserved'}


def validate_charge(value):
    keys(value, ('payloadHash', 'inputCeiling', 'outputCeiling', 'imageInput', 'priceVersion',
                 'model', 'modelVersion', 'reservedMicroJPY', 'usage', 'chargedMicroJPY', 'usageStatus'))
    validate_request({key: value[key] for key in
                      ('payloadHash', 'inputCeiling', 'outputCeiling', 'imageInput')})
    require(value['priceVersion'] == PRICE_VERSION)
    require(value['reservedMicroJPY'] == ceiling(
        value['model'], value['modelVersion'], value['inputCeiling'], value['outputCeiling']))
    integer(value['reservedMicroJPY'], 1)
    if value['usage'] is None:
        require(value['chargedMicroJPY'] is None
                and value['usageStatus'] in ('reserved', 'usage_missing', 'usage_inconsistent'))
    else:
        require(value['usageStatus'] == 'settled')
        tokens = value['usage']
        keys(tokens, ('input', 'cachedRead', 'cachedWrite', 'output'))
        for count in tokens.values():
            integer(count)
        require(tokens['input'] <= value['inputCeiling'] and tokens['output'] <= value['outputCeiling'])
        expected = cost(value['model'], value['modelVersion'], tokens['input'],
                        tokens['cachedRead'], tokens['cachedWrite'], tokens['output'])
        integer(value['chargedMicroJPY'])
        require(value['chargedMicroJPY'] == expected <= value['reservedMicroJPY'])


class UsageMissing(Exception):
    pass


class UsageInconsistent(Exception):
    pass


def settle(charge, envelope):
    if not isinstance(envelope, dict) or envelope.get('model') not in (
            charge['model'], charge['model'] + '-' + charge['modelVersion']):
        raise UsageInconsistent()
    usage = envelope.get('usage')
    if usage is None:
        raise UsageMissing()
    if not isinstance(usage, dict):
        raise UsageInconsistent()
    details = usage.get('prompt_tokens_details')
    if details is not None and not isinstance(details, dict):
        raise UsageInconsistent()
    details = details or {}
    tokens = {'input': usage.get('prompt_tokens'), 'output': usage.get('completion_tokens'),
              'cachedRead': details.get('cached_tokens'), 'cachedWrite': details.get('cache_write_tokens')}
    for count in tokens.values():
        if count is not None and (type(count) is not int or count < 0):
            raise UsageInconsistent()
    for key, bound in (('input', charge['inputCeiling']), ('output', charge['outputCeiling'])):
        if tokens[key] is not None and tokens[key] > bound:
            raise UsageInconsistent()
    if tokens['input'] is not None:
        if (tokens['cachedRead'] is not None and tokens['cachedRead'] > tokens['input']
                or tokens['cachedWrite'] is not None and tokens['cachedWrite'] > MAX_CACHE_WRITES * tokens['input']):
            raise UsageInconsistent()
    total = usage.get('total_tokens')
    if total is not None and (type(total) is not int or total < 0):
        raise UsageInconsistent()
    if total is not None and tokens['input'] is not None and tokens['output'] is not None:
        if total != tokens['input'] + tokens['output']:
            raise UsageInconsistent()
    if total is None or any(value is None for value in tokens.values()):
        raise UsageMissing()
    result = {**charge, 'usage': tokens, 'chargedMicroJPY': cost(
        charge['model'], charge['modelVersion'], tokens['input'],
        tokens['cachedRead'], tokens['cachedWrite'], tokens['output']), 'usageStatus': 'settled'}
    validate_charge(result)
    return result


def record(state, key):
    kind, receipt_id = key.split(':', 1)
    require(kind in ('native', 'import'))
    return state['receipts' if kind == 'native' else 'imports'][receipt_id]


def validate(state):
    money = state['money']
    keys(money, ('schemaVersion', 'currency', 'limitMicroJPY', 'priceVersion', 'opening', 'billing'))
    require(type(money['schemaVersion']) is int and money['schemaVersion'] == 1
            and money['currency'] == 'JPY' and money['limitMicroJPY'] == LIMIT
            and type(money['limitMicroJPY']) is int and money['priceVersion'] == PRICE_VERSION)
    require(isinstance(money['opening'], dict))
    for period, opening in money['opening'].items():
        require(isinstance(period, str) and re.fullmatch(r'\d{4}-\d{2}', period))
        dt.date.fromisoformat(period + '-01')
        keys(opening, ('amountMicroJPY', 'basisHash', 'records', 'kind', 'throughDate', 'observedAt'))
        integer(opening['amountMicroJPY'], 1)
        require(opening['kind'] == 'provisional-azure-actual')
        timestamp(opening['observedAt'])
        require(dt.date.fromisoformat(opening['throughDate'])
                < timestamp(opening['observedAt']).astimezone(JST).date())
        require(isinstance(opening['basisHash'], str) and HASH.fullmatch(opening['basisHash']))
        require(isinstance(opening['records'], dict) and bool(opening['records']))
        for key, expected_hash in opening['records'].items():
            require(isinstance(key, str) and re.fullmatch(r'(native|import):[0-9a-f]{64}', key))
            receipt = record(state, key)
            require(receipt['date'].startswith(period + '-') and 'money' not in receipt
                    and expected_hash == digest(receipt))
            if key.startswith('native:'):
                require(receipt['completedAt'] is not None)
    billing = money['billing']
    keys(billing, ('attemptedAt', 'lastSuccess', 'failure', 'retryAt', 'highWater'))
    require(isinstance(billing['highWater'], dict))
    for day, amount in billing['highWater'].items():
        dt.date.fromisoformat(day)
        integer(amount)
    if billing['attemptedAt'] is not None:
        timestamp(billing['attemptedAt'])
    require(billing['failure'] in (None, 'auth_not_configured', 'auth_failed', 'query_failed',
                                    'invalid_response', 'currency_mismatch', 'scope_mismatch', 'rate_limited',
                                    'no_closed_usage_day'))
    if billing['retryAt'] is not None:
        timestamp(billing['retryAt'])
    if billing['lastSuccess'] is not None:
        value = billing['lastSuccess']
        keys(value, ('fetchedAt', 'from', 'to', 'resourceId', 'currency', 'preTaxMicroJPY',
                     'lastUsageDate', 'month', 'calendarBasis', 'dailyMicroJPY'))
        for field in ('fetchedAt', 'from', 'to'):
            timestamp(value[field])
        require(timestamp(value['from']) <= timestamp(value['to']) <= timestamp(value['fetchedAt']))
        require(value['resourceId'].lower() == RESOURCE_ID.lower() and value['currency'] == 'JPY'
                and value['calendarBasis'] == 'UTC-daily' and value['month'] == month(timestamp(value['fetchedAt'])))
        integer(value['preTaxMicroJPY'])
        require(isinstance(value['dailyMicroJPY'], dict)
                and sum(value['dailyMicroJPY'].values()) == value['preTaxMicroJPY'])
        for day, amount in value['dailyMicroJPY'].items():
            dt.date.fromisoformat(day)
            integer(amount)
            require(billing['highWater'].get(day, -1) >= amount)
        if value['lastUsageDate'] is not None:
            dt.date.fromisoformat(value['lastUsageDate'])
        require(billing['attemptedAt'] is not None)


def timestamp(value):
    require(isinstance(value, str) and value.endswith('Z'))
    result = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
    require(result.tzinfo is not None)
    return result


def balance(state, now, exclude=None):
    period = month(now)
    result = {'month': period, 'currency': 'JPY', 'limitMicroJPY': LIMIT,
              'openingProvisionalMicroJPY': 0, 'tokenPricedMicroJPY': 0, 'reservedMicroJPY': 0,
              'reconciledMicroJPY': 0,
              'utcBoundaryHoldMicroJPY': 0,
              'provisionalRequests': 0, 'unsettledUsageRecords': 0, 'inconsistentUsageRecords': 0,
              'openingThrough': None, 'openingObservedAt': None,
              'unknownRecords': 0, 'availableMicroJPY': 0, 'reason': 'monthly_accounting_required',
              'azurePreTaxMicroJPY': None, 'azureRemainingMicroJPY': None,
              'azureLastSuccessAt': None, 'azureAgeSeconds': None, 'azureFailure': None,
              'azureFrom': None, 'azureTo': None, 'azureLastUsageDate': None,
              'azureScope': 'resource-including-other-deployments'}
    if 'money' not in state:
        return result
    opening = state['money']['opening'].get(period, {})
    result['openingProvisionalMicroJPY'] = opening.get('amountMicroJPY', 0)
    covered = opening.get('records', {})
    result['inconsistentUsageRecords'] = sum(
        receipt.get('money', {}).get('usageStatus') == 'usage_inconsistent'
        for receipt in state['receipts'].values())
    result.update(openingThrough=opening.get('throughDate'), openingObservedAt=opening.get('observedAt'))
    result['provisionalRequests'] = sum(
        1 if key.startswith('native:') else record(state, key)['counts']['requests'] for key in covered)
    local_days = {}
    for kind, receipts in (('native', state['receipts']), ('import', state['imports'])):
        for key, receipt in receipts.items():
            if not receipt['date'].startswith(period + '-') or kind + ':' + key in covered:
                continue
            if kind == 'native' and key == exclude:
                continue
            charge = receipt.get('money')
            if charge is None:
                result['unknownRecords'] += 1
            elif charge['chargedMicroJPY'] is None:
                result['reservedMicroJPY'] += charge['reservedMicroJPY']
                result['unsettledUsageRecords'] += 1
            else:
                result['tokenPricedMicroJPY'] += charge['chargedMicroJPY']
                day = timestamp(receipt['issuedAt']).date().isoformat()
                local_days[day] = local_days.get(day, 0) + charge['chargedMicroJPY']
    billing = state['money']['billing']
    reported = {day: amount for day, amount in billing['highWater'].items() if day.startswith(period + '-')}
    boundary = (dt.date.fromisoformat(period + '-01') - dt.timedelta(days=1)).isoformat()
    if boundary in billing['highWater'] and not opening:
        # A UTC bucket straddles two JST months. Keep its unexplained remainder
        # as an explicit conservative hold, after removing already accounted
        # receipts on both sides; do not call the whole bucket this month's cost.
        accounted = 0
        for key, receipt in state['receipts'].items():
            charge = receipt.get('money')
            if key != exclude and charge and timestamp(receipt['issuedAt'] or receipt['reservedAt']).date().isoformat() == boundary:
                accounted += (charge['chargedMicroJPY'] if charge['chargedMicroJPY'] is not None
                              else charge['reservedMicroJPY'])
        result['utcBoundaryHoldMicroJPY'] = max(0, billing['highWater'][boundary] - accounted)
    through = opening.get('throughDate', '')
    baseline = max(result['openingProvisionalMicroJPY'],
                   sum(amount for day, amount in reported.items() if day <= through))
    # Match UTC billing days rather than adding the monthly invoice to the same
    # token receipts again. High-water observations never refund local usage.
    result['reconciledMicroJPY'] = baseline + sum(
        max(local_days.get(day, 0), reported.get(day, 0))
        for day in set(local_days) | set(reported) if day > through)
    result['reconciledMicroJPY'] += result['utcBoundaryHoldMicroJPY']
    spent = result['reconciledMicroJPY']
    result['availableMicroJPY'] = max(0, LIMIT - spent - result['reservedMicroJPY'])
    result['reason'] = ('monthly_usage_inconsistent' if result['inconsistentUsageRecords'] else
                        'monthly_cost_unknown' if result['unknownRecords'] else
                        'monthly_budget_exhausted' if not result['availableMicroJPY'] else 'ok')
    result['azureFailure'] = billing['failure']
    last = billing['lastSuccess']
    if last:
        result['azureLastSuccessAt'] = last['fetchedAt']
        result.update(azureFrom=last['from'], azureTo=last['to'], azureLastUsageDate=last['lastUsageDate'])
        result['azureAgeSeconds'] = max(0, int((now - timestamp(last['fetchedAt'])).total_seconds()))
        if last['month'] == period:
            result['azurePreTaxMicroJPY'] = last['preTaxMicroJPY']
            result['azureRemainingMicroJPY'] = max(0, LIMIT - last['preTaxMicroJPY'])
    if result['reason'] != 'ok':
        result['availableMicroJPY'] = 0
    return result


class BillingFailure(Exception):
    def __init__(self, reason, retry_at=None):
        super().__init__(reason)
        self.reason, self.retry_at = reason, retry_at


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise BillingFailure('query_failed')


def stamp(now):
    return now.astimezone(dt.timezone.utc).isoformat().replace('+00:00', 'Z')


def query_body(now):
    yesterday = now.astimezone(JST).date() - dt.timedelta(days=1)
    first_jst = now.astimezone(JST).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    boundary_day = first_jst.astimezone(dt.timezone.utc).date()
    if yesterday == boundary_day and now.astimezone(dt.timezone.utc).date() == boundary_day:
        raise BillingFailure('no_closed_usage_day')
    start = dt.datetime.combine(boundary_day, dt.time(), dt.timezone.utc)
    end = min(now, dt.datetime.combine(yesterday, dt.time(23, 59, 59), dt.timezone.utc))
    return {'type': 'ActualCost', 'timeframe': 'Custom',
            'timePeriod': {'from': stamp(start), 'to': stamp(end)},
            'dataset': {'granularity': 'Daily',
                        'aggregation': {'totalCost': {'name': 'PreTaxCost', 'function': 'Sum'}},
                        'filter': {'dimensions': {'name': 'ResourceId', 'operator': 'In',
                                                  'values': [RESOURCE_ID]}},
                        'grouping': [{'type': 'Dimension', 'name': 'ResourceId'}]}}


def parse_billing(response, body, now):
    try:
        properties = response['properties']
        require(properties.get('nextLink') in (None, ''))
        columns = [value['name'] for value in properties['columns']]
        require(len(columns) == len(set(columns)))
        require(set(('PreTaxCost', 'UsageDate', 'ResourceId', 'Currency')) <= set(columns))
        rows = properties['rows']
        require(isinstance(rows, list) and bool(rows))
        amounts = {}
        for raw in rows:
            require(isinstance(raw, list) and len(raw) == len(columns))
            row = dict(zip(columns, raw))
            if row['Currency'] != 'JPY':
                raise BillingFailure('currency_mismatch')
            if not isinstance(row['ResourceId'], str) or row['ResourceId'].lower() != RESOURCE_ID.lower():
                raise BillingFailure('scope_mismatch')
            require(type(row['UsageDate']) is int)
            day = dt.datetime.strptime(str(row['UsageDate']), '%Y%m%d').date()
            require(timestamp(body['timePeriod']['from']).date() <= day <= timestamp(body['timePeriod']['to']).date()
                    and day < now.astimezone(JST).date())
            value = row['PreTaxCost']
            require(isinstance(value, (Decimal, int)) and not isinstance(value, bool))
            amount = Decimal(value)
            require(amount.is_finite() and amount >= 0)
            amounts[day.isoformat()] = amounts.get(day.isoformat(), Decimal(0)) + amount
        daily = {day: int((amount * 1_000_000).to_integral_value(rounding='ROUND_CEILING'))
                 for day, amount in amounts.items()}
        return {'fetchedAt': stamp(now), **body['timePeriod'], 'resourceId': RESOURCE_ID,
                'currency': 'JPY', 'preTaxMicroJPY': sum(daily.values()), 'dailyMicroJPY': daily,
                'lastUsageDate': max(daily), 'month': month(now), 'calendarBasis': 'UTC-daily'}
    except (KeyError, TypeError, ValueError, ArithmeticError):
        raise BillingFailure('invalid_response') from None


def query_azure(environment, now):
    body = query_body(now)
    if environment.get('AZURE_COST_AUTHENTICATED') != 'true':
        raise BillingFailure('auth_not_configured')
    try:
        result = subprocess.run(
            ['az', 'account', 'get-access-token', '--resource', 'https://management.azure.com/',
             '--subscription', RESOURCE_ID.split('/')[2], '--output', 'json'],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding='utf-8', timeout=30, check=False,
            env={key: value for key, value in environment.items()
                 if not key.startswith(('AZURE_OPENAI_', 'ACTIONS_ID_TOKEN_'))
                 and key not in ('GH_TOKEN', 'GITHUB_TOKEN')},
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        if result.returncode:
            raise BillingFailure('auth_failed')
        token = json.loads(result.stdout)['accessToken']
        require(isinstance(token, str) and token and '\r' not in token and '\n' not in token)
    except (OSError, subprocess.TimeoutExpired, KeyError, TypeError, ValueError):
        raise BillingFailure('auth_failed') from None
    scope = RESOURCE_ID.split('/providers/')[0]
    request = urllib.request.Request(
        'https://management.azure.com' + scope
        + '/providers/Microsoft.CostManagement/query?api-version=2025-03-01',
        data=json.dumps(body).encode(), headers={'Authorization': 'Bearer ' + token,
                                                'Content-Type': 'application/json'})
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=30) as response:
            raw = response.read(1_000_001)
            if response.getcode() != 200 or len(raw) > 1_000_000:
                raise BillingFailure('invalid_response')
        finished = dt.datetime.now(dt.timezone.utc)
        if month(finished) != month(now):
            raise BillingFailure('invalid_response')
        return parse_billing(json.loads(raw, parse_float=Decimal), body, finished)
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            value = exc.headers.get('Retry-After')
            until = now + dt.timedelta(minutes=5)
            if value:
                try:
                    until = max(until, now + dt.timedelta(seconds=int(value)))
                except ValueError:
                    try:
                        until = max(until, email.utils.parsedate_to_datetime(value))
                    except (ValueError, TypeError):
                        pass
            exc.close()
            raise BillingFailure('rate_limited', stamp(until)) from None
        exc.close()
        raise BillingFailure('query_failed') from None
    except (OSError, TimeoutError):
        raise BillingFailure('query_failed') from None
    except (ValueError, UnicodeError):
        raise BillingFailure('invalid_response') from None


def sync(ledger, environment, *, query=query_azure):
    """Once per actual JST day, inside the existing lock and cloud lease."""
    now = ledger.clock()
    billing = ledger.state['money']['billing']
    attempted = billing['attemptedAt']
    if attempted and timestamp(attempted).astimezone(JST).date() == now.astimezone(JST).date():
        return False
    if billing['retryAt'] and timestamp(billing['retryAt']) > now:
        return False
    billing['attemptedAt'] = stamp(now)
    ledger._save()
    try:
        observation = query(environment, now)
    except BillingFailure as exc:
        billing['failure'], billing['retryAt'] = exc.reason, exc.retry_at
    else:
        previous, previous_high = billing['lastSuccess'], dict(billing['highWater'])
        billing.update(lastSuccess=observation, failure=None, retryAt=None)
        try:
            for day, amount in observation['dailyMicroJPY'].items():
                billing['highWater'][day] = max(billing['highWater'].get(day, 0), amount)
            validate(ledger.state)
        except (ValueError, KeyError, TypeError):
            billing.update(lastSuccess=previous, highWater=previous_high, failure='invalid_response')
    ledger._save()
    return True
