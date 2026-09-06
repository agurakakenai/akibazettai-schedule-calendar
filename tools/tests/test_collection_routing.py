"""Offline routing and actual production workflow guard regressions."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
from types import SimpleNamespace
import unittest
from unittest import mock
import uuid


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('collection_routing', ROOT / 'tools' / 'collection-routing.py')
routing = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(routing)
WORKFLOW = (ROOT / '.github' / 'workflows' / 'deploy-pages.yml').read_text(encoding='utf-8')
REPOSITORY = 'agurakakenai/akibazettai-schedule-calendar'


def job_block(name):
    return re.search(r'^  ' + name + r':\n(.*?)(?=^  [a-z]+:|\Z)',
                     WORKFLOW, re.M | re.S).group(1)


def job_condition(name):
    lines = job_block(name).splitlines()
    for index, line in enumerate(lines):
        if line.startswith('    if: '):
            expression = line.removeprefix('    if: ')
            if expression != '>-':
                return expression
            continuation = []
            for part in lines[index + 1:]:
                if not part.startswith('      '):
                    break
                continuation.append(part.strip())
            return ' '.join(continuation)
    raise AssertionError('Missing job guard')


def evaluate(expression, *, event='workflow_dispatch', mode='deploy', ref='refs/heads/main',
             repository=REPOSITORY, collect='skipped', build='success', cancelled=False):
    # Evaluate only the checked-in boolean guard, with no builtins or API calls.
    expression = expression.replace('&&', ' and ').replace('||', ' or ')
    expression = re.sub(r'!(?!=)', 'not ', expression)
    return eval(expression, {'__builtins__': {}}, {
        'github': SimpleNamespace(
            event_name=event, ref=ref, repository=repository,
            event=SimpleNamespace(pull_request=SimpleNamespace(number=32))),
        'inputs': SimpleNamespace(mode=mode),
        'needs': SimpleNamespace(collect=SimpleNamespace(result=collect),
                                 build=SimpleNamespace(result=build)),
        'always': lambda: True, 'cancelled': lambda: cancelled,
        'format': lambda template, value: template.format(value),
    })


def hours(schedule):
    result = set()
    for value in schedule.split()[1].split(','):
        bounds = value.split('-')
        start, end = int(bounds[0]), int(bounds[-1])
        result.update(range(start, end + 1))
    return result


class RoutingTests(unittest.TestCase):
    def test_current_schedule_and_manual_collect_remain_official_only(self):
        self.assertEqual(routing.collection_mode('schedule', 'both', routing.LEGACY_SCHEDULE), 'collect')
        self.assertEqual(routing.collection_mode('workflow_dispatch', 'collect'), 'collect')
        self.assertEqual(routing.collection_mode('workflow_dispatch', 'personal'), 'personal')
        self.assertEqual(routing.collection_mode('workflow_dispatch', 'both'), 'both')

    def test_future_schedule_mapping_is_exact_and_has_20_8_21_slots(self):
        expected = {
            '30 3-6,8-10 * * *': 'both',
            '30 11 * * *': 'collect',
            '30 0-2,7,15-23 * * *': 'personal',
        }
        for schedule, mode in expected.items():
            self.assertEqual(routing.collection_mode('schedule', 'collect', schedule), mode)
        official = set().union(*(hours(s) for s, m in expected.items() if m in ('both', 'collect')))
        personal = set().union(*(hours(s) for s, m in expected.items() if m in ('both', 'personal')))
        self.assertEqual((len(official), len(personal), len(official | personal)), (8, 20, 21))
        self.assertEqual({(hour + 9) % 24 for hour in personal}, set(range(20)))

    def test_unknown_schedules_and_unapproved_events_fail_closed(self):
        for schedule in ('', '30 3-6,8-11 * * * ', '30 3-6,8-11  * * *',
                         '0 * * * *', '30 3-6,8-11 * * *\ncollectionMode=both'):
            with self.subTest(schedule=schedule), self.assertRaisesRegex(
                    ValueError, 'unknown_collection_schedule'):
                routing.collection_mode('schedule', 'collect', schedule)
        for event in ('push', 'pull_request', 'pull_request_target', ''):
            with self.subTest(event=event), self.assertRaises(ValueError):
                routing.collection_mode(event, 'personal', routing.LEGACY_SCHEDULE)
        for mode in ('deploy', 'probe', '', 'collect; echo secret'):
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                routing.collection_mode('workflow_dispatch', mode)

    def test_cli_exports_only_a_validated_fixed_mode(self):
        output = ROOT / ('.routing-output-' + uuid.uuid4().hex)
        self.addCleanup(output.unlink, missing_ok=True)
        with mock.patch.dict(os.environ, {
                'EVENT_NAME': 'schedule', 'EVENT_SCHEDULE': routing.LEGACY_SCHEDULE,
                'REQUESTED_MODE': 'personal', 'GITHUB_OUTPUT': str(output)}, clear=True), \
                contextlib.redirect_stdout(io.StringIO()) as stream:
            self.assertEqual(routing.main(), 0)
        self.assertEqual(json.loads(stream.getvalue()), {'collectionMode': 'collect'})
        self.assertEqual(output.read_text(), 'collectionMode=collect\n')
        with mock.patch.dict(os.environ, {
                'EVENT_NAME': 'schedule', 'EVENT_SCHEDULE': 'private-invalid-value',
                'GITHUB_OUTPUT': str(output)}, clear=True), \
                contextlib.redirect_stdout(io.StringIO()) as stream:
            self.assertEqual(routing.main(), 1)
        self.assertNotIn('private-invalid-value', stream.getvalue())
        self.assertEqual(output.read_text(), 'collectionMode=collect\n')


class ProductionWorkflowTests(unittest.TestCase):
    def test_only_legacy_cron_is_enabled_and_manual_modes_are_explicit(self):
        enabled = re.findall(r'^\s+- cron: "([^"]+)"$', WORKFLOW, re.M)
        self.assertEqual(enabled, [routing.LEGACY_SCHEDULE])
        options = re.search(r'        options:\n(.*?)        default:', WORKFLOW, re.S).group(1)
        self.assertEqual(re.findall(r'          - (\S+)', options),
                         ['deploy', 'collect', 'personal', 'both', 'probe'])
        collect = job_block('collect')
        self.assertIn('EVENT_SCHEDULE: ${{ github.event.schedule }}', collect)
        self.assertIn('run: python tools/collection-routing.py', collect)
        self.assertIn('--mode "${{ steps.route.outputs.collectionMode }}"', collect)
        self.assertNotIn('continue-on-error', collect)

    def test_collection_guard_runs_only_explicit_main_or_scheduled_work(self):
        expression = job_condition('collect')
        for mode in ('collect', 'personal', 'both'):
            self.assertTrue(evaluate(expression, mode=mode))
        self.assertTrue(evaluate(expression, event='schedule', mode=''))
        for overrides in (
                {'mode': 'deploy'}, {'mode': 'probe'}, {'mode': 'unknown'},
                {'event': 'push', 'mode': 'personal'},
                {'event': 'pull_request', 'mode': 'both'},
                {'event': 'pull_request_target', 'mode': 'both'},
                {'mode': 'both', 'ref': 'refs/heads/feature'},
                {'mode': 'both', 'repository': 'someone/fork'}):
            with self.subTest(overrides=overrides):
                self.assertFalse(evaluate(expression, **overrides))

    def test_build_guard_cannot_deploy_after_infra_lease_or_routing_failure(self):
        expression = job_condition('build')
        for mode in ('deploy', 'collect', 'personal', 'both'):
            self.assertTrue(evaluate(expression, mode=mode, collect='success'))
        self.assertTrue(evaluate(expression, event='push', mode='', collect='skipped'))
        self.assertTrue(evaluate(expression, event='schedule', mode='', collect='success'))
        for result in ('failure', 'cancelled'):
            self.assertFalse(evaluate(expression, mode='both', collect=result))
        for overrides in (
                {'mode': 'probe'}, {'mode': 'unknown'}, {'cancelled': True},
                {'event': 'pull_request'}, {'event': 'pull_request_target'},
                {'ref': 'refs/heads/feature'}, {'repository': 'someone/fork'}):
            with self.subTest(overrides=overrides):
                self.assertFalse(evaluate(expression, **overrides))

    def test_deploy_requires_successful_same_run_build_not_collection_alone(self):
        expression = job_condition('deploy')
        self.assertTrue(evaluate(expression, mode='both', collect='success', build='success'))
        for result in ('skipped', 'failure', 'cancelled'):
            self.assertFalse(evaluate(expression, build=result))
        for overrides in (
                {'mode': 'probe'}, {'cancelled': True}, {'event': 'pull_request'},
                {'ref': 'refs/heads/feature'}, {'repository': 'someone/fork'}):
            with self.subTest(overrides=overrides):
                self.assertFalse(evaluate(expression, **overrides))
        self.assertIn('needs: build', job_block('deploy'))
        self.assertIn('actions/deploy-pages@v4', job_block('deploy'))
        self.assertIn('actions/upload-pages-artifact@v3', job_block('build'))
        self.assertIn('--mode restore --output data/observed-shifts.json', job_block('build'))
        self.assertNotIn('_recovery-state', job_block('build'))

    def test_all_production_jobs_share_pages_and_pr_pending_is_separate(self):
        expression = re.search(r'^  group: \$\{\{ (.*) \}\}$', WORKFLOW, re.M).group(1)
        for event in ('push', 'schedule', 'workflow_dispatch'):
            for mode in ('deploy', 'collect', 'personal', 'both', 'probe'):
                self.assertEqual(evaluate(expression, event=event, mode=mode), 'pages')
        self.assertEqual(evaluate(expression, event='pull_request'), 'pages-pr-32')
        self.assertIn('  cancel-in-progress: false', WORKFLOW)
        for name in ('collect', 'build'):
            self.assertIn('          ref: main', job_block(name))
            self.assertIn('          persist-credentials: false', job_block(name))
        self.assertNotIn('ref: main', job_block('validate'))
        self.assertNotIn('ref: main', job_block('probe'))


if __name__ == '__main__':
    unittest.main()
