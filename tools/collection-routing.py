"""Select an explicit collector mode from the event, never the runner's clock."""
import json
import os
from pathlib import Path
import sys


LEGACY_SCHEDULE = '30 3-6,8-11 * * *'
MANUAL_MODES = frozenset(('collect', 'personal', 'both', 'apply-saved', 'cost-sync'))


def collection_mode(event_name, requested_mode='', schedule='', enabled=False):
    if event_name == 'schedule':
        if schedule != LEGACY_SCHEDULE:
            raise ValueError('unknown_collection_schedule')
        return 'daily-guidance' if enabled is True else 'collect'
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
        if os.environ.get('GITHUB_OUTPUT'):
            with Path(os.environ['GITHUB_OUTPUT']).open('a', encoding='utf-8', newline='\n') as target:
                target.write('collectionMode=' + mode + '\n')
        print(json.dumps({'collectionMode': mode}))
        return 0
    except (ValueError, OSError) as exc:
        reason = str(exc) if isinstance(exc, ValueError) else 'routing_output_failed'
        print(json.dumps({'collectionStatus': 'failed', 'reason': reason}))
        return 1


if __name__ == '__main__':
    sys.exit(main())
