"""Pure routing of scheduled events using GitHub's run creation timestamp."""
import calendar
import datetime as dt


UTC = dt.timezone.utc
JST = dt.timezone(dt.timedelta(hours=9))
OFFICIAL_SCHEDULE = '30 3-6,8-11 * * *'
PERSONAL_SCHEDULES = {
    '0 16 * * *': 16,
    '0 22 * * *': 22,
    '0 1 * * *': 1,
    '0 3 * * *': 3,
}
HALF_MONTH_SCHEDULES = {
    '30 15 * * *': 15,
    '30 21 12,28-31 * *': 21,
    '30 2 1,13 * *': 2,
}
HALF_MONTH_START = dt.datetime(2026, 10, 1, 0, 30, tzinfo=JST)


def timestamp(value):
    try:
        result = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
    except (ValueError, AttributeError):
        raise ValueError('invalid_scheduled_event_time') from None
    if result.tzinfo is None:
        raise ValueError('scheduled_event_timestamp_requires_timezone')
    return result.astimezone(UTC)


def half_month_period(day):
    first = day.day < 13
    return {'from': day.replace(day=1 if first else 16).isoformat(),
            'to': day.replace(day=15 if first else calendar.monthrange(day.year, day.month)[1]).isoformat()}


def event_slot(schedule, created_at):
    created = timestamp(created_at)
    if schedule == OFFICIAL_SCHEDULE:
        kind, hours, minute = 'official', (3, 4, 5, 6, 8, 9, 10, 11), 30
    elif schedule in PERSONAL_SCHEDULES:
        kind, hours, minute = 'personal', (PERSONAL_SCHEDULES[schedule],), 0
    elif schedule in HALF_MONTH_SCHEDULES:
        kind, hours, minute = 'half-month', (HALF_MONTH_SCHEDULES[schedule],), 30
    else:
        raise ValueError('unknown_collection_schedule')
    candidates = []
    for ago in (0, 1):
        date = (created - dt.timedelta(days=ago)).date()
        if schedule == '30 21 12,28-31 * *' and date.day not in (12, 28, 29, 30, 31):
            continue
        if schedule == '30 2 1,13 * *' and date.day not in (1, 13):
            continue
        for hour in hours:
            candidate = dt.datetime.combine(date, dt.time(hour, minute), UTC)
            if candidate <= created and created - candidate < dt.timedelta(days=1):
                candidates.append(candidate)
    if not candidates:
        raise ValueError('scheduled_event_outside_valid_slot')
    scheduled = max(candidates).astimezone(JST)
    reason = None
    if kind == 'half-month':
        if schedule != '30 15 * * *' and scheduled.day not in (1, 13):
            reason = 'not_half_month_extra_day'
        elif scheduled < HALF_MONTH_START:
            reason = 'half_month_automation_not_started'
    return {
        'kind': kind,
        'slotId': kind + ':' + scheduled.isoformat(timespec='minutes'),
        'scheduledAt': scheduled.astimezone(UTC).isoformat().replace('+00:00', 'Z'),
        'serviceDate': scheduled.date().isoformat(),
        'shouldCollect': reason is None,
        'reason': reason,
        'period': half_month_period(scheduled.date()) if kind == 'half-month' else None,
    }
