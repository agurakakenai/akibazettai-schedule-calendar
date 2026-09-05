"""Offline tests: python -m unittest discover -s tools/tests -p test_collector_schedule.py."""
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import unittest
from unittest import mock
import uuid
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / 'tools' / 'setup-collector-schedule.ps1'
LAUNCHER = ROOT / 'tools' / 'collector-launcher.pyw'
LOADER = importlib.machinery.SourceFileLoader('collector_launcher_test', str(LAUNCHER))
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)
launcher = importlib.util.module_from_spec(SPEC)
LOADER.exec_module(launcher)
PROBE_SPEC = importlib.util.spec_from_file_location(
    'collector_schedule_verifier', ROOT / 'tools' / 'verify-collector-schedule.py')
verifier = importlib.util.module_from_spec(PROBE_SPEC)
PROBE_SPEC.loader.exec_module(verifier)


def ps_quote(value):
    return "'" + str(value).replace("'", "''") + "'"


class ScratchTest(unittest.TestCase):
    def setUp(self):
        self.scratch = ROOT / 'tools' / 'tests' / ('.schedule-test-' + uuid.uuid4().hex)
        self.scratch.mkdir()

    def tearDown(self):
        shutil.rmtree(self.scratch)


class VerificationTests(unittest.TestCase):
    def test_status_read_retries_transient_windows_sharing_violation(self):
        with mock.patch.object(Path, 'read_text', side_effect=[
                PermissionError('atomic replacement'), '{"status":"finished"}']):
            self.assertIsNone(verifier.read_json('launcher-status.json'))
            self.assertEqual(verifier.read_json('launcher-status.json'),
                             {'status': 'finished'})

    def test_exit_between_process_and_image_queries_is_not_a_failure(self):
        probe = verifier.DesktopProbe.__new__(verifier.DesktopProbe)
        probe.kernel32 = mock.Mock()
        probe.kernel32.OpenProcess.return_value = 42
        codes = iter((259, 0))

        def get_code(handle, result):
            result._obj.value = next(codes)
            return True

        probe.kernel32.GetExitCodeProcess.side_effect = get_code
        probe.kernel32.QueryFullProcessImageNameW.return_value = False
        self.assertEqual(probe.process(123),
                         {'pid': 123, 'image': None, 'active': False, 'exitCode': 0})
        probe.kernel32.CloseHandle.assert_called_once_with(42)

    def test_already_exited_process_never_queries_unavailable_image(self):
        probe = verifier.DesktopProbe.__new__(verifier.DesktopProbe)
        probe.kernel32 = mock.Mock()
        probe.kernel32.OpenProcess.return_value = 42

        def get_code(handle, result):
            result._obj.value = 3
            return True

        probe.kernel32.GetExitCodeProcess.side_effect = get_code
        self.assertEqual(probe.process(123)['exitCode'], 3)
        probe.kernel32.QueryFullProcessImageNameW.assert_not_called()


class LauncherTests(ScratchTest):
    def setUp(self):
        super().setUp()
        self.base = self.scratch / 'persistent'
        self.base.mkdir()
        self.repo = self.scratch / 'worktree'
        (self.repo / 'tools').mkdir(parents=True)
        (self.repo / 'data').mkdir()
        self.config = {
            'owner': launcher.OWNER,
            'schemaVersion': 1,
            'installationId': str(uuid.uuid4()),
            'userSid': 'S-1-5-21-1001',
            'dataRoot': str(self.base),
            'repoRoot': str(self.repo),
            'publishPath': str(self.repo / 'data' / 'observed-shifts.json'),
        }
        for key, name in (
                ('snapshotPath', 'observed-shifts.json'),
                ('reportPath', 'last-run.json'),
                ('statusPath', 'launcher-status.json'),
                ('logPath', 'collector.log')):
            self.config[key] = str(self.base / name)
        self.config_path = self.base / launcher.CONFIG_NAME

    def write_config(self):
        self.config_path.write_text(json.dumps(self.config), encoding='utf-8')

    def source(self, text):
        (self.repo / 'tools' / 'collect-shifts.py').write_text(text, encoding='utf-8')
        self.write_config()

    def status(self):
        return json.loads(Path(self.config['statusPath']).read_text(encoding='utf-8'))

    def test_preserves_all_collector_exit_codes_and_same_process(self):
        for code in (0, 2, 3, 4):
            with self.subTest(code=code):
                self.source(
                    'import os, sys\n'
                    'def main(args):\n'
                    '    print("fact-only stdout")\n'
                    '    print("fact-only stderr", file=sys.stderr)\n'
                    f'    return {code}\n')
                # Avoid timestamp-granularity bytecode caching across fixture rewrites.
                shutil.rmtree(self.repo / 'tools' / '__pycache__', ignore_errors=True)
                self.assertEqual(launcher.run(self.config_path), code)
                self.assertEqual(self.status()['exitCode'], code)
                self.assertEqual(self.status()['pid'], os.getpid())
        log = Path(self.config['logPath']).read_text(encoding='utf-8')
        self.assertIn('fact-only stdout', log)
        self.assertIn('fact-only stderr', log)
        self.assertEqual(log.count('"event": "launcher-finish"'), 4)

    def test_arguments_and_persistent_report(self):
        self.source(
            'import json\nfrom pathlib import Path\n'
            'def main(args):\n'
            '    assert args[:5] == ["--once", "--days", "2", "--max-posts", "20"]\n'
            '    options = dict(zip(args[5::2], args[6::2]))\n'
            '    Path(options["--snapshot"]).write_text(\'{"posts":[]}\\n\')\n'
            '    Path(options["--publish"]).write_text(\'{"posts":[]}\\n\')\n'
            '    Path(options["--report"]).write_text(json.dumps({"status":"ok"}))\n'
            '    return 0\n')
        self.assertEqual(launcher.run(self.config_path), 0)
        self.assertTrue(Path(self.config['snapshotPath']).is_file())
        self.assertTrue(Path(self.config['publishPath']).is_file())
        self.assertEqual(json.loads(Path(self.config['reportPath']).read_text()),
                         {'status': 'ok'})

    def test_exception_is_failure_without_exception_message(self):
        self.source('def main(args):\n    raise RuntimeError("sensitive message")\n')
        self.assertEqual(launcher.run(self.config_path), 4)
        self.assertEqual(self.status()['errorType'], 'RuntimeError')
        self.assertNotIn('sensitive message',
                         Path(self.config['logPath']).read_text(encoding='utf-8'))

    def test_system_exit_and_import_failure_are_not_success(self):
        self.source('def main(args):\n    raise SystemExit(3)\n')
        self.assertEqual(launcher.run(self.config_path), 3)
        self.source('raise ImportError("fixture")\n')
        self.assertEqual(launcher.run(self.config_path), 4)
        self.assertEqual(self.status()['errorType'], 'ImportError')

    def test_missing_worktree_retains_snapshot_and_fails(self):
        self.write_config()
        snapshot = Path(self.config['snapshotPath'])
        snapshot.write_text('{"preserved": true}', encoding='utf-8')
        shutil.rmtree(self.repo)
        self.assertEqual(launcher.run(self.config_path), 4)
        self.assertEqual(snapshot.read_text(), '{"preserved": true}')
        self.assertEqual(self.status()['errorType'], 'FileNotFoundError')

    def test_unowned_or_redirected_config_writes_nothing(self):
        for key, value in (('owner', 'another-owner'),
                           ('logPath', str(self.scratch / 'unowned.log'))):
            with self.subTest(key=key):
                original = self.config[key]
                self.config[key] = value
                self.write_config()
                self.assertEqual(launcher.run(self.config_path), 4)
                self.assertFalse(Path(self.config['statusPath']).exists())
                self.assertFalse(Path(self.config['logPath']).exists())
                self.config[key] = original

    def test_relative_root_is_rejected_and_cwd_is_restored(self):
        previous = Path.cwd()
        self.source('def main(args):\n    return 0\n')
        self.assertEqual(launcher.run(self.config_path), 0)
        self.assertEqual(Path.cwd(), previous)
        self.config['repoRoot'] = 'relative'
        self.write_config()
        self.assertEqual(launcher.run(self.config_path), 4)

    def test_invalid_config_and_return_code_are_failures(self):
        self.config_path.write_text('{invalid', encoding='utf-8')
        self.assertEqual(launcher.run(self.config_path), 4)
        self.source('def main(args):\n    return "not-success"\n')
        self.assertEqual(launcher.run(self.config_path), 4)

    @unittest.skipUnless(os.name == 'nt', 'Windows console-less Python executable')
    def test_pythonw_process_returns_real_exit_codes(self):
        pythonw = Path(sys.executable).with_name('pythonw.exe')
        for code in (0, 2, 3, 4):
            with self.subTest(code=code):
                self.source(f'def main(args):\n    return {code}\n')
                shutil.rmtree(self.repo / 'tools' / '__pycache__', ignore_errors=True)
                process = subprocess.run(
                    [str(pythonw), str(LAUNCHER), '--config', str(self.config_path)],
                    creationflags=subprocess.CREATE_NO_WINDOW, timeout=15)
                self.assertEqual(process.returncode, code)
                self.assertEqual(self.status()['exitCode'], code)
                self.assertNotEqual(self.status()['pid'], os.getpid())
                self.assertTrue(self.status()['executable'].endswith('pythonw.exe'))

    @unittest.skipUnless(os.name == 'nt', 'Windows console-less lifecycle monitoring')
    def test_offline_pythonw_run_can_be_monitored_to_completion(self):
        self.source('import time\ndef main(args):\n    time.sleep(0.3)\n    return 3\n')
        owned = {os.getpid()}
        probe = verifier.DesktopProbe(owned)
        active = None
        pythonw = Path(sys.executable).with_name('pythonw.exe')
        with subprocess.Popen(
                [str(pythonw), str(LAUNCHER), '--config', str(self.config_path)],
                creationflags=subprocess.CREATE_NO_WINDOW) as process:
            owned.add(process.pid)
            deadline = time.monotonic() + 10
            while process.poll() is None and time.monotonic() < deadline:
                probe.sample()
                status = verifier.read_json(self.config['statusPath'])
                if status:
                    observed = probe.process(process.pid)
                    if observed and observed['active']:
                        active = observed
                time.sleep(0.01)
            self.assertEqual(process.wait(timeout=2), 3)
        self.assertIsNotNone(active)
        self.assertTrue(active['image'].endswith('pythonw.exe'))
        self.assertEqual(verifier.read_json(self.config['statusPath'])['exitCode'], 3)
        self.assertEqual(probe.evidence(owned)['ownedVisibleWindows'], [])


@unittest.skipUnless(os.name == 'nt', 'Windows PowerShell offline scheduler fixtures')
class SetupTests(ScratchTest):
    def powershell(self, body):
        import base64
        script = f"""
[Console]::OutputEncoding = New-Object Text.UTF8Encoding($false)
$ProgressPreference = 'SilentlyContinue'
$ErrorActionPreference = 'Stop'
. {ps_quote(SCRIPT)} -Action Functions
$env:LOCALAPPDATA = {ps_quote(self.scratch)}
$fixtureRoot = Join-Path $env:LOCALAPPDATA 'repo'
[void][IO.Directory]::CreateDirectory((Join-Path $fixtureRoot 'tools'))
[IO.File]::WriteAllText((Join-Path $fixtureRoot 'tools\\collect-shifts.py'), '--once --snapshot --publish')
$fixturePythonw = Join-Path $env:LOCALAPPDATA 'pythonw.exe'
[IO.File]::WriteAllText($fixturePythonw, '')
$script:FixtureTask = $null
$script:FixtureXml = $null
$script:Calls = New-Object 'System.Collections.Generic.List[string]'
$script:OriginalGetCollectorTask = ${{function:Get-CollectorTask}}
function Get-CollectorIdentity {{
    [pscustomobject]@{{ UserSid = 'S-1-5-21-1001'; UserName = 'fixture' }}
}}
function Get-CollectorTask {{ param($Paths) return $script:FixtureTask }}
function Register-ScheduledTask {{
    param($TaskName, $TaskPath, $Xml, [switch]$Force)
    $script:Calls.Add('Install')
    $script:FixtureXml = $Xml
    $parsed = [xml]$Xml
    $script:FixtureTask = [pscustomobject]@{{
        TaskName = $TaskName; TaskPath = $TaskPath; State = 'Ready'
        Description = $parsed.Task.RegistrationInfo.Description
        Principal = [pscustomobject]@{{
            UserId = $parsed.Task.Principals.Principal.UserId
            LogonType = 'Interactive'; RunLevel = 'Limited'
        }}
        Actions = @([pscustomobject]@{{
            Execute = $parsed.Task.Actions.Exec.Command
            Arguments = $parsed.Task.Actions.Exec.Arguments
            WorkingDirectory = $parsed.Task.Actions.Exec.WorkingDirectory
        }})
    }}
}}
function Get-ScheduledTaskInfo {{
    param($TaskName, $TaskPath)
    [pscustomobject]@{{ NextRunTime = [datetime]::Now; LastRunTime = [datetime]::Now; LastTaskResult = 0 }}
}}
function Export-ScheduledTask {{ param($TaskName, $TaskPath) return $script:FixtureXml }}
function Unregister-ScheduledTask {{
    param($TaskName, $TaskPath, $Confirm)
    $script:Calls.Add('Uninstall'); $script:FixtureTask = $null
}}
function Start-ScheduledTask {{ param($TaskName, $TaskPath) $script:Calls.Add('Run') }}
function Stop-ScheduledTask {{ param($TaskName, $TaskPath) $script:Calls.Add('Stop') }}
{body}
"""
        encoded = base64.b64encode(script.encode('utf-16-le')).decode('ascii')
        process = subprocess.run(
            ['powershell.exe', '-NoProfile', '-NonInteractive', '-WindowStyle',
             'Hidden', '-EncodedCommand', encoded],
            capture_output=True, text=True, encoding='utf-8', errors='replace',
            creationflags=subprocess.CREATE_NO_WINDOW, timeout=45)
        self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
        self.assertNotIn('S="Error"', process.stderr, process.stderr)
        return json.loads(process.stdout)

    def test_xml_has_fixed_jst_triggers_and_limited_interactive_action(self):
        result = self.powershell("""
$result = Invoke-CollectorSchedule Plan $fixtureRoot $fixturePythonw TwiceDaily
[pscustomobject]@{ Xml = $result.TaskXml; Calls = @($script:Calls) } | ConvertTo-Json -Depth 5
""")
        self.assertEqual(result['Calls'], [])
        root = ET.fromstring(result['Xml'])
        ns = {'t': 'http://schemas.microsoft.com/windows/2004/02/mit/task'}
        triggers = root.findall('t:Triggers/t:CalendarTrigger', ns)
        self.assertEqual(len(triggers), 2)
        self.assertTrue(triggers[0].find('t:StartBoundary', ns).text.endswith('T13:30:00+09:00'))
        self.assertTrue(triggers[1].find('t:StartBoundary', ns).text.endswith('T19:30:00+09:00'))
        for trigger in triggers:
            self.assertEqual(trigger.find('t:ScheduleByDay/t:DaysInterval', ns).text, '1')
        for path, expected in (
                ('Principals/Principal/LogonType', 'InteractiveToken'),
                ('Principals/Principal/RunLevel', 'LeastPrivilege'),
                ('Settings/MultipleInstancesPolicy', 'IgnoreNew'),
                ('Settings/StartWhenAvailable', 'true'),
                ('Settings/WakeToRun', 'false'),
                ('Settings/RunOnlyIfNetworkAvailable', 'true'),
                ('Settings/ExecutionTimeLimit', 'PT10M')):
            self.assertEqual(root.find('/'.join('t:' + p for p in path.split('/')), ns).text,
                             expected)
        self.assertTrue(root.find('t:Actions/t:Exec/t:Command', ns).text.endswith('pythonw.exe'))
        self.assertNotIn('Password', result['Xml'])
        self.assertNotIn('powershell.exe', result['Xml'])

    def test_hourly_has_one_indefinite_trigger_aligned_to_next_jst_hour(self):
        result = self.powershell("""
$paths = Get-CollectorPaths 'S-1-5-21-1001'
$config = New-CollectorConfig $paths (Get-CollectorIdentity) $fixtureRoot $fixturePythonw $null Hourly
$xml = New-CollectorTaskXml $config $paths ([datetimeoffset]'2026-09-05T19:59:58Z')
$rollover = New-CollectorTaskXml $config $paths ([datetimeoffset]'2026-09-06T23:45:00+09:00')
[pscustomobject]@{ Xml = $xml; Rollover = $rollover; Config = $config; Calls = @($script:Calls) } | ConvertTo-Json -Depth 5
""")
        ns = {'t': 'http://schemas.microsoft.com/windows/2004/02/mit/task'}
        xml = ET.fromstring(result['Xml'])
        triggers = xml.find('t:Triggers', ns)
        self.assertEqual(len(triggers), 1)
        trigger = triggers.find('t:TimeTrigger', ns)
        self.assertEqual(trigger.find('t:StartBoundary', ns).text,
                         '2026-09-06T05:00:00+09:00')
        self.assertEqual(trigger.find('t:Repetition/t:Interval', ns).text, 'PT1H')
        self.assertIsNone(trigger.find('t:Repetition/t:Duration', ns))
        self.assertIsNone(trigger.find('t:EndBoundary', ns))
        rollover = ET.fromstring(result['Rollover'])
        self.assertEqual(rollover.find('t:Triggers/t:TimeTrigger/t:StartBoundary', ns).text,
                         '2026-09-07T00:00:00+09:00')
        self.assertEqual(result['Config']['scheduleCadence'], 'Hourly')
        self.assertEqual(len(result['Config']['scheduleTimes']), 24)
        self.assertEqual(result['Calls'], [])

    def test_cadence_changes_only_when_requested_and_preserves_legacy_daily(self):
        result = self.powershell("""
$first = Invoke-CollectorSchedule Install $fixtureRoot $fixturePythonw TwiceDaily
$legacy = $first.Config
$legacy.PSObject.Properties.Remove('scheduleCadence')
Write-CollectorFile $first.ConfigPath ($legacy | ConvertTo-Json -Depth 5)
$preservedLegacy = Invoke-CollectorSchedule Plan $fixtureRoot $fixturePythonw
$hourly = Invoke-CollectorSchedule Install $fixtureRoot $fixturePythonw Hourly
$preservedHourly = Invoke-CollectorSchedule Install $fixtureRoot $fixturePythonw
$daily = Invoke-CollectorSchedule Install $fixtureRoot $fixturePythonw TwiceDaily
[pscustomobject]@{
    LegacyCadence = $preservedLegacy.Config.scheduleCadence
    HourlyCadence = $hourly.Config.scheduleCadence
    PreservedHourly = $preservedHourly.Config.scheduleCadence
    DailyCadence = $daily.Config.scheduleCadence
    SameId = ($first.Config.installationId -ceq $daily.Config.installationId)
    Calls = @($script:Calls)
} | ConvertTo-Json -Depth 5
""")
        self.assertEqual(result['LegacyCadence'], 'TwiceDaily')
        self.assertEqual(result['HourlyCadence'], 'Hourly')
        self.assertEqual(result['PreservedHourly'], 'Hourly')
        self.assertEqual(result['DailyCadence'], 'TwiceDaily')
        self.assertTrue(result['SameId'])
        self.assertEqual(result['Calls'], ['Install'] * 4)

    def test_new_install_defaults_hourly_and_invalid_cadence_is_rejected(self):
        result = self.powershell("""
$plan = Invoke-CollectorSchedule Plan $fixtureRoot $fixturePythonw
$checks = @()
foreach ($operation in @('Install', 'Plan', 'Run', 'Stop', 'Uninstall', 'Status')) {
    $blocked = $false
    try { $null = Invoke-CollectorSchedule $operation $fixtureRoot $fixturePythonw Unknown }
    catch { $blocked = $true }
    $checks += $blocked
}
[pscustomobject]@{ NewCadence = $plan.Config.scheduleCadence;
    Checks = $checks; Calls = @($script:Calls) } | ConvertTo-Json
""")
        self.assertEqual(result['NewCadence'], 'Hourly')
        self.assertTrue(all(result['Checks']))
        self.assertEqual(result['Calls'], [])

    def test_reinstall_moves_root_and_uninstall_preserves_all_data(self):
        result = self.powershell("""
$first = Invoke-CollectorSchedule Install $fixtureRoot $fixturePythonw
$config = $first.Config
foreach ($name in @('observed-shifts.json','last-run.json','collector.log')) {
    [IO.File]::WriteAllText((Join-Path $config.dataRoot $name), 'preserved')
}
$moved = Join-Path $env:LOCALAPPDATA 'moved'
[void][IO.Directory]::CreateDirectory((Join-Path $moved 'tools'))
[IO.File]::WriteAllText((Join-Path $moved 'tools\\collect-shifts.py'), '--once --snapshot --publish')
$second = Invoke-CollectorSchedule Install $moved $fixturePythonw
$run = Invoke-CollectorSchedule Run $moved $fixturePythonw
$stop = Invoke-CollectorSchedule Stop $moved $fixturePythonw
$removed = Invoke-CollectorSchedule Uninstall $moved $fixturePythonw
[pscustomobject]@{
    SameId = ($first.Config.installationId -ceq $second.Config.installationId)
    Root = $second.Config.repoRoot
    Moved = $moved
    Snapshot = [IO.File]::ReadAllText($config.snapshotPath)
    Report = [IO.File]::ReadAllText($config.reportPath)
    Log = [IO.File]::ReadAllText($config.logPath)
    ConfigKept = (Test-Path -LiteralPath $first.ConfigPath)
    LauncherKept = (Test-Path -LiteralPath $config.launcherPath)
    Installed = $removed.Installed
    Calls = @($script:Calls)
} | ConvertTo-Json -Depth 5
""")
        self.assertTrue(result['SameId'])
        self.assertEqual(result['Root'], result['Moved'])
        self.assertEqual(result['Snapshot'], 'preserved')
        self.assertEqual(result['Report'], 'preserved')
        self.assertEqual(result['Log'], 'preserved')
        self.assertTrue(result['ConfigKept'])
        self.assertTrue(result['LauncherKept'])
        self.assertFalse(result['Installed'])
        self.assertEqual(result['Calls'], ['Install', 'Install', 'Run', 'Stop', 'Uninstall'])

    def test_description_principal_and_action_guards_for_every_operation(self):
        result = self.powershell("""
$initial = Invoke-CollectorSchedule Install $fixtureRoot $fixturePythonw
$checks = @()
foreach ($field in @('Description', 'UserId', 'LogonType', 'RunLevel', 'Execute', 'Arguments', 'WorkingDirectory')) {
    $original = $script:FixtureTask | ConvertTo-Json -Depth 8
    switch ($field) {
        'Description' { $script:FixtureTask.Description = 'not-owned' }
        'UserId' { $script:FixtureTask.Principal.UserId = 'S-1-5-21-9999' }
        'LogonType' { $script:FixtureTask.Principal.LogonType = 'Password' }
        'RunLevel' { $script:FixtureTask.Principal.RunLevel = 'Highest' }
        default { $script:FixtureTask.Actions[0].$field = 'not-owned' }
    }
    foreach ($operation in @('Install', 'Uninstall', 'Run', 'Stop', 'Status')) {
        $blocked = $false
        try { $null = Invoke-CollectorSchedule $operation $fixtureRoot $fixturePythonw }
        catch { $blocked = $true }
        $checks += $blocked
    }
    $script:FixtureTask = $original | ConvertFrom-Json
}
[pscustomobject]@{ Checks = $checks; Calls = @($script:Calls) } | ConvertTo-Json
""")
        self.assertEqual(len(result['Checks']), 35)
        self.assertTrue(all(result['Checks']))
        self.assertEqual(result['Calls'], ['Install'])

    def test_unowned_directory_and_task_without_config_are_not_overwritten(self):
        result = self.powershell("""
$paths = Get-CollectorPaths 'S-1-5-21-1001'
[void][IO.Directory]::CreateDirectory($paths.DataRoot)
$foreign = Join-Path $paths.DataRoot 'foreign.txt'
[IO.File]::WriteAllText($foreign, 'keep')
$directoryBlocked = $false
try { $null = Invoke-CollectorSchedule Install $fixtureRoot $fixturePythonw }
catch { $directoryBlocked = $true }
$preserved = [IO.File]::ReadAllText($foreign)
Remove-Item -LiteralPath $foreign
$script:FixtureTask = [pscustomobject]@{ TaskName = $paths.TaskName }
$taskBlocked = $false
try { $null = Invoke-CollectorSchedule Install $fixtureRoot $fixturePythonw }
catch { $taskBlocked = $true }
[pscustomobject]@{ DirectoryBlocked = $directoryBlocked; TaskBlocked = $taskBlocked;
    Preserved = $preserved; Calls = @($script:Calls) } | ConvertTo-Json
""")
        self.assertTrue(result['DirectoryBlocked'])
        self.assertTrue(result['TaskBlocked'])
        self.assertEqual(result['Preserved'], 'keep')
        self.assertEqual(result['Calls'], [])

    def test_existing_task_lookup_is_case_insensitive(self):
        result = self.powershell("""
${function:Get-CollectorTask} = $script:OriginalGetCollectorTask
$paths = Get-CollectorPaths 'S-1-5-21-1001'
function Get-ScheduledTask {
    param($TaskPath)
    [pscustomobject]@{ TaskName = $paths.TaskName.ToUpperInvariant() }
}
$blocked = $false
try { $null = Invoke-CollectorSchedule Install $fixtureRoot $fixturePythonw }
catch { $blocked = $true }
[pscustomobject]@{ Blocked = $blocked; Calls = @($script:Calls) } | ConvertTo-Json
""")
        self.assertTrue(result['Blocked'])
        self.assertEqual(result['Calls'], [])

    def test_config_owner_guard_and_missing_cli_prevent_start(self):
        result = self.powershell("""
$installed = Invoke-CollectorSchedule Install $fixtureRoot $fixturePythonw
[IO.File]::WriteAllText((Join-Path $fixtureRoot 'tools\\collect-shifts.py'), '--watch only')
$cliBlocked = $false
try { $null = Invoke-CollectorSchedule Run $fixtureRoot $fixturePythonw }
catch { $cliBlocked = $true }
$bad = $installed.Config
$bad.owner = 'foreign'
Write-CollectorFile $installed.ConfigPath ($bad | ConvertTo-Json)
$ownerBlocked = $false
try { $null = Invoke-CollectorSchedule Uninstall $fixtureRoot $fixturePythonw }
catch { $ownerBlocked = $true }
[pscustomobject]@{ CliBlocked = $cliBlocked; OwnerBlocked = $ownerBlocked; Calls = @($script:Calls) } | ConvertTo-Json
""")
        self.assertTrue(result['CliBlocked'])
        self.assertTrue(result['OwnerBlocked'])
        self.assertEqual(result['Calls'], ['Install'])

    def test_running_task_cannot_be_overwritten_or_uninstalled(self):
        result = self.powershell("""
$null = Invoke-CollectorSchedule Install $fixtureRoot $fixturePythonw
$script:FixtureTask.State = 'Running'
$checks = @()
foreach ($operation in @('Install', 'Uninstall')) {
    $blocked = $false
    try { $null = Invoke-CollectorSchedule $operation $fixtureRoot $fixturePythonw }
    catch { $blocked = $true }
    $checks += $blocked
}
[pscustomobject]@{ Checks = $checks; Calls = @($script:Calls) } | ConvertTo-Json
""")
        self.assertTrue(all(result['Checks']))
        self.assertEqual(result['Calls'], ['Install'])


if __name__ == '__main__':
    unittest.main()
