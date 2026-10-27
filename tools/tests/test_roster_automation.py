"""Offline: python -m unittest discover -s tools/tests -p test_roster_automation.py"""
import argparse
import copy
import datetime as dt
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock


TOOLS = Path(__file__).resolve().parents[1]


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, TOOLS / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


roster = load('test_roster_automation_module', 'roster-automation.py')
members = roster.members
ledger = roster.ledger
sources = roster.source_state
UTC = dt.timezone.utc
NOW = dt.datetime(2026, 10, 14, 3, 0, tzinfo=UTC)
ENV = {'AZURE_OPENAI_ENDPOINT': 'https://offline.openai.azure.com/',
       'AZURE_OPENAI_DEPLOYMENT': 'gpt-5.6-luna', 'AZURE_OPENAI_API_KEY': 'OFFLINE_AZURE_SENTINEL'}
YUME_ID, AMU_ID = 'm-' + '1' * 32, 'm-' + '2' * 32
YUME_UID, OTHER_UID = '1234567890123', '9876543210987'


def post_id(created, salt=0):
    return str(((int(created.timestamp() * 1000) - 1288834974657) << 22) + salt)


def registry():
    value = members.empty_registry()
    value['members'] = [
        members.new_member('ゆめ', 'https://x.com/yume_test', NOW - dt.timedelta(days=200), member_id=YUME_ID),
        members.new_member('あむ', 'https://x.com/amu_test', NOW - dt.timedelta(days=200), member_id=AMU_ID)]
    return members.validate_registry(value)


def entry(created, text, *, handle='yume_test', uid=YUME_UID, mentions=(), salt=0):
    return {'id': post_id(created, salt), 'screenName': handle, 'userId': uid,
            'createdAt': int(created.timestamp()), 'displayText': text, 'replyMentions': list(mentions),
            'url': f'https://x.com/{handle}/status/{post_id(created, salt)}'}


def payload(item, text=None, **changes):
    created = dt.datetime.fromtimestamp(item['createdAt'], UTC)
    value = {'id_str': item['id'], 'user': {'id_str': item['userId'], 'screen_name': item['screenName']},
             'created_at': created.isoformat().replace('+00:00', '.000Z'),
             'text': item['displayText'] if text is None else text}
    value.update(changes)
    return value


def search_document(entries):
    data = {'props': {'pageProps': {'pageData': {'timeline': {'entry': entries}, 'searchError': {'errorType': ''}}}}}
    return ('<html><script id="__NEXT_DATA__" type="application/json">' + json.dumps(data)
            + '</script></html>').encode('utf-8')


class Reply:
    def __init__(self, body, status=200):
        self.body, self.status = body, status

    def getcode(self):
        return self.status

    def read(self, limit=None):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class SourceOpener:
    def __init__(self, searches, posts):
        self.searches, self.posts, self.urls = searches, posts, []

    def open(self, request, timeout=None):
        url = request.full_url
        self.urls.append(url)
        if url.startswith('https://search.yahoo.co.jp/'):
            handle = roster.urllib.parse.parse_qs(roster.urllib.parse.urlsplit(url).query)['p'][0].split()[0][3:]
            return Reply(search_document(self.searches.get(handle, [])))
        tid = roster.urllib.parse.parse_qs(roster.urllib.parse.urlsplit(url).query)['id'][0]
        return Reply(json.dumps(self.posts[tid]).encode('utf-8'))


def ai_reply(kind='self_graduation', date=(10, 27), lines=(1,)):
    value = {'kind': kind, 'lastServiceDate': None if date is None else {'month': date[0], 'day': date[1]},
             'evidenceLineIds': list(lines)}
    envelope = {'model': 'gpt-6-luna-2026-09-22', 'choices': [{'finish_reason': 'stop', 'message': {
        'content': json.dumps(value), 'refusal': None}}],
        'usage': {'prompt_tokens': 300, 'completion_tokens': 30, 'total_tokens': 330,
                  'prompt_tokens_details': {'cached_tokens': 0, 'cache_write_tokens': 0}}}
    reply = mock.MagicMock()
    reply.getcode.return_value = 200
    reply.read.return_value = json.dumps(envelope).encode()
    reply.__enter__.return_value = reply
    return reply


ANNOUNCEMENT = '大切なおしらせ\n10月27日のお給仕をもってゆめはアキバ絶対領域を卒業します！\n最後までよろしくね'


class DateTests(unittest.TestCase):
    def test_written_dates_resolve_and_relative_words_never_do(self):
        created = NOW
        cases = {
            '10月27日のお給仕をもって卒業します': [dt.date(2026, 10, 27)],
            '10/27 で卒業': [dt.date(2026, 10, 27)],
            '16日（金）夜にゃん 卒業': [dt.date(2026, 10, 16)],
            '２０２６年１０月２７日 卒業': [dt.date(2026, 10, 27)],
            '1月10日で卒業します': [dt.date(2027, 1, 10)],
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(sorted(roster.explicit_dates(text, created)), expected)
        for text in ('明日卒業です', '本日卒業です', '卒業します！ありがとう', '16日（土）卒業', '2026年2月30日 卒業'):
            with self.subTest(text=text):
                self.assertEqual(roster.explicit_dates(text, created), set())

    def test_model_answer_counts_only_with_its_date_written_in_the_post(self):
        parsed = {'kind': 'self_graduation', 'date': (10, 27), 'lines': ['10月27日のお給仕をもって卒業します']}
        self.assertEqual(roster.adopt(parsed, ANNOUNCEMENT, NOW), (dt.date(2026, 10, 27), 'adopted'))
        self.assertEqual(roster.adopt({**parsed, 'date': (10, 28)}, ANNOUNCEMENT, NOW), (None, 'date_not_in_text'))
        self.assertEqual(roster.adopt({**parsed, 'date': None}, ANNOUNCEMENT, NOW), (None, 'no_explicit_date'))
        for kind in ('other_graduation', 'event_or_plan', 'unrelated'):
            self.assertEqual(roster.adopt({**parsed, 'kind': kind}, ANNOUNCEMENT, NOW)[0], None)
        self.assertEqual(roster.adopt(None, ANNOUNCEMENT, NOW), (None, 'not_analyzed'))


class FilterTests(unittest.TestCase):
    def test_search_candidates_are_her_own_dated_graduation_posts(self):
        created = NOW - dt.timedelta(hours=3)
        own = entry(created, ANNOUNCEMENT, salt=1)
        self_reply = entry(created, '10/27で卒業します', mentions=['yume_test'], salt=2)
        rows = [own, self_reply,
                entry(created, '@other 10/27で卒業おめでとう', mentions=['other'], salt=3),
                entry(created, '卒業します（日付は後日）', salt=4),
                entry(created, '明日卒業', salt=5),
                entry(created, ANNOUNCEMENT, handle='other_member', salt=6),
                entry(created, ANNOUNCEMENT, uid=roster.OFFICIAL_AUTHOR_ID, salt=7),
                entry(created, '10月27日はシフトです', salt=8),
                entry(NOW - dt.timedelta(days=200), ANNOUNCEMENT, salt=9)]
        document = search_document(rows).decode()
        found = roster.candidate_entries(document, 'yume_test', None, NOW)
        self.assertEqual({row['id'] for row in found}, {own['id'], self_reply['id']})
        bound = roster.candidate_entries(document, 'yume_test', {'authorId': OTHER_UID}, NOW)
        self.assertEqual(bound, [])

    def test_post_body_must_be_her_own_top_level_or_self_reply(self):
        item = entry(NOW - dt.timedelta(hours=1), ANNOUNCEMENT)
        candidate = {'id': item['id'], 'userId': item['userId'],
                     'createdAt': dt.datetime.fromtimestamp(item['createdAt'], UTC)}
        self.assertEqual(roster.verified_text(payload(item), candidate, 'yume_test')[1], 'ok')
        self.assertEqual(roster.verified_text(payload(item, in_reply_to_status_id_str='1' * 19,
                                                      in_reply_to_user_id_str=YUME_UID), candidate, 'yume_test')[1], 'ok')
        for changes, reason in (({'in_reply_to_status_id_str': '1' * 19, 'in_reply_to_user_id_str': OTHER_UID},
                                 'reply_to_other'),
                                ({'retweeted_status': {'id_str': '1'}}, 'repost'),
                                ({'user': {'id_str': OTHER_UID, 'screen_name': 'yume_test'}}, 'author_mismatch'),
                                ({'text': '10月27日はおやすみ'}, 'no_keyword'),
                                ({'id_str': '1' * 19}, 'response_id_mismatch')):
            with self.subTest(reason=reason):
                self.assertEqual(roster.verified_text(payload(item, **changes), candidate, 'yume_test'),
                                 (None, reason))


class RunTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.members = self.root / 'members.json'
        members.atomic_write(self.members, members.json_bytes(registry()))
        self.state_path = self.root / 'roster-updates.json'
        self.source_path = self.root / 'source-usage.json'
        sources.initialize(self.source_path, {'budgets': {}, 'paused': None}, source_hash='0' * 64,
                           at=NOW - dt.timedelta(days=1))
        self.ai_path = self.root / 'ai-usage.json'
        state = ledger.empty_state()
        state['money'] = ledger.costs.empty()
        ledger.atomic_json(self.ai_path, state)
        self.now = NOW
        self.created = NOW - dt.timedelta(hours=5)
        self.announcement = entry(self.created, ANNOUNCEMENT, salt=1)
        self.searches = {'yume_test': [self.announcement,
                                       entry(self.created, '@fan 10/27卒業だよ', mentions=['fan'], salt=2)],
                         'amu_test': []}
        self.posts = {self.announcement['id']: payload(self.announcement)}
        self.ai = mock.MagicMock()
        self.ai.open.return_value = ai_reply()

    def run_once(self, run_id='roster-1-1'):
        args = argparse.Namespace(state=self.state_path, members=self.members, personal_state=None,
                                  half_month_state=None, source_state=self.source_path, ai_state=self.ai_path,
                                  run_id=run_id, report=self.root / 'report.json')
        self.source = SourceOpener(self.searches, self.posts)
        elapsed = [0.0]

        def clock():
            elapsed[0] += 1
            return self.now + dt.timedelta(seconds=elapsed[0])

        def sleep(seconds):
            elapsed[0] += seconds
        return roster.run(args, clock=clock, sleep=sleep, environment=ENV, opener=self.source,
                          analyzer_factory=lambda usage, environment: roster.GraduationAnalyzer(
                              usage, environment, opener=self.ai))

    def receipts(self):
        return ledger.load_state(self.ai_path)['receipts']

    def test_self_announced_dated_graduation_is_recorded_once_with_evidence(self):
        report = self.run_once()
        self.assertEqual((report['status'], report['exitCode']), ('ok', 0))
        state = roster.load_state(self.state_path)[0]
        self.assertEqual(len(state['graduations']), 1)
        row = state['graduations'][0]
        self.assertEqual((row['memberId'], row['name'], row['lastServiceDate']), (YUME_ID, 'ゆめ', '2026-10-27'))
        self.assertEqual(row['evidenceUrl'], f"https://x.com/yume_test/status/{self.announcement['id']}")
        self.assertEqual(row['bodySha256'], roster.digest(ANNOUNCEMENT))
        self.assertEqual(report['changes'][0]['effectiveFrom'], '2026-10-28')
        self.assertEqual(state['changes'], report['changes'])
        self.assertEqual(report['requests'], {'searches': 2, 'posts': 1, 'analysis': 1})
        receipts = list(self.receipts().values())
        self.assertEqual([(item['component'], item['reason']) for item in receipts], [('personal', 'events')])
        self.assertEqual(receipts[0]['identity']['model'], 'gpt-6-luna')
        source = sources.load_state(self.source_path, required=True)
        self.assertEqual(sorted((item['component'], item['kind']) for item in source['receipts'].values()),
                         [('roster', 'posts'), ('roster', 'searches'), ('roster', 'searches')])
        self.assertEqual(self.ai.open.call_count, 1)
        # The reply to someone else was never fetched or analysed.
        self.assertEqual(len([url for url in self.source.urls if 'tweet-result' in url]), 1)
        effective, applied, _ = roster.effective_registry(members.load_registry(self.members), state)
        self.assertEqual(len(applied), 1)
        self.assertIn(YUME_ID, members.collection_population(effective, today='2026-10-27')[0])
        self.assertNotIn(YUME_ID, members.collection_population(effective, today='2026-10-28')[0])
        self.assertNotIn('ゆめ', members.display_projection(effective, '2026-10-28')['roster'])
        # A later day neither searches her again nor reissues AI.
        self.now += dt.timedelta(days=1)
        report = self.run_once('roster-2-1')
        self.assertEqual(report['requests'], {'searches': 1, 'posts': 0, 'analysis': 0})
        self.assertEqual(len(self.receipts()), 1)

    def test_rejected_or_failed_answers_are_cached_and_never_reissued(self):
        for kind, date, reason in (('other_graduation', (10, 27), 'not_self_graduation'),
                                   ('event_or_plan', (10, 27), 'not_self_graduation'),
                                   ('self_graduation', None, 'no_explicit_date'),
                                   ('self_graduation', (10, 28), 'date_not_in_text')):
            with self.subTest(kind=kind, date=date):
                self.setUp()
                self.ai.open.return_value = ai_reply(kind, date)
                report = self.run_once()
                state = roster.load_state(self.state_path)[0]
                self.assertEqual(state['graduations'], [])
                self.assertEqual(report['status'], 'no-new')
                self.assertEqual(state['posts'][self.announcement['id']]['reason'], reason)
                self.now += dt.timedelta(days=1)
                report = self.run_once('roster-2-1')
                self.assertEqual(report['requests']['analysis'], 0)
                self.assertEqual(report['requests']['posts'], 0)
                self.assertEqual(len(self.receipts()), 1)
                self.assertEqual(self.ai.open.call_count, 1)

    def test_interrupted_analysis_is_not_retried(self):
        self.ai.open.side_effect = TimeoutError('offline')
        self.run_once()
        state = roster.load_state(self.state_path)[0]
        self.assertEqual(state['posts'][self.announcement['id']]['reason'], 'ai_timeout')
        self.assertEqual([item['reason'] for item in self.receipts().values()], ['azure_timeout'])
        self.now += dt.timedelta(days=1)
        self.ai.open.side_effect = None
        self.run_once('roster-2-1')
        self.assertEqual(len(self.receipts()), 1)
        self.assertEqual(self.ai.open.call_count, 1)
        # Even with the cache lost, the ledger receipt refuses a second issue.
        state['posts'].clear()
        roster.save_state(self.state_path, state)
        self.now += dt.timedelta(days=1)
        self.run_once('roster-3-1')
        self.assertEqual(len(self.receipts()), 1)
        self.assertEqual(roster.load_state(self.state_path)[0]['posts'][self.announcement['id']]['reason'],
                         'ai_timeout')

    def test_undated_announcements_cost_no_post_or_ai(self):
        undated = entry(self.created, '大切なおしらせ\nわたしは卒業することになりました', salt=3)
        self.searches['yume_test'] = [undated]
        report = self.run_once()
        self.assertEqual(report['requests'], {'searches': 2, 'posts': 0, 'analysis': 0})
        self.assertEqual(roster.load_state(self.state_path)[0]['graduations'], [])
        self.ai.open.assert_not_called()

    def test_budget_exhaustion_stops_before_any_post_and_retries_later(self):
        state = ledger.load_state(self.ai_path)
        state['paused'] = {'reason': 'azure_auth_stopped', 'httpStatus': 401, 'at': '2026-10-13T00:00:00Z'}
        ledger.atomic_json(self.ai_path, state)
        report = self.run_once()
        self.assertEqual((report['status'], report['reason']), ('partial', 'ai_auth_stopped'))
        self.assertEqual(report['requests']['posts'], 0)
        self.assertEqual(roster.load_state(self.state_path)[0]['posts'], {})

    def test_access_denial_stops_the_run_and_cools_the_host(self):
        class Denied(SourceOpener):
            def open(self, request, timeout=None):
                raise roster.urllib.error.HTTPError(request.full_url, 403, 'denied', {}, None)
        args = argparse.Namespace(state=self.state_path, members=self.members, personal_state=None,
                                  half_month_state=None, source_state=self.source_path, ai_state=self.ai_path,
                                  run_id='roster-1-1', report=None)
        report = roster.run(args, clock=lambda: self.now, sleep=lambda seconds: None, environment=ENV,
                            opener=Denied({}, {}),
                            analyzer_factory=lambda usage, environment: roster.GraduationAnalyzer(
                                usage, environment, opener=self.ai))
        self.assertEqual((report['status'], report['reason'], report['exitCode']), ('unavailable', 'access_denied', 3))
        source = sources.load_state(self.source_path, required=True)
        self.assertIn(roster.SEARCH_HOST, source['cooldowns'])
        self.assertEqual(roster.load_state(self.state_path)[0]['lastRun']['status'], 'unavailable')


class OverlayTests(unittest.TestCase):
    def state(self):
        value = roster.empty_state()
        value['graduations'].append({
            'memberId': YUME_ID, 'name': 'ゆめ', 'lastServiceDate': '2026-10-27',
            'evidenceUrl': 'https://x.com/yume_test/status/2109661316923396251', 'postId': '2109661316923396251',
            'authorId': YUME_UID, 'bodySha256': '0' * 64, 'postCreatedAt': '2026-10-14T03:00:00Z',
            'detectedAt': '2026-10-15T00:00:00Z', 'runId': 'roster-1-1'})
        return roster.validate_state(value)

    def test_manual_graduation_and_rejections_win_over_automatic_ones(self):
        base = registry()
        effective, applied, skipped = roster.effective_registry(base, self.state())
        self.assertEqual(len(applied), 1)
        self.assertIsNone(base['members'][0]['graduation'])
        manual = members.set_graduation(base, 'ゆめ', last_service_date='2026-10-20',
                                        evidence_url='https://x.com/akibazettai/status/2109661316923396999')
        effective, applied, skipped = roster.effective_registry(manual, self.state())
        self.assertEqual(effective['members'][0]['graduation']['lastServiceDate'], '2026-10-20')
        self.assertEqual((applied, skipped[0]['skipReason']), ([], 'manual_graduation'))
        for post in ('2109661316923396251', None):
            rejected = members.reject_auto_graduation(base, 'ゆめ', now=NOW, post_id=post)
            effective, applied, skipped = roster.effective_registry(rejected, self.state())
            self.assertIsNone(effective['members'][0]['graduation'])
            self.assertEqual(skipped[0]['skipReason'], 'rejected_by_override')
        other_post = members.reject_auto_graduation(base, 'ゆめ', now=NOW, post_id='2109661316923399999')
        self.assertEqual(len(roster.effective_registry(other_post, self.state())[1]), 1)
        cleared = members.clear_graduation(manual, 'ゆめ')
        self.assertEqual(len(roster.effective_registry(cleared, self.state())[1]), 1)
        with self.assertRaisesRegex(members.RegistryError, 'no_manual_graduation'):
            members.clear_graduation(base, 'ゆめ')

    def test_publish_writes_only_the_checkout_and_keeps_the_base(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'data').mkdir()
            path = root / 'data' / 'members.json'
            base = members.json_bytes(registry())
            path.write_bytes(base)
            state_path = root / 'state.json'
            self.assertEqual(roster.publish(state_path, root, stamp_assets=False)['changed'], False)
            self.assertEqual(path.read_bytes(), base)
            roster.save_state(state_path, self.state())
            result = roster.publish(state_path, root, stamp_assets=False)
            self.assertTrue(result['changed'])
            published = members.load_registry(path)
            self.assertEqual(published['members'][0]['graduation']['lastServiceDate'], '2026-10-27')
            members.check_projection(path, published)
            # Collectors always start again from the committed bytes.
            roster.publish(state_path, root, stamp_assets=False, base_bytes=base)
            self.assertEqual(members.load_registry(path), published)

    def test_state_rejects_official_evidence_bad_history_and_unknown_fields(self):
        good = self.state()
        cases = []
        value = copy.deepcopy(good)
        value['graduations'][0]['evidenceUrl'] = 'https://x.com/akibazettai/status/2109661316923396251'
        cases.append(value)
        value = copy.deepcopy(good)
        value['graduations'][0]['extra'] = True
        cases.append(value)
        value = copy.deepcopy(good)
        value['changes'].append({'at': '2026-10-15T00:00:00Z', 'runId': 'roster-1-1', 'kind': 'graduation',
                                 'memberId': YUME_ID, 'name': 'ゆめ', 'lastServiceDate': '2026-10-27',
                                 'effectiveFrom': '2026-10-27', 'evidenceUrl': good['graduations'][0]['evidenceUrl'],
                                 'postId': '2109661316923396251'})
        cases.append(value)
        value = copy.deepcopy(good)
        value['graduations'].append(copy.deepcopy(value['graduations'][0]))
        cases.append(value)
        for value in cases:
            with self.assertRaises(roster.RosterError):
                roster.validate_state(value)

    def test_due_once_a_day_with_bounded_retries(self):
        self.assertTrue(roster.due(None, NOW))
        state = roster.empty_state()
        state['lastRun'] = {'at': '2026-10-14T01:00:00Z', 'date': '2026-10-14', 'runs': 1, 'status': 'ok',
                            'reason': None}
        self.assertFalse(roster.due(state, NOW))
        self.assertTrue(roster.due(state, NOW + dt.timedelta(days=1)))
        state['lastRun'].update(status='partial', runs=2)
        self.assertTrue(roster.due(state, NOW))
        state['lastRun']['runs'] = roster.RUNS_PER_DAY
        self.assertFalse(roster.due(state, NOW))


if __name__ == '__main__':
    unittest.main()
