import datetime as dt
import importlib.util
from pathlib import Path
import unittest
from unittest import mock


SPEC = importlib.util.spec_from_file_location(
    'cache_artifact', Path(__file__).resolve().parents[1] / 'schedule-cache-artifact.py')
artifact = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(artifact)
NOW = dt.datetime(2026, 10, 2, tzinfo=dt.timezone.utc)


def candidate(number, **changes):
    return {'id': number, 'name': artifact.ARTIFACT_NAME,
            'created_at': '2026-10-01T22:00:00Z', 'expired': False, 'size_in_bytes': 1024,
            'workflow_run': {'id': number, 'head_branch': 'main'}, **changes}


def run(**changes):
    return {'head_repository': {'full_name': artifact.REPOSITORY},
            'head_branch': 'main', 'path': artifact.WORKFLOW,
            'event': 'schedule', 'status': 'completed', **changes}


class CacheArtifactTests(unittest.TestCase):
    def test_only_trusted_main_workflow_artifact_is_selected(self):
        query = mock.Mock(side_effect=[
            {'artifacts': [candidate(1), candidate(2), candidate(3)]},
            run(event='pull_request'), run(head_repository={'full_name': 'fork/repo'}), run()])
        self.assertEqual(artifact.find_archive(request=query, now=NOW), 1)
        self.assertEqual(len(query.call_args_list), 4)

    def test_expired_wrong_name_branch_or_oversized_entries_are_not_downloaded(self):
        query = mock.Mock(return_value={'artifacts': [
            candidate(1, expired=True), candidate(2, name='collector-recovery-2'),
            candidate(3, workflow_run={'id': 3, 'head_branch': 'feature'}),
            candidate(4, size_in_bytes=artifact.MAX_BYTES + 1),
            candidate(5, created_at='2026-09-20T00:00:00Z')]})
        self.assertIsNone(artifact.find_archive(request=query, now=NOW))
        self.assertEqual(query.call_count, 1)

    def test_failed_collection_artifact_can_preserve_issued_request_cache(self):
        query = mock.Mock(side_effect=[{'artifacts': [candidate(1)]}, run(conclusion='failure')])
        self.assertEqual(artifact.find_archive(request=query, now=NOW), 1)

    def test_in_progress_or_different_workflow_is_not_used(self):
        query = mock.Mock(side_effect=[{'artifacts': [candidate(1), candidate(2)]},
                                      run(status='in_progress'), run(path='.github/workflows/other.yml')])
        self.assertIsNone(artifact.find_archive(request=query, now=NOW))

    def test_named_pagination_does_not_scan_unrelated_publication_artifacts(self):
        query = mock.Mock(side_effect=[
            {'total_count': 101, 'artifacts': []},
            {'artifacts': [candidate(1)]}, run()])
        self.assertEqual(artifact.find_archive(request=query, now=NOW), 1)
        self.assertIn(f'name={artifact.ARTIFACT_NAME}', query.call_args_list[0].args[0])
        self.assertTrue(query.call_args_list[1].args[0].endswith('&page=2'))

    def test_excessive_or_invalid_listing_is_explicit_not_an_empty_cache(self):
        for total in (1001, -1, True):
            with self.subTest(total=total):
                query = mock.Mock(return_value={'total_count': total, 'artifacts': []})
                with self.assertRaisesRegex(ValueError, 'evidence_artifact_listing_limit'):
                    artifact.find_archive(request=query, now=NOW)
                self.assertEqual(query.call_count, 1)

    def test_github_download_subprocess_does_not_inherit_model_or_cache_secrets(self):
        result = mock.Mock(returncode=0, stdout=b'{}')
        with mock.patch.dict(artifact.os.environ, {
                'AZURE_API_KEY': 'private-model-key', 'SCHEDULE_EVIDENCE_KEY': 'private-cache-key',
                'GH_TOKEN': 'github-only-token'}), \
                mock.patch.object(artifact.subprocess, 'run', return_value=result) as execute:
            self.assertEqual(artifact.api('repos/test/test'), {})
        environment = execute.call_args.kwargs['env']
        self.assertFalse(any(key.startswith(('AZURE_', 'SCHEDULE_EVIDENCE_')) for key in environment))
        self.assertEqual(environment['GH_TOKEN'], 'github-only-token')


if __name__ == '__main__':
    unittest.main()
