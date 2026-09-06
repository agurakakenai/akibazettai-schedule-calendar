"""Select an explicit collector mode from the event, never the runner's clock.

Future schedule entries are recognized here but are NOT enabled by this module.
Enabling them in the workflow requires a separate change after manual main runs.
"""
import json
import os
from pathlib import Path
import sys


LEGACY_SCHEDULE = '30 3-6,8-11 * * *'
SCHEDULE_MODES = {
    LEGACY_SCHEDULE: 'collect',
    '30 3-6,8-10 * * *': 'both',
    '30 11 * * *': 'collect',
    '30 0-2,7,15-23 * * *': 'personal',
}
MANUAL_MODES = frozenset(('collect', 'personal', 'both'))


def collection_mode(event_name, requested_mode='', schedule=''):
    if event_name == 'schedule':
        if schedule not in SCHEDULE_MODES:
            raise ValueError('unknown_collection_schedule')
        return SCHEDULE_MODES[schedule]
    if event_name == 'workflow_dispatch' and requested_mode in MANUAL_MODES:
        return requested_mode
    raise ValueError('unsupported_collection_event')


def main():
    try:
        mode = collection_mode(
            os.environ.get('EVENT_NAME', ''),
            os.environ.get('REQUESTED_MODE', ''),
            os.environ.get('EVENT_SCHEDULE', ''))
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
