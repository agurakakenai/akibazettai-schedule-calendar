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


if __name__ == '__main__':
    unittest.main()
