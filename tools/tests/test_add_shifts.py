"""Offline importer regressions: python -m unittest discover -s tools/tests."""
import contextlib
import csv
import importlib.util
import io
import json
from pathlib import Path
import unittest
from unittest import mock
import urllib.error


TOOLS = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('add_shifts', TOOLS / 'add-shifts.py')
add_shifts = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(add_shifts)
POSTS = json.loads(
    (Path(__file__).parent / 'fixtures' / 'shift-posts.json').read_text(encoding='utf-8'))
CREATED = '2026-09-03T03:00:00.000Z'
HEADER = '【アキバ絶対領域】\nひるにゃんこ🐈🍓\n\n'
FACE = '⊂(´ω´⊂)))'
TID = '2095346563048767527'
URL = 'https://x.com/akibazettai/status/' + TID
FIELDS = ['date', 'store', 'shift', 'maid', 'tweet_id']


def minimal_post(post, blank_before_face=False):
    header = f"【{post['store_header']}】\n{post['shift']}にゃんこ\n\n"
    gap = '\n\n' if blank_before_face else '\n'
    return header + '\n'.join(post['names']) + gap + FACE


class ParseTests(unittest.TestCase):
    def test_six_public_post_minimal_reproductions_without_blank_before_face(self):
        self.assertEqual(len(POSTS), 6)
        self.assertEqual(sum(len(post['names']) for post in POSTS), 35)
        for post in POSTS:
            with self.subTest(tweet_id=post['id']):
                self.assertEqual(set(post), {'id', 'created_at', 'store_header',
                                             'date', 'store', 'shift', 'names'})
                text = minimal_post(post)
                self.assertNotIn('\n\n' + FACE, text)
                self.assertEqual(add_shifts.parse(text, post['created_at']),
                                 (post['date'], post['store'], post['shift'], post['names']))

    def test_blank_line_before_face_is_equivalent(self):
        for post in POSTS:
            with self.subTest(tweet_id=post['id']):
                text = minimal_post(post, blank_before_face=True)
                self.assertEqual(add_shifts.parse(text, post['created_at']),
                                 (post['date'], post['store'], post['shift'], post['names']))

    def test_face_terminates_names_even_when_short_body_follows(self):
        for gap in ('\n', '\n\n'):
            with self.subTest(gap=gap):
                text = HEADER + 'あむ\nこい' + gap + FACE + '\nお知らせ\n\nまこと'
                self.assertEqual(add_shifts.parse(text, CREATED)[3], ['あむ', 'こい'])

    def test_face_does_not_turn_separate_footer_into_a_name(self):
        text = '【アキバ絶対領域】\nひるにゃんこ\n\nあむ\n\nお知らせ\n' + FACE
        self.assertEqual(add_shifts.parse(text, CREATED)[3], ['あむ'])

    def test_unconfirmed_roster_cannot_be_replaced_by_footer(self):
        text = HEADER + 'あむ\nてすとにゃん\n\nお知らせ\n' + FACE
        self.assertIsNone(add_shifts.parse(text, CREATED))
        self.assertEqual(
            add_shifts.parse(text, CREATED, confirmed_names={'てすとにゃん'})[3],
            ['あむ', 'てすとにゃん'])

    def test_separate_store_and_shift_headers_do_not_block_missing_blank_repair(self):
        text = '【アキバ絶対領域】\n\nひるにゃんこ\n\nあむ\nこい\n' + FACE
        self.assertEqual(add_shifts.parse(text, CREATED)[3], ['あむ', 'こい'])

    def test_confirmed_host_and_roster_blocks_are_both_preserved(self):
        # Facts from the six published posts; do not embed their prose.
        cases = [
            ('1517710980888678400', 'すなお🎂', ['きなこ', 'うみ', 'しろ', 'りこ']),
            ('1517780729274404864', 'つき🌸', ['えーる', 'いの', 'きなこ', 'めい', 'やみ', 'あらた']),
            ('1520601723567370240', 'あむ🎂', ['くすり', 'やみ', 'きなこ', 'しずく', 'さほ', 'もなか', 'みどり']),
            ('1524701397614141442', 'くすり🎂', ['のん', 'しずく', 'ななか', 'こむぎ', 'いなり', 'あおい']),
            ('1525310476279656448', 'あやたか🎂', ['こむぎ', 'ややち', 'てんか']),
            ('1526759194338000897', 'みどり🎉', ['ちの', 'みりあ', 'しずく', 'さほ', 'ぱるむ', 'たま']),
        ]
        for tid, host, names in cases:
            for footer in ('\n\n' + FACE, '\n\nお知らせ\n' + FACE):
                with self.subTest(tweet_id=tid, footer=footer):
                    text = HEADER + host + '\n\n' + '\n'.join(names) + footer
                    self.assertEqual(add_shifts.parse(text, CREATED)[3], [host[:-1], *names])

    def test_face_does_not_make_a_mixed_body_block_into_names(self):
        for body in ('あとから、えみるちゃんもくるにゃんね〜',
                     'えいちゃん卒業にゃん', '待ってるにゃんね',
                     '巫女コスデーにゃんね～'):
            with self.subTest(body=body):
                self.assertIsNone(add_shifts.parse(
                    HEADER + 'あむ\n' + body + '\n' + FACE, CREATED))

    def test_only_exact_standalone_face_is_a_boundary(self):
        for face in (FACE + '待ってるにゃんね', '⊂これは本文', '⊂(´ω´⊂))'):
            with self.subTest(face=face):
                self.assertIsNone(add_shifts.parse(HEADER + 'あむ\n' + face, CREATED))

    def test_empty_face_section_does_not_extract_later_body(self):
        self.assertIsNone(add_shifts.parse(HEADER + FACE + '\n\nまこと', CREATED))

    def test_known_body_and_late_arrivals_are_not_added(self):
        for body in ('巫女コスデーにゃんね～',
                     'まこっちゃん周年にゃんね',
                     'えいちゃん卒業にゃん',
                     '待ってるにゃんね',
                     '浴衣コスデーにゃんね',
                     'あとから、えみるちゃんもくるにゃんね〜' + FACE,
                     'みんとちゃん、こいちゃんもあとから来るにゃんね~' + FACE):
            with self.subTest(body=body):
                got = add_shifts.parse(HEADER + 'あむ\nまこと🎉\n\n' + body, CREATED)
                self.assertEqual(got[3], ['あむ', 'まこと'])

    def test_verified_azunyan_is_accepted_by_default_without_losing_block(self):
        for suffix in ('', '🎉'):
            with self.subTest(suffix=suffix):
                text = HEADER + 'あむ\nあずにゃん' + suffix + '\n' + FACE
                self.assertEqual(add_shifts.parse(text, CREATED)[3], ['あむ', 'あずにゃん'])

    def test_unknown_nyan_suffix_still_requires_explicit_confirmation(self):
        text = HEADER + 'あむ\nてすとにゃん🎉\n' + FACE
        self.assertIsNone(add_shifts.parse(text, CREATED))
        self.assertEqual(
            add_shifts.parse(text, CREATED, confirmed_names={'てすとにゃん'})[3],
            ['あむ', 'てすとにゃん'])

    def test_verified_name_does_not_relax_default_prose_filter(self):
        for body in ('えいちゃん卒業にゃん', '待ってるにゃん', '巫女コスデーにゃんね'):
            with self.subTest(body=body):
                text = HEADER + 'あむ\nあずにゃん\n\n' + body
                self.assertEqual(add_shifts.parse(text, CREATED)[3], ['あむ', 'あずにゃん'])

    def test_confirmation_does_not_globally_relax_prose_suffix_filter(self):
        for body in ('えいちゃん卒業にゃん', '待ってるにゃん', '巫女コスデーにゃんね'):
            with self.subTest(body=body):
                got = add_shifts.parse(
                    HEADER + 'あむ\nあずにゃん\n\n' + body, CREATED,
                    confirmed_names={'あずにゃん'})
                self.assertEqual(got[3], ['あむ', 'あずにゃん'])

    def test_confirmation_cannot_add_missing_names_or_natural_language(self):
        body = 'あとから、えみるちゃんもくるにゃんね〜'
        got = add_shifts.parse(HEADER + 'あむ\n\n' + body, CREATED,
                               confirmed_names={'えみる', 'こい', body})
        self.assertEqual(got[3], ['あむ'])
        self.assertIsNone(add_shifts.parse(
            HEADER + 'あむ\n' + body, CREATED, confirmed_names={body}))

    def test_unknown_names_and_suspect_spellings_are_not_corrected(self):
        got = add_shifts.parse(HEADER + 'もな\nみずれ\nみひん\nあるか\nあむ\nあむ🎉', CREATED)
        self.assertEqual(got[3], ['もな', 'みずれ', 'みひん', 'あるか', 'あむ'])

    def test_store_and_shift_normalization_remains_supported(self):
        cases = [('アキバ絶対 A.D.1912', '昼', '2号店 A.D.1912', 'ひる'),
                 ('アキバ絶対領域＋ｅ', 'ヒル', '3号店 +e', 'ひる'),
                 ('アキバ絶対領域 Ａ．Ｄ．２０４５', '夜', '4号店 A.D.2045', 'よる')]
        for store, word, expected_store, expected_shift in cases:
            with self.subTest(store=store, shift=word):
                text = f'【{store}】\n{word}にゃんこ\n\nあむ\nこい'
                self.assertEqual(add_shifts.parse(text, CREATED),
                                 ('2026-09-03', expected_store, expected_shift,
                                  ['あむ', 'こい']))

    def test_dates_preserve_jst_and_early_morning_rule(self):
        for created, expected in [
                ('2026-09-02T16:00:00.000Z', '2026-09-02'),
                ('2026-09-02T20:00:00.000Z', '2026-09-03'),
                ('Thu Sep 03 03:00:00 +0000 2026', '2026-09-03')]:
            with self.subTest(created=created):
                self.assertEqual(add_shifts.parse(HEADER + 'あむ', created)[0], expected)
        for created in (None, '', 'invalid'):
            with self.subTest(created=created):
                self.assertIsNone(add_shifts.parse(HEADER + 'あむ', created))

    def test_non_shift_announcements_are_rejected(self):
        for text in ('新人にゃんこ\n\nあむ', 'お知らせ\n\nあむ', HEADER):
            with self.subTest(text=text):
                self.assertIsNone(add_shifts.parse(text, CREATED))


class MainTests(unittest.TestCase):
    def run_main(self, args, responses=(), initial_rows=()):
        initial = io.StringIO(newline='')
        writer = csv.DictWriter(initial, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(initial_rows)
        written = []

        class WriteBuffer(io.StringIO):
            def close(self):
                written.append(self.getvalue())
                super().close()

        def csv_open(path, mode='r', **kwargs):
            self.assertEqual(path, add_shifts.SHIFTS)
            if mode == 'w':
                return WriteBuffer(newline='')
            self.assertEqual(mode, 'r')
            return io.StringIO(initial.getvalue(), newline='')

        output = io.StringIO()
        with (mock.patch.object(add_shifts, 'open', side_effect=csv_open, create=True),
              mock.patch.object(add_shifts, 'load_known', return_value=({}, set())),
              mock.patch.object(add_shifts, 'fetch', side_effect=responses) as fetch,
              mock.patch('sys.argv', ['add-shifts.py', *args]),
              contextlib.redirect_stdout(output)):
            code = add_shifts.main()
        rows = list(csv.DictReader(io.StringIO(written[0]))) if written else None
        return code, output.getvalue(), fetch, rows

    def post(self, names=('あむ', 'こい', 'もなか', 'まこと')):
        return {'text': HEADER + '\n'.join(names), 'created_at': CREATED}

    def test_same_new_url_and_id_are_added_once(self):
        code, output, fetch, rows = self.run_main(
            [URL, TID, URL], [self.post(), self.post(), self.post()])
        self.assertEqual(code, 0)
        fetch.assert_called_once_with(TID)
        self.assertEqual(len(rows), 4)
        self.assertEqual({row['maid'] for row in rows}, {'あむ', 'こい', 'もなか', 'まこと'})
        self.assertTrue(all(row['tweet_id'] == TID for row in rows))
        self.assertIn('4 行を追加します。', output)
        self.assertEqual(output.count('すでに入っています'), 2)

    def test_dry_run_deduplicates_without_writing(self):
        code, output, fetch, rows = self.run_main(
            ['--dry-run', URL, TID], [self.post(), self.post()])
        self.assertEqual(code, 0)
        fetch.assert_called_once_with(TID)
        self.assertIsNone(rows)
        self.assertIn('4 行を追加します。', output)
        self.assertIn('--dry-run なので書きません。', output)

    def test_existing_id_is_never_fetched_or_rewritten(self):
        existing = dict(zip(FIELDS, ['2026-09-02', '1号店 アキバ絶対領域', 'ひる', 'あむ', TID]))
        code, output, fetch, rows = self.run_main([URL, TID], initial_rows=[existing])
        self.assertEqual(code, 1)
        fetch.assert_not_called()
        self.assertIsNone(rows)
        self.assertEqual(output.count('すでに入っています'), 2)

    def test_distinct_posts_preserve_existing_rows_and_sort_output(self):
        other_id = '2095345641237131724'
        existing = dict(zip(FIELDS, ['2026-09-04', '1号店 アキバ絶対領域',
                                     'よる', 'あむ', '2095446715176485157']))
        code, _, fetch, rows = self.run_main(
            [URL, other_id, TID], [self.post(['こい']), self.post(['あむ'])],
            initial_rows=[existing])
        self.assertEqual(code, 0)
        self.assertEqual(fetch.call_args_list, [mock.call(TID), mock.call(other_id)])
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[-1], existing)
        self.assertEqual([(row['maid'], row['tweet_id']) for row in rows[:2]],
                         [('あむ', other_id), ('こい', TID)])

    def test_invalid_url_is_skipped_without_fetching_or_writing(self):
        code, output, fetch, rows = self.run_main(['not-a-post'])
        self.assertEqual(code, 1)
        fetch.assert_not_called()
        self.assertIsNone(rows)
        self.assertIn('URL から ID を読めません', output)

    def test_failed_fetch_or_parse_can_retry_same_id_later_in_batch(self):
        failures = [
            urllib.error.HTTPError(URL, 503, 'unavailable', {}, None),
            urllib.error.URLError('offline'),
            {'text': 'お知らせ', 'created_at': CREATED},
            {'text': HEADER + 'あむ', 'created_at': 'invalid'},
        ]
        for failure in failures:
            with self.subTest(failure=failure):
                code, output, fetch, rows = self.run_main(
                    [URL, TID, URL], [failure, self.post(), self.post()])
                self.assertEqual(code, 0)
                self.assertEqual(fetch.call_count, 2)
                self.assertEqual(len(rows), 4)
                self.assertEqual(output.count('すでに入っています'), 1)

    def test_all_failures_leave_csv_untouched(self):
        code, _, fetch, rows = self.run_main(
            [URL, TID], [urllib.error.URLError('offline'), {'text': 'お知らせ'}])
        self.assertEqual(code, 1)
        self.assertEqual(fetch.call_count, 2)
        self.assertIsNone(rows)

    def test_confirm_name_option_is_explicit_repeatable_and_not_a_rename(self):
        code, _, fetch, rows = self.run_main(
            ['--confirm-name', 'てすとにゃん', '--confirm-name', 'ふぃくすにゃん', URL],
            [self.post(['あむ', 'あずにゃん', 'てすとにゃん', 'ふぃくすにゃん',
                        'もな', 'みずれ', 'みひん'])])
        self.assertEqual(code, 0)
        fetch.assert_called_once_with(TID)
        self.assertEqual({row['maid'] for row in rows},
                         {'あむ', 'あずにゃん', 'てすとにゃん', 'ふぃくすにゃん',
                          'もな', 'みずれ', 'みひん'})

    def test_verified_name_is_imported_without_cli_confirmation(self):
        code, _, _, rows = self.run_main([URL], [self.post(['あむ', 'あずにゃん'])])
        self.assertEqual(code, 0)
        self.assertEqual({row['maid'] for row in rows}, {'あむ', 'あずにゃん'})

    def test_unconfirmed_suffix_reports_manual_review_option_without_writing(self):
        code, output, _, rows = self.run_main([URL], [self.post(['あむ', 'てすとにゃん'])])
        self.assertEqual(code, 1)
        self.assertIn('--confirm-name NAME', output)
        self.assertIsNone(rows)

    def test_footer_is_not_written_as_a_person(self):
        post = {'text': HEADER + 'あむ\n\nお知らせ\n' + FACE, 'created_at': CREATED}
        code, _, _, rows = self.run_main([URL], [post])
        self.assertEqual(code, 0)
        self.assertEqual([row['maid'] for row in rows], ['あむ'])

    def test_footer_cannot_make_ambiguous_roster_succeed_or_skip_retry(self):
        failed = {'text': HEADER + 'あむ\nてすとにゃん\n\nお知らせ\n' + FACE,
                  'created_at': CREATED}
        code, output, _, rows = self.run_main([URL], [failed])
        self.assertEqual(code, 1)
        self.assertIn('--confirm-name NAME', output)
        self.assertIsNone(rows)
        code, _, fetch, rows = self.run_main([URL, TID], [failed, self.post(['あむ'])])
        self.assertEqual(code, 0)
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual([row['maid'] for row in rows], ['あむ'])


if __name__ == '__main__':
    unittest.main()
