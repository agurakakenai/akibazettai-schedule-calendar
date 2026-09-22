"""Bounded correction checks; an omitted name never becomes an absence fact."""
import copy
import datetime as dt
import hashlib
import json


SHIFTS = ('昼', '夜')


def _time(post):
    return dt.datetime.fromisoformat(post['createdAt'].replace('Z', '+00:00')), int(post['id'])


def prepare(targets, observations, personal, date, *, required_stores, slot_id,
            checked=(), canonical=lambda name: name, name_corrections=None):
    day = dt.date.fromisoformat(date)
    if day.isoformat() != date or not required_stores or not slot_id:
        raise ValueError('invalid_correction_scope')
    stores = set(required_stores)
    if stores != {'s1', 's2', 's3', 's4'}:
        raise ValueError('invalid_correction_scope')
    corrections = name_corrections or {}
    posts = [post for post in observations.get('posts', []) if post['date'] == date]
    superseded = {tid for post in posts for tid in post.get('editTweetIds', [])[:-1]}
    posts = [post for post in posts if post['id'] not in superseded]

    def name(post, value):
        return canonical(corrections.get(post['id'], {}).get(value, {}).get('name', value))

    scopes = {}
    for shift in SHIFTS:
        selected = [post for post in posts if post['shift'] == shift and post['storeId'] in stores]
        rosters = {}
        for post in selected:
            if post['names'] and not post.get('replyTo'):
                if post['storeId'] not in rosters or _time(post) > _time(rosters[post['storeId']]):
                    rosters[post['storeId']] = post
        covered = set(rosters)
        present, cancelled = {}, {}
        for post in selected:
            for raw_name in post['names'] if rosters.get(post['storeId']) is post else ():
                person = name(post, raw_name)
                present[person] = max(present.get(person, _time(post)), _time(post))
            for notice in post.get('notices', []):
                if notice['kind'] == 'late' and post['storeId'] in rosters \
                        and _time(post) < _time(rosters[post['storeId']]):
                    continue
                person = name(post, notice['name'])
                destination = cancelled if notice['kind'] == 'absent' else present
                destination[person] = max(destination.get(person, _time(post)), _time(post))
        for post in personal.get('posts', []):
            if post['date'] != date:
                continue
            person = canonical(post['name'])
            for event in post['events']:
                if event['shift'] != shift:
                    continue
                if event['kind'] == 'absence':
                    cancelled[person] = max(cancelled.get(person, _time(post)), _time(post))
                elif event['kind'] in ('placement', 'late', 'return') and (
                        not selected or _time(post) > max(map(_time, selected))):
                    present[person] = max(present.get(person, _time(post)), _time(post))
        resolved = set(present) | set(cancelled)
        scopes[shift] = {
            'complete': covered == stores,
            'missingStores': sorted(stores - covered),
            'postIds': sorted((post['id'] for post in selected), key=int),
            'resolvedNames': resolved,
        }
    result = []
    checked = set(checked)
    for person, target in sorted(targets.items()):
        possible = target['shifts'] or list(SHIFTS)
        missing = [shift for shift in possible if scopes[shift]['complete']
                   and canonical(person) not in scopes[shift]['resolvedNames']]
        if not target['shifts'] and any(not scopes[shift]['complete'] for shift in SHIFTS):
            missing = []
        if not missing:
            continue
        basis = {'date': date, 'name': person, 'shifts': missing, 'slotId': slot_id,
                 'officialPostIds': sorted({tid for shift in missing for tid in scopes[shift]['postIds']}, key=int)}
        key = hashlib.sha256(json.dumps(basis, ensure_ascii=False, sort_keys=True,
                                       separators=(',', ':')).encode()).hexdigest()
        if key not in checked:
            result.append({'key': key, **basis, 'target': copy.deepcopy(target)})
    return {
        'checks': result,
        'scopes': {shift: {key: value for key, value in scope.items() if key != 'resolvedNames'}
                   for shift, scope in scopes.items()},
        'meaning': 'correction_check_only_not_absence',
    }
