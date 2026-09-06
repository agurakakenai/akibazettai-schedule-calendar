"""Offline only: python -m unittest discover -s tools/tests."""
import contextlib
import copy
import datetime as dt
import hashlib
import http.client
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from unittest import mock
import urllib.error
import urllib.request
import uuid


TOOLS = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('collect_shifts', TOOLS / 'collect-shifts.py')
collector = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(collector)
NOW = dt.datetime(2026, 9, 5, 18, 50, tzinfo=collector.UTC)
CREATED = '2026-09-05T09:17:05Z'
START = dt.date(2026, 9, 4)
END = dt.date(2026, 9, 5)
TID = '2096165714604486679'
URL = collector.canonical(TID)


def make_id(created):
    delta = collector.timestamp(created) - dt.datetime(1970, 1, 1, tzinfo=collector.UTC)
    milliseconds = delta.days * 86400000 + delta.seconds * 1000 + delta.microseconds // 1000
    return str((milliseconds - 1288834974657) << 22)


OTHER = make_id('2026-09-04T03:00:00Z')


def payload(tid=TID, created=CREATED, names=('あむ', 'こい')):
    return {'id_str': tid,
            'user': {'id_str': collector.AUTHOR_ID, 'screen_name': collector.AUTHOR},
            'created_at': created,
            'text': '【アキバ絶対領域】\nよるにゃんこ\n\n' + '\n'.join(names) + '\n⊂(´ω´⊂)))'}


class FakeClient:
    def __init__(self, searches=None, posts=None):
        self.searches = searches if searches is not None else [[TID], [TID]]
        self.posts = posts if posts is not None else {TID: payload()}
        self.calls = []
        self.source_calls = []

    def begin_run(self):
        self.source_index = 0

    def search(self, url):
        self.source_calls.append(url)
        result = self.searches[self.source_index]
        self.source_index += 1
        if isinstance(result, Exception):
            raise result
        return result

    def fetch_post(self, tid):
        self.calls.append(tid)
        result = self.posts[tid]
        if isinstance(result, Exception):
            raise result
        return result


def collect(client=None, state=None, known=None, max_posts=20):
    return collector.collect(
        state or collector.empty_snapshot(), known or set(), client or FakeClient(),
        START, END, max_posts, clock=lambda: NOW)


def pending(tid=TID, reason='parse_failed'):
    return {'id': tid, 'url': collector.canonical(tid), 'reason': reason,
            'firstSeenAt': '2026-09-05T12:00:00Z',
            'lastAttemptAt': '2026-09-05T13:00:00Z', 'attempts': 1}


class DiscoveryTests(unittest.TestCase):
    def test_only_actual_official_urls_not_arbitrary_numbers(self):
        document = (
            '<div>2096165714604486679</div>'
            '<a href="https://x.com/other/status/2096165714604486679">other</a>'
            '<a href="https://x.com/akibazettai/status/2096165714604486679evil">bad</a>'
            '<a href="https://evil.example/akibazettai/status/2096165714604486679">bad</a>'
            f'<a href="{URL}">post</a>')
        self.assertEqual(collector.discover(document), [TID])

    def test_escaped_slashes_unicode_entities_and_duplicates(self):
        escaped = URL.replace('/', r'\/')
        unicode_url = URL.replace('/', r'\u002f').replace(':', r'\u003a')
        document = (f'<a href="{escaped}">one</a>'
                    f'<a href="{URL}?s=20&amp;t=a">two</a>'
                    f'<a href="{URL.replace("https:", "https&#58;")}">three</a>'
                    '<script id="__NEXT_DATA__">'
                    + json.dumps({'props': {'url': unicode_url}}) + '</script>')
        self.assertEqual(collector.discover(document), [TID])

    def test_json_ignores_quote_other_author_and_prose_urls(self):
        document = '<script id="__NEXT_DATA__">' + json.dumps({'items': [
            {'id': TID, 'user': {'screen_name': 'other'}, 'url': URL},
            {'user': {'screen_name': 'other'},
             'quoted_status': {'user': {'screen_name': collector.AUTHOR}, 'url': URL}},
            {'user': {'screen_name': collector.AUTHOR}, 'quotedTweet': {'url': URL}},
            {'text': URL, 'id': int(TID)},
            {'user': {'id_str': '123456789012345678', 'screen_name': collector.AUTHOR},
             'url': URL},
        ]}) + '</script>'
        self.assertEqual(collector.discover(document), [])

    def test_json_top_level_author_matches_and_ids_stay_exact(self):
        document = '<script id="__NEXT_DATA__">' + json.dumps({'items': [
            {'id': int(TID), 'user': {'id_str': collector.AUTHOR_ID,
                                     'screenName': collector.AUTHOR}, 'url': URL},
        ]}) + '</script>'
        self.assertEqual(collector.discover(document), [TID])

    def test_json_entities_inside_ignored_prose_do_not_break_structure(self):
        document = '<script id="__NEXT_DATA__">' + json.dumps({
            'text': '&quot;prose&quot;', 'url': URL + '?a=1&amp;b=2',
        }) + '</script>'
        self.assertEqual(collector.discover(document), [TID])

    def test_html_quote_links_and_unrelated_scripts_are_excluded(self):
        document = (f'<blockquote><a href="{URL}">quoted</a></blockquote>'
                    f'<div class="QuotedTweet"><a href="{URL}">quoted</a></div>'
                    f'<script>let url="{URL}";</script>'
                    '<script id="__NEXT_DATA__">{}</script>')
        self.assertEqual(collector.discover(document), [])

    def test_error_page_is_not_zero_result(self):
        with self.assertRaisesRegex(collector.FetchFailure, 'unrecognized_search_page'):
            collector.discover('<html><h1>Access denied</h1></html>')

    def test_explicit_zero_result_page(self):
        self.assertEqual(collector.discover('<p>検索結果はありません</p>'), [])

    def test_canonical_twitter_url_is_normalized(self):
        self.assertEqual(collector.discover(
            f'<a href="https://twitter.com/akibazettai/status/{TID}">link</a>'), [TID])


class ValidationTests(unittest.TestCase):
    def validate(self, value, tid=TID):
        return collector.validate_post(tid, value, START, END, NOW)

    def test_valid_post_exact_contract_and_no_alias_correction(self):
        post = self.validate(payload(names=('もな', 'あずにゃん', 'あむ')))
        self.assertEqual(set(post), {'id', 'url', 'authorId', 'authorScreenName',
                                     'createdAt', 'date', 'shift', 'storeId',
                                     'names', 'observedAt'})
        self.assertEqual(post['names'], ['もな', 'あずにゃん', 'あむ'])
        self.assertEqual((post['date'], post['shift'], post['storeId']),
                         ('2026-09-05', '夜', 's1'))

    def test_requires_both_author_id_and_handle(self):
        for key, value in [('id_str', '123456789012345678'),
                           ('screen_name', 'other'), ('screen_name', None),
                           ('id_str', None), ('id_str', float(collector.AUTHOR_ID))]:
            with self.subTest(key=key, value=value):
                item = payload()
                item['user'][key] = value
                with self.assertRaisesRegex(collector.FetchFailure, 'author_mismatch'):
                    self.validate(item)

    def test_integer_ids_supported_but_float_and_conflicting_ids_rejected(self):
        item = payload()
        item['id_str'] = int(TID)
        item['user']['id_str'] = int(collector.AUTHOR_ID)
        self.assertEqual(self.validate(item)['id'], TID)
        for invalid in (float(TID), OTHER, None):
            with self.subTest(invalid=invalid):
                item['id'] = invalid
                with self.assertRaisesRegex(collector.FetchFailure, 'response_id_mismatch'):
                    self.validate(item)

    def test_missing_response_id_rejected(self):
        item = payload()
        del item['id_str']
        with self.assertRaisesRegex(collector.FetchFailure, 'response_id_mismatch'):
            self.validate(item)

    def test_quoted_tweet_text_cannot_supply_outer_roster(self):
        item = payload()
        item['text'] = 'お知らせ'
        item['quoted_tweet'] = payload()
        self.assertIsNone(self.validate(item))
        item['text'] = None
        with self.assertRaisesRegex(collector.FetchFailure, 'missing_post_text'):
            self.validate(item)

    def test_other_author_cannot_borrow_quoted_official_metadata(self):
        item = payload()
        item['user']['screen_name'] = 'other'
        item['quoted_tweet'] = payload()
        with self.assertRaisesRegex(collector.FetchFailure, 'author_mismatch'):
            self.validate(item)

    def test_unconfirmed_name_never_overrides_parser_default(self):
        item = payload(names=('あむ', 'てすとにゃん'))
        with mock.patch.object(collector.IMPORTER, 'parse', wraps=collector.IMPORTER.parse) as parse:
            with self.assertRaisesRegex(collector.FetchFailure, 'parse_failed'):
                self.validate(item)
            self.assertEqual(parse.call_args.args, (item['text'], CREATED))
            self.assertEqual(parse.call_args.kwargs, {})

    def test_footer_boundary_is_reused(self):
        item = payload()
        item['text'] += '\nお知らせ\n\nまこと'
        self.assertEqual(self.validate(item)['names'], ['あむ', 'こい'])

    def test_requires_timezone_and_rejects_invalid_future_or_old_metadata(self):
        cases = [('2026-09-05T09:17:05', 'invalid_created_at'),
                 ('invalid', 'invalid_created_at'), (None, 'invalid_created_at'),
                 ('2026-09-06T00:00:00Z', 'future_post'),
                 ('2026-09-01T03:00:00Z', 'outside_date_range'),
                 ('2026-09-05T08:17:05Z', 'id_timestamp_mismatch')]
        for created, reason in cases:
            with self.subTest(created=created):
                item = payload(created=created)
                with self.assertRaisesRegex(collector.FetchFailure, reason):
                    self.validate(item)

    def test_jst_early_morning_and_five_am_boundary(self):
        for created, expected in [('2026-09-04T19:59:59Z', '2026-09-04'),
                                  ('2026-09-04T20:00:00Z', '2026-09-05'),
                                  ('2026-09-04T16:00:00Z', '2026-09-04')]:
            with self.subTest(created=created):
                tid = make_id(created)
                self.assertEqual(self.validate(payload(tid, created), tid)['date'], expected)

    def test_legacy_timestamp_is_explicitly_zoned(self):
        item = payload(created='Sat Sep 05 09:17:05 +0000 2026')
        self.assertEqual(self.validate(item)['createdAt'], CREATED)

    def test_store_and_shift_normalization(self):
        item = payload()
        item['text'] = '【アキバ絶対領域 Ａ．Ｄ．２０４５】\nヒルにゃんこ\n\nあむ\n'
        post = self.validate(item)
        self.assertEqual((post['storeId'], post['shift']), ('s4', '昼'))


class HttpTests(unittest.TestCase):
    def client(self):
        return collector.PublicClient(clock=lambda: NOW, sleep=mock.Mock(),
                                      monotonic=lambda: 100.0)

    def test_stale_cache_age_date_warning_and_future_date_rejected(self):
        cases = [({'Age': '3601'}, 'stale_http_cache'),
                 ({'Age': '121', 'Cache-Control': 'max-age=60'}, 'stale_http_cache'),
                 ({'Date': 'Fri, 04 Sep 2026 18:50:00 GMT'}, 'stale_http_cache'),
                 ({'Date': 'Sat, 05 Sep 2026 19:50:00 GMT'}, 'future_http_date'),
                 ({'Warning': '110 cache "stale"'}, 'stale_http_cache'),
                 ({'Age': 'bad'}, 'invalid_http_metadata'),
                 ({'Age': '-1'}, 'invalid_http_metadata'),
                 ({'Date': 'not a date'}, 'invalid_http_metadata'),
                 ({'Date': 'Sat, 05 Sep 2026 18:50:00'}, 'invalid_http_metadata')]
        for headers, reason in cases:
            with self.subTest(headers=headers):
                with self.assertRaisesRegex(collector.FetchFailure, reason):
                    collector.check_http_metadata(headers, NOW)

    def test_fresh_http_metadata(self):
        collector.check_http_metadata(
            {'Date': 'Sat, 05 Sep 2026 18:50:00 GMT', 'Age': '0',
             'Cache-Control': 'must-revalidate, max-age=60'}, NOW)

    def test_403_429_stop_same_host_and_respect_retry_after_across_runs(self):
        for status in (403, 429):
            with self.subTest(status=status):
                client = self.client()
                error = urllib.error.HTTPError(
                    collector.SEARCH_URLS[0], status, 'blocked',
                    {'Retry-After': '7200'}, None)
                with mock.patch.object(client.opener, 'open', side_effect=error) as opener:
                    with self.assertRaises(collector.FetchFailure) as result:
                        client.search(collector.SEARCH_URLS[0])
                    self.assertEqual(result.exception.status, status)
                    expected = NOW + dt.timedelta(hours=2)
                    self.assertEqual(result.exception.retry_at, expected)
                    for _ in range(2):
                        with self.assertRaisesRegex(collector.FetchFailure, 'host_rate_limited'):
                            client.search(collector.SEARCH_URLS[1])
                        client.begin_run()
                    self.assertEqual(opener.call_count, 1)

    def test_retry_after_http_date(self):
        client = self.client()
        error = urllib.error.HTTPError(collector.SEARCH_URLS[0], 429, 'blocked',
                                      {'Retry-After': 'Sat, 05 Sep 2026 20:50:00 GMT'}, None)
        with mock.patch.object(client.opener, 'open', side_effect=error):
            with self.assertRaises(collector.FetchFailure) as result:
                client.search(collector.SEARCH_URLS[0])
        self.assertEqual(result.exception.retry_at, NOW + dt.timedelta(hours=2))

    def test_existing_fetch_is_reused_with_header_validation_and_two_second_spacing(self):
        client = self.client()
        response = mock.Mock()
        response.getcode.return_value = 200
        response.headers = {}
        response.read.return_value = json.dumps(payload()).encode()
        original_urlopen = urllib.request.urlopen
        with mock.patch.object(client.opener, 'open', return_value=response) as opener:
            self.assertEqual(client.fetch_post(TID)['id_str'], TID)
            client.fetch_post(TID)
        self.assertIs(urllib.request.urlopen, original_urlopen)
        self.assertEqual(opener.call_count, 2)
        client.sleep.assert_called_once_with(2.0)
        self.assertIn('tweet-result?id=' + TID, opener.call_args.args[0].full_url)

    def test_public_get_does_not_forward_browser_or_auth_headers(self):
        client = self.client()
        response = mock.Mock()
        response.getcode.return_value = 200
        response.headers = {}
        response.read.return_value = json.dumps(payload()).encode()
        request = urllib.request.Request(URL.replace(
            'https://x.com/akibazettai/status/', 'https://cdn.syndication.twimg.com/tweet-result?id='),
            headers={'User-Agent': 'browser-identity', 'Cookie': 'private', 'Authorization': 'private'})
        with mock.patch.object(client.opener, 'open', return_value=response) as opener:
            with client.open(request):
                pass
        self.assertEqual(opener.call_args.args[0].header_items(), [])

    def test_no_unapproved_routes_or_redirects(self):
        client = self.client()
        with mock.patch.object(client.opener, 'open') as opener:
            for url in ('https://x.com/akibazettai', 'https://www.google.com/',
                        'https://search.yahoo.co.jp/realtime/search?p=from%3Aakibazettai'):
                with self.assertRaisesRegex(collector.FetchFailure, 'route_refused'):
                    client.open(urllib.request.Request(url))
            opener.assert_not_called()
        with self.assertRaisesRegex(collector.FetchFailure, 'redirect_refused'):
            collector.NoRedirect().redirect_request(None, None, 302, '', {}, 'https://x.com/')

    def test_stale_response_is_closed_and_never_parsed(self):
        client = self.client()
        response = mock.Mock()
        response.getcode.return_value = 200
        response.headers = {'Age': '9000'}
        with mock.patch.object(client.opener, 'open', return_value=response):
            with self.assertRaisesRegex(collector.FetchFailure, 'stale_http_cache'):
                client.fetch_post(TID)
        response.read.assert_not_called()
        response.close.assert_called_once()

    def test_invalid_json_and_read_failure_are_short_codes(self):
        for data, reason in [(b'{', 'invalid_post_json'),
                             (OSError('private-response-body'), 'network_error'),
                             (http.client.IncompleteRead(b'private-response-body'),
                              'network_error')]:
            with self.subTest(reason=reason):
                client = self.client()
                response = mock.Mock()
                response.getcode.return_value = 200
                response.headers = {}
                if isinstance(data, Exception):
                    response.read.side_effect = data
                else:
                    response.read.return_value = data
                with mock.patch.object(client.opener, 'open', return_value=response):
                    with self.assertRaisesRegex(collector.FetchFailure, reason):
                        client.fetch_post(TID)


class CollectionTests(unittest.TestCase):
    def test_first_and_second_run_only_append_once_and_skip_before_get(self):
        client = FakeClient()
        first, report, code = collect(client)
        self.assertEqual((code, report['status'], report['newPostCount']), (0, 'ok', 1))
        self.assertEqual(client.calls, [TID])
        client.calls.clear()
        second, report, code = collect(client, first)
        self.assertEqual((code, report['status']), (0, 'no-new'))
        self.assertEqual(second['posts'], first['posts'])
        self.assertEqual(client.calls, [])

    def test_curated_csv_id_is_skipped_before_fetch(self):
        client = FakeClient()
        result, report, code = collect(client, known={TID})
        self.assertEqual(result['posts'], [])
        self.assertEqual(client.calls, [])
        self.assertEqual((code, report['skippedCuratedCount'], report['status']),
                         (0, 1, 'no-new'))

    def test_zero_results_keeps_old_posts_and_updates_success(self):
        old, _, _ = collect()
        result, report, code = collect(FakeClient([[], []]), old)
        self.assertEqual((code, report['status']), (0, 'no-results'))
        self.assertEqual(result['posts'], old['posts'])
        self.assertFalse(result['complete'])
        self.assertIsNotNone(result['lastSuccessAt'])

    def test_saved_observations_survive_rollover_outside_new_date_window(self):
        old, _, _ = collect()
        client = FakeClient([[], []])
        result, report, code = collector.collect(
            old, set(), client, dt.date(2026, 9, 6), dt.date(2026, 9, 7), 20,
            clock=lambda: NOW + dt.timedelta(days=2))
        self.assertEqual(result['posts'], old['posts'])
        self.assertEqual(client.calls, [])
        self.assertEqual((code, report['status']), (0, 'no-results'))

    def test_search_host_limit_stops_remaining_query_and_survives_restart(self):
        for status in (403, 429):
            with self.subTest(status=status):
                until = NOW + dt.timedelta(hours=2)
                client = FakeClient([collector.FetchFailure('http_error', status, until),
                                     AssertionError('must not request second keyword')])
                first, report, code = collect(client)
                self.assertEqual(client.source_calls, [collector.SEARCH_URLS[0]])
                self.assertEqual((code, report['status']), (3, 'unavailable'))
                self.assertEqual(first['cooldowns']['search.yahoo.co.jp'], collector.iso(until))
                restarted = FakeClient()
                second, _, _ = collect(restarted, first)
                self.assertEqual(restarted.source_calls, [])
                self.assertEqual(second['cooldowns'], first['cooldowns'])
                after, _, code = collector.collect(
                    second, set(), restarted, START, END, 20,
                    clock=lambda: NOW + dt.timedelta(hours=3))
                self.assertEqual(len(after['posts']), 1)
                self.assertEqual(code, 0)

    def test_search_limit_after_success_keeps_facts_as_partial(self):
        client = FakeClient([[TID], collector.FetchFailure('http_error', 429)])
        result, report, code = collect(client)
        self.assertEqual((code, report['status']), (2, 'partial'))
        self.assertEqual(len(result['posts']), 1)
        self.assertIsNone(result['lastSuccessAt'])

    def test_both_sources_fail_keeps_old_posts_and_success_time(self):
        old, _, _ = collect()
        old['lastSuccessAt'] = '2026-09-05T17:00:00Z'
        fail = collector.FetchFailure('network_error')
        result, report, code = collect(FakeClient([fail, fail]), old)
        self.assertEqual((code, report['status']), (3, 'unavailable'))
        self.assertEqual(result['posts'], old['posts'])
        self.assertEqual(result['lastSuccessAt'], old['lastSuccessAt'])

    def test_one_source_failure_saves_verified_facts_but_is_partial(self):
        fail = collector.FetchFailure('http_error', 500)
        result, report, code = collect(FakeClient([fail, [TID]]))
        self.assertEqual((code, report['status'], report['sourceCount']), (2, 'partial', 1))
        self.assertEqual(len(result['posts']), 1)
        self.assertIsNone(result['lastSuccessAt'])

    def test_failed_post_stays_pending_and_is_retried_even_if_not_rediscovered(self):
        failed = FakeClient(posts={TID: collector.FetchFailure('network_error')})
        first, report, code = collect(failed)
        self.assertEqual(code, 2)
        self.assertEqual(first['pending'][0]['id'], TID)
        retry = FakeClient([[], []])
        second, report, code = collect(retry, first)
        self.assertEqual(retry.calls, [TID])
        self.assertEqual(second['pending'], [])
        self.assertEqual(len(second['posts']), 1)
        self.assertEqual(code, 0)

    def test_parse_failure_never_becomes_success_and_contains_no_body(self):
        value = payload(names=('てすとにゃん',))
        result, report, code = collect(FakeClient(posts={TID: value}))
        self.assertEqual((code, result['pending'][0]['reason']), (2, 'parse_failed'))
        encoded = json.dumps(report, ensure_ascii=False)
        self.assertNotIn(value['text'], encoded)
        self.assertNotIn('てすとにゃん', encoded)
        self.assertNotIn('text', report)

    def test_pending_outside_current_range_is_retained_without_get(self):
        old = collector.empty_snapshot()
        tid = make_id('2026-09-01T03:00:00Z')
        old['pending'] = [pending(tid)]
        client = FakeClient([[], []])
        result, report, code = collect(client, old)
        self.assertEqual(result['pending'], old['pending'])
        self.assertEqual(client.calls, [])
        self.assertEqual((code, report['status']), (2, 'partial'))
        self.assertEqual(report['pendingOutsideRangeCount'], 1)
        self.assertEqual(result['lastSuccessAt'], old['lastSuccessAt'])

    def test_cap_defers_without_losing_pending_or_starving_old_failures(self):
        old = collector.empty_snapshot()
        old['pending'] = [pending(OTHER)]
        client = FakeClient(posts={OTHER: payload(OTHER, '2026-09-04T03:00:00Z')})
        result, report, code = collect(client, old, max_posts=1)
        self.assertEqual(client.calls, [OTHER])
        self.assertEqual([item['id'] for item in result['pending']], [TID])
        self.assertEqual(result['pending'][0]['reason'], 'post_limit')
        self.assertEqual((code, report['deferredCount']), (2, 1))

    def test_individual_403_429_stops_host_for_remaining_candidates(self):
        for status in (403, 429):
            with self.subTest(status=status):
                client = FakeClient([[TID, OTHER], []],
                                    {TID: collector.FetchFailure('http_error', status)})
                result, report, code = collect(client)
                self.assertEqual(client.calls, [TID])
                self.assertEqual(len(result['pending']), 2)
                self.assertEqual(report['deferredCount'], 1)
                self.assertEqual(code, 2)

    def test_post_retry_after_survives_new_client_and_preserves_pending(self):
        until = NOW + dt.timedelta(hours=2)
        first, _, _ = collect(FakeClient(
            [[TID, OTHER], []], {TID: collector.FetchFailure('http_error', 429, until)}))
        restarted = FakeClient([[TID, OTHER], []])
        second, report, code = collect(restarted, first)
        self.assertEqual(restarted.calls, [])
        self.assertEqual(len(second['pending']), 2)
        self.assertEqual((code, report['status'], report['deferredCount']), (2, 'partial', 2))
        self.assertTrue(all(item['retryAt'] == collector.iso(until) for item in second['pending']))

    def test_response_id_mismatch_stays_pending_and_existing_facts_unchanged(self):
        old, _, _ = collect()
        client = FakeClient([[OTHER], []], {OTHER: payload()})
        result, report, code = collect(client, old)
        self.assertEqual(result['posts'], old['posts'])
        self.assertEqual(result['pending'][0]['reason'], 'response_id_mismatch')
        self.assertEqual(code, 2)

    def test_future_and_old_candidates_are_explicitly_rejected_before_get(self):
        future = make_id('2026-09-06T03:00:00Z')
        old = make_id('2026-09-01T03:00:00Z')
        client = FakeClient([[old, future], []])
        _, report, code = collect(client)
        self.assertEqual(client.calls, [])
        self.assertEqual({item['reason'] for item in report['rejected']},
                         {'outside_date_range', 'future_candidate'})
        self.assertEqual(code, 0)

    def test_input_snapshot_is_not_mutated(self):
        old = collector.empty_snapshot()
        before = copy.deepcopy(old)
        collect(state=old)
        self.assertEqual(old, before)


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.directory = TOOLS / 'tests' / ('collector-test-' + uuid.uuid4().hex)
        self.directory.mkdir()
        self.addCleanup(lambda: shutil.rmtree(self.directory))
        self.snapshot = self.directory / 'observed.json'
        self.curated = self.directory / 'shifts.csv'
        self.curated.write_text('date,store,shift,maid,tweet_id\n', encoding='utf-8')

    def args(self, *extra):
        return collector.argument_parser().parse_args(list(extra))

    def run_collector(self, args=None, client=None, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()):
            return collector.run(args or self.args(), self.snapshot, self.curated,
                                 client or FakeClient(), clock=lambda: NOW, **kwargs)

    def run_child(self, body):
        code = (
            "import importlib.util, pathlib, sys\n"
            f"spec=importlib.util.spec_from_file_location('collector', {str(TOOLS / 'collect-shifts.py')!r})\n"
            "module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)\n"
            + body)
        return subprocess.run(
            [sys.executable, '-B', '-c', code], capture_output=True, cwd=collector.ROOT,
            timeout=20, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))

    def test_dry_run_limit_is_respected_by_a_fresh_process_without_saving_facts(self):
        collector.atomic_json(self.snapshot, collector.empty_snapshot())
        before = self.snapshot.read_bytes()
        client = FakeClient([collector.FetchFailure(
            'http_error', 429, NOW + dt.timedelta(hours=2)),
            AssertionError('second keyword must be blocked')])
        self.assertEqual(self.run_collector(self.args('--dry-run'), client), 3)
        self.assertEqual(self.snapshot.read_bytes(), before)
        child = self.run_child(
            f"now=module.timestamp({NOW.isoformat()!r})\n"
            "client=module.PublicClient(clock=lambda:now)\n"
            "def forbidden(*a,**k): raise AssertionError('unexpected HTTP request before Retry-After')\n"
            "client.opener.open=forbidden\n"
            "args=module.argument_parser().parse_args(['--dry-run'])\n"
            f"sys.exit(module.run(args, snapshot=pathlib.Path({str(self.snapshot)!r}), "
            f"curated=pathlib.Path({str(self.curated)!r}), client=client, clock=lambda:now))\n")
        self.assertEqual(child.returncode, 3, child.stderr.decode())
        self.assertEqual(self.snapshot.read_bytes(), before)

    def test_dry_post_limit_is_shared_between_durable_and_default_paths(self):
        collector.atomic_json(self.snapshot, collector.empty_snapshot())
        before = self.snapshot.read_bytes()
        durable = self.directory / 'durable.json'
        limited = FakeClient(posts={TID: collector.FetchFailure(
            'http_error', 429, NOW + dt.timedelta(hours=2))})
        self.assertEqual(self.run_collector(self.args(
            '--dry-run', '--snapshot', str(durable), '--publish', str(self.snapshot)), limited), 2)
        self.assertFalse(durable.exists())
        fresh = FakeClient(posts={TID: AssertionError('post host must remain blocked')})
        self.assertEqual(self.run_collector(self.args('--dry-run'), fresh), 2)
        self.assertEqual(fresh.calls, [])
        self.assertEqual(self.snapshot.read_bytes(), before)

    def test_transport_storage_failure_stops_before_any_http(self):
        client = FakeClient()
        with mock.patch.object(collector, 'atomic_json', side_effect=OSError('read only')):
            with self.assertRaises(OSError):
                self.run_collector(self.args('--dry-run'), client)
        self.assertEqual(client.source_calls, [])
        self.assertEqual(client.calls, [])

    def test_non_shift_resolution_survives_failed_publish_and_later_process(self):
        state = collector.empty_snapshot()
        state['pending'] = [pending()]
        collector.atomic_json(self.snapshot, state)
        durable = self.directory / 'durable.json'
        report = self.directory / 'report.json'
        original = collector.atomic_json
        non_shift = payload()
        non_shift['text'] = 'お知らせ'

        def save(path, value):
            if path == self.snapshot:
                raise OSError('publication blocked')
            return original(path, value)

        with mock.patch.object(collector, 'atomic_json', side_effect=save):
            code = self.run_collector(self.args(
                '--snapshot', str(durable), '--publish', str(self.snapshot), '--report', str(report)),
                FakeClient(posts={TID: non_shift}))
        self.assertEqual(code, 4)
        self.assertEqual(collector.load_snapshot(durable)['pending'], [])
        self.assertEqual(len(collector.load_snapshot(self.snapshot)['pending']), 1)
        child = self.run_child(
            "class Client:\n"
            " def begin_run(self): pass\n"
            " def search(self,url): return []\n"
            " def fetch_post(self,tid): raise AssertionError('resolved post was fetched')\n"
            f"args=module.argument_parser().parse_args(['--snapshot',{str(durable)!r},'--publish',{str(self.snapshot)!r}])\n"
            f"now=module.timestamp({(NOW + dt.timedelta(days=2)).isoformat()!r})\n"
            f"sys.exit(module.run(args, curated=pathlib.Path({str(self.curated)!r}), client=Client(),clock=lambda:now))\n")
        self.assertEqual(child.returncode, 0, child.stdout.decode() + child.stderr.decode())
        self.assertEqual(collector.load_snapshot(durable)['pending'], [])
        self.assertEqual(collector.load_snapshot(self.snapshot)['pending'], [])

    def test_atomic_snapshot_roundtrip_and_no_scratch_remains(self):
        state, _, _ = collect()
        collector.atomic_json(self.snapshot, state)
        self.assertEqual(collector.load_snapshot(self.snapshot), state)
        self.assertEqual(list(self.directory.glob('*.tmp')), [])
        self.assertTrue(self.snapshot.read_bytes().endswith(b'\n'))

    def test_durable_state_is_seeded_from_existing_frontend_without_loss(self):
        old, _, _ = collect()
        collector.atomic_json(self.snapshot, old)
        durable = self.directory / 'durable' / 'observations.json'
        client = FakeClient([[], []])
        args = self.args('--once', '--snapshot', str(durable), '--publish', str(self.snapshot))
        self.assertEqual(self.run_collector(args, client), 0)
        self.assertEqual(collector.load_snapshot(durable)['posts'], old['posts'])
        self.assertEqual(collector.load_snapshot(durable), collector.load_snapshot(self.snapshot))
        self.snapshot.unlink()
        self.assertEqual(self.run_collector(args, FakeClient([[], []])), 0)
        self.assertEqual(collector.load_snapshot(self.snapshot)['posts'], old['posts'])

    def test_published_new_facts_merge_without_duplicate_or_overwriting_old_facts(self):
        old, _, _ = collect()
        newer = copy.deepcopy(old)
        newer['posts'].append(collector.validate_post(
            OTHER, payload(OTHER, '2026-09-04T03:00:00Z'), START, END, NOW))
        merged = collector.merge_snapshots(old, newer)
        self.assertEqual(len(merged['posts']), 2)
        self.assertEqual(len(collector.merge_snapshots(merged, newer)['posts']), 2)
        newer['posts'][0]['names'] = ['changed']
        with self.assertRaisesRegex(ValueError, 'observation_conflict'):
            collector.merge_snapshots(old, newer)

    def test_durable_task_and_default_collector_share_the_publication_lock(self):
        durable = self.directory / 'durable.json'
        args = self.args('--snapshot', str(durable), '--publish', str(self.snapshot))
        client = FakeClient()
        for path in (durable, self.snapshot):
            with self.subTest(path=path), collector.ProcessLock(path.with_suffix('.lock')):
                with self.assertRaisesRegex(ValueError, 'collector_locked'):
                    self.run_collector(args, client)
        self.assertEqual(client.source_calls, [])

    def test_publication_failure_keeps_durable_facts_and_reports_failure(self):
        durable = self.directory / 'durable.json'
        report = self.directory / 'report.json'
        collector.atomic_json(self.snapshot, collector.empty_snapshot())
        previous = self.snapshot.read_bytes()
        original = collector.atomic_json

        def save(path, value):
            if path == self.snapshot:
                raise OSError('publication blocked')
            return original(path, value)

        with mock.patch.object(collector, 'atomic_json', side_effect=save):
            code = self.run_collector(self.args(
                '--snapshot', str(durable), '--publish', str(self.snapshot), '--report', str(report)))
        self.assertEqual(code, 4)
        self.assertEqual(self.snapshot.read_bytes(), previous)
        self.assertEqual(len(collector.load_snapshot(durable)['posts']), 1)
        result = json.loads(report.read_text(encoding='utf-8'))
        self.assertEqual(result['reason'], 'publication_failed')
        self.assertTrue(result['saved'])
        self.assertFalse(result['published'])

    def test_atomic_replace_and_fsync_failures_preserve_old_bytes(self):
        collector.atomic_json(self.snapshot, collector.empty_snapshot())
        initial = self.snapshot.read_bytes()
        for target in ('replace', 'fsync'):
            with self.subTest(target=target):
                with mock.patch.object(collector.os, target, side_effect=OSError('disk failed')):
                    with self.assertRaises(OSError):
                        self.run_collector()
                self.assertEqual(self.snapshot.read_bytes(), initial)
                self.assertEqual(list(self.directory.glob('*.tmp')), [])

    def test_unexpected_interruption_keeps_snapshot_and_releases_lock(self):
        collector.atomic_json(self.snapshot, collector.empty_snapshot())
        initial = self.snapshot.read_bytes()
        client = FakeClient()
        client.fetch_post = mock.Mock(side_effect=KeyboardInterrupt)
        with self.assertRaises(KeyboardInterrupt):
            self.run_collector(client=client)
        self.assertEqual(self.snapshot.read_bytes(), initial)
        with collector.ProcessLock(self.snapshot.with_suffix('.lock')):
            pass

    def test_dry_run_does_not_change_snapshot_or_curated(self):
        collector.atomic_json(self.snapshot, collector.empty_snapshot())
        before = self.snapshot.read_bytes(), self.curated.read_bytes()
        self.assertEqual(self.run_collector(self.args('--dry-run')), 0)
        self.assertEqual((self.snapshot.read_bytes(), self.curated.read_bytes()), before)

    def test_curated_and_real_statistics_unchanged(self):
        paths = [collector.CURATED, collector.ROOT / 'data' / 'schedule.js',
                 collector.ROOT / 'data' / 'store-insights.js',
                 collector.ROOT / 'tools' / 'data' / 'forecast-log.csv']
        before = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}
        self.run_collector()
        self.assertEqual(before, {
            path: hashlib.sha256(path.read_bytes()).hexdigest() for path in paths})

    def test_invalid_snapshot_is_not_replaced(self):
        self.snapshot.write_text('{"posts": []}', encoding='utf-8')
        before = self.snapshot.read_bytes()
        with self.assertRaisesRegex(ValueError, 'invalid_snapshot'):
            self.run_collector()
        self.assertEqual(self.snapshot.read_bytes(), before)

    def test_duplicate_single_run_lock_refused_without_network(self):
        client = FakeClient()
        with collector.ProcessLock(self.snapshot.with_suffix('.lock')):
            with self.assertRaisesRegex(ValueError, 'collector_locked'):
                self.run_collector(client=client)
        self.assertEqual(client.source_calls, [])

    def test_watch_retains_lock_between_runs_and_releases_on_stop(self):
        sleeps = []

        def stop(interval):
            sleeps.append(interval)
            with self.assertRaisesRegex(ValueError, 'collector_locked'):
                with collector.ProcessLock(self.snapshot.with_suffix('.lock')):
                    pass
            raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            self.run_collector(self.args('--watch'), sleep=stop)
        self.assertEqual(sleeps, [3600])
        with collector.ProcessLock(self.snapshot.with_suffix('.lock')):
            pass
        self.assertEqual(len(collector.load_snapshot(self.snapshot)['posts']), 1)

    def test_dead_process_lock_is_released_without_pid_probing(self):
        lock = self.snapshot.with_suffix('.lock')
        code = (
            "import importlib.util, pathlib, os\n"
            f"spec=importlib.util.spec_from_file_location('collector', {str(TOOLS / 'collect-shifts.py')!r})\n"
            "module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)\n"
            f"lock=module.ProcessLock(pathlib.Path({str(lock)!r})); lock.__enter__()\n"
            "os._exit(0)\n")
        kwargs = {}
        if os.name == 'nt':
            kwargs['creationflags'] = subprocess.CREATE_NO_WINDOW
        result = subprocess.run([sys.executable, '-c', code], capture_output=True,
                                cwd=collector.ROOT, timeout=20, **kwargs)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        with collector.ProcessLock(lock):
            pass

    def test_separate_process_cannot_acquire_live_lock(self):
        lock = self.snapshot.with_suffix('.lock')
        code = (
            "import importlib.util, pathlib, sys\n"
            f"spec=importlib.util.spec_from_file_location('collector', {str(TOOLS / 'collect-shifts.py')!r})\n"
            "module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)\n"
            "try:\n"
            f"    module.ProcessLock(pathlib.Path({str(lock)!r})).__enter__()\n"
            "except ValueError as exc:\n"
            "    sys.exit(4 if str(exc) == 'collector_locked' else 5)\n"
            "sys.exit(6)\n")
        kwargs = {}
        if os.name == 'nt':
            kwargs['creationflags'] = subprocess.CREATE_NO_WINDOW
        with collector.ProcessLock(lock):
            result = subprocess.run([sys.executable, '-c', code], capture_output=True,
                                    cwd=collector.ROOT, timeout=20, **kwargs)
        self.assertEqual(result.returncode, 4, result.stderr.decode())

    def test_report_is_fact_only_json_and_does_not_replace_snapshot(self):
        report = self.directory / 'report.json'
        self.run_collector(self.args('--report', str(report)))
        result = json.loads(report.read_text(encoding='utf-8'))
        self.assertEqual(result['newPostCount'], 1)
        self.assertTrue(result['saved'])
        self.assertNotIn('text', result['newFacts'][0])
        self.assertNotIn('newFacts', collector.load_snapshot(self.snapshot))

    def test_report_cannot_overwrite_canonical_or_curated(self):
        for path in (self.snapshot, self.curated, collector.ROOT / 'index.html'):
            with self.subTest(path=path):
                with self.assertRaisesRegex(ValueError, 'unsafe_report_path'):
                    self.run_collector(self.args('--report', str(path)))


class CliTests(unittest.TestCase):
    def test_default_jst_service_days_before_and_after_five(self):
        args = collector.argument_parser().parse_args([])
        self.assertEqual(collector.date_range(args, NOW), (START, END))
        after_five = dt.datetime(2026, 9, 5, 20, 0, tzinfo=collector.UTC)
        self.assertEqual(collector.date_range(args, after_five),
                         (dt.date(2026, 9, 5), dt.date(2026, 9, 6)))

    def test_explicit_range_and_invalid_range(self):
        args = collector.argument_parser().parse_args([
            '--date-from', '2026-08-01', '--date-to', '2026-08-03'])
        self.assertEqual(collector.date_range(args, NOW),
                         (dt.date(2026, 8, 1), dt.date(2026, 8, 3)))
        args.date_to = '2026-07-31'
        with self.assertRaises(ValueError):
            collector.date_range(args, NOW)

    def test_cli_enforces_bounded_requests_and_interval(self):
        for argv in (['--max-posts', '21'], ['--max-posts', '0'],
                     ['--interval', '1'], ['--days', '0'], ['--once', '--watch']):
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    collector.main(argv)

    def test_refresh_default_and_limit(self):
        self.assertEqual(collector.argument_parser().parse_args([]).refresh_known, 0)
        for number in ('-1', '4'):
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    collector.main(['--refresh-known', number])


NOTICE_ID = '2096436633973526890'
NOTICE_CREATED = '2026-09-06T03:13:38Z'
NOTICE_DATE = dt.date(2026, 9, 6)
NOTICE_OBSERVED = dt.datetime(2026, 9, 6, 5, tzinfo=collector.UTC)
NOTICE_NOW = dt.datetime(2026, 9, 6, 6, 5, tzinfo=collector.UTC)
NOTICE_NAMES = ['るるか', 'ちぇる', 'まこと']
SHORT_NOTICE = 'みりあちゃんもあとから来るにゃんね~'
NOTICE_EVIDENCE = {'るるか': 'るるか', 'ちぇる': 'ちぇる', 'みりあ': 'みりあ',
                   'あむ': 'あむ', 'まこと': 'まこっちゃん', 'まこっちゃん': 'まこっちゃん'}
EDIT_CREATED = '2026-09-06T03:20:00Z'
EDIT_ID = make_id(EDIT_CREATED)
NEXT_EDIT_CREATED = '2026-09-06T03:25:00Z'
NEXT_EDIT_ID = make_id(NEXT_EDIT_CREATED)


def notice_payload(footer=SHORT_NOTICE, names=NOTICE_NAMES, tid=NOTICE_ID,
                   created=NOTICE_CREATED, shift='ひる', store='A.D.2045'):
    value = payload(tid, created, names)
    value['text'] = ('【アキバ絶対領域 ' + store + '】\n' + shift + 'にゃんこ\n\n'
                     + '\n'.join(names))
    if footer:
        value['text'] += '\n\n' + footer + '⊂(´ω´⊂)))'
    return value


def edit_payload(ids, tid=NOTICE_ID, created=NOTICE_CREATED,
                 names=NOTICE_NAMES, footer=SHORT_NOTICE):
    value = notice_payload(footer, names, tid, created)
    value.update(isEdited=len(ids) > 1, isStaleEdit=tid != str(ids[-1]),
                 edit_control={'edit_tweet_ids': ids})
    return value


class NoticeRefreshTests(unittest.TestCase):
    def setUp(self):
        self.directory = TOOLS / 'tests' / ('official-refresh-' + uuid.uuid4().hex)
        self.directory.mkdir()
        self.addCleanup(lambda: shutil.rmtree(self.directory))
        self.evidence_patch = mock.patch.object(collector, 'notice_name_evidence',
                                                return_value=NOTICE_EVIDENCE)
        self.evidence_patch.start()
        self.addCleanup(self.evidence_patch.stop)
        network = mock.patch.object(urllib.request.OpenerDirector, 'open',
                                    side_effect=AssertionError('Live HTTP forbidden'))
        network.start()
        self.addCleanup(network.stop)

    def validate(self, value=None, now=NOTICE_OBSERVED):
        value = notice_payload() if value is None else value
        return collector.validate_post(value['id_str'], value, NOTICE_DATE, NOTICE_DATE, now)

    def state(self, footer=None, names=NOTICE_NAMES):
        state = collector.empty_snapshot()
        state['posts'] = [self.validate(notice_payload(footer, names))]
        return state

    def refresh(self, state, value=None, now=NOTICE_NOW, max_posts=20,
                refresh_known=3, client=None):
        client = client or FakeClient([[], []], {
            NOTICE_ID: notice_payload() if value is None else value})
        result = collector.collect(state, set(), client, NOTICE_DATE, NOTICE_DATE,
                                   max_posts, clock=lambda: now, refresh_known=refresh_known)
        return (*result, client)

    def test_verified_minimal_footer_adds_notice_without_changing_three_raw_names(self):
        value = notice_payload()
        value.update(isEdited=False, isStaleEdit=False, edit_tweet_ids=[NOTICE_ID])
        post = self.validate(value)
        self.assertEqual(post['names'], NOTICE_NAMES)
        self.assertEqual((post['date'], post['shift'], post['storeId']),
                         ('2026-09-06', '昼', 's4'))
        self.assertEqual(post['notices'], [{
            'name': 'みりあ', 'kind': 'late', 'excerpt': SHORT_NOTICE}])
        self.assertNotIn('time', post['notices'][0])
        self.assertNotIn('text', post)

    def test_short_arrival_prose_cannot_become_a_fourth_roster_name(self):
        value = notice_payload('みりあちゃんも遅れて合流')
        post = self.validate(value)
        self.assertEqual(post['names'], NOTICE_NAMES)
        self.assertEqual(post['notices'][0]['name'], 'みりあ')
        for footer in ('あの遅れて合流', '遅れてみりあ合流'):
            post = self.validate(notice_payload(footer))
            self.assertEqual(post['names'], NOTICE_NAMES)
            self.assertNotIn('notices', post)

    def test_confirmed_alias_is_recognized_but_raw_name_is_not_rewritten(self):
        value = notice_payload('まこっちゃんも遅れて合流します', names=['るるか'])
        self.assertEqual(self.validate(value)['notices'][0]['name'], 'まこっちゃん')
        value = notice_payload('まこっちゃんも遅れて合流します', names=['まこと'])
        self.assertNotIn('notices', self.validate(value))

    def test_real_local_name_sources_include_verified_roster_and_alias(self):
        self.evidence_patch.stop()
        try:
            evidence = collector.notice_name_evidence()
            self.assertIn('みりあ', evidence)
            self.assertEqual(evidence['まこと'], evidence['まこっちゃん'])
        finally:
            self.evidence_patch.start()

    def test_explicit_time_only_and_same_store_are_supported(self):
        value = notice_payload('4号店のみりあちゃんも14:30から遅れて合流します')
        self.assertEqual(self.validate(value)['notices'][0]['time'], '14:30')
        for raw in ('24:30', '14:99', '14-16'):
            with self.subTest(raw=raw):
                self.assertNotIn('notices', self.validate(notice_payload(
                    'みりあちゃんも' + raw + 'から遅れて合流します')))

    def test_negative_future_quoted_question_old_correction_and_third_party_are_not_notices(self):
        footers = [
            'みりあちゃんもあとから来ないにゃんね',
            '明日みりあちゃんもあとから来るにゃんね',
            '昨日みりあちゃんもあとから来ると言っていた',
            '「みりあちゃんもあとから来るにゃんね」',
            '「お知らせ\n\nみりあちゃんもあとから来るにゃんね\n\n」',
            'みりあちゃんもあとから来るにゃんね?',
            'みりあちゃんもあとから来るのは勘違い',
            'みりあちゃんの友達もあとから来るにゃんね',
            'みりあちゃんもあとから来ると聞いた',
            'みりあちゃんもあとから来るにゃんね\x00',
            '9月7日 みりあちゃんもあとから来るにゃんね',
            '2号店のみりあちゃんもあとから来るにゃんね',
            '夜 みりあちゃんもあとから来るにゃんね',
            'てすとにゃんちゃんもあとから来るにゃんね',
        ]
        for footer in footers:
            with self.subTest(footer=footer):
                post = self.validate(notice_payload(footer))
                self.assertEqual(post['names'], NOTICE_NAMES)
                self.assertNotIn('notices', post)

    def test_quoted_response_cannot_supply_a_notice(self):
        value = notice_payload()
        value['quoted_tweet'] = {'text': SHORT_NOTICE}
        self.assertNotIn('notices', self.validate(value))

    def test_stale_edit_metadata_is_rejected_without_claiming_source_was_edited(self):
        value = notice_payload()
        value['isStaleEdit'] = True
        with self.assertRaisesRegex(collector.FetchFailure, 'stale_edit_response'):
            self.validate(value)

    def test_changed_refresh_preserves_old_extracted_facts_as_one_revision(self):
        old = self.state()
        before = copy.deepcopy(old)
        state, report, code, client = self.refresh(old)
        post = state['posts'][0]
        self.assertEqual(old, before)
        self.assertEqual(client.calls, [NOTICE_ID])
        self.assertEqual((code, report['status'], report['newPostCount']), (0, 'ok', 0))
        self.assertEqual((report['refreshedCount'], report['updatedPostCount'], report['noticeCount']),
                         (1, 1, 1))
        self.assertEqual(post['revisions'], [collector.version_facts(before['posts'][0])])
        self.assertEqual(post['names'], NOTICE_NAMES)
        self.assertEqual(post['lastCheckedAt'], collector.iso(NOTICE_NOW))
        self.assertEqual(post['observedAt'], collector.iso(NOTICE_NOW))

    def test_unchanged_refresh_only_advances_last_checked_and_never_grows_history(self):
        old = self.state()
        state, report, code, _ = self.refresh(old, notice_payload(None))
        self.assertEqual(state['posts'][0]['observedAt'], old['posts'][0]['observedAt'])
        self.assertNotIn('revisions', state['posts'][0])
        self.assertEqual((report['refreshedCount'], report['updatedPostCount']), (1, 0))
        later = NOTICE_NOW + dt.timedelta(minutes=30)
        again, report, _, _ = self.refresh(state, notice_payload(None), now=later)
        self.assertNotIn('revisions', again['posts'][0])
        self.assertEqual(again['posts'][0]['lastCheckedAt'], collector.iso(later))

    def test_refresh_failures_keep_current_history_and_last_checked_unchanged(self):
        old, _, _, _ = self.refresh(self.state())
        later = NOTICE_NOW + dt.timedelta(minutes=30)
        cases = [collector.FetchFailure('network_error')]
        for key, value in [('id_str', OTHER), ('created_at', '2026-09-05T03:13:38Z'),
                           ('text', '【アキバ絶対領域】\nひるにゃんこ')]:
            item = notice_payload()
            item[key] = value
            cases.append(item)
        item = notice_payload(); item['user']['screen_name'] = 'other'; cases.append(item)
        for response in cases:
            with self.subTest(response_type=type(response).__name__):
                client = FakeClient([[], []], {NOTICE_ID: response})
                state, report, code, _ = self.refresh(old, now=later, client=client)
                self.assertEqual(code, 2)
                self.assertEqual(state['posts'], old['posts'])
                self.assertEqual((report['refreshedCount'], report['updatedPostCount']), (0, 0))
                self.assertTrue(state['pending'][0]['reason'].startswith('refresh_'))

    def test_removed_notice_and_arrived_roster_name_affect_only_current_version(self):
        planned, _, _, _ = self.refresh(self.state())
        arrived, report, _, _ = self.refresh(
            planned, notice_payload(names=NOTICE_NAMES + ['みりあ']),
            now=NOTICE_NOW + dt.timedelta(minutes=30))
        self.assertEqual(arrived['posts'][0]['names'], NOTICE_NAMES + ['みりあ'])
        self.assertNotIn('notices', arrived['posts'][0])
        self.assertEqual(arrived['posts'][0]['revisions'][-1]['notices'][0]['name'], 'みりあ')
        removed, _, _, _ = self.refresh(
            arrived, notice_payload(None), now=NOTICE_NOW + dt.timedelta(minutes=60))
        self.assertEqual(removed['posts'][0]['names'], NOTICE_NAMES)
        self.assertNotIn('notices', removed['posts'][0])
        self.assertEqual(collector.merge_snapshots(removed, arrived)['posts'], removed['posts'])
        self.assertEqual(report['noticeCount'], 0)

    def test_notice_can_be_removed_without_unioning_it_back_from_an_old_mirror(self):
        planned, _, _, _ = self.refresh(self.state())
        removed, report, _, _ = self.refresh(
            planned, notice_payload(None), now=NOTICE_NOW + dt.timedelta(minutes=30))
        self.assertEqual(removed['posts'][0]['names'], NOTICE_NAMES)
        self.assertNotIn('notices', removed['posts'][0])
        self.assertEqual((report['updatedPostCount'], report['noticeCount']), (1, 0))
        self.assertEqual(collector.merge_snapshots(removed, planned)['posts'], removed['posts'])

    def test_refresh_uses_source_header_shift_and_store_not_clock_to_rewrite_facts(self):
        state, report, _, _ = self.refresh(
            self.state(), notice_payload(None, shift='よる', store=''))
        self.assertEqual((state['posts'][0]['shift'], state['posts'][0]['storeId']), ('夜', 's1'))
        self.assertEqual((state['posts'][0]['revisions'][0]['shift'],
                          state['posts'][0]['revisions'][0]['storeId']), ('昼', 's4'))
        self.assertEqual(report['updatedPostCount'], 1)

    def test_merge_old_mirror_cannot_roll_back_current_history_or_last_checked(self):
        old = self.state()
        changed, _, _, _ = self.refresh(old)
        checked, _, _, _ = self.refresh(
            changed, now=NOTICE_NOW + dt.timedelta(minutes=30))
        for first, second in ((checked, old), (old, checked), (changed, checked), (checked, changed)):
            with self.subTest(first_checked=first['posts'][0].get('lastCheckedAt')):
                merged = collector.merge_snapshots(first, second)
                self.assertEqual(merged['posts'], checked['posts'])
        self.assertEqual(len(checked['posts'][0]['revisions']), 1)

    def test_merge_return_to_earlier_facts_preserves_intermediate_revision(self):
        original = self.state()
        changed, _, _, _ = self.refresh(original)
        returned, _, _, _ = self.refresh(
            changed, notice_payload(None), now=NOTICE_NOW + dt.timedelta(minutes=30))
        self.assertEqual(len(returned['posts'][0]['revisions']), 2)
        self.assertEqual(collector.merge_snapshots(original, returned)['posts'], returned['posts'])
        self.assertEqual(collector.merge_snapshots(returned, original)['posts'], returned['posts'])

    def test_fractional_last_checked_is_not_rounded_back_by_refresh_or_merge(self):
        now = NOTICE_NOW + dt.timedelta(microseconds=900000)
        state, _, _, _ = self.refresh(self.state(), notice_payload(None), now=now)
        self.assertEqual(collector.last_checked(state['posts'][0]), now)
        merged = collector.merge_snapshots(self.state(), state)
        self.assertEqual(collector.last_checked(merged['posts'][0]), now)
        client = FakeClient([[], []], {})
        self.refresh(state, now=now + dt.timedelta(minutes=30, microseconds=-1), client=client)
        self.assertEqual(client.calls, [])

    def test_merge_success_drops_stale_refresh_pending_but_failed_attempt_survives_old_mirror(self):
        original = self.state()
        failed, _, _, _ = self.refresh(original, client=FakeClient(
            [[], []], {NOTICE_ID: collector.FetchFailure('network_error')}))
        self.assertEqual(len(collector.merge_snapshots(failed, original)['pending']), 1)
        checked, _, _, _ = self.refresh(
            failed, now=NOTICE_NOW + dt.timedelta(minutes=30))
        self.assertEqual(collector.merge_snapshots(checked, failed)['pending'], [])

    def test_minimum_interval_applies_to_success_and_failure_and_default_is_no_refresh(self):
        old = self.state()
        state, _, _, _ = self.refresh(old, notice_payload(None))
        failed, _, _, _ = self.refresh(old, client=FakeClient(
            [[], []], {NOTICE_ID: collector.FetchFailure('network_error')}))
        for previous in (state, failed):
            client = FakeClient([[], []], {})
            self.refresh(previous, now=NOTICE_NOW + dt.timedelta(minutes=29, seconds=59), client=client)
            self.assertEqual(client.calls, [])
        client = FakeClient([[], []], {})
        self.refresh(old, refresh_known=0, client=client)
        self.assertEqual(client.calls, [])

    def test_current_service_date_and_shift_gate_without_changing_source_shift(self):
        state = self.state()
        state['posts'][0]['lastCheckedAt'] = state['posts'][0]['observedAt']
        for when in (dt.datetime(2026, 9, 6, 8, tzinfo=collector.UTC),
                     dt.datetime(2026, 9, 7, 6, tzinfo=collector.UTC)):
            client = FakeClient([[], []], {})
            self.refresh(state, now=when, client=client)
            self.assertEqual(client.calls, [])
        night_created = '2026-09-06T09:00:00Z'
        tid = make_id(night_created)
        night = self.validate(notice_payload(None, tid=tid, created=night_created, shift='よる'),
                              now=dt.datetime(2026, 9, 6, 10, tzinfo=collector.UTC))
        selected = collector.refresh_candidates(
            {'posts': [night], 'pending': []}, NOTICE_DATE, NOTICE_DATE,
            dt.datetime(2026, 9, 6, 19, tzinfo=collector.UTC), 3)
        self.assertEqual([post['id'] for post in selected], [tid])

    def test_legacy_day_post_bootstraps_once_after_seventeen_without_changing_its_shift(self):
        when = dt.datetime(2026, 9, 6, 8, 5, tzinfo=collector.UTC)
        for footer in (SHORT_NOTICE, None):
            with self.subTest(footer=footer):
                old = self.state()
                state, report, code, client = self.refresh(
                    old, notice_payload(footer), now=when)
                self.assertEqual((code, client.calls, report['refreshedCount']),
                                 (0, [NOTICE_ID], 1))
                post = state['posts'][0]
                self.assertEqual((post['date'], post['shift'], post['storeId'], post['names']),
                                 ('2026-09-06', '昼', 's4', NOTICE_NAMES))
                self.assertEqual(post['lastCheckedAt'], collector.iso(when))
                self.assertEqual(report['updatedPostCount'], int(footer is not None))
                if footer:
                    self.assertEqual(post['notices'][0]['name'], 'みりあ')
                    self.assertEqual(post['revisions'][0]['notices'], [])
                again = FakeClient([[], []], {})
                result, _, _, _ = self.refresh(
                    state, now=when + dt.timedelta(hours=1), client=again)
                self.assertEqual(again.calls, [])
                self.assertEqual(result['posts'], state['posts'])

    def test_legacy_bootstrap_precedes_already_checked_current_night_post(self):
        when = dt.datetime(2026, 9, 6, 8, 5, tzinfo=collector.UTC)
        old = self.state()
        created = '2026-09-06T02:00:00Z'
        tid = make_id(created)
        value = notice_payload(None, tid=tid, created=created, shift='よる')
        night = self.validate(value, now=dt.datetime(2026, 9, 6, 4, tzinfo=collector.UTC))
        night['lastCheckedAt'] = night['observedAt']
        old['posts'].append(night)
        state, _, _, client = self.refresh(old, now=when, refresh_known=1)
        self.assertEqual(client.calls, [NOTICE_ID])
        next_client = FakeClient([[], []], {tid: value})
        result, report, _, _ = self.refresh(
            state, now=when + dt.timedelta(minutes=1), refresh_known=1, client=next_client)
        self.assertEqual((next_client.calls, report['refreshedCount']), ([tid], 1))
        self.assertEqual({item['id']: item['shift'] for item in result['posts']},
                         {NOTICE_ID: '昼', tid: '夜'})

    def test_legacy_bootstrap_keeps_new_first_shared_cap_three_limit_and_disabled_default(self):
        when = dt.datetime(2026, 9, 6, 8, 5, tzinfo=collector.UTC)
        old = self.state()
        values = {NOTICE_ID: notice_payload()}
        ids = [NOTICE_ID]
        for minute in range(20, 23):
            created = f'2026-09-06T03:{minute}:00Z'
            tid = make_id(created)
            values[tid] = notice_payload(None, tid=tid, created=created)
            old['posts'].append(self.validate(values[tid]))
            ids.append(tid)
        disabled = FakeClient([[], []], {})
        self.refresh(old, now=when, refresh_known=0, client=disabled)
        self.assertEqual(disabled.calls, [])
        created = '2026-09-06T07:20:00Z'
        fresh = make_id(created)
        values[fresh] = notice_payload(None, tid=fresh, created=created, shift='よる')
        first = FakeClient([[fresh], []], values)
        state, report, _, _ = self.refresh(old, now=when, max_posts=2, client=first)
        self.assertEqual(first.calls, [fresh, NOTICE_ID])
        self.assertEqual((report['attemptedCount'], report['refreshedCount']), (2, 1))
        second = FakeClient([[], []], values)
        result, report, _, _ = self.refresh(
            state, now=when + dt.timedelta(minutes=1), max_posts=20, client=second)
        self.assertEqual(second.calls, ids[1:])
        self.assertEqual((report['attemptedCount'], report['refreshedCount']), (3, 3))
        self.assertTrue(all('lastCheckedAt' in post for post in result['posts']))

    def test_legacy_bootstrap_never_bypasses_thirty_minutes_or_current_service_day(self):
        old = self.state()
        old['posts'][0]['observedAt'] = '2026-09-06T07:50:00Z'
        early = FakeClient([[], []], {})
        self.refresh(old, now=dt.datetime(2026, 9, 6, 8, 19, 59, tzinfo=collector.UTC), client=early)
        self.assertEqual(early.calls, [])
        _, _, _, at_boundary = self.refresh(
            old, now=dt.datetime(2026, 9, 6, 8, 20, tzinfo=collector.UTC))
        self.assertEqual(at_boundary.calls, [NOTICE_ID])
        previous_day = FakeClient([[], []], {})
        state, _, _ = collector.collect(
            old, set(), previous_day, NOTICE_DATE, dt.date(2026, 9, 7), 20,
            clock=lambda: dt.datetime(2026, 9, 6, 20, tzinfo=collector.UTC), refresh_known=3)
        self.assertEqual(previous_day.calls, [])
        self.assertEqual(state['posts'], old['posts'])

    def test_failed_legacy_bootstrap_retains_pending_then_stops_after_success(self):
        when = dt.datetime(2026, 9, 6, 8, 5, tzinfo=collector.UTC)
        old = self.state()
        failed, _, code, _ = self.refresh(old, now=when, client=FakeClient(
            [[], []], {NOTICE_ID: collector.FetchFailure('network_error')}))
        self.assertEqual((code, failed['posts']), (2, old['posts']))
        self.assertEqual(failed['pending'][0]['reason'], 'refresh_network_error')
        early = FakeClient([[], []], {})
        self.refresh(failed, now=when + dt.timedelta(minutes=29, seconds=59), client=early)
        self.assertEqual(early.calls, [])
        result, _, _, client = self.refresh(failed, now=when + dt.timedelta(minutes=30))
        self.assertEqual((client.calls, result['pending']), ([NOTICE_ID], []))
        done = FakeClient([[], []], {})
        self.refresh(result, now=when + dt.timedelta(hours=1), client=done)
        self.assertEqual(done.calls, [])

    def test_fair_oldest_checked_order_and_three_post_limit(self):
        state = collector.empty_snapshot()
        values, ids = {}, []
        for index in range(8):
            created = '2026-09-06T03:%02d:00Z' % (20 + index)
            tid = make_id(created)
            values[tid] = notice_payload(None, tid=tid, created=created)
            item = self.validate(values[tid])
            item['lastCheckedAt'] = collector.iso(NOTICE_OBSERVED + dt.timedelta(minutes=index))
            state['posts'].append(item)
            ids.append(tid)
        first_client = FakeClient([[], []], values)
        first, report, _, _ = self.refresh(state, client=first_client)
        self.assertEqual(first_client.calls, ids[:3])
        self.assertEqual(report['refreshedCount'], 3)
        second_client = FakeClient([[], []], values)
        self.refresh(first, now=NOTICE_NOW + dt.timedelta(minutes=5), client=second_client)
        self.assertEqual(second_client.calls, ids[3:6])

    def test_new_and_pending_requests_have_priority_inside_shared_cap(self):
        created = '2026-09-06T03:30:00Z'
        tid = make_id(created)
        value = notice_payload(None, tid=tid, created=created)
        for pending_first in (False, True):
            state = self.state()
            searches = [[tid], []]
            if pending_first:
                state['pending'] = [{'id': tid, 'url': collector.canonical(tid),
                    'reason': 'network_error', 'firstSeenAt': collector.iso(NOTICE_OBSERVED),
                    'lastAttemptAt': collector.iso(NOTICE_OBSERVED), 'attempts': 1}]
                searches = [[], []]
            client = FakeClient(searches, {tid: value})
            result, report, _, _ = self.refresh(state, max_posts=1, client=client)
            self.assertEqual(client.calls, [tid])
            self.assertEqual((report['attemptedCount'], report['refreshedCount']), (1, 0))
            self.assertEqual(len(result['posts']), 2)

    def test_new_id_is_not_fetched_twice_in_same_run_even_with_refresh_enabled(self):
        created = '2026-09-06T03:30:00Z'
        tid = make_id(created)
        client = FakeClient([[tid], [tid]], {
            tid: notice_payload(None, tid=tid, created=created), NOTICE_ID: notice_payload()})
        _, report, _, _ = self.refresh(self.state(), client=client)
        self.assertEqual(client.calls, [tid, NOTICE_ID])
        self.assertEqual(report['attemptedCount'], 2)

    def test_refresh_403_429_stop_remaining_known_gets_and_keep_facts(self):
        for status in (403, 429):
            state = self.state()
            created = '2026-09-06T03:30:00Z'; tid = make_id(created)
            second = self.validate(notice_payload(None, tid=tid, created=created))
            second['lastCheckedAt'] = collector.iso(NOTICE_OBSERVED + dt.timedelta(minutes=1))
            state['posts'].append(second)
            client = FakeClient([[], []], {
                NOTICE_ID: collector.FetchFailure('http_error', status,
                                                  NOTICE_NOW + dt.timedelta(hours=2))})
            result, report, code, _ = self.refresh(state, client=client)
            self.assertEqual(client.calls, [NOTICE_ID])
            self.assertEqual((code, report['refreshedCount']), (2, 0))
            self.assertEqual(result['posts'], state['posts'])
            self.assertEqual(result['cooldowns'][collector.POST_HOST],
                             collector.iso(NOTICE_NOW + dt.timedelta(hours=2)))

    def test_snapshot_validates_new_optional_fields_and_rejects_bad_history(self):
        state, _, _, _ = self.refresh(self.state())
        path = self.directory / 'snapshot.json'
        collector.atomic_json(path, state)
        self.assertEqual(collector.load_snapshot(path), state)
        bad_posts = []
        for field, value in [('kind', 'placement'), ('time', None), ('time', '25:00'),
                             ('excerpt', ''), ('excerpt', 'a\nb'), ('excerpt', 'x' * 161),
                             ('text', 'forbidden')]:
            item = copy.deepcopy(state['posts'][0])
            item['notices'][0][field] = value
            bad_posts.append(item)
        item = copy.deepcopy(state['posts'][0]); item['lastCheckedAt'] = NOTICE_CREATED; bad_posts.append(item)
        item = copy.deepcopy(state['posts'][0]); item['revisions'][0]['text'] = 'forbidden'; bad_posts.append(item)
        item = copy.deepcopy(state['posts'][0]); item['revisions'][0]['date'] = '2026-09-05'; bad_posts.append(item)
        item = copy.deepcopy(state['posts'][0]); item['revisions'][0]['observedAt'] = item['observedAt']; bad_posts.append(item)
        for post in bad_posts:
            invalid = copy.deepcopy(state); invalid['posts'] = [post]
            collector.atomic_json(path, invalid)
            with self.assertRaisesRegex(ValueError, 'invalid_snapshot'):
                collector.load_snapshot(path)
        legacy = self.state()
        collector.atomic_json(path, legacy)
        self.assertEqual(collector.load_snapshot(path), legacy)

    def test_dry_run_keeps_extracted_facts_and_history_but_reserves_refresh_interval(self):
        path = self.directory / 'snapshot.json'
        curated = self.directory / 'shifts.csv'
        curated.write_text('date,store,shift,maid,tweet_id\n', encoding='utf-8')
        old = self.state()
        collector.atomic_json(path, old)
        protected = [curated, collector.CURATED, collector.ROOT / 'data' / 'store-insights.js',
                     collector.ROOT / 'data' / 'schedule.js']
        before = {item: hashlib.sha256(item.read_bytes()).digest() for item in protected}
        args = collector.argument_parser().parse_args([
            '--dry-run', '--once', '--refresh-known', '3', '--snapshot', str(path)])
        client = FakeClient([[], []], {NOTICE_ID: notice_payload()})
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(collector.run(args, curated=curated, client=client,
                                            clock=lambda: NOTICE_NOW), 0)
        saved = collector.load_snapshot(path)
        self.assertEqual(saved['posts'], old['posts'])
        self.assertEqual(saved['pending'][0]['reason'], 'refresh_requested')
        self.assertEqual(client.calls, [NOTICE_ID])
        report = json.loads(output.getvalue())
        self.assertFalse(report['saved'])
        self.assertEqual(report['updatedPostCount'], 1)
        again = FakeClient([[], []], {})
        with contextlib.redirect_stdout(io.StringIO()):
            collector.run(args, curated=curated, client=again,
                          clock=lambda: NOTICE_NOW + dt.timedelta(minutes=5))
        self.assertEqual(again.calls, [])
        self.assertEqual(before, {item: hashlib.sha256(item.read_bytes()).digest() for item in protected})

    def test_refresh_reservation_save_failure_prevents_get(self):
        client = FakeClient([[], []], {NOTICE_ID: notice_payload()})
        with self.assertRaises(OSError):
            collector.collect(self.state(), set(), client, NOTICE_DATE, NOTICE_DATE, 20,
                              clock=lambda: NOTICE_NOW, refresh_known=3,
                              on_refresh_attempt=mock.Mock(side_effect=OSError('disk')))
        self.assertEqual(client.calls, [])

    def test_final_atomic_failure_keeps_original_current_facts_and_refresh_reservation(self):
        path = self.directory / 'snapshot.json'
        curated = self.directory / 'shifts.csv'
        curated.write_text('date,store,shift,maid,tweet_id\n', encoding='utf-8')
        old = self.state()
        collector.atomic_json(path, old)
        args = collector.argument_parser().parse_args([
            '--once', '--refresh-known', '3', '--snapshot', str(path)])
        atomic = collector.atomic_json
        writes = []

        def save(target, value):
            if target == path:
                writes.append(value)
                if len(writes) == 2:
                    raise OSError('final snapshot unavailable')
            return atomic(target, value)
        client = FakeClient([[], []], {NOTICE_ID: notice_payload()})
        with mock.patch.object(collector, 'atomic_json', side_effect=save):
            with self.assertRaises(OSError):
                collector.run(args, curated=curated, client=client, clock=lambda: NOTICE_NOW)
        saved = collector.load_snapshot(path)
        self.assertEqual(saved['posts'], old['posts'])
        self.assertEqual(saved['pending'][0]['reason'], 'refresh_requested')
        self.assertEqual(client.calls, [NOTICE_ID])

    def test_edit_root_chain_is_saved_only_for_its_verified_latest_id(self):
        value = edit_payload([NOTICE_ID, EDIT_ID], EDIT_ID, EDIT_CREATED)
        self.assertEqual(self.validate(value)['editTweetIds'], [NOTICE_ID, EDIT_ID])
        value['edit_control']['edit_tweet_ids'] = [int(NOTICE_ID), int(EDIT_ID)]
        self.assertEqual(self.validate(value)['editTweetIds'], [NOTICE_ID, EDIT_ID])
        old = edit_payload([NOTICE_ID, EDIT_ID])
        with self.assertRaises(collector.StaleEditResponse) as result:
            self.validate(old)
        self.assertEqual(result.exception.latest_id, EDIT_ID)
        singleton = edit_payload([NOTICE_ID])
        singleton['isEdited'] = False
        post = self.validate(singleton)
        self.assertEqual(post['editTweetIds'], [NOTICE_ID])
        self.assertNotIn('isEdited', post)
        self.assertEqual(post['names'], NOTICE_NAMES)
        self.assertEqual(post['notices'][0]['name'], 'みりあ')

    def test_edit_invalid_or_unknown_metadata_is_held_not_inferred(self):
        cases = [None, [], {}, {'edit_tweet_ids': []},
                 {'edit_tweet_ids': [NOTICE_ID, NOTICE_ID]},
                 {'edit_tweet_ids': [EDIT_ID, NOTICE_ID]},
                 {'edit_tweet_ids': [EDIT_ID]},
                 {'edit_tweet_ids': [float(NOTICE_ID)]},
                 {'edit_tweet_ids': [NOTICE_ID, '9999999999999999999999999']}]
        future = make_id('2026-09-07T03:00:00Z')
        cases.append({'edit_tweet_ids': [NOTICE_ID, future]})
        for control in cases:
            value = notice_payload()
            value['edit_control'] = control
            with self.subTest(control=control):
                with self.assertRaisesRegex(collector.FetchFailure, 'invalid_edit_metadata'):
                    self.validate(value)
        for field in ('isEdited', 'isStaleEdit'):
            value = edit_payload([NOTICE_ID])
            value[field] = 'false'
            with self.assertRaisesRegex(collector.FetchFailure, 'invalid_edit_metadata'):
                self.validate(value)
        value = notice_payload()
        value['isEdited'] = True
        with self.assertRaisesRegex(collector.FetchFailure, 'invalid_edit_metadata'):
            self.validate(value)

    def test_edit_third_party_wrong_id_time_and_quoted_control_never_authorize_follow(self):
        for field in ('author', 'id', 'time'):
            response = edit_payload([NOTICE_ID, EDIT_ID])
            if field == 'author':
                response['user']['screen_name'] = 'other'
            elif field == 'id':
                response['id_str'] = EDIT_ID
            else:
                response['created_at'] = '2026-09-05T03:13:38Z'
            old = self.state()
            client = FakeClient([[], []], {NOTICE_ID: response})
            state, _, _, _ = self.refresh(old, client=client)
            self.assertEqual(client.calls, [NOTICE_ID])
            self.assertEqual(state['posts'], old['posts'])
            self.assertFalse(any(item.get('editSourceId') for item in state['pending']))
        value = notice_payload(None)
        value['quoted_tweet'] = edit_payload([NOTICE_ID, EDIT_ID])
        self.assertNotIn('editTweetIds', self.validate(value))

    def test_edit_stale_refresh_follows_verified_latest_without_deleting_old_raw_post(self):
        old = self.state(SHORT_NOTICE)
        client = FakeClient([[], []], {
            NOTICE_ID: edit_payload([NOTICE_ID, EDIT_ID]),
            EDIT_ID: edit_payload([NOTICE_ID, EDIT_ID], EDIT_ID, EDIT_CREATED,
                                  NOTICE_NAMES + ['みりあ'], None)})
        state, report, code, _ = self.refresh(old, client=client)
        self.assertEqual(client.calls, [NOTICE_ID, EDIT_ID])
        self.assertEqual(state['posts'][0], old['posts'][0])
        self.assertEqual(state['posts'][1]['editTweetIds'], [NOTICE_ID, EDIT_ID])
        self.assertEqual(state['posts'][1]['names'], NOTICE_NAMES + ['みりあ'])
        self.assertEqual(collector.superseded_ids(state['posts']), {NOTICE_ID})
        self.assertEqual(state['pending'], [])
        self.assertEqual(report['failures'], [])
        self.assertEqual((code, report['attemptedCount'], report['newPostCount']), (0, 2, 1))
        self.assertEqual(report['noticeCount'], 0)

    def test_edit_unfetched_latest_keeps_old_active_and_durable_origin_for_retry(self):
        old = self.state(SHORT_NOTICE)
        client = FakeClient([[], []], {NOTICE_ID: edit_payload([NOTICE_ID, EDIT_ID])})
        state, report, code, _ = self.refresh(old, max_posts=1, client=client)
        self.assertEqual((code, client.calls), (2, [NOTICE_ID]))
        self.assertEqual(state['posts'], old['posts'])
        self.assertEqual(collector.superseded_ids(state['posts']), set())
        pending = {item['id']: item for item in state['pending']}
        self.assertEqual(pending[EDIT_ID]['editSourceId'], NOTICE_ID)
        self.assertIsNone(pending[EDIT_ID]['lastAttemptAt'])
        self.assertEqual((report['deferredCount'], report['pendingOutsideRangeCount']), (1, 0))
        path = self.directory / 'pending-edit.json'
        collector.atomic_json(path, state)
        self.assertEqual(collector.load_snapshot(path), state)
        latest = edit_payload([NOTICE_ID, EDIT_ID], EDIT_ID, EDIT_CREATED, footer=None)
        retry = FakeClient([[], []], {EDIT_ID: latest})
        result, _, code, _ = self.refresh(state, max_posts=1, refresh_known=0,
                                         now=NOTICE_NOW + dt.timedelta(minutes=1), client=retry)
        self.assertEqual((code, retry.calls), (0, [EDIT_ID]))
        self.assertEqual(result['pending'], [])
        self.assertEqual(result['posts'][0], old['posts'][0])

    def test_edit_follow_is_one_extra_get_not_recursive_chasing(self):
        old = self.state()
        responses = {
            NOTICE_ID: edit_payload([NOTICE_ID, EDIT_ID]),
            EDIT_ID: edit_payload([NOTICE_ID, EDIT_ID, NEXT_EDIT_ID], EDIT_ID, EDIT_CREATED),
        }
        state, report, code, client = self.refresh(old, client=FakeClient([[], []], responses))
        self.assertEqual(client.calls, [NOTICE_ID, EDIT_ID])
        self.assertEqual((report['attemptedCount'], code), (2, 2))
        self.assertEqual(state['posts'], old['posts'])
        pending = {item['id']: item for item in state['pending']}
        self.assertEqual(pending[NEXT_EDIT_ID]['editSourceId'], EDIT_ID)
        latest = edit_payload([NOTICE_ID, EDIT_ID, NEXT_EDIT_ID],
                              NEXT_EDIT_ID, NEXT_EDIT_CREATED, footer=None)
        retry = FakeClient([[], []], {NEXT_EDIT_ID: latest})
        result, _, code, _ = self.refresh(state, now=NOTICE_NOW + dt.timedelta(minutes=1),
                                         max_posts=1, refresh_known=0, client=retry)
        self.assertEqual((code, retry.calls), (0, [NEXT_EDIT_ID]))
        self.assertEqual(result['pending'], [])
        self.assertEqual(collector.superseded_ids(result['posts']), {NOTICE_ID, EDIT_ID})

    def test_edit_latest_without_matching_root_chain_or_roster_stays_pending(self):
        other = make_id('2026-09-06T03:10:00Z')
        responses = [
            notice_payload(None, tid=EDIT_ID, created=EDIT_CREATED),
            edit_payload([EDIT_ID], EDIT_ID, EDIT_CREATED),
            edit_payload([other, EDIT_ID], EDIT_ID, EDIT_CREATED),
            edit_payload([other, EDIT_ID, NEXT_EDIT_ID], EDIT_ID, EDIT_CREATED),
        ]
        missing_roster = edit_payload([NOTICE_ID, EDIT_ID], EDIT_ID, EDIT_CREATED)
        missing_roster['text'] = 'お知らせ'
        responses.append(missing_roster)
        wrong_author = edit_payload([NOTICE_ID, EDIT_ID], EDIT_ID, EDIT_CREATED)
        wrong_author['user']['screen_name'] = 'other'
        responses.append(wrong_author)
        for response in responses:
            with self.subTest(response_id=response['id_str']):
                old = self.state()
                client = FakeClient([[], []], {
                    NOTICE_ID: edit_payload([NOTICE_ID, EDIT_ID]), EDIT_ID: response})
                state, _, code, _ = self.refresh(old, client=client)
                self.assertEqual((code, client.calls), (2, [NOTICE_ID, EDIT_ID]))
                self.assertEqual(state['posts'], old['posts'])
                self.assertEqual(collector.superseded_ids(state['posts']), set())
                pending = {item['id']: item for item in state['pending']}
                self.assertEqual(pending[EDIT_ID]['editSourceId'], NOTICE_ID)
                self.assertNotIn(NEXT_EDIT_ID, pending)

    def test_edit_existing_latest_waits_for_interval_and_known_refresh_budget(self):
        old = self.state()
        old['posts'].append(self.validate(
            notice_payload(None, tid=EDIT_ID, created=EDIT_CREATED),
            now=NOTICE_NOW - dt.timedelta(minutes=10)))
        client = FakeClient([[], []], {NOTICE_ID: edit_payload([NOTICE_ID, EDIT_ID])})
        state, _, code, _ = self.refresh(old, client=client)
        self.assertEqual((code, client.calls), (2, [NOTICE_ID]))
        self.assertEqual(state['posts'], old['posts'])
        no_budget = FakeClient([[], []], {})
        self.refresh(state, now=NOTICE_NOW + dt.timedelta(minutes=30),
                     refresh_known=0, client=no_budget)
        self.assertEqual(no_budget.calls, [])
        latest = edit_payload([NOTICE_ID, EDIT_ID], EDIT_ID, EDIT_CREATED, footer=None)
        retry = FakeClient([[], []], {EDIT_ID: latest})
        result, report, code, _ = self.refresh(
            state, now=NOTICE_NOW + dt.timedelta(minutes=30),
            max_posts=1, refresh_known=1, client=retry)
        self.assertEqual((code, retry.calls), (0, [EDIT_ID]))
        self.assertEqual((report['refreshedCount'], report['updatedPostCount']), (1, 0))
        self.assertEqual(result['pending'], [])
        self.assertEqual(result['posts'][0], old['posts'][0])

    def test_edit_follow_obeys_new_work_priority_and_never_refetches_same_run_id(self):
        new_created = '2026-09-06T03:40:00Z'
        new_id = make_id(new_created)
        client = FakeClient([[new_id], []], {
            new_id: notice_payload(None, tid=new_id, created=new_created),
            NOTICE_ID: edit_payload([NOTICE_ID, EDIT_ID])})
        _, report, _, _ = self.refresh(self.state(), max_posts=2, client=client)
        self.assertEqual(client.calls, [new_id, NOTICE_ID])
        self.assertEqual(report['attemptedCount'], 2)
        latest = edit_payload([NOTICE_ID, EDIT_ID], EDIT_ID, EDIT_CREATED, footer=None)
        client = FakeClient([[NOTICE_ID, EDIT_ID], [EDIT_ID]], {EDIT_ID: latest})
        result, _, _, _ = self.refresh(self.state(), client=client)
        self.assertEqual(client.calls, [EDIT_ID])
        self.assertEqual(len(result['posts']), 2)

    def test_edit_follow_403_429_stops_host_and_never_hides_old_facts(self):
        for status in (403, 429):
            old = self.state()
            other_created = '2026-09-06T03:12:00Z'
            other_id = make_id(other_created)
            extra = self.validate(notice_payload(None, tid=other_id, created=other_created),
                                  now=NOTICE_OBSERVED + dt.timedelta(minutes=1))
            old['posts'].append(extra)
            client = FakeClient([[], []], {
                NOTICE_ID: edit_payload([NOTICE_ID, EDIT_ID]),
                EDIT_ID: collector.FetchFailure('http_error', status,
                                                 NOTICE_NOW + dt.timedelta(hours=2))})
            state, report, code, _ = self.refresh(old, client=client)
            self.assertEqual((code, client.calls), (2, [NOTICE_ID, EDIT_ID]))
            self.assertEqual({p['id']: p for p in state['posts']},
                             {p['id']: p for p in old['posts']})
            self.assertEqual(collector.superseded_ids(state['posts']), set())
            self.assertEqual(report['attemptedCount'], 2)
            self.assertEqual(state['cooldowns'][collector.POST_HOST],
                             collector.iso(NOTICE_NOW + dt.timedelta(hours=2)))

    def test_edit_snapshot_validator_rejects_invalid_chain_and_pending_origin(self):
        current = self.validate(edit_payload([NOTICE_ID, EDIT_ID], EDIT_ID, EDIT_CREATED))
        state = collector.empty_snapshot()
        state['posts'] = [current]
        path = self.directory / 'chain.json'
        collector.atomic_json(path, state)
        self.assertEqual(collector.load_snapshot(path), state)
        for ids in ([], [NOTICE_ID], [NOTICE_ID, NOTICE_ID, EDIT_ID],
                    [EDIT_ID, NOTICE_ID], [int(NOTICE_ID), EDIT_ID], None):
            invalid = copy.deepcopy(state)
            invalid['posts'][0]['editTweetIds'] = ids
            collector.atomic_json(path, invalid)
            with self.assertRaisesRegex(ValueError, 'invalid_snapshot'):
                collector.load_snapshot(path)
        for source in (EDIT_ID, float(NOTICE_ID), 'invalid', NEXT_EDIT_ID):
            invalid = copy.deepcopy(state)
            invalid['pending'] = [{'id': EDIT_ID, 'url': collector.canonical(EDIT_ID),
                'reason': 'edit_latest_unverified', 'editSourceId': source,
                'firstSeenAt': collector.iso(NOTICE_NOW), 'lastAttemptAt': None, 'attempts': 0}]
            collector.atomic_json(path, invalid)
            with self.assertRaisesRegex(ValueError, 'invalid_snapshot'):
                collector.load_snapshot(path)

    def test_edit_chain_merge_keeps_proof_without_unioning_incompatible_ancestry(self):
        plain = self.validate(notice_payload(None, tid=EDIT_ID, created=EDIT_CREATED))
        proven = self.validate(edit_payload([NOTICE_ID, EDIT_ID], EDIT_ID, EDIT_CREATED, footer=None))
        proven['lastCheckedAt'] = collector.checked_iso(NOTICE_NOW)
        for first, second in ((plain, proven), (proven, plain)):
            merged = collector.merge_post_versions(first, second)
            self.assertEqual(merged['editTweetIds'], [NOTICE_ID, EDIT_ID])
            self.assertEqual(merged['lastCheckedAt'], collector.checked_iso(NOTICE_NOW))
        incompatible = copy.deepcopy(proven)
        incompatible['editTweetIds'] = [make_id('2026-09-06T03:10:00Z'), EDIT_ID]
        with self.assertRaisesRegex(ValueError, 'observation_conflict'):
            collector.merge_post_versions(proven, incompatible)

    def test_edit_known_chain_is_not_erased_when_refresh_metadata_disappears(self):
        old = self.state()
        old['posts'] = [self.validate(edit_payload([NOTICE_ID, EDIT_ID],
                                                   EDIT_ID, EDIT_CREATED, footer=None))]
        response = notice_payload(None, tid=EDIT_ID, created=EDIT_CREATED)
        state, _, code, client = self.refresh(old, client=FakeClient([[], []], {EDIT_ID: response}))
        self.assertEqual((code, client.calls), (2, [EDIT_ID]))
        self.assertEqual(state['posts'], old['posts'])
        self.assertEqual(state['pending'][0]['reason'], 'refresh_edit_metadata_missing')

    def test_edit_pending_merge_preserves_source_provenance_against_older_mirror(self):
        state, _, _, _ = self.refresh(self.state(), max_posts=1, client=FakeClient(
            [[], []], {NOTICE_ID: edit_payload([NOTICE_ID, EDIT_ID])}))
        mirror = copy.deepcopy(state)
        for item in mirror['pending']:
            item.pop('editSourceId', None)
        for first, second in ((state, mirror), (mirror, state)):
            merged = collector.merge_snapshots(first, second)
            pending = {item['id']: item for item in merged['pending']}
            self.assertEqual(pending[EDIT_ID]['editSourceId'], NOTICE_ID)

    def test_edit_failed_known_retry_keeps_interval_even_after_budget_deferral(self):
        old = self.state()
        old['posts'].append(self.validate(notice_payload(None, tid=EDIT_ID, created=EDIT_CREATED)))
        old['pending'] = [{
            'id': EDIT_ID, 'url': collector.canonical(EDIT_ID), 'editSourceId': NOTICE_ID,
            'reason': 'refresh_network_error', 'firstSeenAt': collector.iso(NOTICE_OBSERVED),
            'lastAttemptAt': collector.checked_iso(NOTICE_NOW), 'attempts': 1}]
        no_budget = FakeClient([[], []], {})
        deferred, _, _, _ = self.refresh(
            old, refresh_known=0, now=NOTICE_NOW + dt.timedelta(minutes=5), client=no_budget)
        self.assertEqual(no_budget.calls, [])
        again = FakeClient([[], []], {})
        self.refresh(deferred, now=NOTICE_NOW + dt.timedelta(minutes=10), client=again)
        self.assertEqual(again.calls, [])

    def test_edit_cross_service_day_follower_remains_pending_without_hiding_old(self):
        created = '2026-09-07T03:20:00Z'
        tid = make_id(created)
        now = dt.datetime(2026, 9, 7, 6, tzinfo=collector.UTC)
        old = self.state()
        old['pending'] = [{
            'id': tid, 'url': collector.canonical(tid), 'editSourceId': NOTICE_ID,
            'reason': 'edit_latest_unverified', 'firstSeenAt': collector.iso(now),
            'lastAttemptAt': None, 'attempts': 0}]
        client = FakeClient([[], []], {tid: edit_payload([NOTICE_ID, tid], tid, created)})
        state, report, code = collector.collect(
            old, set(), client, NOTICE_DATE, dt.date(2026, 9, 7), 20,
            clock=lambda: now, refresh_known=3)
        self.assertEqual((code, client.calls), (2, [tid]))
        self.assertEqual(state['posts'], old['posts'])
        self.assertEqual(collector.superseded_ids(state['posts']), set())
        self.assertEqual(report['failures'][0]['reason'], 'edit_date_mismatch')

    def test_edit_latest_curated_id_is_not_refetched_or_used_to_hide_old_post(self):
        old = self.state()
        client = FakeClient([[], []], {NOTICE_ID: edit_payload([NOTICE_ID, EDIT_ID])})
        state, _, code = collector.collect(
            old, {EDIT_ID}, client, NOTICE_DATE, NOTICE_DATE, 20,
            clock=lambda: NOTICE_NOW, refresh_known=3)
        self.assertEqual((code, client.calls), (2, [NOTICE_ID]))
        self.assertEqual(state['posts'], old['posts'])
        self.assertEqual(collector.superseded_ids(state['posts']), set())
        pending = {item['id']: item for item in state['pending']}
        self.assertEqual(pending[EDIT_ID]['reason'], 'edit_latest_curated')
        again = FakeClient([[], []], {})
        result, report, _ = collector.collect(
            state, {EDIT_ID}, again, NOTICE_DATE, NOTICE_DATE, 20,
            clock=lambda: NOTICE_NOW + dt.timedelta(hours=1), refresh_known=3)
        self.assertEqual(again.calls, [])
        self.assertEqual(report['skippedCuratedCount'], 1)
        self.assertEqual({item['id'] for item in result['pending']}, {NOTICE_ID, EDIT_ID})

    def test_refresh_skips_curated_posts_without_starving_remaining_known_budget(self):
        old = self.state()
        old['posts'].append(self.validate(notice_payload(None, tid=EDIT_ID, created=EDIT_CREATED)))
        client = FakeClient([[], []], {
            EDIT_ID: notice_payload(None, tid=EDIT_ID, created=EDIT_CREATED)})
        state, report, code = collector.collect(
            old, {NOTICE_ID}, client, NOTICE_DATE, NOTICE_DATE, 20,
            clock=lambda: NOTICE_NOW, refresh_known=1)
        self.assertEqual((code, client.calls, report['refreshedCount']), (0, [EDIT_ID], 1))
        self.assertEqual(state['posts'][0], old['posts'][0])

    def test_edit_known_pending_does_not_take_budget_before_unfetched_discovery(self):
        old = self.state()
        old['posts'].append(self.validate(notice_payload(None, tid=EDIT_ID, created=EDIT_CREATED)))
        old['pending'] = [{
            'id': EDIT_ID, 'url': collector.canonical(EDIT_ID), 'editSourceId': NOTICE_ID,
            'reason': 'refresh_edit_latest_unverified',
            'firstSeenAt': collector.iso(NOTICE_OBSERVED), 'lastAttemptAt': None, 'attempts': 0}]
        client = FakeClient([[NEXT_EDIT_ID], []], {
            NEXT_EDIT_ID: notice_payload(None, tid=NEXT_EDIT_ID, created=NEXT_EDIT_CREATED)})
        state, report, _, _ = self.refresh(old, max_posts=1, client=client)
        self.assertEqual(client.calls, [NEXT_EDIT_ID])
        self.assertEqual((report['newPostCount'], report['refreshedCount']), (1, 0))
        self.assertIn(EDIT_ID, {item['id'] for item in state['pending']})

    def test_cross_day_stale_chain_is_held_before_following_outside_explicit_range(self):
        created = '2026-09-07T03:20:00Z'
        tid = make_id(created)
        now = dt.datetime(2026, 9, 7, 6, tzinfo=collector.UTC)
        client = FakeClient([[NOTICE_ID], []], {
            NOTICE_ID: edit_payload([NOTICE_ID, tid])})
        state, report, code = collector.collect(
            collector.empty_snapshot(), set(), client, NOTICE_DATE, NOTICE_DATE, 20,
            clock=lambda: now)
        self.assertEqual((code, client.calls), (2, [NOTICE_ID]))
        self.assertEqual(report['failures'][0]['reason'], 'edit_date_mismatch')
        self.assertEqual(state['posts'], [])
        self.assertEqual({item['id'] for item in state['pending']}, {NOTICE_ID})

    def test_failed_known_refresh_cannot_starve_an_older_unattempted_post(self):
        old = self.state()
        old['posts'].append(self.validate(notice_payload(None, tid=EDIT_ID, created=EDIT_CREATED)))
        failed = FakeClient([[], []], {NOTICE_ID: collector.FetchFailure('parse_failed')})
        state, _, _, _ = self.refresh(old, max_posts=1, refresh_known=1, client=failed)
        self.assertEqual(failed.calls, [NOTICE_ID])
        next_client = FakeClient([[], []], {
            EDIT_ID: notice_payload(None, tid=EDIT_ID, created=EDIT_CREATED)})
        self.refresh(state, max_posts=1, refresh_known=1,
                     now=NOTICE_NOW + dt.timedelta(hours=1), client=next_client)
        self.assertEqual(next_client.calls, [EDIT_ID])

    def test_edit_notice_counter_excludes_superseded_raw_post_without_known_refresh(self):
        old = self.state(SHORT_NOTICE)
        client = FakeClient([[EDIT_ID], []], {
            EDIT_ID: edit_payload([NOTICE_ID, EDIT_ID], EDIT_ID, EDIT_CREATED, footer=None)})
        state, report, code, _ = self.refresh(old, refresh_known=0, client=client)
        self.assertEqual((code, report['noticeCount'], report['refreshedCount']), (0, 0, 0))
        self.assertEqual(state['posts'][0], old['posts'][0])
        self.assertEqual(state['posts'][0]['notices'][0]['name'], 'みりあ')
        self.assertEqual(collector.superseded_ids(state['posts']), {NOTICE_ID})

    def test_optional_refresh_counters_require_nonnegative_integers(self):
        path = self.directory / 'counts.json'
        for field in ('refreshedCount', 'updatedPostCount', 'noticeCount'):
            for invalid in (True, False, -1, 1.0, '1', None):
                state = self.state()
                state['lastRun'][field] = invalid
                collector.atomic_json(path, state)
                with self.subTest(field=field, invalid=invalid):
                    with self.assertRaisesRegex(ValueError, 'invalid_snapshot'):
                        collector.load_snapshot(path)

    def test_directly_discovered_cross_day_latest_keeps_old_facts_and_notice_count(self):
        old = self.state(SHORT_NOTICE)
        created = '2026-09-07T03:20:00Z'
        tid = make_id(created)
        now = dt.datetime(2026, 9, 7, 6, tzinfo=collector.UTC)
        value = edit_payload([NOTICE_ID, tid], tid, created, footer=None)
        client = FakeClient([[tid], [tid]], {tid: value})
        state, report, code = collector.collect(
            old, set(), client, NOTICE_DATE, dt.date(2026, 9, 7), 20, clock=lambda: now)
        self.assertEqual((code, client.calls), (2, [tid]))
        self.assertEqual(state['posts'], old['posts'])
        self.assertEqual(collector.superseded_ids(state['posts']), set())
        self.assertEqual((report['newPostCount'], report['noticeCount']), (0, 1))
        self.assertEqual(state['pending'][0]['reason'], 'edit_date_mismatch')
        self.assertNotIn('editSourceId', state['pending'][0])

    def test_cross_day_snapshot_rejected_and_unvalidated_helpers_never_hide_old_notice(self):
        old = self.state(SHORT_NOTICE)
        created = '2026-09-07T03:20:00Z'
        tid = make_id(created)
        latest = collector.validate_post(
            tid, notice_payload(None, tid=tid, created=created),
            NOTICE_DATE, dt.date(2026, 9, 7),
            dt.datetime(2026, 9, 7, 6, tzinfo=collector.UTC))
        latest['editTweetIds'] = [NOTICE_ID, tid]
        invalid = copy.deepcopy(old)
        invalid['posts'].append(latest)
        before = copy.deepcopy(invalid)
        self.assertEqual(collector.superseded_ids(invalid['posts']), set())
        self.assertEqual(collector.current_notice_count(invalid['posts']), 1)
        with self.assertRaisesRegex(ValueError, 'edit_date_mismatch'):
            collector.validate_edit_tweet_ids(latest)
        path = self.directory / 'cross-day.json'
        collector.atomic_json(path, invalid)
        with self.assertRaisesRegex(ValueError, 'invalid_snapshot'):
            collector.load_snapshot(path)
        for first, second in ((old, invalid), (invalid, old)):
            with self.assertRaisesRegex(ValueError, 'edit_date_mismatch'):
                collector.merge_snapshots(first, second)
        self.assertEqual(invalid, before)

    def test_edit_chain_service_day_uses_five_am_not_midnight_or_utc_date(self):
        cases = [
            ('2026-09-06T14:55:00Z', '2026-09-06T15:05:00Z', True),
            ('2026-09-06T19:59:00Z', '2026-09-06T20:00:00Z', False),
        ]
        for source_created, created, accepted in cases:
            source, tid = make_id(source_created), make_id(created)
            value = edit_payload([source, tid], tid, created, footer=None)
            value['text'] = value['text'].replace('ひるにゃんこ', 'よるにゃんこ')
            now = collector.timestamp(created) + dt.timedelta(hours=1)
            with self.subTest(created=created):
                if accepted:
                    post = collector.validate_post(tid, value, NOTICE_DATE, dt.date(2026, 9, 7), now)
                    self.assertEqual((post['date'], post['shift']), ('2026-09-06', '夜'))
                    collector.validate_edit_tweet_ids(post)
                    self.assertEqual(collector.superseded_ids([post]), {source})
                else:
                    with self.assertRaisesRegex(collector.FetchFailure, 'edit_date_mismatch'):
                        collector.validate_post(tid, value, NOTICE_DATE, dt.date(2026, 9, 7), now)

    def test_saved_ancestor_day_is_not_replaced_by_its_snowflake_at_five_am(self):
        source_created = '2026-09-06T19:59:59Z'
        source = make_id('2026-09-06T20:00:00Z')
        created = '2026-09-06T20:10:00Z'
        tid = make_id(created)
        now = dt.datetime(2026, 9, 6, 20, 20, tzinfo=collector.UTC)
        end = dt.date(2026, 9, 7)
        previous = collector.validate_post(
            source, notice_payload(tid=source, created=source_created),
            NOTICE_DATE, end, now)
        old = collector.empty_snapshot()
        old['posts'] = [previous]
        value = edit_payload([source, tid], tid, created, footer=None)
        latest = collector.validate_post(tid, value, NOTICE_DATE, end, now)
        self.assertEqual((previous['date'], latest['date']), ('2026-09-06', '2026-09-07'))
        self.assertEqual(collector.current_notice_count([previous, latest]), 1)
        self.assertEqual(collector.superseded_ids([previous, latest]), set())
        client = FakeClient([[tid], []], {tid: value})
        result, report, code = collector.collect(old, set(), client, NOTICE_DATE, end, 20,
                                                clock=lambda: now)
        self.assertEqual((code, client.calls), (2, [tid]))
        self.assertEqual(result['posts'], old['posts'])
        self.assertEqual(report['failures'][0]['reason'], 'edit_date_mismatch')
        isolated = collector.empty_snapshot()
        isolated['posts'] = [latest]
        for name, state in (('previous', old), ('latest', isolated)):
            path = self.directory / (name + '.json')
            collector.atomic_json(path, state)
            self.assertEqual(collector.load_snapshot(path), state)
        for first, second in ((old, isolated), (isolated, old)):
            with self.assertRaisesRegex(ValueError, 'edit_date_mismatch'):
                collector.merge_snapshots(first, second)
        invalid = copy.deepcopy(old)
        invalid['posts'].append(latest)
        path = self.directory / 'combined.json'
        collector.atomic_json(path, invalid)
        with self.assertRaisesRegex(ValueError, 'invalid_snapshot'):
            collector.load_snapshot(path)

    def test_edit_source_pending_survives_general_resolved_mirror_in_both_merge_orders(self):
        old = self.state(SHORT_NOTICE)
        requested, _, _, _ = self.refresh(old, max_posts=1, client=FakeClient(
            [[], []], {NOTICE_ID: edit_payload([NOTICE_ID, EDIT_ID])}))
        requested_path = self.directory / 'requested.json'
        mirror_path = self.directory / 'resolved.json'
        collector.atomic_json(requested_path, requested)
        requested = collector.load_snapshot(requested_path)
        for resolved_at in (NOTICE_OBSERVED, NOTICE_NOW + dt.timedelta(minutes=1)):
            mirror = copy.deepcopy(old)
            mirror['resolved'] = [{
                'id': EDIT_ID, 'url': collector.canonical(EDIT_ID),
                'reason': 'not_shift_post', 'resolvedAt': collector.iso(resolved_at)}]
            collector.atomic_json(mirror_path, mirror)
            mirror = collector.load_snapshot(mirror_path)
            for first, second in ((requested, mirror), (mirror, requested)):
                with self.subTest(resolved_at=resolved_at, first_requested=first is requested):
                    merged = collector.merge_snapshots(first, second)
                    pending_by_id = {item['id']: item for item in merged['pending']}
                    self.assertEqual(pending_by_id[EDIT_ID]['editSourceId'], NOTICE_ID)
                    self.assertIsNone(pending_by_id[EDIT_ID]['lastAttemptAt'])
                    self.assertNotIn(EDIT_ID, {item['id'] for item in merged['resolved']})
                    collector.atomic_json(self.directory / 'merged.json', merged)
                    self.assertEqual(collector.load_snapshot(self.directory / 'merged.json'), merged)
                    latest = edit_payload([NOTICE_ID, EDIT_ID], EDIT_ID, EDIT_CREATED, footer=None)
                    client = FakeClient([[], []], {EDIT_ID: latest})
                    complete, _, code, _ = self.refresh(
                        merged, now=NOTICE_NOW + dt.timedelta(minutes=31), client=client)
                    self.assertEqual((code, client.calls), (0, [EDIT_ID]))
                    self.assertEqual(complete['pending'], [])

    def test_general_resolved_still_wins_without_edit_source_context(self):
        ordinary = self.state()
        ordinary['pending'] = [{
            'id': EDIT_ID, 'url': collector.canonical(EDIT_ID), 'reason': 'network_error',
            'firstSeenAt': collector.iso(NOTICE_OBSERVED),
            'lastAttemptAt': collector.iso(NOTICE_NOW), 'attempts': 1}]
        mirror = self.state()
        mirror['resolved'] = [{
            'id': EDIT_ID, 'url': collector.canonical(EDIT_ID),
            'reason': 'not_shift_post', 'resolvedAt': collector.iso(NOTICE_OBSERVED)}]
        for first, second in ((ordinary, mirror), (mirror, ordinary)):
            merged = collector.merge_snapshots(first, second)
            self.assertEqual(merged['pending'], [])
            self.assertEqual(merged['resolved'], mirror['resolved'])

    def test_edit_pending_requires_verified_source_membership_not_last_checked_or_nonshift(self):
        requested, _, _, _ = self.refresh(self.state(), max_posts=1, client=FakeClient(
            [[], []], {NOTICE_ID: edit_payload([NOTICE_ID, EDIT_ID])}))
        missing_proof = self.state()
        latest = self.validate(notice_payload(None, tid=EDIT_ID, created=EDIT_CREATED))
        latest['lastCheckedAt'] = collector.iso(NOTICE_NOW)
        missing_proof['posts'].append(latest)
        for first, second in ((requested, missing_proof), (missing_proof, requested)):
            merged = collector.merge_snapshots(first, second)
            self.assertIn(EDIT_ID, {item['id'] for item in merged['pending']})
        verified = copy.deepcopy(missing_proof)
        verified['posts'][-1]['editTweetIds'] = [NOTICE_ID, EDIT_ID]
        self.assertEqual(collector.merge_snapshots(requested, verified)['pending'], [])
        nonshift = edit_payload([NOTICE_ID, EDIT_ID], EDIT_ID, EDIT_CREATED)
        nonshift['text'] = 'お知らせ'
        client = FakeClient([[], []], {EDIT_ID: nonshift})
        retained, report, code, _ = self.refresh(requested, client=client)
        self.assertEqual((code, client.calls), (2, [EDIT_ID]))
        self.assertEqual(retained['posts'], requested['posts'])
        self.assertEqual(report['failures'][0]['reason'], 'edit_latest_unparsed')
        self.assertNotIn(EDIT_ID, {item['id'] for item in retained['resolved']})


if __name__ == '__main__':
    unittest.main()
