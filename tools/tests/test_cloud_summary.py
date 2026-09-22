import contextlib
import io
from pathlib import Path
import tempfile
import unittest

from test_cloud_collection import cloud


class ScheduleSummaryTests(unittest.TestCase):
    def test_diagnostic_and_next_continuation_are_visible_without_private_evidence(self):
        row = {
            'name': 'あむ', 'postId': '2096252018260062487',
            'postUrl': 'https://x.com/amu_zettai/status/2096252018260062487',
            'imageIndex': None, 'failedAt': '2026-10-01T00:00:00Z',
            'host': None, 'httpStatus': None, 'retryAt': None,
            'stage': 'cache', 'nextStage': 'held', 'reason': 'image_cache_expired',
        }
        result = {
            'halfMonthAcquisition': {'period': '2026-10-01', 'searched': 1,
                                     'targets': 40, 'confirmed': 0, 'pending': 1},
            'halfMonthContinuation': {'cursor': 1, 'names': ['あむ', 'いと'],
                                      'reason': 'time_limit', 'ready': True, 'nextAt': None},
            'halfMonthDiagnostics': [row],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'summary.txt'
            with contextlib.redirect_stdout(io.StringIO()):
                cloud.emit(result, {'GITHUB_STEP_SUMMARY': str(path)})
            text = path.read_text(encoding='utf-8')
        self.assertIn(row['postUrl'], text)
        self.assertIn('image_cache_expired', text)
        self.assertIn('nextStage=held', text)
        self.assertIn('cursor=1/2', text)
        self.assertIn('next=after successful publication', text)
        self.assertNotIn('pbs.twimg.com/media', text)
        self.assertNotIn(directory, text)


if __name__ == '__main__':
    unittest.main()
