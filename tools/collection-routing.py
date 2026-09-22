"""Select an explicit collector mode from the event, never the runner's clock."""
import json
import importlib.util
import os
from pathlib import Path
import sys


LEGACY_SCHEDULE = '30 3-6,8-11 * * *'
MANUAL_MODES = frozenset(('collect', 'personal', 'both', 'schedule', 'apply-saved', 'cost-sync'))
SPEC = importlib.util.spec_from_file_location('collection_slots', Path(__file__).with_name('collection-slots.py'))
slots = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(slots)


def collection_mode(event_name, requested_mode='', schedule='', enabled=False):
    if event_name == 'schedule':
        if schedule in slots.PERSONAL_SCHEDULES:
            return 'personal'
        if schedule in slots.HALF_MONTH_SCHEDULES:
            return 'schedule'
        if schedule != LEGACY_SCHEDULE:
            raise ValueError('unknown_collection_schedule')
        return 'collect'
    if event_name == 'workflow_dispatch' and requested_mode in MANUAL_MODES:
        return requested_mode
    raise ValueError('unsupported_collection_event')


def main():
    try:
        mode = collection_mode(
            os.environ.get('EVENT_NAME', ''),
            os.environ.get('REQUESTED_MODE', ''),
            os.environ.get('EVENT_SCHEDULE', ''),
            os.environ.get('DAILY_GUIDANCE_ENABLED', 'false') == 'true')
        result = {'collectionMode': mode, 'collectionKind': 'manual'}
        if os.environ.get('EVENT_NAME') == 'schedule':
            route = slots.event_slot(os.environ.get('EVENT_SCHEDULE', ''),
                                     os.environ.get('RUN_CREATED_AT', ''))
            result.update({
                'collectionKind': route['kind'],
                'collectionMode': mode if route['shouldCollect'] else 'restore',
                'collectionSlot': route['scheduledAt'],
                'collectionSlotId': route['slotId'],
                'collectionDate': route['serviceDate'],
            })
            if route['reason']:
                result['skipReason'] = route['reason']
        half_enabled = os.environ.get('HALF_MONTH_SCHEDULE_ENABLED', 'false') == 'true'
        if result['collectionMode'] == 'schedule' and os.environ.get('EVENT_NAME') == 'schedule' and not half_enabled:
            result.update(collectionMode='restore', skipReason='half_month_collection_disabled')
        half_requested = (result['collectionMode'] == 'schedule' or
                          result['collectionMode'] == 'both' and half_enabled)
        half_started = (half_requested and
                        slots.timestamp(os.environ.get('RUN_CREATED_AT', '')) >= slots.HALF_MONTH_START)
        result['halfMonthCacheRequired'] = 'true' if half_started else 'false'
        if result['collectionMode'] == 'schedule' and not half_started:
            result.update(collectionMode='restore', skipReason='half_month_automation_not_started')
        if os.environ.get('GITHUB_OUTPUT'):
            with Path(os.environ['GITHUB_OUTPUT']).open('a', encoding='utf-8', newline='\n') as target:
                for key, value in result.items():
                    target.write(key + '=' + value + '\n')
        print(json.dumps(result))
        return 0
    except (ValueError, OSError) as exc:
        reason = str(exc) if isinstance(exc, ValueError) else 'routing_output_failed'
        print(json.dumps({'collectionStatus': 'failed', 'reason': reason}))
        return 1


if __name__ == '__main__':
    sys.exit(main())
