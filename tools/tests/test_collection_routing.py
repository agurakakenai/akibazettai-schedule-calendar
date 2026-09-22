"""Offline routing and actual production workflow guard regressions."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
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
    return re.search(r'^  ' + name + r':\n(.*?)(?=^  [a-z][a-z-]*:|\Z)',
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
             repository=REPOSITORY, collect='skipped', build='success', deploy='success',
             continuation='true', cancelled=False):
    # Evaluate only the checked-in boolean guard, with no builtins or API calls.
    expression = expression.replace('&&', ' and ').replace('||', ' or ')
    expression = re.sub(r'!(?!=)', 'not ', expression)
    return eval(expression, {'__builtins__': {}}, {
        'github': SimpleNamespace(
            event_name=event, ref=ref, repository=repository,
            event=SimpleNamespace(pull_request=SimpleNamespace(number=32))),
        'inputs': SimpleNamespace(mode=mode),
        'needs': SimpleNamespace(collect=SimpleNamespace(
            result=collect, outputs=SimpleNamespace(continuation_ready=continuation)),
            build=SimpleNamespace(result=build, outputs=SimpleNamespace(continuation_ready=continuation)),
            deploy=SimpleNamespace(result=deploy)),
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
    def test_cache_is_only_requested_for_an_active_half_month_route(self):
        for event, mode, schedule, created, expected_mode, cache in [
            ('workflow_dispatch', 'both', '', '2026-09-30T15:29:59Z', 'both', 'false'),
            ('workflow_dispatch', 'both', '', '2026-09-30T15:30:00Z', 'both', 'true'),
            ('workflow_dispatch', 'schedule', '', '2026-09-30T15:29:59Z', 'restore', 'false'),
            ('workflow_dispatch', 'personal', '', '2026-10-01T00:00:00Z', 'personal', 'false'),
            ('schedule', '', routing.LEGACY_SCHEDULE, '2026-10-01T03:35:00Z', 'collect', 'false'),
            ('schedule', '', '30 15 * * *', '2026-09-30T15:35:00Z', 'schedule', 'true'),
            ('schedule', '', '30 21 12,28-31 * *', '2026-10-30T21:35:00Z', 'restore', 'false'),
        ]:
            with self.subTest(mode=mode, created=created), mock.patch.dict(os.environ, {
                    'EVENT_NAME': event, 'REQUESTED_MODE': mode, 'EVENT_SCHEDULE': schedule,
                    'RUN_CREATED_AT': created, 'HALF_MONTH_SCHEDULE_ENABLED': 'true'},
                    clear=True), contextlib.redirect_stdout(io.StringIO()) as stream:
                self.assertEqual(routing.main(), 0)
                result = json.loads(stream.getvalue())
                self.assertEqual(result['collectionMode'], expected_mode)
                self.assertEqual(result['halfMonthCacheRequired'], cache)

    def test_current_schedule_and_manual_collect_remain_official_only(self):
        self.assertEqual(routing.collection_mode('schedule', 'both', routing.LEGACY_SCHEDULE), 'collect')
        self.assertEqual(routing.collection_mode('workflow_dispatch', 'collect'), 'collect')
        self.assertEqual(routing.collection_mode('workflow_dispatch', 'personal'), 'personal')
        self.assertEqual(routing.collection_mode('workflow_dispatch', 'both'), 'both')

    def test_activation_uses_same_eight_frames_without_clock_routing(self):
        self.assertEqual(routing.collection_mode(
            'schedule', 'collect', routing.LEGACY_SCHEDULE, enabled=True), 'collect')
        self.assertEqual({(hour + 9) % 24 for hour in hours(routing.LEGACY_SCHEDULE)},
                         {12, 13, 14, 15, 17, 18, 19, 20})
        for schedule in ('30 3-6,8-10 * * *', '30 11 * * *', '30 0-2,7,15-23 * * *'):
            with self.assertRaisesRegex(ValueError, 'unknown_collection_schedule'):
                routing.collection_mode('schedule', schedule=schedule, enabled=True)
        self.assertEqual(routing.collection_mode('workflow_dispatch', 'apply-saved'), 'apply-saved')
        with self.assertRaises(ValueError):
            routing.collection_mode('workflow_dispatch', 'daily-guidance', enabled=True)

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
                'RUN_CREATED_AT': '2026-09-23T03:33:00Z',
                'REQUESTED_MODE': 'personal', 'GITHUB_OUTPUT': str(output)}, clear=True), \
                contextlib.redirect_stdout(io.StringIO()) as stream:
            self.assertEqual(routing.main(), 0)
        result = json.loads(stream.getvalue())
        self.assertEqual(result['collectionMode'], 'collect')
        self.assertEqual(result['collectionKind'], 'official')
        self.assertEqual(result['collectionDate'], '2026-09-23')
        self.assertEqual(result['collectionSlot'], '2026-09-23T03:30:00Z')
        self.assertEqual(result['collectionSlotId'], 'official:2026-09-23T12:30+09:00')
        saved_output = output.read_text()
        with mock.patch.dict(os.environ, {
                'EVENT_NAME': 'schedule', 'EVENT_SCHEDULE': 'private-invalid-value',
                'GITHUB_OUTPUT': str(output)}, clear=True), \
                contextlib.redirect_stdout(io.StringIO()) as stream:
            self.assertEqual(routing.main(), 1)
        self.assertNotIn('private-invalid-value', stream.getvalue())
        self.assertEqual(output.read_text(), saved_output)


class ProductionWorkflowTests(unittest.TestCase):
    def test_private_cache_is_encrypted_scoped_and_preserved_on_failure(self):
        collect = job_block('collect')
        self.assertIn('steps.route.outputs.halfMonthCacheRequired', collect)
        self.assertIn('--cache-dir "$RUNNER_TEMP/_private-evidence"', collect)
        self.assertIn('path: ${{ runner.temp }}/_private-evidence/cache.bin', collect)
        self.assertIn('retention-days: 7', collect)
        self.assertIn("if: always() && steps.evidence.outputs.exists == 'true'", collect)
        self.assertIn("steps.route.outputs.collectionMode == 'schedule'", collect)
        for name in ('build', 'deploy', 'continue-collection', 'validate', 'probe'):
            self.assertNotIn('SCHEDULE_EVIDENCE_KEY', job_block(name))
        self.assertNotIn('path: ${{ runner.temp }}/_private-evidence\n', collect)

    def test_dispatch_script_preserves_queued_runs_and_reports_api_failure(self):
        if os.name == 'nt':
            git = shutil.which('git')
            bash = Path(git).parent.parent / 'bin' / 'bash.exe' if git else None
            if bash is None or not bash.is_file():
                self.skipTest('Git Bash is unavailable')
        else:
            bash = shutil.which('bash')
            if not bash:
                self.skipTest('Bash is unavailable')
        block = job_block('continue-collection')
        raw = block.split('        run: |\n', 1)[1]
        script = '\n'.join(line[10:] for line in raw.splitlines() if line.startswith('          '))
        mock_gh = '''
gh() {
  case "$*" in
    *"--method POST"*) echo dispatched >> "$DISPATCH_LOG"; return "$POST_CODE";;
    *"status=queued"*) printf '%s\\n' "$QUEUED"; return "$GET_CODE";;
    *) echo 0; return "$GET_CODE";;
  esac
}
'''
        for queued, get_code, post_code, expected_calls, success in [
                ('0', '0', '0', 1, True), ('1', '0', '0', 0, True),
                ('0', '1', '0', 0, False), ('0', '0', '1', 1, False)]:
            with self.subTest(queued=queued, get=get_code, post=post_code), \
                    tempfile.TemporaryDirectory() as folder:
                result = subprocess.run(
                    [str(bash), '--noprofile', '--norc', '-e', '-o', 'pipefail', '-s'],
                    input=mock_gh + script, text=True, capture_output=True, cwd=folder, timeout=15,
                    env={**os.environ, 'CHECKPOINT_SHA': 'a' * 40, 'CONTINUATION_MODE': 'personal',
                         'GITHUB_REPOSITORY': REPOSITORY, 'GITHUB_RUN_ID': '12345',
                         'GITHUB_STEP_SUMMARY': 'summary.txt', 'DISPATCH_LOG': 'dispatch.txt',
                         'QUEUED': queued, 'GET_CODE': get_code, 'POST_CODE': post_code,
                         'BASH_ENV': ''},
                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                self.assertEqual(result.returncode == 0, success, result.stderr)
                log = Path(folder) / 'dispatch.txt'
                calls = log.read_text().splitlines() if log.exists() else []
                self.assertEqual(len(calls), expected_calls)
                if queued == '1':
                    self.assertIn('checkpoint ' + 'a' * 40 + ' is retained',
                                  (Path(folder) / 'summary.txt').read_text())

    def test_azure_backend_and_credentials_exclude_saved_restore_and_build(self):
        collect = job_block('collect')
        for name in ('PERSONAL_ANALYSIS_BACKEND', 'AZURE_OPENAI_API_KEY',
                     'AZURE_OPENAI_ENDPOINT', 'AZURE_OPENAI_DEPLOYMENT'):
            line = re.search(r'^          ' + name + r': (.+)$', collect, re.M).group(1)
            self.assertIn("steps.route.outputs.collectionMode == 'personal'", line)
            self.assertIn("steps.route.outputs.collectionMode == 'both'", line)
            self.assertEqual(WORKFLOW.count(name + ':'), 1)
        self.assertIn("&& 'azure' || 'rules'", collect)
        self.assertIn("steps.route.outputs.collectionMode == 'daily-guidance'", collect)
        self.assertIn("vars.DAILY_GUIDANCE_ENABLED == 'true'", collect)
        azure_lines = [line for line in collect.splitlines() if 'AZURE_OPENAI_' in line]
        self.assertTrue(all("apply-saved" not in line for line in azure_lines))
        for job in ('validate', 'probe', 'build', 'deploy'):
            self.assertNotIn('AZURE_OPENAI_', job_block(job))
            self.assertNotIn('PERSONAL_ANALYSIS_BACKEND', job_block(job))

    def test_personal_slots_are_independent_of_unchanged_official_cron(self):
        enabled = re.findall(r'^\s+- cron: "([^"]+)"$', WORKFLOW, re.M)
        self.assertEqual(enabled, [routing.LEGACY_SCHEDULE, *routing.slots.PERSONAL_SCHEDULES,
                                   *routing.slots.HALF_MONTH_SCHEDULES])
        options = re.search(r'        options:\n(.*?)        default:', WORKFLOW, re.S).group(1)
        self.assertEqual(re.findall(r'          - (\S+)', options),
                         ['deploy', 'collect', 'personal', 'both', 'schedule', 'apply-saved', 'cost-sync', 'probe'])
        collect = job_block('collect')
        self.assertIn('EVENT_SCHEDULE: ${{ github.event.schedule }}', collect)
        self.assertIn('          python tools/collection-routing.py', collect)
        self.assertIn('actions/runs/$GITHUB_RUN_ID', collect)
        self.assertIn('echo "RUN_CREATED_AT=$RUN_CREATED_AT" >> "$GITHUB_ENV"', collect)
        for key in ('KIND', 'SLOT', 'SLOT_ID', 'DATE'):
            self.assertIn('COLLECTION_' + key + ': ${{ steps.route.outputs.', collect)
        self.assertIn('--mode "$COLLECTION_MODE"', collect)
        self.assertIn("DAILY_GUIDANCE_ENABLED: ${{ vars.DAILY_GUIDANCE_ENABLED || 'false' }}", collect)
        self.assertIn('APPLY_SAVED_MANIFEST: ${{ inputs.saved_manifest }}', collect)
        self.assertNotRegex(WORKFLOW, r'run:.*\$\{\{ inputs\.saved_manifest')
        self.assertEqual(collect.count('continue-on-error'), 1)
        login, collection = collect.split('      - name: Collect with durable state', 1)
        self.assertIn('id: azure-cost-login', login)
        self.assertIn('continue-on-error: true', login)
        self.assertNotIn('continue-on-error', collection)
        for variable in ('CLIENT_ID', 'TENANT_ID', 'SUBSCRIPTION_ID'):
            self.assertIn('AZURE_COST_' + variable + ': ${{ vars.AZURE_COST_' + variable + ' }}', collection)
        self.assertNotIn('steps.azure-cost-login.outcome', collection)

    def test_collection_guard_runs_only_explicit_main_or_scheduled_work(self):
        expression = job_condition('collect')
        for mode in ('collect', 'personal', 'both', 'schedule', 'apply-saved', 'cost-sync'):
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
        for mode in ('deploy', 'collect', 'personal', 'both', 'apply-saved'):
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

    def test_continuation_requires_saved_data_and_successful_publication(self):
        condition = job_condition('continue-collection')
        self.assertTrue(evaluate(condition, collect='success', deploy='success'))
        self.assertTrue(evaluate(condition, collect='skipped', build='success', deploy='success'),
                        "a code-only deployment can resume the checkpoint it actually published")
        for change in (
                {'collect': 'failure'}, {'collect': 'cancelled'}, {'deploy': 'failure'},
                {'build': 'failure'}, {'build': 'skipped'}, {'build': 'cancelled'},
                {'deploy': 'cancelled'}, {'deploy': 'skipped'}, {'continuation': 'false'},
                {'continuation': ''}, {'cancelled': True}, {'ref': 'refs/heads/feature'},
                {'repository': 'someone/fork'}):
            args = {'collect': 'success', 'deploy': 'success', **change}
            with self.subTest(change=change):
                self.assertFalse(evaluate(condition, **args))
        block = job_block('continue-collection')
        self.assertIn('needs: [collect, build, deploy]', block)
        self.assertIn('needs.build.outputs.state_commit', block)
        self.assertIn('steps.restore.outputs.stateCommit', job_block('build'))
        self.assertNotIn('needs.collect.outputs.state_commit', block)
        self.assertLess(block.index('for STATUS in queued pending waiting'),
                        block.index('gh api --method POST'))
        self.assertIn('exit 0', block)
        self.assertEqual(block.count('gh api --method POST'), 1)
        self.assertIn('actions: write', block)
        self.assertNotIn('actions: write', job_block('collect'))
        self.assertIn('^[0-9a-f]{40}$', block)
        self.assertNotIn('continue-on-error', block)
        self.assertNotIn('AZURE_OPENAI_', block)
        self.assertNotIn('SCHEDULE_EVIDENCE_KEY', block)


if __name__ == '__main__':
    unittest.main()
