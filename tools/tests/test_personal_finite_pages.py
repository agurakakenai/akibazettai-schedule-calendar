"""Finite collector snapshots must survive the ordinary public staging path."""
import hashlib
import json
import unittest

import test_pages as fixtures


class FinitePersonalPublicationTests(fixtures.WorkspaceTests):
    def test_saved_eighteen_search_report_stages_without_losing_facts_or_private_state(self):
        state = fixtures.personal_snapshot()
        state['lastRun'].update(status='partial', sourceCount=18, newPostCount=1,
                                requests={'searches': 18, 'posts': 16}, targetCount=18)
        collector = fixtures.pages.load_personal_collector()
        path = self.write('data/personal-shifts.json', fixtures.pages.json_bytes(state))
        before = path.read_bytes()
        saved = collector.load_snapshot(path)
        self.assertEqual(saved['lastRun']['sourceCount'], 18)
        public = fixtures.pages.load_public_personal_snapshot(path)
        self.assertEqual(public['lastRun']['sourceCount'], 18)
        self.assertEqual(public['posts'], state['posts'])
        self.assertNotIn('identityBindings', public)
        self.assertNotIn('requests', public['lastRun'])
        output = self.root / '_site'
        manifest = fixtures.pages.stage(output, fixtures.SHA, root=self.root, clock=lambda: fixtures.NOW)
        published = output / 'data' / 'personal-shifts.json'
        self.assertEqual(json.loads(published.read_text()), public)
        self.assertEqual(manifest['files']['data/personal-shifts.json']['rawSHA256'],
                         hashlib.sha256(published.read_bytes()).hexdigest())
        self.assertEqual(path.read_bytes(), before)

    def test_counts_still_require_nonnegative_integers_and_real_new_posts(self):
        for changes in ({'sourceCount': True}, {'sourceCount': -1},
                        {'sourceCount': '18'}, {'sourceCount': 18, 'newPostCount': 2}):
            with self.subTest(changes=changes):
                state = fixtures.personal_snapshot()
                state['lastRun'].update(changes)
                with self.assertRaises(fixtures.pages.PagesError):
                    fixtures.pages.personal_projection(state)


if __name__ == '__main__':
    unittest.main()
