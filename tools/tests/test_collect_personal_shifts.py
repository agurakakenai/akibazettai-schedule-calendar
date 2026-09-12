"""Personal collection regressions. Network is forbidden throughout this suite."""
import contextlib
import copy
import datetime as dt
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import unittest
from unittest import mock
import urllib.error
import urllib.request
import uuid


TOOLS = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('personal', TOOLS / 'collect-personal-shifts.py')
personal = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(personal)
DATE = dt.date(2026, 9, 6)
NOW = dt.datetime(2026, 9, 6, 3, tzinfo=personal.UTC)
AMU = {'name': 'あむ', 'handle': 'amu_zettai', 'shifts': ['昼', '夜']}
RARAKO = {'name': 'ららこ', 'handle': 'rarako_zettai', 'shifts': ['昼']}
UID = '1180156105181159424'
TID = '2096252018260062487'
CREATED = '2026-09-05T15:00:02Z'


def candidate(tid=TID, created=CREATED, target=AMU, uid=UID):
    return {'id': tid, 'url': personal.public_url(target['handle'], tid),
            'name': target['name'], 'authorScreenName': target['handle'], 'authorId': uid,
            'date': DATE.isoformat(), 'searchCreatedAt': created}


def post(text='9月6日\n1号店昼\n2号店夜', tid=TID, created=CREATED, target=AMU, uid=UID):
    return {'id_str': tid, 'created_at': created,
            'user': {'id_str': uid, 'screen_name': target['handle']}, 'text': text}


def snowflake(created):
    milliseconds = int(personal.official.timestamp(created).timestamp()) * 1000
    return str((milliseconds - 1288834974657) << 22)


def entry(item=None):
    item = item or candidate()
    return {'id': item['id'], 'url': item['url'], 'screenName': item['authorScreenName'],
            'userId': item['authorId'],
            'createdAt': int(personal.official.timestamp(item['searchCreatedAt']).timestamp())}


def page(entries=(), error='', best=None):
    return '<script id="__NEXT_DATA__">' + json.dumps({
        'props': {'pageProps': {'pageData': {
            'searchError': {'errorType': error},
            'bestTweet': best,
            'timeline': {'entry': list(entries), 'head': {'totalResultsReturned': len(entries)}},
        }}},
    }) + '</script>'


def registry_fixture(*targets):
    value = personal.members.empty_registry()
    value['members'] = [
        personal.members.new_member(
            target['name'], 'https://x.com/' + target['handle'] if target.get('handle') else None,
            NOW - dt.timedelta(days=10), member_id='m-' + f'{index + 1:032x}')
        for index, target in enumerate(targets or (AMU, RARAKO))]
    return personal.members.validate_registry(value)


class Offline(unittest.TestCase):
    def setUp(self):
        self.net = mock.patch.object(urllib.request.OpenerDirector, 'open',
                                     side_effect=AssertionError('Live HTTP forbidden in offline tests'))
        self.net.start()
        self.addCleanup(self.net.stop)


class ParsingTests(Offline):
    def parse(self, text, shifts=('昼', '夜')):
        return personal.parse_events(text, personal.official.timestamp(CREATED),
                                     DATE, shifts, 'あむ', ('あむ', 'ららこ', 'まこと'))

    def test_manual_pilot_amu_facts(self):
        events, reason = self.parse('9月6日\n1号店昼\n2号店夜\n12-22')
        self.assertEqual(reason, 'events')
        self.assertEqual([(e['shift'], e['storeId']) for e in events],
                         [('昼', 's1'), ('夜', 's2')])

    def test_manual_pilot_rarako_no_inferred_night_or_old_correction(self):
        text = '9月6日\nお昼1号店\n1号店➡️2号店\n12-22\n2→1だと勘違いしていた\n途中休憩'
        events, _ = self.parse(text)
        self.assertEqual(events, [{'shift': '昼', 'kind': 'placement',
                                   'storeId': 's1', 'excerpt': 'お昼1号店'}])

    def test_calendar_midnight_is_not_official_service_day(self):
        created = personal.official.timestamp(CREATED)
        self.assertEqual(personal.calendar_day(created), DATE)
        self.assertEqual(personal.official.service_day(created), dt.date(2026, 9, 5))
        self.assertEqual(self.parse('今日\n昼1号店')[0][0]['storeId'], 's1')

    def test_missing_date_yesterday_tomorrow_and_other_explicit_day_are_not_today(self):
        for text in ('昼1号店', '昨日昼1号店', '明日昼1号店', 'あした昼1号店',
                     '9月5日\n昼1号店', '9月7日夜2号店'):
            with self.subTest(text=text):
                self.assertEqual(self.parse(text)[0], [])

    def test_date_variants_and_fullwidth(self):
        for text in ('9/6 お昼1号店', '９月６日\n１号店昼'):
            self.assertEqual(self.parse(text)[0][0]['storeId'], 's1')

    def test_date_scopes_do_not_leak_from_tomorrow_to_today(self):
        events, _ = self.parse('9月6日\n昼1号店\n明日\n夜2号店')
        self.assertEqual([e['shift'] for e in events], ['昼'])

    def test_clear_all_day_absence_applies_only_to_original_shifts(self):
        events, _ = self.parse('今日は終日お休みします')
        self.assertEqual([(e['shift'], e['kind']) for e in events],
                         [('昼', 'absence'), ('夜', 'absence')])
        self.assertEqual(len(self.parse('今日はお休みします', ('夜',))[0]), 1)
        self.assertTrue(all('storeId' not in e for e in events))

    def test_explicit_one_shift_absence(self):
        events, _ = self.parse('今日\n昼はお休みします')
        self.assertEqual([e['shift'] for e in events], ['昼'])
        self.assertEqual(events[0]['kind'], 'absence')

    def test_other_shift_absence_is_not_converted_to_original_daytime(self):
        self.assertEqual(self.parse('今日夜はお休みします', ('昼',))[0], [])

    def test_conflicting_return_and_absence_stays_unresolved(self):
        self.assertEqual(self.parse('今日夜はお休みですが復帰します')[0], [])

    def test_late_is_not_absence_and_only_explicit_time_store(self):
        events, _ = self.parse('今日は夜2号店に19時30分から遅れて行きます')
        self.assertEqual(events, [{'shift': '夜', 'kind': 'late', 'storeId': 's2',
                                   'time': '19:30', 'excerpt': '遅れて'}])
        events, _ = self.parse('今日は夜遅れます')
        self.assertEqual(set(events[0]), {'shift', 'kind', 'excerpt'})

    def test_explicit_return_does_not_restore_unspecified_store(self):
        events, _ = self.parse('今日\n夜は復帰します')
        self.assertEqual(events[0]['kind'], 'return')
        self.assertNotIn('storeId', events[0])
        self.assertEqual(self.parse('今日\n夜2号店へ戻ります')[0][0]['storeId'], 's2')

    def test_rest_is_never_absence_late_or_return(self):
        for text in ('今日昼休み', '今日夜は休憩します', '今日夜は休憩から戻りました'):
            self.assertEqual(self.parse(text)[0], [])

    def test_euphemisms_only_uncertain_with_explicit_day_and_shift(self):
        for phrase in ('人間の姿になれませんでした', '魔法がうまくかかりませんでした'):
            self.assertEqual(self.parse(phrase)[0], [])
            events, reason = self.parse('今日\n' + phrase)
            self.assertEqual((events, reason), ([], 'unresolved_shift'))
            events, _ = self.parse('今日夜は' + phrase)
            self.assertEqual(events[0]['kind'], 'uncertain')
            self.assertNotIn('storeId', events[0])

    def test_unknown_shift_late_or_return_is_unresolved(self):
        for text in ('今日遅れます', '今日復帰します'):
            self.assertEqual(self.parse(text), ([], 'unresolved_shift'))

    def test_conditional_absence_is_uncertain_not_absence(self):
        events, _ = self.parse('今日夜はお休みかもしれません')
        self.assertEqual(events[0]['kind'], 'uncertain')

    def test_negative_and_corrected_old_values_are_not_placements(self):
        for text in ('今日昼1号店じゃない', '今日昼1号店ではありません',
                     '今日昼1号店にはいません', '今日夜2号店だと勘違いしていた',
                     '今日は欠勤しません', '今日夜は復帰しません'):
            with self.subTest(text=text):
                self.assertEqual(self.parse(text)[0], [])
        events, _ = self.parse('今日昼1号店ではなく、昼2号店です')
        self.assertEqual([e['storeId'] for e in events], ['s2'])

    def test_third_party_and_quoted_prose_is_not_the_authors_fact(self):
        for text in ('今日ららこがお休みします', '今日ららこちゃんはお休みです',
                     '今日まことが夜2号店', '今日友達がお休みします',
                     '今日夜1号店とのこと', '今日\n「昼1号店」', 'RT @other 今日昼1号店',
                     '@other 今日昼1号店'):
            with self.subTest(text=text):
                self.assertEqual(self.parse(text)[0], [])

    def test_conflicting_same_shift_stores_are_uncertain(self):
        events, _ = self.parse('今日\n昼1号店\n昼2号店')
        self.assertEqual(events[0]['kind'], 'uncertain')
        self.assertNotIn('storeId', events[0])

    def test_health_context_not_saved_in_excerpt(self):
        events, _ = self.parse('今日は体調の事情で夜お休みします')
        self.assertEqual(events[0]['excerpt'], 'お休みします')
        self.assertNotIn('体調', json.dumps(events, ensure_ascii=False))

    def test_excerpt_is_single_line_even_with_embedded_whitespace(self):
        events, _ = self.parse('9月6日\n1号店\r昼')
        self.assertEqual(events[0]['excerpt'], '1号店 昼')
        self.assertNotIn('\r', events[0]['excerpt'])

    def test_mixed_daytime_absence_and_night_placement_bind_to_own_clauses(self):
        for separator in ('ですが', 'ですけど', 'です、', 'です\n'):
            text = '9月6日 昼はお休み' + separator + '夜2号店です'
            with self.subTest(text=text):
                events, _ = self.parse(text)
                self.assertEqual([(event['shift'], event['kind'], event.get('storeId'))
                                  for event in events],
                                 [('昼', 'absence', None), ('夜', 'placement', 's2')])

    def test_mixed_daytime_late_does_not_borrow_night_store_or_time(self):
        events, _ = self.parse('9月6日 昼は遅れますが夜2号店19時です')
        self.assertEqual([(event['shift'], event['kind'], event.get('storeId'))
                          for event in events],
                         [('昼', 'late', None), ('夜', 'placement', 's2')])
        self.assertNotIn('time', events[0])

    def test_unseparated_multi_shift_predicates_stay_unresolved(self):
        events, _ = self.parse('9月6日 昼お休み夜2号店')
        self.assertEqual(events, [])
        events, _ = self.parse('9月6日 昼も夜もお休みします')
        self.assertEqual([event['kind'] for event in events], ['absence', 'absence'])

    def test_quote_context_spans_newlines_and_punctuation(self):
        for text in ('9月6日\n「昼1号店\n夜2号店」',
                     '9月6日\n『昼1号店、夜2号店』',
                     '9月6日\n“昼1号店。夜2号店”',
                     '9月6日\n「昼1号店\n夜2号店'):
            with self.subTest(text=text):
                self.assertEqual(self.parse(text)[0], [])

    def test_third_party_subject_is_not_reset_by_line_or_clause_separator(self):
        for separator in ('\n', '、', '。'):
            for subject in ('ららこは', 'ららこ は', 'ららこちゃんは'):
                text = '9月6日 ' + subject + separator + '夜2号店です'
                with self.subTest(text=text):
                    self.assertEqual(self.parse(text)[0], [])
        self.assertEqual(personal.parse_events(
            '9月6日 ららこは\n夜2号店です', personal.official.timestamp(CREATED),
            DATE, ['昼', '夜'], name='あむ')[0], [])

    def test_correction_retracts_old_value_across_separator(self):
        for separator in ('、', '\n', '。'):
            text = '9月6日 昼1号店' + separator + 'ではなく昼2号店です'
            with self.subTest(text=text):
                events, _ = self.parse(text)
                self.assertEqual([(event['shift'], event.get('storeId'))
                                  for event in events], [('昼', 's2')])
        self.assertEqual(self.parse('9月6日 昼1号店\n訂正です\n昼2号店です')[0], [])
        self.assertEqual(self.parse('9月6日 昼1号店、間違いで昼2号店です')[0], [])

    def test_retracted_past_absence_is_never_current_absence(self):
        for text in ('9月6日 昼お休みする予定でしたが出勤します',
                     '9月6日 昼お休みのつもりだったけど出勤します',
                     '9月6日 昼お休みだったけど出勤します',
                     '9月6日 昼はお休み、撤回します',
                     '9月6日 昼お休みする予定\nでしたが出勤します',
                     '9月6日 昼お休みと思っていましたが出勤します'):
            with self.subTest(text=text):
                self.assertEqual(self.parse(text)[0], [])

    def test_unspecified_return_retracts_ambiguous_prior_absence_across_separator(self):
        for separator in ('ですが', 'です。', 'です\n'):
            with self.subTest(separator=separator):
                self.assertEqual(self.parse('今日夜はお休み' + separator + '復帰します')[0], [])
        events, _ = self.parse('今日昼はお休みですが夜は復帰します')
        self.assertEqual([(event['shift'], event['kind']) for event in events],
                         [('昼', 'absence'), ('夜', 'return')])

    def test_subordinate_absence_is_not_all_day_and_cannot_confirm_conflicting_placement(self):
        for boundary in ('。', '！', '!'):
            text = '9月6日 昼は1号店ですがお休みします' + boundary + '夜は2号店です'
            with self.subTest(boundary=boundary):
                events, _ = self.parse(text)
                self.assertEqual([(event['shift'], event['kind'], event.get('storeId'))
                                  for event in events],
                                 [('昼', 'uncertain', None), ('夜', 'placement', 's2')])
                self.assertFalse(any(event['kind'] == 'absence' for event in events))

    def test_shift_context_survives_clause_newline_but_not_sentence_boundary(self):
        events, _ = self.parse('9月6日 昼は\nお休みします。夜は2号店です')
        self.assertEqual([(event['shift'], event['kind']) for event in events],
                         [('昼', 'absence'), ('夜', 'placement')])
        events, _ = self.parse('9月6日 昼は。お休みします。夜は2号店です')
        self.assertFalse(any(event['kind'] == 'absence' for event in events))
        self.assertEqual([event['storeId'] for event in events], ['s2'])

    def test_independent_explicit_all_day_absence_still_applies_to_original_shifts(self):
        for text in ('9月6日 今日は終日お休みします', '9月6日\n終日お休みします',
                     '9月6日\n今日はお休みします'):
            with self.subTest(text=text):
                events, _ = self.parse(text)
                self.assertEqual([(event['shift'], event['kind']) for event in events],
                                 [('昼', 'absence'), ('夜', 'absence')])


class MetadataTests(Offline):
    def test_best_tweet_and_timeline_are_peer_candidates_not_nested_quotes(self):
        item = entry()
        self.assertEqual(personal.discover(page(best=item), {'あむ': AMU}, DATE, NOW, {}),
                         [candidate()])
        self.assertEqual(personal.discover(page([item], best=item), {'あむ': AMU}, DATE, NOW, {}),
                         [candidate()])
        unrelated = dict(item, screenName='unrelated', bestTweetReply=item, quotedTweet=item)
        self.assertEqual(personal.discover(page(best=unrelated), {'あむ': AMU}, DATE, NOW, {}), [])

    def test_conflicting_peer_metadata_does_not_choose_a_convenient_author(self):
        conflicting = dict(entry(), userId='999999999999999999')
        self.assertEqual(personal.discover(page([conflicting], best=entry()),
                                          {'あむ': AMU}, DATE, NOW, {}), [])

    def test_search_urls_allow_only_canonical_verified_account_or_legacy_query_shapes(self):
        for url in (*personal.search_urls(DATE), personal.account_search_url(AMU['handle'])):
            self.assertTrue(personal.valid_search_url(url))
        for url in ('https://x.com/amu_zettai', personal.account_search_url(AMU['handle']) + '&extra=1',
                    personal.account_search_url(AMU['handle']) + '#fragment',
                    personal.account_search_url(AMU['handle']).replace('id%3A', 'from%3A'),
                    personal.account_search_url(AMU['handle']).replace('https:', 'http:')):
            self.assertFalse(personal.valid_search_url(url))

    def test_independent_link_does_not_require_a_store_or_generate_events(self):
        analyzer = mock.Mock()
        analyzer.parse_with_timing.return_value = ([], [{'scope': 'unspecified', 'status': 'work'}], [], 'links')
        value, reason = personal.validate_post(candidate(), post('今日お給仕します'), AMU, NOW,
                                               analyzer=analyzer)
        self.assertEqual(reason, 'links')
        self.assertEqual(value['events'], [])
        self.assertEqual(value['links'], [{'scope': 'unspecified', 'status': 'work'}])
        personal.valid_post(value)
        for links in ([], [{'scope': 'unspecified', 'status': 'withdrawn'}],
                      [{'scope': '昼', 'status': 'work'}] * 2,
                      [{'scope': '夜', 'status': 'work', 'evidenceLineIds': [1]}]):
            with self.subTest(links=links), self.assertRaises(ValueError):
                personal.valid_post({**value, 'links': links})

    def test_timing_only_post_has_strict_identity_date_and_public_source(self):
        self.assertIs(personal.timing, personal.work_timing)
        analyzer = mock.Mock()
        fact = {'serviceDate': DATE.isoformat(), 'shift': '昼', 'boundary': 'end',
                'status': 'set', 'qualifier': 'short', 'explicitTime': '16:00'}
        analyzer.parse_with_timing.return_value = ([], [], [fact], 'work_timing')
        value, reason = personal.validate_post(candidate(), post('今日昼16時終了'), AMU, NOW, analyzer=analyzer)
        self.assertEqual(reason, 'work_timing')
        self.assertEqual((value['events'], value['links'], personal.legacy_links(value)), ([], [], []))
        personal.valid_post(value)
        for field, changed in (('serviceDate', '2026-09-07'), ('status', 'pending'),
                               ('boundary', 'start'), ('evidenceLineIds', [1])):
            bad = copy.deepcopy(value)
            bad['workTiming']['facts'][0][field] = changed
            with self.subTest(field=field), self.assertRaises(ValueError):
                personal.valid_post(bad)
        for field, changed in (('name', '別人'), ('authorId', '123'), ('sourceKind', 'half-month-schedule'),
                               ('id', snowflake('2026-09-05T15:00:03Z')), ('createdAt', '2026-09-05T15:00:03Z')):
            bad = copy.deepcopy(value)
            bad['workTiming']['facts'][0]['source'][field] = changed
            with self.subTest(source=field), self.assertRaises(ValueError):
                personal.valid_post(bad)
        for channel in (None, {}, {'schemaVersion': 1, 'facts': []},
                        {'schemaVersion': 1, 'facts': value['workTiming']['facts'] * 2}):
            with self.subTest(channel=channel), self.assertRaises(ValueError):
                personal.valid_post({**value, 'workTiming': channel})
        legacy, _ = personal.validate_post(candidate(), post(), AMU, NOW)
        self.assertNotIn('workTiming', legacy)

    def test_saved_binding_metadata_is_not_a_fabricated_search_timestamp(self):
        state = personal.empty_state()
        state['resolved'] = [{'id': TID, 'url': candidate()['url'], 'name': 'あむ',
                              'date': DATE.isoformat(), 'reason': 'no_event', 'resolvedAt': CREATED}]
        binding = {'authorId': UID, 'authorScreenName': AMU['handle'], 'verifiedAt': CREATED}
        state['identityBindings']['あむ'] = binding
        saved = personal.saved_candidates(state, {TID: post()}, DATE)[0]
        self.assertIsNone(saved['searchCreatedAt'])
        self.assertEqual(saved['metadataSource'], 'saved_binding')
        self.assertIsNotNone(personal.validate_post(saved, post(), AMU, NOW, binding)[0])
        with self.assertRaisesRegex(personal.Failure, 'saved_post_identity_required'):
            personal.validate_post(saved, post(), AMU, NOW)

    def test_valid_metadata_and_exact_post_contract(self):
        value, reason = personal.validate_post(candidate(), post(), AMU, NOW)
        self.assertEqual(reason, 'events')
        self.assertEqual(set(value), {'id', 'url', 'name', 'authorId', 'authorScreenName',
                                     'createdAt', 'observedAt', 'date', 'events'})

    def test_author_response_search_binding_and_timestamp_mismatches(self):
        cases = []
        bad = post(); bad['id_str'] = '2096253883677044837'; cases.append(bad)
        bad = post(); bad['id'] = float(TID); cases.append(bad)
        bad = post(); bad['user']['id_str'] = '2065375500131028992'; cases.append(bad)
        bad = post(); bad['user']['screen_name'] = 'other'; cases.append(bad)
        bad = post(); bad['created_at'] = '2026-09-05T15:00:02'; cases.append(bad)
        bad = post(); bad['created_at'] = '2026-09-05T15:00:10Z'; cases.append(bad)
        bad = post(); bad['created_at'] = '2026-09-07T15:00:02Z'; cases.append(bad)
        for value in cases:
            with self.subTest(value=value):
                with self.assertRaises(personal.Failure):
                    personal.validate_post(candidate(), value, AMU, NOW)
        with self.assertRaisesRegex(personal.Failure, 'author_mismatch'):
            personal.validate_post(candidate(), post(), AMU, NOW,
                                   {'authorId': '2065375500131028992',
                                    'authorScreenName': AMU['handle']})

    def test_retweets_are_resolved_without_using_someone_elses_events(self):
        for key in ('retweeted_tweet', 'retweeted_status'):
            value = post()
            value[key] = 'present'
            self.assertEqual(personal.validate_post(candidate(), value, AMU, NOW),
                             (None, 'quoted_or_reply'))

    def test_own_reply_and_quote_text_is_read_without_nested_authors(self):
        for key in ('quoted_tweet', 'quoted_status', 'in_reply_to_status_id_str', 'in_reply_to_screen_name'):
            value = post('今日 昼1号店')
            value[key] = {'text': '今日 夜4号店', 'user': {'screen_name': 'other'}}
            parsed, _ = personal.validate_post(candidate(), value, AMU, NOW)
            self.assertEqual([(event['shift'], event['storeId']) for event in parsed['events']], [('昼', 's1')])
            value['text'] = 'かわいいね'
            self.assertIsNone(personal.validate_post(candidate(), value, AMU, NOW)[0])

    def test_yahoo_only_top_level_timeline_entries(self):
        quoted = entry()
        other = dict(entry(), screenName='other', quotedTweet=quoted, displayText=quoted['url'])
        document = page([other, entry(), entry()]) + f'<a href="{quoted["url"]}">not discovery</a>'
        self.assertEqual(personal.discover(document, {'あむ': AMU}, DATE, NOW, {}),
                         [candidate()])

    def test_yahoo_bad_metadata_never_becomes_candidate(self):
        cases = []
        for key, value in [('id', float(TID)), ('userId', float(UID)),
                           ('url', 'https://x.com/other/status/' + TID),
                           ('url', 'https://evil.example/' + TID),
                           ('createdAt', 1788620402.0), ('createdAt', 1788706802),
                           ('createdAt', 1788534002), ('userId', personal.official.AUTHOR_ID)]:
            item = entry(); item[key] = value; cases.append(item)
        self.assertEqual(personal.discover(page(cases), {'あむ': AMU}, DATE, NOW, {}), [])

    def test_binding_rejects_old_generation_same_handle(self):
        self.assertEqual(personal.discover(page([entry()]), {'あむ': AMU}, DATE, NOW,
                         {'あむ': {'authorId': '2065375500131028992'}}), [])

    def test_empty_search_is_success_not_absence(self):
        self.assertEqual(personal.discover(page(), {'あむ': AMU}, DATE, NOW, {}), [])

    def test_search_errors_are_not_empty_success(self):
        for document in ('Access denied', page(error='servererror')):
            with self.assertRaises(personal.Failure):
                personal.discover(document, {'あむ': AMU}, DATE, NOW, {})
        with self.assertRaisesRegex(personal.Failure, 'access_denied'):
            personal.discover(page(error='access denied'), {'あむ': AMU}, DATE, NOW, {})

    def test_malformed_search_hierarchy_is_component_failure_not_attribute_error(self):
        values = [None, [], {'props': None}, {'props': {'pageProps': []}},
                  {'props': {'pageProps': {'pageData': None}}},
                  {'props': {'pageProps': {'pageData': []}}},
                  {'props': {'pageProps': {'pageData': {'searchError': []}}}},
                  {'props': {'pageProps': {'pageData': {'timeline': None}}}},
                  {'props': {'pageProps': {'pageData': {'timeline': {'entry': {}}}}}},
                  {'props': {'pageProps': {'pageData': {'timeline': {'entry': [None]}}}}}]
        for value in values:
            document = '<script id="__NEXT_DATA__">' + json.dumps(value) + '</script>'
            with self.subTest(value=value):
                with self.assertRaisesRegex(personal.Failure, 'invalid_search_response'):
                    personal.discover(document, {'あむ': AMU}, DATE, NOW, {})


class CliTests(Offline):
    def test_zero_search_allocation_is_accepted_without_changing_post_or_deadline_flags(self):
        with mock.patch.object(personal, 'run', return_value=0) as run:
            self.assertEqual(personal.main([
                '--snapshot', 'private.json', '--http-state', 'observed-shifts.http-state.json',
                '--max-searches', '0', '--scheduled']), 0)
            args = run.call_args.args[0]
            self.assertEqual(args.max_searches, 0)
            self.assertEqual(args.max_posts, 3)
            self.assertTrue(args.scheduled)

    def test_local_failures_are_exit_four_not_http_unavailable_three(self):
        for error in (OSError('disk failure'), ValueError('collector_locked'),
                      ValueError('invalid_personal_state'),
                      personal.InfrastructureFailure('transport_state_save_failed')):
            with self.subTest(error=type(error).__name__):
                output = io.StringIO()
                with mock.patch.object(personal, 'run', side_effect=error), contextlib.redirect_stdout(output):
                    code = personal.main(['--snapshot', 'private.json',
                                          '--http-state', 'observed-shifts.http-state.json'])
                self.assertEqual(code, 4)
                self.assertEqual(json.loads(output.getvalue())['exitCode'], 4)

    def test_invalid_cli_is_infrastructure_not_partial(self):
        for argv in ([], ['--official-snapshot', 'observed.json'],
                     ['--snapshot', 'private.json', '--http-state',
                      'observed-shifts.http-state.json', '--max-searches', '4']):
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(personal.main(argv), 4)

    def test_http_component_codes_are_preserved(self):
        for expected in (0, 2, 3):
            with mock.patch.object(personal, 'run', return_value=expected):
                self.assertEqual(personal.main(['--snapshot', 'private.json',
                                               '--http-state', 'observed-shifts.http-state.json']),
                                 expected)


class StateTests(Offline):
    def setUp(self):
        super().setUp()
        self.folder = TOOLS / 'tests' / ('personal-test-' + uuid.uuid4().hex)
        self.folder.mkdir()
        self.addCleanup(lambda: shutil.rmtree(self.folder))
        self.snapshot = self.folder / 'personal.state.json'
        self.http = self.folder / 'observed-shifts.http-state.json'
        self.seed = self.folder / 'seed.json'
        self.schedule = {'roster': ['あむ', 'ららこ', 'まこと'],
                         'schedule': {DATE.isoformat(): {'昼': [{'name': 'あむ'}, {'name': 'ららこ'}],
                                                        '夜': [{'name': 'あむ'}]}}}
        self.insights = {'maidTendency': {'あむ': {'x': 'amu_zettai'},
                                        'ららこ': {'x': 'rarako_zettai'}}}
        self.accounts = [{'name': 'あむ', 'handle': 'amu_zettai', 'source': '公式サイト'},
                         {'name': 'ららこ', 'handle': 'rarako_zettai', 'source': '本人確認済み'}]
        personal.official.atomic_json(self.seed, personal.public_state(personal.empty_state()))
        self.state = personal.empty_state()
        self.targets = {'あむ': AMU, 'ららこ': RARAKO}
        self.sleeps = []

    def durable(self, state=None, searches=2, posts=3, now=NOW):
        durable = personal.DurableHttp(state if state is not None else self.state,
                                       self.snapshot, self.http, DATE, self.targets,
                                       searches, posts, clock=lambda: now,
                                       sleep=self.sleeps.append)
        durable.post_target = AMU['name']
        return durable

    def fake_client(self, durable, entries=None, payloads=None, source_failure=None):
        entries = [candidate()] if entries is None else entries
        payloads = {TID: post()} if payloads is None else payloads
        client = mock.Mock()

        def search(*args):
            durable.reserve(personal.SEARCH_HOST, 'searches')
            if source_failure:
                raise source_failure
            return entries

        def fetch(tid):
            durable.reserve(personal.POST_HOST, 'posts')
            result = payloads[tid]
            if isinstance(result, Exception):
                raise result
            return result
        client.search.side_effect = search
        client.fetch_post.side_effect = fetch
        return client

    def collect(self, client=None, durable=None, state=None):
        state = self.state if state is None else state
        durable = durable or self.durable(state)
        durable.preflight()
        client = client or self.fake_client(durable)
        return personal.collect(state, durable, client, self.targets, DATE,
                                durable.caps['searches'], durable.caps['posts'],
                                clock=durable.clock, roster=self.schedule['roster'])

    def test_catch_up_searches_all_fourteen_announced_people_even_after_day_cutoff(self):
        people = [{'name': f'人{i}', 'handle': f'person{i}'} for i in range(14)]
        registry = registry_fixture(*people)
        schedule = {'schedule': {DATE.isoformat(): {
            '昼': [{'name': row['name']} for row in people[:8]],
            '夜': [{'name': row['name']} for row in people[8:]]}}}
        late = NOW.replace(hour=13)  # 22:00 JST, later than both old cutoffs.
        durable = self.durable(searches=14, posts=0, now=late)
        durable.catch_up = True
        report, code = personal.collect_recovery(
            self.state, durable, schedule, None, None, registry, (),
            lambda current: self.fake_client(current, entries=[]), None, lambda: late)
        self.assertEqual((report['requests']['searches'], report['requests']['posts']), (14, 0))
        day = report['acquisition']['days'][0]
        self.assertEqual((day['targets'], day['searched'], day['unsearched'], day['dayOnly']), (14, 14, 0, 8))
        self.assertEqual(len(day['withoutWorkLink']), 14)
        self.assertEqual(code, 2)
        personal.read_state(self.snapshot)

    def test_entire_announced_cohort_gets_first_valid_sources_before_supplement_and_extras(self):
        people = [{'name': f'人{index:02}', 'handle': f'person{index}',
                   'shifts': ['昼', '夜'] if index == 0 else ['昼'] if index < 8 else ['夜']}
                  for index in range(18)]
        extra = {'name': '補助', 'handle': 'supplement', 'shifts': []}
        registry = registry_fixture(*people, extra)
        schedule = {'schedule': {DATE.isoformat(): {
            shift: [{'name': row['name']} for row in people if shift in row['shifts']]
            for shift in ('昼', '夜')}}}
        candidates, payloads = [], {}
        for index, target in enumerate([*people, extra]):
            uid = str(int(UID) + index)
            for offset in (0, 1):
                tid = str(int(TID) + index * 10 + offset)
                candidates.append(candidate(tid=tid, target=target, uid=uid))
                text = '\n'.join(f'本日1号店{shift}' for shift in target['shifts']) or '本日2号店夜'
                if index == 8 and offset == 1:
                    text = 'コーヒーを飲みました'
                payloads[tid] = post(text, tid=tid, target=target, uid=uid)
        self.state['pending'] = [{**item, 'reason': 'post_limit', 'firstSeenAt': CREATED,
                                  'lastAttemptAt': None, 'attempts': 0} for item in candidates]
        self.state['resolved'] = [{
            'id': str(int(TID) + 10000 + index), 'name': person['name'],
            'url': personal.public_url(person['handle'], str(int(TID) + 10000 + index)),
            'date': DATE.isoformat(), 'reason': 'no_event', 'resolvedAt': CREATED}
            for index, person in enumerate(people) if index < 8 or index >= 12]
        calls = []
        for hour, cap in ((0, 14), (2, 6)):
            now = NOW + dt.timedelta(hours=hour)
            durable = self.durable(searches=0, posts=cap, now=now)
            durable.catch_up = True
            durable.caps['posts'] = cap
            def client_factory(current):
                client = self.fake_client(current, entries=[], payloads=payloads)
                original = client.fetch_post.side_effect
                def fetch(tid):
                    calls.append(tid)
                    return original(tid)
                client.fetch_post.side_effect = fetch
                return client
            report, _ = personal.collect_recovery(
                self.state, durable, schedule, None, None, registry, (),
                client_factory, None, lambda: now)
            day = report['acquisition']['days'][0]
            self.assertEqual((day['targets'], day['personShifts']), (18, 19))
            if hour == 0:
                first_names = [next(item['name'] for item in candidates if item['id'] == tid) for tid in calls]
                self.assertEqual(len(first_names), 14)
                self.assertEqual(len(set(first_names)), 14)
                self.assertNotIn(extra['name'], first_names)
                self.assertIn(people[8]['name'], day['withoutWorkLink'], 'no_event is not a work source')
            else:
                self.assertEqual(day['withoutWorkLink'], [])
                self.assertEqual(day['workLinkShifts'], 19)
                self.assertEqual(report['acquisition']['supplemental']['bodyChecked'], 1)
                self.assertEqual(calls[-1], str(int(TID) + 181))
                self.assertEqual(sum(post['name'] == extra['name'] for post in self.state['posts']), 1)
            personal.read_state(self.snapshot)

    def test_catch_up_first_body_precedes_extra_day_only_candidates(self):
        night = {**AMU, 'shifts': ['夜']}
        self.targets = {AMU['name']: night, RARAKO['name']: RARAKO}
        day_ids = [str(int(TID) + index) for index in (1, 2)]
        candidates = [candidate(), *[candidate(tid=tid, target=RARAKO, uid='1234567890') for tid in day_ids]]
        for previously_read, expected in (
                (True, [TID]), (False, [day_ids[-1], TID])):
            with self.subTest(previously_read=previously_read):
                self.state = personal.empty_state()
                self.state['pending'] = [{**item, 'reason': 'post_limit', 'firstSeenAt': CREATED,
                                          'lastAttemptAt': None, 'attempts': 0} for item in candidates]
                if previously_read:
                    self.state['resolved'] = [{
                        'id': str(int(TID) + 99), 'name': RARAKO['name'],
                        'url': personal.public_url(RARAKO['handle'], str(int(TID) + 99)),
                        'date': DATE.isoformat(), 'reason': 'no_event', 'resolvedAt': CREATED}]
                durable = self.durable(searches=0, posts=len(expected))
                durable.catch_up = True
                payloads = {TID: post('本日2号店夜')}
                payloads.update({tid: post('今日はコーヒー', tid=tid, target=RARAKO, uid='1234567890')
                                 for tid in day_ids})
                client = self.fake_client(durable, payloads=payloads)
                self.collect(client, durable)
                self.assertEqual([call.args[0] for call in client.fetch_post.call_args_list], expected)
                self.assertEqual(self.state['posts'][0]['name'], AMU['name'])
                personal.read_state(self.snapshot)

    def test_announced_then_bounded_unannounced_today_then_past(self):
        extra = {'name': 'あい', 'handle': 'extra_member'}
        registry = registry_fixture(AMU, RARAKO, extra)
        yesterday = DATE - dt.timedelta(days=1)
        schedule = {'schedule': {
            DATE.isoformat(): {'夜': [{'name': AMU['name']}]},
            yesterday.isoformat(): {'昼': [{'name': RARAKO['name']}]}}}
        durable = self.durable(searches=3, posts=0)
        durable.catch_up = True
        report, _ = personal.collect_recovery(
            self.state, durable, schedule, None, None, registry, (),
            lambda current: self.fake_client(current, entries=[]), None, lambda: NOW)
        today = self.state['searchHistory'][DATE.isoformat()]
        self.assertEqual(set(today), {AMU['name'], extra['name']})
        self.assertEqual(set(self.state['searchHistory'][yesterday.isoformat()]), {RARAKO['name']})
        self.assertEqual(self.state['coverage'][DATE.isoformat()][extra['name']]['shifts'], [])
        self.assertEqual(report['acquisition']['supplemental']['searched'], 1)
        self.assertEqual(report['acquisition']['days'][0]['targets'], 1)
        self.assertEqual(report['acquisition']['days'][0]['unavailableTargets'], 0)
        self.assertEqual(durable.used, {'searches': 3, 'posts': 0})
        self.assertEqual(self.state['posts'], [])
        personal.read_state(self.snapshot)

    def test_unannounced_supplement_never_bypasses_today_or_legacy_shift_guards(self):
        registry = registry_fixture(AMU)
        targets = personal.select_targets(
            {'schedule': {}}, None, [], DATE, self.state, registry=registry,
            include_unannounced=True)
        self.assertEqual(targets[AMU['name']]['shifts'], [])
        self.assertTrue(personal.active_targets(targets, DATE, NOW, catch_up=True))
        self.assertEqual(personal.active_targets(targets, DATE, NOW), {})
        self.assertEqual(personal.active_targets(targets, DATE, NOW + dt.timedelta(days=1), catch_up=True), {})
        self.assertEqual(self.state['originalTargets'][DATE.isoformat()], {})

    def test_catch_up_recovers_yesterday_with_its_original_shift_and_actual_day_budget(self):
        registry = registry_fixture(AMU)
        tomorrow = NOW + dt.timedelta(days=1)
        self.state['pending'] = [{**candidate(), 'reason': 'post_limit', 'firstSeenAt': CREATED,
                                  'lastAttemptAt': None, 'attempts': 0}]
        durable = self.durable(searches=0, posts=1, now=tomorrow)
        durable.catch_up = True
        report, _ = personal.collect_recovery(
            self.state, durable, self.schedule, None, None, registry, (),
            lambda current: self.fake_client(current), None, lambda: tomorrow)
        self.assertEqual(report['newPostCount'], 1)
        self.assertEqual(self.state['posts'][0]['date'], DATE.isoformat())
        self.assertEqual(self.state['budgets'][(DATE + dt.timedelta(days=1)).isoformat()]['posts'], 1)
        self.assertNotIn(DATE.isoformat(), self.state['budgets'])
        self.assertEqual(report['acquisition']['days'][0]['ageDays'], 1)
        personal.read_state(self.snapshot)

    def test_recovery_summary_does_not_mix_unavailable_accounts_into_search_denominator(self):
        registry = registry_fixture(AMU, {'name': '不明', 'handle': None})
        schedule = {'schedule': {DATE.isoformat(): {
            '昼': [{'name': 'あむ'}, {'name': '不明'}, {'name': '名簿外'}]}}}
        durable = self.durable(searches=0, posts=0)
        durable.catch_up = True
        report, code = personal.collect_recovery(
            self.state, durable, schedule, None, None, registry, (),
            lambda current: self.fake_client(current, entries=[]), None, lambda: NOW)
        day = report['acquisition']['days'][0]
        self.assertEqual((day['targets'], day['searched'], day['unsearched']), (1, 0, 1))
        self.assertEqual(day['unavailableTargets'], 2)
        self.assertEqual(code, 2)
        registry['members'][0]['xProfileUrl'] = None
        registry['members'][0]['accountTrust'] = None
        report, code = personal.collect_recovery(
            self.state, durable, schedule, None, None, registry, (),
            lambda current: self.fake_client(current, entries=[]), None, lambda: NOW)
        day = report['acquisition']['days'][0]
        self.assertEqual((day['targets'], day['unsearched'], day['unavailableTargets']), (0, 0, 3))
        self.assertEqual(code, 2)
    def test_catch_up_preserves_expired_and_semantic_failures_without_refetch(self):
        registry = registry_fixture(AMU)
        old = candidate()
        self.state['pending'] = [{**old, 'reason': 'azure_ungrounded', 'firstSeenAt': CREATED,
                                  'lastAttemptAt': CREATED, 'attempts': 1}]
        future = NOW + dt.timedelta(days=8)
        durable = self.durable(searches=0, posts=1, now=future)
        durable.catch_up = True
        client = self.fake_client(durable)
        report, _ = personal.collect_recovery(self.state, durable, self.schedule, None, None,
                                              registry, (), lambda _: client, None, lambda: future)
        client.fetch_post.assert_not_called()
        self.assertEqual(report['acquisition']['expired'], 1)
        self.assertEqual(self.state['pending'][0]['id'], old['id'])

    def test_selection_requires_current_roster_verified_account_and_exact_insights(self):
        targets = personal.select_targets(self.schedule, self.insights, self.accounts, DATE, self.state)
        self.assertEqual(set(targets), {'あむ', 'ららこ'})
        self.accounts[1]['source'] = '卒業済み'
        self.assertEqual(set(personal.select_targets(
            self.schedule, self.insights, self.accounts, DATE, self.state)), {'あむ'})
        self.insights['maidTendency']['あむ']['x'] = 'amu_old'
        self.assertEqual(personal.select_targets(
            self.schedule, self.insights, self.accounts, DATE, self.state), {})

    def test_registration_requires_dated_work_but_not_statistics_home_or_promotion(self):
        registry = registry_fixture(AMU, {'name': '追加', 'handle': 'new_member'},
                                    {'name': '不明', 'handle': None})
        accounts = [*self.accounts, {'name': '旧名', 'handle': 'old_member', 'source': '公式サイト'}]
        targets = personal.select_targets({}, None, accounts, DATE, self.state, registry=registry)
        self.assertEqual(targets, {})
        self.assertEqual(self.state['originalTargets'][DATE.isoformat()], {})
        self.assertEqual(self.state['coverage'][DATE.isoformat()], {})
        self.assertEqual(personal.target_searches(targets, DATE, self.state, NOW, 3), [])
        schedule = {'schedule': {DATE.isoformat(): {'昼': [{'name': '追加'}, {'name': '不明'}]}}}
        targets = personal.select_targets(schedule, None, accounts, DATE, self.state, registry=registry)
        self.assertEqual(set(targets), {'追加'})
        self.assertEqual(targets['追加']['shifts'], ['昼'])
        self.assertEqual(self.state['coverage'][DATE.isoformat()]['不明']['reason'], 'account_unknown')
        self.assertEqual(self.state['coverage'][DATE.isoformat()]['追加']['origins'], ['registry', 'scheduled'])
        personal.validate_collection_coverage(self.state)
        self.assertNotIn('旧名', targets)

    def test_forty_managed_members_only_supply_the_announced_daily_population(self):
        registry = registry_fixture(*({'name': f'人{index}', 'handle': f'person{index}'}
                                      for index in range(40)))
        self.assertEqual(len(registry['members']), 40)
        names = [member['canonicalName'] for member in registry['members'][:14]]
        schedule = {'schedule': {DATE.isoformat(): {'夜': [{'name': name} for name in names]}}}
        targets = personal.select_targets(schedule, None, [], DATE, self.state, registry=registry)
        self.assertEqual(set(targets), set(names))
        first = personal.target_searches(targets, DATE, self.state, NOW, 3)
        self.assertEqual(len(first), 3)
        self.state['searchHistory'] = {DATE.isoformat(): {
            name: {'handle': targets[name]['handle'], 'attemptedAt': personal.stamp(NOW)}
            for name, _ in first}}
        second = personal.target_searches(targets, DATE, self.state, NOW + dt.timedelta(minutes=1), 3)
        self.assertEqual(len(second), 3)
        self.assertFalse(set(name for name, _ in first) & set(name for name, _ in second))

    def test_registry_cross_collector_raw_name_and_alias_bindings_are_not_rebound(self):
        registry = registry_fixture(AMU)
        registry['members'][0]['aliases'] = ['昔あむ']
        bound = {'authorId': UID, 'authorScreenName': AMU['handle'], 'verifiedAt': CREATED}
        other = {'旧名': bound}
        before = copy.deepcopy(other)
        targets = personal.select_targets(self.schedule, None, [], DATE, self.state,
                                          registry=registry, binding_maps=(other,))
        self.assertEqual(targets, {})
        self.assertEqual(self.state['coverage'][DATE.isoformat()]['あむ']['reason'],
                         'account_identity_mismatch')
        self.assertEqual(other, before)
        targets = personal.select_targets(self.schedule, {'maidTendency': None}, [], DATE, self.state,
                                          registry=registry, binding_maps=({'昔あむ': bound},))
        self.assertEqual(set(targets), {'あむ'})
        self.assertEqual(personal.identity_bindings(registry, {'昔あむ': bound})['あむ'], bound)
        self.assertEqual(personal.target_for_name(targets, '昔あむ')['name'], '昔あむ')

    def test_registry_inactive_paused_review_override_old_targets_without_mutating_facts(self):
        baseline = registry_fixture(AMU)
        self.state['originalTargets'] = {DATE.isoformat(): {'あむ': copy.deepcopy(AMU)}}
        self.state['pending'] = [{**candidate(), 'reason': 'network_error', 'firstSeenAt': CREATED,
                                  'lastAttemptAt': CREATED, 'attempts': 1}]
        old = copy.deepcopy(self.state)
        for changes, reason in (({'membership': 'inactive'}, 'membership_inactive'),
                                ({'collection': 'paused'}, 'collection_paused'),
                                ({'collection': 'review'}, 'collection_review')):
            with self.subTest(reason=reason):
                self.state = copy.deepcopy(old)
                registry = personal.members.update_member(baseline, 'あむ', now=NOW, **changes)
                self.targets = personal.select_targets(self.schedule, self.insights, self.accounts,
                                                       DATE, self.state, registry=registry)
                self.assertEqual(self.targets, {})
                durable = self.durable()
                client = self.fake_client(durable)
                report, _ = self.collect(client, durable)
                client.search.assert_not_called()
                client.fetch_post.assert_not_called()
                for field in ('pending', 'originalTargets', 'posts', 'identityBindings', 'budgets'):
                    self.assertEqual(self.state[field], old[field])
                self.assertEqual(report['coverage']['あむ']['reason'], reason)

    def test_unknown_shifts_do_not_open_a_daily_discovery_window(self):
        target = {**AMU, 'shifts': []}
        for scheduled, last in ((True, dt.time(18)), (False, dt.time(19, 30))):
            now = dt.datetime.combine(DATE, last, personal.JST)
            self.assertEqual(personal.active_targets({'あむ': target}, DATE, now,
                                                     scheduled=scheduled), {})
            self.assertEqual(personal.active_targets({'あむ': target}, DATE,
                                                     now + dt.timedelta(seconds=1),
                                                     scheduled=scheduled), {})
        self.assertEqual(personal.active_targets({'あむ': target}, DATE,
                         NOW + dt.timedelta(days=1)), {})
        self.assertEqual(target['shifts'], [])

    def test_unknown_rule_context_admits_only_explicit_shifts_and_never_all_day_expansion(self):
        created = personal.official.timestamp(CREATED)
        events, _ = personal.parse_events('今日 夜2号店', created, DATE, [], 'あむ')
        self.assertEqual(events, [{'shift': '夜', 'kind': 'placement', 'storeId': 's2', 'excerpt': '夜2号店'}])
        for text in ('今日 終日お休みします', '今日 お給仕します', '明日 夜2号店', '昼は短め'):
            self.assertEqual(personal.parse_events(text, created, DATE, [], 'あむ')[0], [])

    def test_registry_guard_stops_get_after_wait_and_never_refunds_reserved_budget(self):
        registry = registry_fixture(AMU)
        path = self.folder / 'members.json'
        personal.official.atomic_json(path, registry)
        self.targets = personal.select_targets(self.schedule, None, [], DATE, self.state, registry=registry)
        for phase in ('wait', 'reserved'):
            with self.subTest(phase=phase):
                personal.official.atomic_json(path, registry)
                self.state = personal.empty_state()
                guard = personal.members.RegistryGuard(path, bindings=({},))
                durable = self.durable(searches=1)
                durable.registry_guard = guard

                def change():
                    personal.official.atomic_json(path, personal.members.update_member(
                        registry, 'あむ', now=NOW, collection='paused'))

                if phase == 'wait':
                    durable.sleep = lambda _: change()
                else:
                    save = durable.save

                    def save_and_change():
                        save()
                        if durable.used['searches']:
                            change()

                    durable.save = save_and_change
                client = personal.PersonalClient(durable)
                client.opener = mock.Mock()
                with self.assertRaisesRegex(personal.Failure, 'registry_changed'):
                    client.search(personal.account_search_url(AMU['handle']),
                                  self.targets, DATE, NOW, {})
                client.opener.open.assert_not_called()
                self.assertEqual(durable.used['searches'], int(phase == 'reserved'))
                saved = personal.read_state(self.snapshot)
                self.assertEqual(saved['budgets'].get(DATE.isoformat(), {}).get('searches', 0),
                                 int(phase == 'reserved'))

    def test_search_history_is_cross_day_alias_aware_and_new_candidates_never_starve(self):
        targets = [{'name': f'人{index}', 'handle': f'person{index}'} for index in range(6)]
        registry = registry_fixture(*targets)
        registry['members'][0]['aliases'] = ['古名']
        schedule = {'schedule': {DATE.isoformat(): {'夜': [{'name': target['name']} for target in targets]}}}
        self.targets = personal.select_targets(schedule, None, [], DATE, self.state, registry=registry)
        prior_day = (DATE - dt.timedelta(days=1)).isoformat()
        self.state['searchHistory'] = {prior_day: {
            '古名': {'handle': 'person0', 'attemptedAt': '2026-09-05T02:00:00Z'},
            '人1': {'handle': 'person1', 'attemptedAt': '2026-09-05T01:00:00Z'}}}
        served, repeated = set(), set()
        now = NOW
        for _ in range(12):
            searched = {name for rows in self.state['searchHistory'].values() for name in rows} | {'人0'}
            queue = personal.target_searches(self.targets, DATE, self.state, now, 2)
            self.assertEqual(len(queue), 2)
            for name, _ in queue:
                if name in served:
                    repeated.add(name)
                served.add(name)
                self.state['searchHistory'].setdefault(DATE.isoformat(), {})[name] = {
                    'handle': self.targets[name]['handle'], 'attemptedAt': personal.stamp(now)}
            now += dt.timedelta(minutes=1)
        self.assertEqual(served, set(self.targets))
        self.assertEqual(repeated, set(self.targets))
        self.assertEqual(self.state['searchHistory'][prior_day]['古名']['attemptedAt'],
                         '2026-09-05T02:00:00Z')
        personal.validate_collection_coverage(self.state)

    def test_cli_always_requires_explicit_or_default_registry_even_with_custom_inputs(self):
        args = self.args()
        args.members = self.folder / 'missing-members.json'
        with self.assertRaises(OSError):
            personal.run(args, clock=lambda: NOW,
                         client_factory=lambda _: self.fail('must not construct a source client'))
        self.assertFalse(self.snapshot.exists())

    def test_cli_missing_insights_and_empty_schedule_make_no_daily_requests(self):
        self.schedule = {}
        args = self.args(['--max-searches', '0', '--max-posts', '0'])
        args.insights.unlink()
        args.observations = self.folder / 'empty-observations.json'
        personal.official.atomic_json(args.observations, personal.official.empty_snapshot())
        client = mock.Mock()
        with contextlib.redirect_stdout(io.StringIO()):
            code = personal.run(args, clock=lambda: NOW, sleep=self.sleeps.append,
                                client_factory=lambda _: client)
        self.assertEqual(code, 0)
        client.search.assert_not_called()
        client.fetch_post.assert_not_called()
        state = personal.read_state(self.snapshot)
        self.assertEqual(state['originalTargets'][DATE.isoformat()], {})
        self.assertEqual(state['coverage'][DATE.isoformat()], {})

    def test_single_search_slot_does_not_include_unannounced_registry_members(self):
        registry = registry_fixture(AMU, RARAKO, {'name': '追加', 'handle': 'new_member'})
        self.state['searchHistory'] = {'2026-09-05': {
            'あむ': {'handle': AMU['handle'], 'attemptedAt': '2026-09-05T03:00:00Z'}}}
        served = set()
        for offset in range(6):
            day = DATE + dt.timedelta(days=offset)
            schedule = {'schedule': {day.isoformat(): {'昼': [{'name': 'あむ'}]}}}
            targets = personal.select_targets(schedule, None, [], day, self.state, registry=registry)
            now = NOW + dt.timedelta(days=offset)
            queue = personal.target_searches(targets, day, self.state, now, 1)
            self.assertEqual(len(queue), 1)
            name = queue[0][0]
            served.add(name)
            self.state['searchHistory'].setdefault(day.isoformat(), {})[name] = {
                'handle': targets[name]['handle'], 'attemptedAt': personal.stamp(now)}
        self.assertEqual(served, {'あむ'})

    def test_guard_change_while_fetching_keeps_old_pending_and_spent_get(self):
        registry = registry_fixture(AMU)
        path = self.folder / 'members.json'
        personal.official.atomic_json(path, registry)
        self.targets = personal.select_targets(self.schedule, None, [], DATE, self.state, registry=registry)
        self.state['pending'] = [{**candidate(), 'reason': 'network_error', 'firstSeenAt': CREATED,
                                  'lastAttemptAt': CREATED, 'attempts': 1}]
        original = copy.deepcopy(self.state['pending'])
        durable = self.durable(searches=0)
        durable.registry_guard = personal.members.RegistryGuard(path, bindings=({},))
        client = self.fake_client(durable)
        original_fetch = client.fetch_post.side_effect

        def fetch_then_pause(tid):
            payload = original_fetch(tid)
            personal.official.atomic_json(path, personal.members.update_member(
                registry, 'あむ', now=NOW, collection='paused'))
            return payload

        client.fetch_post.side_effect = fetch_then_pause
        report, code = self.collect(client, durable)
        self.assertEqual(code, 3)
        self.assertEqual(self.state['pending'], original)
        self.assertEqual(self.state['posts'], [])
        self.assertEqual(self.state['budgets'][DATE.isoformat()]['posts'], 1)
        self.assertEqual(report['failures'][-1]['reason'], 'registry_changed')
        personal.read_state(self.snapshot)

    def test_unknown_link_and_timing_stay_annotations_in_coverage_and_next_population(self):
        registry = registry_fixture(AMU)
        parsed, _ = personal.validate_post(candidate(), post(), AMU, NOW)
        parsed['events'] = []
        parsed['links'] = [{'scope': 'unspecified', 'status': 'work'}]
        self.state['posts'] = [parsed]
        self.targets = personal.select_targets({}, None, [], DATE, self.state, registry=registry)
        self.assertEqual(self.targets, {})
        rows = personal.update_coverage(self.state, self.targets, DATE, NOW)
        self.assertEqual(rows, {})
        self.assertEqual(self.state['originalTargets'][DATE.isoformat()], {})
        self.assertEqual(self.state['posts'], [parsed])
        self.targets = personal.select_targets(self.schedule, None, [], DATE, self.state, registry=registry)
        self.assertEqual(self.targets['あむ']['shifts'], AMU['shifts'])
        rows = personal.update_coverage(self.state, self.targets, DATE, NOW)
        self.assertEqual(rows['あむ']['postIds'], [TID])
        personal.validate_collection_coverage(self.state)

    def test_registry_profile_case_does_not_rewrite_verified_raw_post_provenance(self):
        registry = registry_fixture(AMU)
        targets = personal.select_targets(self.schedule, None, [], DATE, self.state, registry=registry)
        raw = {**AMU, 'handle': 'Amu_Zettai'}
        original = candidate(target=raw)
        discovered = personal.discover(page([entry(original)]), targets, DATE, NOW, {})
        self.assertEqual(discovered, [original])
        verified, _ = personal.validate_post(original, post(target=raw), targets['あむ'], NOW)
        self.assertEqual(verified['authorScreenName'], 'Amu_Zettai')
        self.assertEqual(verified['url'], original['url'])
        personal.valid_post(verified)

    def test_registry_coverage_separates_old_facts_from_new_source_failure(self):
        registry = registry_fixture(AMU)
        verified, _ = personal.validate_post(candidate(), post(), AMU, NOW)
        self.state['posts'] = [verified]
        self.targets = personal.select_targets({}, None, [], DATE, self.state, registry=registry)
        self.state['lastRun'] = {'status': 'unavailable', 'sources': [{
            'url': personal.account_search_url(AMU['handle']), 'status': 'failed',
            'reason': 'network_error'}]}
        rows = personal.update_coverage(self.state, self.targets, DATE, NOW)
        self.assertEqual(rows['あむ']['postIds'], [TID])
        self.assertEqual(rows['あむ']['reason'], 'network_error')
        self.assertEqual(self.state['posts'], [verified])
        inactive = personal.members.update_member(registry, 'あむ', now=NOW, membership='inactive')
        self.targets = personal.select_targets({}, None, [], DATE, self.state, registry=inactive)
        rows = personal.update_coverage(self.state, self.targets, DATE, NOW)
        self.assertEqual(rows['あむ']['reason'], 'membership_inactive')
        self.assertEqual(rows['あむ']['postIds'], [TID])
        personal.validate_collection_coverage(self.state)

    def test_half_month_private_binding_conflict_is_retained_and_blocks_personal_discovery(self):
        spec = importlib.util.spec_from_file_location(
            'personal_test_half_month', TOOLS / 'half-month-schedules.py')
        half = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(half)
        feed = half.empty_state()
        feed['identityBindings']['旧名'] = {
            'authorId': UID, 'authorScreenName': AMU['handle'], 'verifiedAt': CREATED}
        half.validate_state(feed)
        path = self.folder / 'half-private.json'
        personal.official.atomic_json(path, feed)
        args = self.args(['--half-month-snapshot', str(path)])
        captured = []

        def client_factory(durable):
            self.assertNotIn('あむ', durable.targets)
            self.assertEqual(durable.state['coverage'][DATE.isoformat()]['あむ']['reason'],
                             'account_identity_mismatch')
            captured.append(durable)
            return self.fake_client(durable, entries=[])

        with contextlib.redirect_stdout(io.StringIO()):
            personal.run(args, clock=lambda: NOW, sleep=self.sleeps.append,
                         client_factory=client_factory)
        self.assertEqual(len(captured), 1)
        self.assertEqual(json.loads(path.read_text(encoding='utf-8')), feed)

    def test_population_includes_new_official_notice_and_personal_members_without_roster_gate(self):
        for name, handle in (('追加集合', 'added_group'), ('追加補足', 'added_notice'),
                             ('追加本人', 'added_personal')):
            self.accounts.append({'name': name, 'handle': handle, 'source': '公式サイト'})
            self.insights['maidTendency'][name] = {'x': handle}
        first = personal.select_targets(self.schedule, self.insights, self.accounts, DATE, self.state)
        original = copy.deepcopy(self.state['originalTargets'])
        observed = {'posts': [{'id': TID, 'date': DATE.isoformat(), 'shift': '夜',
                               'names': ['追加集合', 'みらい'],
                               'notices': [{'name': '追加補足'}]}]}
        self.state['posts'] = [{'id': '2096252018260062488', 'date': DATE.isoformat(),
                               'name': '追加本人', 'events': [{'shift': '昼'}]}]
        targets = personal.select_targets(self.schedule, self.insights, self.accounts, DATE,
                                          self.state, observed)
        self.assertEqual(set(targets) - set(first), {'追加集合', '追加補足', '追加本人'})
        self.assertEqual(targets['追加補足']['shifts'], ['夜'])
        self.assertEqual(self.state['originalTargets'], original)
        self.assertEqual(self.state['coverage'][DATE.isoformat()]['みらい']['reason'], 'account_unknown')
        self.assertNotIn('みらい', targets)

    def test_new_shift_and_schedule_member_refresh_without_mutating_original_targets(self):
        personal.select_targets(self.schedule, self.insights, self.accounts, DATE, self.state)
        original = copy.deepcopy(self.state['originalTargets'])
        self.schedule['schedule'][DATE.isoformat()]['夜'].append({'name': 'ららこ'})
        targets = personal.select_targets(self.schedule, self.insights, self.accounts, DATE, self.state)
        self.assertEqual(targets['ららこ']['shifts'], ['昼', '夜'])
        self.assertEqual(self.state['originalTargets'], original)

    def test_alias_and_post_specific_correction_do_not_rewrite_evidence_or_similar_names(self):
        for name, handle, alias in (('まこっちゃん', 'makoto2_zettai', 'まこと'),
                                     ('つぼみ', 'tsubomi_zettai', None),
                                     ('みりあ', 'miria_zettai', None)):
            self.accounts.append({'name': name, 'handle': handle, 'source': '公式サイト'})
            self.insights['maidTendency'][name] = {'x': handle, 'alias': alias}
        self.schedule['observationNameCorrections'] = {TID: {'つぽみ': {'name': 'つぼみ'}}}
        observed = {'posts': [{'id': TID, 'date': DATE.isoformat(), 'shift': '昼',
                               'names': ['まこと', 'みりあ', 'みらい'],
                               'notices': [{'name': 'つぽみ'}]},
                              {'id': '2096252018260062488', 'date': DATE.isoformat(), 'shift': '夜',
                               'names': ['つぽみ']}]}
        unchanged = copy.deepcopy(observed)
        targets = personal.select_targets(self.schedule, self.insights, self.accounts, DATE,
                                          self.state, observed)
        self.assertEqual(targets['まこっちゃん']['handle'], 'makoto2_zettai')
        self.assertEqual(targets['つぼみ']['shifts'], ['昼'])
        self.assertNotIn('みらい', targets)
        self.assertEqual(observed, unchanged)
        self.assertEqual(self.state['coverage'][DATE.isoformat()]['つぽみ']['shifts'], ['夜'])

    def test_link_only_post_never_creates_a_target_or_infers_a_shift(self):
        self.accounts.append({'name': '追加', 'handle': 'added', 'source': '公式サイト'})
        self.insights['maidTendency']['追加'] = {'x': 'added'}
        self.state['posts'] = [{'id': TID, 'date': DATE.isoformat(), 'name': '追加',
                               'events': [], 'links': [{'scope': 'unspecified', 'status': 'work'}]}]
        targets = personal.select_targets(self.schedule, self.insights, self.accounts, DATE, self.state)
        self.assertNotIn('追加', targets)
        self.assertNotIn('追加', self.state['coverage'][DATE.isoformat()])

    def test_target_search_queue_rotates_unsearched_people_with_the_same_deadline(self):
        targets = {f'人{index}': {'name': f'人{index}', 'handle': f'person{index}', 'shifts': ['昼']}
                   for index in range(5)}
        first = personal.target_searches(targets, DATE, self.state, NOW, 3)
        self.state['searchHistory'] = {DATE.isoformat(): {
            name: {'handle': targets[name]['handle'], 'attemptedAt': CREATED} for name, _ in first}}
        second = personal.target_searches(targets, DATE, self.state, NOW, 3)
        self.assertEqual([name for name, _ in second[:2]], ['人3', '人4'])
        self.assertTrue(all('id%3A' in url for _, url in first + second))
        closed = dt.datetime(2026, 9, 6, 13, 31, tzinfo=personal.JST)
        self.assertEqual(personal.target_searches(targets, DATE, self.state, closed, 3), [])

    def test_private_coverage_separates_unknown_account_no_candidate_and_unsearched(self):
        self.schedule['schedule'][DATE.isoformat()]['昼'].append({'name': 'みらい'})
        self.targets = personal.select_targets(self.schedule, self.insights, self.accounts, DATE, self.state)
        durable = self.durable(searches=1)
        client = self.fake_client(durable, entries=[])
        report, _ = self.collect(client, durable)
        rows = report['coverage']
        self.assertEqual(rows['みらい']['reason'], 'account_unknown')
        self.assertEqual(rows['ららこ']['reason'], 'no_candidate_in_checked_pages')
        self.assertEqual(rows['あむ']['reason'], 'not_searched')
        self.assertNotIn('coverage', personal.public_state(self.state))
        self.assertNotIn('searchHistory', personal.public_state(self.state))
        personal.read_state(self.snapshot)

    def test_same_deadline_post_queue_serves_distinct_people_before_second_post(self):
        targets = {name: dict(target, shifts=['夜']) for name, target in self.targets.items()}
        newer = snowflake('2026-09-06T01:00:00Z')
        rarako_id = snowflake('2026-09-06T00:30:00Z')
        candidates = [candidate(), candidate(newer, '2026-09-06T01:00:00Z'),
                      candidate(rarako_id, '2026-09-06T00:30:00Z', RARAKO, '2065375500131028992')]
        self.state['pending'] = [{**item, 'reason': 'discovered', 'firstSeenAt': CREATED,
                                  'lastAttemptAt': None, 'attempts': 0} for item in candidates]
        durable = self.durable(posts=2)
        durable.targets = targets
        client = self.fake_client(durable, entries=[], payloads={
            newer: post('今日は晴れ', newer, '2026-09-06T01:00:00Z'),
            rarako_id: post('今日は晴れ', rarako_id, '2026-09-06T00:30:00Z', RARAKO, '2065375500131028992')})
        personal.collect(self.state, durable, client, targets, DATE, 0, 2, clock=durable.clock)
        self.assertEqual(client.fetch_post.call_args_list, [mock.call(newer), mock.call(rarako_id)])

    def test_bound_author_mismatch_in_pending_does_not_spend_a_post_get(self):
        self.state['identityBindings']['あむ'] = {
            'authorId': UID, 'authorScreenName': AMU['handle'], 'verifiedAt': CREATED}
        self.state['pending'] = [{**candidate(uid='2065375500131028992'), 'reason': 'discovered',
                                  'firstSeenAt': CREATED, 'lastAttemptAt': None, 'attempts': 0}]
        durable = self.durable()
        client = self.fake_client(durable, entries=[])
        report, _ = self.collect(client, durable)
        client.fetch_post.assert_not_called()
        self.assertEqual(report['requests']['posts'], 0)
        self.assertEqual(self.state['pending'][0]['reason'], 'author_mismatch')

    def test_no_roster_weekday_or_unscheduled_names_added(self):
        self.accounts.append({'name': 'まこと', 'handle': 'makoto', 'source': '公式サイト'})
        self.insights['maidTendency']['まこと'] = {'x': 'makoto'}
        self.schedule['schedule']['日'] = {'昼': [{'name': 'まこと'}]}
        targets = personal.select_targets(self.schedule, self.insights, self.accounts, DATE, self.state)
        self.assertNotIn('まこと', targets)

    def test_ambiguous_duplicate_accounts_not_guessed(self):
        self.accounts.append({'name': 'あむ', 'handle': 'amu_old', 'source': '公式サイト'})
        self.assertNotIn('あむ', personal.select_targets(
            self.schedule, self.insights, self.accounts, DATE, self.state))

    def test_original_targets_survive_cancellations_and_placement_discovery(self):
        first = personal.select_targets(self.schedule, self.insights, self.accounts, DATE, self.state)
        self.schedule['schedule'][DATE.isoformat()] = {}
        self.state['posts'] = [personal.validate_post(candidate(), post('今日お休みします'), AMU, NOW)[0]]
        second = personal.select_targets(self.schedule, self.insights, self.accounts, DATE, self.state)
        self.assertEqual(first, second)

    def test_daytime_and_night_windows(self):
        for hour, minute, expected in [(13, 30, {'あむ', 'ららこ'}), (13, 31, {'あむ'}),
                                       (19, 30, {'あむ'}), (19, 31, set())]:
            now = dt.datetime(2026, 9, 6, hour, minute, tzinfo=personal.JST)
            self.assertEqual(set(personal.active_targets(self.targets, DATE, now)), expected)

    def test_scheduled_window_is_stricter_without_changing_manual(self):
        for hour, minute, expected in ((12, 30, {'あむ', 'ららこ'}), (13, 30, {'あむ', 'ららこ'}),
                                       (14, 30, {'あむ'}), (15, 30, {'あむ'}),
                                       (17, 30, {'あむ'}), (18, 0, {'あむ'}),
                                       (18, 30, set()), (19, 30, set()), (20, 30, set())):
            now = dt.datetime(2026, 9, 6, hour, minute, tzinfo=personal.JST)
            self.assertEqual(set(personal.active_targets(self.targets, DATE, now, scheduled=True)), expected)
        now = dt.datetime(2026, 9, 6, 18, 30, tzinfo=personal.JST)
        self.assertEqual(set(personal.active_targets(self.targets, DATE, now)), {'あむ'})

    def test_scheduled_closed_window_does_not_search_or_fetch(self):
        durable = self.durable(now=dt.datetime(2026, 9, 6, 18, 30, tzinfo=personal.JST))
        durable.scheduled = True
        client = self.fake_client(durable)
        report, code = self.collect(client, durable)
        self.assertEqual((code, report['status']), (0, 'outside-window'))
        self.assertEqual(report['requests'], {'searches': 0, 'posts': 0})
        client.search.assert_not_called()
        client.fetch_post.assert_not_called()

    def test_ai_capacity_deferral_keeps_candidate_without_fetch_or_failure_cache(self):
        durable = self.durable()
        durable.preflight()
        client = self.fake_client(durable)
        analyzer = mock.Mock()
        analyzer.check_capacity.side_effect = personal.azure.AnalysisFailure('azure_budget_exhausted')
        report, code = personal.collect(
            self.state, durable, client, self.targets, DATE, 2, 3,
            clock=durable.clock, roster=self.schedule['roster'], analyzer=analyzer)
        self.assertEqual((code, report['status']), (2, 'budget-exhausted'))
        self.assertEqual(self.state['pending'][0]['reason'], 'analysis_capacity_deferred')
        self.assertEqual(self.state['pending'][0]['attempts'], 0)
        client.fetch_post.assert_not_called()
        analyzer.parse_with_timing.assert_not_called()
        analyzer.check_capacity.side_effect = None
        analyzer.parse_with_timing.return_value = ([{'shift': '昼', 'kind': 'placement',
                                         'storeId': 's1', 'excerpt': '昼1号店'}], [], [], 'events')
        next_durable = self.durable()
        next_durable.preflight()
        next_client = self.fake_client(next_durable, entries=[])
        personal.collect(self.state, next_durable, next_client, self.targets, DATE, 2, 3,
                         clock=next_durable.clock, roster=self.schedule['roster'], analyzer=analyzer)
        self.assertEqual(len(self.state['posts']), 1)
        self.assertEqual(self.state['pending'], [])
        next_client.fetch_post.assert_called_once_with(TID)

    def test_saved_partial_analysis_keeps_unmentioned_shift_and_prior_history(self):
        previous, _ = personal.validate_post(candidate(), post(), AMU, NOW)
        self.state['posts'] = [previous]
        self.state['identityBindings'][AMU['name']] = {
            'authorId': UID, 'authorScreenName': AMU['handle'], 'verifiedAt': personal.stamp(NOW)}
        durable = self.durable()
        durable.preflight()
        analyzer = mock.Mock()
        analyzer.state = {'history': []}
        analyzer.parse_with_timing.return_value = ([{'shift': '夜', 'kind': 'placement',
                                         'storeId': 's4', 'excerpt': '夜4号店'}], [], [], 'events')
        personal.collect(self.state, durable, None, self.targets, DATE, 2, 3,
                         clock=durable.clock, roster=self.schedule['roster'], analyzer=analyzer,
                         saved_payloads={TID: post('9月6日 夜4号店')})
        self.assertEqual([(event['shift'], event['storeId']) for event in self.state['posts'][0]['events']],
                         [('昼', 's1'), ('夜', 's4')])
        self.assertEqual(analyzer.state['history'], [previous])

    def test_timing_reanalysis_preserves_other_facets_and_core_without_relabeling_sources(self):
        previous, _ = personal.validate_post(candidate(), post(), AMU, NOW)
        previous['links'] = [{'scope': '昼', 'status': 'work'}, {'scope': '夜', 'status': 'work'}]
        facts = [
            {'serviceDate': DATE.isoformat(), 'shift': '昼', 'boundary': 'end',
             'status': 'set', 'qualifier': 'long', 'explicitTime': '18:00'},
            {'serviceDate': DATE.isoformat(), 'shift': '夜', 'boundary': 'start',
             'status': 'set', 'qualifier': 'early', 'explicitTime': '16:00'},
        ]
        previous['workTiming'] = personal.work_timing.bind(facts, previous, 'personal-work-post')
        for update in (
                [{**facts[0], 'qualifier': None, 'explicitTime': '17:00'}],
                [{**facts[0], 'status': 'withdrawn', 'qualifier': None, 'explicitTime': None}],
                [{**facts[0], 'status': 'conflict', 'qualifier': None, 'explicitTime': None}],
                []):
            with self.subTest(update=update):
                self.state = personal.empty_state()
                self.state['posts'] = [copy.deepcopy(previous)]
                self.state['identityBindings'][AMU['name']] = {
                    'authorId': UID, 'authorScreenName': AMU['handle'], 'verifiedAt': personal.stamp(NOW)}
                durable = self.durable()
                analyzer = mock.Mock()
                analyzer.state = {'history': []}
                analyzer.parse_with_timing.return_value = ([], previous['links'], update, 'links')
                personal.collect(self.state, durable, None, self.targets, DATE, 2, 3,
                                 clock=durable.clock, analyzer=analyzer,
                                 saved_payloads={TID: post('今日昼終了の訂正')})
                current = self.state['posts'][0]
                self.assertEqual(current['events'], previous['events'])
                self.assertEqual(current['links'], previous['links'])
                keyed = {personal.work_timing.scope(fact): fact for fact in current['workTiming']['facts']}
                self.assertEqual(keyed[(DATE.isoformat(), '夜', 'start')], previous['workTiming']['facts'][1])
                if update:
                    self.assertEqual(keyed[(DATE.isoformat(), '昼', 'end')]['explicitTime'], update[0]['explicitTime'])
                    self.assertEqual(keyed[(DATE.isoformat(), '昼', 'end')]['status'], update[0]['status'])
                    self.assertEqual(analyzer.state['history'], [previous])
                else:
                    self.assertEqual(current, previous)
                    self.assertEqual(analyzer.state['history'], [])
                personal.read_state(self.snapshot)

    def test_timing_specific_denial_retains_late_and_does_not_hide_a_later_set_update(self):
        previous, _ = personal.validate_post(candidate(), post(), AMU, NOW)
        previous['links'] = personal.legacy_links(previous)
        late = {'serviceDate': DATE.isoformat(), 'shift': '夜', 'boundary': 'start',
                'status': 'set', 'qualifier': 'late', 'explicitTime': None}
        not_early = {**late, 'status': 'excluded', 'qualifier': 'early'}
        previous['workTiming'] = personal.work_timing.bind([late], previous, 'personal-work-post')
        self.state['posts'] = [copy.deepcopy(previous)]
        self.state['identityBindings'][AMU['name']] = {
            'authorId': UID, 'authorScreenName': AMU['handle'], 'verifiedAt': personal.stamp(NOW)}
        analyzer = mock.Mock()
        analyzer.state = {'history': []}

        def apply(facts, links=()):
            analyzer.parse_with_timing.return_value = ([], list(links), facts, 'work_timing' if facts else 'links')
            durable = self.durable()
            personal.collect(self.state, durable, None, self.targets, DATE, 2, 3,
                             clock=durable.clock, analyzer=analyzer,
                             saved_payloads={TID: post('本日夜はおそめ。早めではありません。19時開始です。')})
            return copy.deepcopy(self.state['posts'][0])

        denied = apply([not_early])
        self.assertEqual([(fact['status'], fact['qualifier']) for fact in denied['workTiming']['facts']],
                         [('set', 'late'), ('excluded', 'early')])
        self.assertEqual(denied['workTiming']['facts'][0], previous['workTiming']['facts'][0])
        updated = apply([{**late, 'qualifier': None, 'explicitTime': '19:00'}])
        self.assertEqual(updated['workTiming']['facts'][0]['explicitTime'], '19:00')
        self.assertEqual(updated['workTiming']['facts'][1], denied['workTiming']['facts'][1])
        self.assertEqual(updated['events'], previous['events'])
        self.assertEqual(updated['links'], previous['links'])
        self.assertEqual(analyzer.state['history'], [previous, denied])
        self.assertEqual(apply([], previous['links']), updated)
        self.assertEqual(analyzer.state['history'], [previous, denied])
        personal.read_state(self.snapshot)

    def test_first_partial_v5_reanalysis_retains_legacy_links_for_unsupplied_scopes(self):
        previous, _ = personal.validate_post(candidate(), post(), AMU, NOW)
        self.state['posts'] = [previous]
        self.state['identityBindings'][AMU['name']] = {
            'authorId': UID, 'authorScreenName': AMU['handle'], 'verifiedAt': personal.stamp(NOW)}
        durable = self.durable()
        durable.preflight()
        analyzer = mock.Mock()
        analyzer.state = {'history': []}
        analyzer.parse_with_timing.return_value = ([], [{'scope': '昼', 'status': 'work'}], [], 'links')
        personal.collect(self.state, durable, None, self.targets, DATE, 2, 3,
                         clock=durable.clock, analyzer=analyzer,
                         saved_payloads={TID: post('9月6日 昼のお給仕')})
        current = self.state['posts'][0]
        self.assertEqual(current['events'], previous['events'])
        self.assertEqual(current['links'], [
            {'scope': '昼', 'status': 'work'}, {'scope': '夜', 'status': 'work'}])
        self.assertEqual(analyzer.state['history'], [previous])
        personal.read_state(self.snapshot)

    def test_explicit_empty_links_keep_only_prior_legacy_scopes_not_new_event_links(self):
        previous, _ = personal.validate_post(candidate(), post('9月6日 昼1号店'), AMU, NOW)
        self.state['posts'] = [previous]
        self.state['identityBindings'][AMU['name']] = {
            'authorId': UID, 'authorScreenName': AMU['handle'], 'verifiedAt': personal.stamp(NOW)}
        durable = self.durable()
        durable.preflight()
        analyzer = mock.Mock()
        analyzer.state = {'history': []}
        analyzer.parse_with_timing.return_value = ([{'shift': '夜', 'kind': 'placement', 'storeId': 's2',
                                         'excerpt': '2号店'}], [], [], 'events')
        personal.collect(self.state, durable, None, self.targets, DATE, 2, 3,
                         clock=durable.clock, analyzer=analyzer,
                         saved_payloads={TID: post('9月6日 夜2号店')})
        current = self.state['posts'][0]
        self.assertEqual([event['shift'] for event in current['events']], ['昼', '夜'])
        self.assertEqual(current['links'], [{'scope': '昼', 'status': 'work'}])
        self.assertEqual(analyzer.state['history'], [previous])
        personal.read_state(self.snapshot)

    def test_seed_facts_observed_times_and_spent_budget_floor(self):
        seed = personal.read_state(TOOLS / 'tests' / 'fixtures' / 'personal-pilot.json', private=False)
        personal.merge_seed(self.state, seed)
        self.assertEqual(len(self.state['posts']), 2)
        self.assertEqual(len(self.state['resolved']), 2)
        self.assertEqual(self.state['budgets']['2026-09-06'], {'searches': 7, 'posts': 2})
        self.assertEqual(self.state['posts'][0]['observedAt'], '2026-09-06T04:13:50.213846Z')
        self.assertEqual(self.state['posts'][1]['observedAt'], '2026-09-06T04:14:02.214117Z')
        self.assertEqual(len(self.state['posts'][1]['events']), 1)
        self.state['budgets']['2026-09-06']['searches'] += 1
        personal.merge_seed(self.state, seed)
        self.assertEqual(self.state['budgets']['2026-09-06']['searches'], 8)
        self.assertEqual(len(self.state['posts']), 2)

    def test_deployment_snapshot_is_valid_without_assuming_public_seed_shape_or_count(self):
        state = personal.load_snapshot(personal.ROOT / 'data' / 'personal-shifts.json')
        self.assertIs(state['complete'], False)
        self.assertIsInstance(state['posts'], list)
        published = personal.public_state(state)
        self.assertEqual(set(published), {
            'schemaVersion', 'complete', 'checkedAt', 'lastSuccessAt', 'posts', 'lastRun'})

    def test_budget_reservation_is_saved_before_actual_request(self):
        durable = self.durable()
        durable.preflight()
        durable.reserve(personal.SEARCH_HOST, 'searches')
        saved = personal.read_state(self.snapshot)
        self.assertEqual(saved['budgets'][DATE.isoformat()]['searches'], 1)
        self.assertEqual(self.sleeps, [12])

    def test_daily_and_run_budgets_are_hard_caps(self):
        self.state['budgets'][DATE.isoformat()] = {'searches': 60, 'posts': 40}
        durable = self.durable()
        durable.preflight()
        for host, kind in ((personal.SEARCH_HOST, 'searches'), (personal.POST_HOST, 'posts')):
            with self.assertRaisesRegex(personal.Failure, 'budget_exhausted'):
                durable.reserve(host, kind)
        self.state['budgets'][DATE.isoformat()] = {'searches': 0, 'posts': 0}
        durable = self.durable(searches=1)
        durable.reserve(personal.SEARCH_HOST, 'searches')
        with self.assertRaisesRegex(personal.Failure, 'budget_exhausted'):
            durable.reserve(personal.SEARCH_HOST, 'searches')

    def test_zero_results_not_absence_and_preserves_facts(self):
        self.state['posts'].append(personal.validate_post(candidate(), post(), AMU, NOW)[0])
        durable = self.durable()
        report, code = self.collect(self.fake_client(durable, entries=[]), durable)
        self.assertEqual((report['status'], code), ('no-results', 0))
        self.assertEqual(len(self.state['posts']), 1)

    def test_same_id_not_refetched_and_resolved_without_events_is_also_skipped(self):
        durable = self.durable()
        client = self.fake_client(durable, payloads={TID: post('日付のない昼1号店')})
        report, code = self.collect(client, durable)
        self.assertEqual(len(self.state['posts']), 0)
        self.assertEqual(self.state['resolved'][0]['reason'], 'explicit_date_required')
        durable = self.durable()
        client = self.fake_client(durable)
        report, code = self.collect(client, durable)
        client.fetch_post.assert_not_called()
        self.assertEqual((report['status'], code), ('no-new', 0))

    def test_semantic_uncertainty_is_partial_but_resolved_id_is_not_refetched(self):
        durable = self.durable()
        client = self.fake_client(durable, payloads={TID: post('今日夜は人間の姿になれませんでした')})
        report, code = self.collect(client, durable)
        self.assertEqual((report['status'], code), ('partial', 2))
        self.assertEqual(report['failures'][0]['reason'], 'uncertain_guidance')
        self.assertEqual(self.state['posts'][0]['events'][0]['kind'], 'uncertain')
        self.assertEqual(self.state['pending'], [])
        self.assertEqual(self.state['resolved'][0]['id'], TID)

    def test_failure_pending_retried_when_no_longer_in_search(self):
        durable = self.durable()
        client = self.fake_client(durable, payloads={TID: personal.Failure('network_error')})
        self.assertEqual(self.collect(client, durable)[1], 2)
        self.assertEqual(len(self.state['pending']), 1)
        durable = self.durable()
        client = self.fake_client(durable, entries=[])
        self.assertEqual(self.collect(client, durable)[1], 0)
        client.fetch_post.assert_called_once_with(TID)
        self.assertEqual(self.state['pending'], [])
        self.assertEqual(len(self.state['posts']), 1)

    def test_cap_defers_pending_without_losing_candidate(self):
        second_time = '2026-09-05T15:01:00Z'
        other = snowflake(second_time)
        durable = self.durable(posts=1)
        client = self.fake_client(durable, entries=[candidate(), candidate(other, second_time)],
                                  payloads={TID: post(), other: post(tid=other, created=second_time)})
        report, code = self.collect(client, durable)
        self.assertEqual((report['deferredCount'], code), (1, 2))
        self.assertEqual(client.fetch_post.call_count, 1)
        self.assertEqual(self.state['pending'][0]['reason'], 'post_limit')

    def test_failure_consumes_budget_and_next_run_does_not_reset(self):
        durable = self.durable()
        client = self.fake_client(durable, source_failure=personal.Failure('network_error'))
        self.collect(client, durable)
        saved = personal.read_state(self.snapshot)
        self.assertEqual(saved['budgets'][DATE.isoformat()]['searches'], 2)
        durable = self.durable(saved)
        self.collect(self.fake_client(durable, entries=[]), durable, saved)
        self.assertEqual(saved['budgets'][DATE.isoformat()]['searches'], 4)

    def test_shared_cooldown_preflight_prevents_requests(self):
        until = personal.stamp(NOW + dt.timedelta(hours=2))
        personal.official.atomic_json(self.http, {'schemaVersion': 1,
            'cooldowns': {personal.SEARCH_HOST: until}})
        durable = self.durable()
        client = self.fake_client(durable)
        report, code = self.collect(client, durable)
        self.assertEqual((report['status'], code), ('paused', 3))
        self.assertEqual(durable.used['searches'], 0)

    def test_pause_persists_across_runs_and_after_retry_after_expiry(self):
        durable = self.durable()
        durable.preflight()
        with self.assertRaises(personal.Failure):
            durable.deny(personal.SEARCH_HOST, 'access_denied', 429, '7200')
        saved = personal.read_state(self.snapshot)
        self.assertEqual(saved['paused']['httpStatus'], 429)
        cooldown = personal.official.load_transport(self.http)[personal.SEARCH_HOST]
        self.assertEqual(personal.official.timestamp(cooldown), NOW + dt.timedelta(hours=2))
        tomorrow = NOW + dt.timedelta(days=1)
        durable = self.durable(saved, now=tomorrow)
        with self.assertRaisesRegex(personal.Failure, 'paused'):
            durable.reserve(personal.SEARCH_HOST, 'searches')

    def test_infrastructure_save_failure_is_fail_closed(self):
        durable = self.durable()
        durable.preflight()
        with mock.patch.object(personal.official, 'atomic_json', side_effect=OSError('disk')):
            with self.assertRaises(OSError):
                durable.reserve(personal.SEARCH_HOST, 'searches')
        self.assertEqual(durable.used['searches'], 0)

    def test_new_shared_cooldown_during_spacing_is_checked_again(self):
        durable = self.durable()
        durable.preflight()

        def update(_):
            personal.official.atomic_json(self.http, {'schemaVersion': 1, 'cooldowns': {
                personal.SEARCH_HOST: personal.stamp(NOW + dt.timedelta(hours=1))}})
        durable.sleep = update
        with self.assertRaisesRegex(personal.Failure, 'shared_host_cooldown'):
            durable.reserve(personal.SEARCH_HOST, 'searches')
        self.assertEqual(durable.used['searches'], 0)

    def test_partial_cannot_advance_last_success(self):
        self.state['lastSuccessAt'] = '2026-09-06T02:00:00Z'
        durable = self.durable()
        self.collect(self.fake_client(durable, payloads={TID: personal.Failure('network_error')}), durable)
        self.assertEqual(self.state['lastSuccessAt'], '2026-09-06T02:00:00Z')

    def test_public_projection_does_not_leak_private_budgets_or_pending(self):
        self.collect()
        projected = personal.public_state(self.state)
        self.assertEqual(set(projected), {'schemaVersion', 'complete', 'checkedAt',
                                          'lastSuccessAt', 'posts', 'lastRun'})
        self.assertNotIn('text', json.dumps(projected))

    def test_zero_search_allocation_does_not_invoke_search(self):
        durable = self.durable(searches=0)
        client = self.fake_client(durable)
        report, code = self.collect(client, durable)
        client.search.assert_not_called()
        client.fetch_post.assert_not_called()
        self.assertEqual(report['requests'], {'searches': 0, 'posts': 0})
        self.assertEqual(code, 0)

    def test_shared_source_hooks_real_client_before_get_and_counts_once_with_legacy(self):
        source = personal.official.source_module()
        ledger_path = self.folder / 'source-usage.json'
        personal.official.atomic_json(self.snapshot, self.state)
        source.initialize(ledger_path, self.state, source_hash='b' * 64, at=NOW)
        current = [NOW]

        def sleep(seconds):
            current[0] += dt.timedelta(seconds=seconds)

        with source.SharedSource(ledger_path, run_id='personal-shared', component='personal',
                                 clock=lambda: current[0], sleep=sleep,
                                 personal_path=self.snapshot) as shared:
            durable = personal.DurableHttp(self.state, self.snapshot, self.http, DATE, self.targets,
                                           3, 3, clock=lambda: current[0], sleep=sleep,
                                           shared_source=shared)
            durable.preflight()
            client = personal.PersonalClient(durable)
            response = mock.MagicMock()
            response.__enter__.return_value = response
            response.getcode.return_value = 200
            response.headers = {}
            response.read.return_value = page([]).encode()

            def open_mock(*args, **kwargs):
                state = source.load_state(ledger_path, required=True)
                source.validate_legacy(state, personal.read_state(self.snapshot))
                self.assertEqual(next(iter(state['receipts'].values()))['status'], 'issued')
                return response

            client.opener = mock.Mock()
            client.opener.open.side_effect = open_mock
            url = personal.account_search_url(AMU['handle'])
            client.search(url, self.targets, DATE, current[0], {})
            with self.assertRaisesRegex(personal.Failure, 'source_already_requested'):
                client.search(url, self.targets, DATE, current[0], {})
            self.assertEqual(client.opener.open.call_count, 1)
            self.assertEqual(durable.used, {'searches': 1, 'posts': 0})
            self.assertEqual(shared.report()['run']['personal']['searches'], 1)
            source.validate_legacy(shared.state, self.state)
        saved = source.load_state(ledger_path)
        self.assertEqual(saved['baseline']['personalBudgets'], {})
        self.assertEqual(self.state['budgets'][DATE.isoformat()], {'searches': 1, 'posts': 0})

    def test_shared_source_capacity_is_denied_before_personal_get_without_legacy_delta(self):
        source = personal.official.source_module()
        ledger_path = self.folder / 'source-usage.json'
        personal.official.atomic_json(self.snapshot, self.state)
        source.initialize(ledger_path, self.state, source_hash='b' * 64, at=NOW)
        current = [NOW]

        def sleep(seconds):
            current[0] += dt.timedelta(seconds=seconds)

        with source.SharedSource(ledger_path, run_id='full', component='official',
                                 clock=lambda: current[0], sleep=sleep) as shared:
            for index in range(20):
                shared.reserve('posts', f'https://x.com/akibazettai/status/{int(TID) + index}')
        with source.SharedSource(ledger_path, run_id='full', component='personal',
                                 clock=lambda: current[0], sleep=sleep,
                                 personal_path=self.snapshot) as shared:
            durable = personal.DurableHttp(self.state, self.snapshot, self.http, DATE, self.targets,
                                           3, 3, clock=lambda: current[0], sleep=sleep,
                                           shared_source=shared)
            durable.post_target = AMU['name']
            durable.preflight()
            client = personal.PersonalClient(durable)
            client.opener = mock.Mock()
            with self.assertRaisesRegex(personal.Failure, 'source_budget_exhausted'):
                client.fetch_post(str(int(TID) + 99))
            client.opener.open.assert_not_called()
            self.assertEqual(self.state['budgets'], {})
            self.assertEqual(durable.used, {'searches': 0, 'posts': 0})
            source.validate_legacy(shared.state, self.state)

    def test_opt_in_cache_reuses_personal_post_but_never_extends_deadline(self):
        source = personal.official.source_module()
        ledger_path = self.folder / 'source-usage.json'
        personal.official.atomic_json(self.snapshot, self.state)
        source.initialize(ledger_path, self.state, source_hash='c' * 64, at=NOW)
        current = [NOW]

        def sleep(seconds):
            current[0] += dt.timedelta(seconds=seconds)

        with source.TransientSourceCache('cached-personal') as cache:
            with source.SharedSource(ledger_path, run_id='cached-personal', component='personal',
                                     clock=lambda: current[0], sleep=sleep,
                                     personal_path=self.snapshot, cache=cache) as shared:
                targets = {'あむ': {**AMU, 'shifts': ['昼']}}
                durable = personal.DurableHttp(self.state, self.snapshot, self.http, DATE, targets,
                                               0, 3, clock=lambda: current[0], sleep=sleep,
                                               shared_source=shared, scheduled=True)
                durable.post_target = AMU['name']
                durable.preflight()
                client = personal.PersonalClient(durable)
                response = mock.MagicMock()
                response.__enter__.return_value = response
                response.getcode.return_value = 200
                response.headers = {}
                response.read.return_value = json.dumps(post()).encode()
                client.opener = mock.Mock()
                client.opener.open.return_value = response
                self.assertEqual(client.fetch_post(TID), client.fetch_post(TID))
                self.assertEqual(client.opener.open.call_count, 1)
                self.assertEqual(durable.used, {'searches': 0, 'posts': 1})
                self.assertEqual(cache.counts()['posts'], 1)
                current[0] = NOW.replace(hour=4, minute=31)
                with self.assertRaisesRegex(personal.Failure, 'outside_window'):
                    client.fetch_post(TID)
                self.assertEqual(client.opener.open.call_count, 1)
        self.assertEqual(cache.counts()['posts'], 0)

    def test_half_month_input_introduces_today_target_without_personal_post_or_deadline_extension(self):
        feed = {'schemaVersion': 1, 'complete': False, 'checkedAt': personal.stamp(NOW),
                'lastSuccessAt': personal.stamp(NOW), 'lastRun': {'status': 'ok'}, 'schedules': [{
                    'id': TID, 'url': personal.public_url(AMU['handle'], TID),
                    'name': AMU['name'], 'authorId': UID, 'authorScreenName': AMU['handle'],
                    'createdAt': CREATED, 'observedAt': personal.stamp(NOW),
                    'sourceKind': 'half-month-schedule',
                    'period': {'from': '2026-09-01', 'to': '2026-09-15',
                               'printedYear': None, 'yearBasis': 'post-context'},
                    'days': [{'date': DATE.isoformat(), 'shifts': ['昼']}]}]}
        feed_path = self.folder / 'half-month.json'
        personal.official.atomic_json(feed_path, feed)
        self.schedule['schedule'] = {}
        args = self.args(['--half-month-snapshot', str(feed_path), '--scheduled'])
        observations = self.folder / 'observations.json'
        personal.official.atomic_json(observations, personal.official.empty_snapshot())
        args.observations = observations
        actual_targets = []

        def collect(state, durable, client, targets, *args, **kwargs):
            actual_targets.append(targets)
            self.assertEqual(state['posts'], [])
            self.assertEqual(targets['あむ']['shifts'], ['昼'])
            self.assertNotIn('ららこ', targets)
            self.assertEqual(set(personal.active_targets(
                targets, DATE, NOW.replace(hour=4, minute=31), scheduled=True)), set())
            return {'component': 'personal'}, 0

        with contextlib.redirect_stdout(io.StringIO()), mock.patch.object(
                personal, 'collect', side_effect=collect):
            personal.run(args, clock=lambda: NOW, sleep=self.sleeps.append,
                         client_factory=lambda durable: None)
        self.assertEqual(len(actual_targets), 1)
        self.assertEqual(json.loads(feed_path.read_text(encoding='utf-8')), feed)
        self.assertEqual(personal.read_state(self.snapshot)['posts'], [])

    def test_required_shared_source_missing_stops_before_client_creation(self):
        path = self.folder / 'missing-source.json'
        args = self.args(['--source-state', str(path), '--source-run-id', 'missing'])
        with self.assertRaisesRegex(ValueError, 'missing_source_usage'):
            personal.run(args, clock=lambda: NOW,
                         client_factory=lambda durable: self.fail('must not construct client'))
        self.assertFalse(path.exists())

    def args(self, extra=()):
        schedule = self.folder / 'schedule.js'
        insights = self.folder / 'insights.js'
        accounts = self.folder / 'accounts.csv'
        registry = self.folder / 'members.json'
        personal.official.atomic_json(registry, registry_fixture())
        schedule.write_text('window.SCHEDULE_DATA = ' + json.dumps(self.schedule) + ';', encoding='utf-8')
        insights.write_text('window.STORE_INSIGHTS = ' + json.dumps(self.insights) + ';', encoding='utf-8')
        accounts.write_text('name,handle,source\nあむ,amu_zettai,公式サイト\nららこ,rarako_zettai,本人確認済み\n',
                            encoding='utf-8')
        return personal.argument_parser().parse_args([
            '--snapshot', str(self.snapshot), '--http-state', str(self.http),
            '--seed', str(self.seed), '--schedule', str(schedule), '--insights', str(insights),
            '--accounts', str(accounts), '--members', str(registry), *extra])

    def test_dry_run_persists_budget_and_ledger_but_does_not_publish(self):
        publish = self.folder / 'public.json'
        args = self.args(['--dry-run', '--publish', str(publish)])
        self.state['searchHistory'] = {'2026-09-05': {
            'ららこ': {'handle': RARAKO['handle'], 'attemptedAt': '2026-09-05T02:00:00Z'}}}
        personal.official.atomic_json(self.snapshot, self.state)
        with contextlib.redirect_stdout(io.StringIO()):
            code = personal.run(args, clock=lambda: NOW, sleep=self.sleeps.append,
                                client_factory=lambda durable: self.fake_client(durable))
        self.assertEqual(code, 0)
        self.assertFalse(publish.exists())
        saved = personal.read_state(self.snapshot)
        self.assertEqual(saved['budgets'][DATE.isoformat()], {'searches': 9, 'posts': 3})
        self.assertEqual(len(saved['resolved']), 1)

    def test_official_lock_collision_and_private_lock_prevent_network(self):
        args = self.args()
        for lock in (self.http.with_name('observed-shifts.lock'), self.snapshot.with_suffix('.lock')):
            with personal.official.ProcessLock(lock):
                with self.assertRaisesRegex(ValueError, 'collector_locked'):
                    personal.run(args, clock=lambda: NOW, sleep=self.sleeps.append,
                                 client_factory=lambda durable: self.fail('must not construct client'))

    def test_shared_ai_lock_collision_is_infrastructure_before_client_creation(self):
        usage = personal.official.analysis_module().ledger
        ledger_path = self.folder / 'ai-usage.json'
        usage.atomic_json(ledger_path, usage.empty_state())
        args = self.args(['--analysis-backend', 'azure', '--ai-state', str(ledger_path),
                          '--analysis-run-id', 'personal-collision'])
        with usage.SharedUsage(ledger_path, run_id='official-holder', component='official',
                               clock=lambda: NOW, sleep=self.sleeps.append):
            with self.assertRaisesRegex(personal.InfrastructureFailure, 'azure_usage_locked'):
                personal.run(args, clock=lambda: NOW, sleep=self.sleeps.append, environment={},
                             client_factory=lambda durable: self.fail('must not construct client'))
        self.assertEqual(usage.load_state(ledger_path)['receipts'], {})

    def test_invalid_budget_state_never_resets_to_zero(self):
        personal.official.atomic_json(self.snapshot, {'schemaVersion': 1})
        with self.assertRaisesRegex(ValueError, 'invalid_personal_state'):
            personal.read_state(self.snapshot)

    def test_pages_loader_validates_private_and_public_without_dropping_safety_fields(self):
        self.assertEqual(set(personal.empty_snapshot()), personal.PUBLIC_FIELDS)
        private = personal.empty_state()
        self.assertIn('budgets', private)
        personal.official.atomic_json(self.snapshot, private)
        self.assertEqual(personal.load_snapshot(self.snapshot), private)
        public = personal.public_state(private)
        personal.official.atomic_json(self.snapshot, public)
        self.assertEqual(personal.load_snapshot(self.snapshot), public)

    def test_pages_loader_rejects_partial_or_malformed_private_state(self):
        cases = []
        partial = personal.public_state(personal.empty_state())
        partial['paused'] = None
        cases.append(partial)
        invalid = personal.empty_state()
        invalid['budgets'] = {DATE.isoformat(): {'searches': -1, 'posts': 0}}
        cases.append(invalid)
        invalid = personal.empty_state()
        invalid['resolved'] = [{'id': TID, 'reason': 'events'}]
        cases.append(invalid)
        invalid = personal.empty_state()
        invalid['identityBindings'] = {'あむ': {'authorId': UID,
            'authorScreenName': 'amu_zettai', 'verifiedAt': CREATED, 'text': 'not allowed'}}
        cases.append(invalid)
        invalid = personal.empty_state()
        invalid['paused'] = {'reason': 'access_denied', 'host': personal.SEARCH_HOST,
                             'at': CREATED, 'retryAt': 'timezone missing'}
        cases.append(invalid)
        invalid = personal.empty_state()
        invalid['lastRun']['text'] = 'not allowed'
        cases.append(invalid)
        for state in cases:
            personal.official.atomic_json(self.snapshot, state)
            with self.assertRaisesRegex(ValueError, 'invalid_personal_state'):
                personal.load_snapshot(self.snapshot)


class TransportTests(StateTests):
    def response(self, body, status=200, headers=None):
        response = mock.Mock()
        response.getcode.return_value = status
        response.headers = headers or {}
        response.read.return_value = body
        response.__enter__ = mock.Mock(return_value=response)
        response.__exit__ = mock.Mock(return_value=False)
        return response

    def test_401_403_429_stop_and_persist_before_next_attempt(self):
        for status in (401, 403, 429):
            with self.subTest(status=status):
                self.state = personal.empty_state()
                self.http.unlink(missing_ok=True)
                durable = self.durable()
                durable.preflight()
                client = personal.PersonalClient(durable)
                error = urllib.error.HTTPError(personal.search_urls(DATE)[0], status, 'denied',
                                               {'Retry-After': '7200'}, None)
                with mock.patch.object(client.opener, 'open', side_effect=error) as opener:
                    with self.assertRaises(personal.Failure):
                        client.search(personal.search_urls(DATE)[0], self.targets, DATE, NOW, {})
                    with self.assertRaisesRegex(personal.Failure, 'paused'):
                        client.fetch_post(TID)
                self.assertEqual(opener.call_count, 1)
                self.assertEqual(personal.read_state(self.snapshot)['paused']['httpStatus'], status)

    def test_title_and_json_challenges_pause(self):
        for body in (b'<title>Access denied</title>', page(error='captcha').encode()):
            self.state = personal.empty_state()
            self.http.unlink(missing_ok=True)
            durable = self.durable()
            durable.preflight()
            client = personal.PersonalClient(durable)
            with mock.patch.object(client.opener, 'open', return_value=self.response(body)):
                with self.assertRaisesRegex(personal.Failure, 'access_denied'):
                    client.search(personal.search_urls(DATE)[0], self.targets, DATE, NOW, {})
            self.assertIsNotNone(self.state['paused'])

    def test_refusal_title_precedes_null_page_data_and_stops_remaining_requests(self):
        durable = self.durable(searches=2)
        client = personal.PersonalClient(durable)
        body = (b'<title>Access denied</title><script id="__NEXT_DATA__">'
                b'{"props":{"pageProps":{"pageData":null}}}</script>')
        response = self.response(body, headers={'Retry-After': '7200'})
        with (mock.patch.object(client.opener, 'open', return_value=response) as opener,
              mock.patch.object(personal, 'search_page', side_effect=AssertionError(
                  'Refusal must be persisted before parsing JSON'))):
            report, code = self.collect(client, durable)
        self.assertEqual((report['status'], code), ('paused', 3))
        self.assertEqual(opener.call_count, 1)
        self.assertEqual(len(report['sources']), 1)
        saved = personal.read_state(self.snapshot)
        self.assertEqual(saved['paused']['reason'], 'access_denied')
        self.assertEqual(saved['budgets'][DATE.isoformat()]['searches'], 1)
        self.assertEqual(personal.official.timestamp(
            personal.official.load_transport(self.http)[personal.SEARCH_HOST]),
            NOW + dt.timedelta(hours=2))

    def test_refusal_title_with_invalid_json_pause_save_failure_remains_infrastructure(self):
        durable = self.durable()
        durable.preflight()
        client = personal.PersonalClient(durable)
        body = b'<title>Access denied</title><script id="__NEXT_DATA__">{</script>'
        with (mock.patch.object(client.opener, 'open', return_value=self.response(body)) as opener,
              mock.patch.object(durable, 'save_http', side_effect=[None, OSError('disk')]),
              mock.patch.object(personal, 'search_page', side_effect=AssertionError(
                  'Refusal must not reach JSON parsing'))):
            with self.assertRaisesRegex(personal.InfrastructureFailure, 'transport_state_save_failed'):
                client.search(personal.search_urls(DATE)[0], self.targets, DATE, NOW, {})
        self.assertEqual(opener.call_count, 1)
        self.assertIsNotNone(self.state['paused'])

    def test_structured_challenge_preserves_retry_after_header(self):
        durable = self.durable()
        durable.preflight()
        client = personal.PersonalClient(durable)
        response = self.response(page(error='captcha').encode(),
                                 headers={'Retry-After': '10800'})
        with mock.patch.object(client.opener, 'open', return_value=response):
            with self.assertRaises(personal.Failure):
                client.search(personal.search_urls(DATE)[0], self.targets, DATE, NOW, {})
        self.assertEqual(personal.official.timestamp(self.state['paused']['retryAt']),
                         NOW + dt.timedelta(hours=3))

    def test_post_error_payload_refusal_pauses_without_treating_it_as_missing_id(self):
        durable = self.durable()
        durable.preflight()
        client = personal.PersonalClient(durable)
        response = self.response(b'{"errors":[{"message":"Could not authenticate"}]}')
        with mock.patch.object(client.opener, 'open', return_value=response):
            with self.assertRaisesRegex(personal.Failure, 'access_denied'):
                client.fetch_post(TID)
        self.assertIsNotNone(self.state['paused'])

    def test_shared_post_cooldown_is_checked_before_fetch(self):
        durable = self.durable()
        durable.preflight()
        personal.official.atomic_json(self.http, {'schemaVersion': 1, 'cooldowns': {
            personal.POST_HOST: personal.stamp(NOW + dt.timedelta(hours=1))}})
        client = personal.PersonalClient(durable)
        with self.assertRaisesRegex(personal.Failure, 'shared_host_cooldown'):
            client.fetch_post(TID)
        self.assertEqual(durable.used['posts'], 0)

    def test_fetch_reuses_importer_without_browser_headers_cookies_or_proxy(self):
        durable = self.durable()
        durable.preflight()
        client = personal.PersonalClient(durable)
        with mock.patch.object(client.opener, 'open',
                               return_value=self.response(json.dumps(post()).encode())) as opener:
            self.assertEqual(client.fetch_post(TID)['id_str'], TID)
        request = opener.call_args.args[0]
        self.assertEqual(request.header_items(), [])
        self.assertEqual(request.get_method(), 'GET')
        self.assertEqual(opener.call_args.kwargs['timeout'], 35)

    def test_stale_cache_and_oversized_response_rejected(self):
        for response, reason in ((self.response(b'{}', headers={'Age': '99999'}), 'stale_http_cache'),
                                  (self.response(b'x' * (personal.MAX_BODY + 1)), 'response_too_large')):
            durable = self.durable()
            durable.preflight()
            client = personal.PersonalClient(durable)
            with mock.patch.object(client.opener, 'open', return_value=response):
                with self.assertRaisesRegex(personal.Failure, reason):
                    client.fetch_post(TID)

    def test_no_new_paths_or_redirects(self):
        durable = self.durable()
        durable.preflight()
        client = personal.PersonalClient(durable)
        for url in ('https://x.com/amu_zettai', 'https://www.google.com/',
                    'https://search.yahoo.co.jp/realtime/search?p=from:amu_zettai'):
            with self.assertRaisesRegex(personal.Failure, 'route_refused'):
                client.open(urllib.request.Request(url))
        with mock.patch.object(client.opener, 'open',
                               side_effect=personal.Failure('redirect_refused', 302)):
            with self.assertRaisesRegex(personal.Failure, 'redirect_refused'):
                client.fetch_post(TID)
        self.assertEqual(self.state['paused']['reason'], 'redirect_refused')

    def test_post_reservation_save_error_is_not_reclassified_as_network_failure(self):
        durable = self.durable()
        durable.preflight()
        client = personal.PersonalClient(durable)
        with mock.patch.object(durable, 'save', side_effect=OSError('disk')):
            with self.assertRaises(OSError):
                client.fetch_post(TID)

    def test_post_transport_state_validation_error_is_not_invalid_post_json(self):
        durable = self.durable()
        durable.preflight()
        client = personal.PersonalClient(durable)
        with mock.patch.object(durable, 'reserve', side_effect=ValueError('invalid_transport_state')):
            with self.assertRaisesRegex(ValueError, 'invalid_transport_state'):
                client.fetch_post(TID)

    def test_challenge_pause_save_failure_stays_infrastructure_not_network(self):
        durable = self.durable()
        durable.preflight()
        client = personal.PersonalClient(durable)
        response = self.response(b'{"error":"Access denied"}')
        with (mock.patch.object(client.opener, 'open', return_value=response),
              mock.patch.object(durable, 'save_http', side_effect=[None, OSError('disk')])):
            with self.assertRaisesRegex(personal.InfrastructureFailure, 'transport_state_save_failed'):
                client.fetch_post(TID)

    def test_http_200_null_page_data_consumes_budget_but_saves_component_result(self):
        durable = self.durable(searches=1)
        client = personal.PersonalClient(durable)
        body = b'<script id="__NEXT_DATA__">{"props":{"pageProps":{"pageData":null}}}</script>'
        with mock.patch.object(client.opener, 'open', return_value=self.response(body)) as opener:
            report, code = self.collect(client, durable)
        self.assertEqual((report['status'], code), ('unavailable', 3))
        self.assertEqual(report['failures'][0]['reason'], 'invalid_search_response')
        self.assertEqual(opener.call_count, 1)
        saved = personal.read_state(self.snapshot)
        self.assertEqual(saved['lastRun']['status'], 'unavailable')
        self.assertEqual(saved['budgets'][DATE.isoformat()]['searches'], 1)
        self.assertEqual(saved['paused'], None)

    def test_wait_crossing_person_deadline_keeps_pending_without_http(self):
        clock = [dt.datetime(2026, 9, 6, 13, 29, 55, tzinfo=personal.JST)]

        def wait(seconds):
            clock[0] += dt.timedelta(seconds=seconds)
        item = candidate(target=RARAKO, uid='2065375500131028992')
        self.state['pending'] = [{
            **item, 'reason': 'discovered', 'firstSeenAt': personal.stamp(NOW),
            'lastAttemptAt': None, 'attempts': 0}]
        self.state['lastRequests'][personal.POST_HOST] = personal.stamp(
            clock[0] - dt.timedelta(seconds=5))
        durable = personal.DurableHttp(
            self.state, self.snapshot, self.http, DATE, self.targets, 2, 3,
            clock=lambda: clock[0], sleep=wait)
        client = personal.PersonalClient(durable)
        with (mock.patch.object(client, 'search', return_value=[]),
              mock.patch.object(client.opener, 'open') as opener):
            report, code = self.collect(client, durable)
        self.assertEqual(clock[0].timetz().replace(tzinfo=None), dt.time(13, 30, 2))
        self.assertIn('あむ', personal.active_targets(self.targets, DATE, clock[0]))
        opener.assert_not_called()
        self.assertEqual((code, report['deferredCount']), (2, 1))
        self.assertEqual(self.state['pending'][0]['reason'], 'outside_window')
        self.assertEqual(self.state['posts'], [])
        self.assertEqual(self.state['resolved'], [])
        self.assertEqual(self.state['budgets'][DATE.isoformat()]['posts'], 0)

    def test_same_wait_still_allows_person_with_night_original_shift(self):
        clock = [dt.datetime(2026, 9, 6, 13, 29, 55, tzinfo=personal.JST)]
        self.state['lastRequests'][personal.POST_HOST] = personal.stamp(
            clock[0] - dt.timedelta(seconds=5))

        def wait(seconds):
            clock[0] += dt.timedelta(seconds=seconds)
        durable = personal.DurableHttp(
            self.state, self.snapshot, self.http, DATE, self.targets, 2, 3,
            clock=lambda: clock[0], sleep=wait)
        durable.post_target = AMU['name']
        durable.preflight()
        client = personal.PersonalClient(durable)
        with mock.patch.object(client.opener, 'open',
                               return_value=self.response(json.dumps(post()).encode())) as opener:
            client.fetch_post(TID)
        self.assertEqual(opener.call_count, 1)
        self.assertEqual(self.state['budgets'][DATE.isoformat()]['posts'], 1)

    def test_scheduled_evening_cutoff_is_rechecked_after_spacing(self):
        clock = [dt.datetime(2026, 9, 6, 17, 59, 55, tzinfo=personal.JST)]

        def wait(seconds):
            clock[0] += dt.timedelta(seconds=seconds)
        durable = personal.DurableHttp(
            self.state, self.snapshot, self.http, DATE, self.targets, 2, 3,
            clock=lambda: clock[0], sleep=wait, scheduled=True)
        durable.post_target = AMU['name']
        durable.preflight()
        client = personal.PersonalClient(durable)
        with mock.patch.object(client.opener, 'open') as opener:
            with self.assertRaisesRegex(personal.Failure, 'outside_window'):
                client.fetch_post(TID)
        opener.assert_not_called()
        self.assertFalse(durable.analysis_allowed())
        self.assertEqual(self.state['budgets'][DATE.isoformat()]['posts'], 0)

    def test_source_returning_after_scheduled_deadline_cannot_start_analysis(self):
        clock = [dt.datetime(2026, 9, 6, 17, 59, 45, tzinfo=personal.JST)]

        def wait(seconds):
            clock[0] += dt.timedelta(seconds=seconds)

        def response_after_deadline(*args, **kwargs):
            clock[0] = dt.datetime(2026, 9, 6, 18, 0, 1, tzinfo=personal.JST)
            return self.response(json.dumps(post()).encode())
        self.state['pending'] = [{
            **candidate(), 'reason': 'discovered', 'firstSeenAt': personal.stamp(NOW),
            'lastAttemptAt': None, 'attempts': 0}]
        durable = personal.DurableHttp(
            self.state, self.snapshot, self.http, DATE, self.targets, 2, 3,
            clock=lambda: clock[0], sleep=wait, scheduled=True)
        durable.preflight()
        client = personal.PersonalClient(durable)
        analyzer = mock.Mock()
        with (mock.patch.object(client, 'search', return_value=[]),
              mock.patch.object(client.opener, 'open', side_effect=response_after_deadline)):
            report, code = personal.collect(
                self.state, durable, client, self.targets, DATE, 2, 3,
                clock=durable.clock, roster=self.schedule['roster'], analyzer=analyzer)
        analyzer.parse_with_timing.assert_not_called()
        self.assertEqual((code, report['deferredCount']), (2, 1))
        self.assertEqual(self.state['pending'][0]['reason'], 'outside_window')

    def test_deadline_is_rechecked_after_durable_reservation_save(self):
        clock = [dt.datetime(2026, 9, 6, 13, 29, 59, tzinfo=personal.JST)]
        durable = personal.DurableHttp(
            self.state, self.snapshot, self.http, DATE, self.targets, 2, 3,
            clock=lambda: clock[0], sleep=lambda _: None)
        durable.post_target = RARAKO['name']
        durable.preflight()
        save = durable.save

        def slow_save():
            save()
            clock[0] += dt.timedelta(seconds=2)
        client = personal.PersonalClient(durable)
        with (mock.patch.object(durable, 'save', side_effect=slow_save),
              mock.patch.object(client.opener, 'open') as opener):
            with self.assertRaisesRegex(personal.Failure, 'outside_window'):
                client.fetch_post(TID)
        opener.assert_not_called()
        self.assertEqual(self.state['budgets'][DATE.isoformat()]['posts'], 1)


if __name__ == '__main__':
    unittest.main()
