"""Read-only window monitoring around an owned scheduler Install or one Run."""
import argparse
import base64
import ctypes
from ctypes import wintypes
import datetime as dt
import json
import os
from pathlib import Path
import subprocess
import sys
import time


ROOT = Path(__file__).resolve().parents[1]


class ProcessEntry(ctypes.Structure):
    _fields_ = [
        ('dwSize', wintypes.DWORD), ('cntUsage', wintypes.DWORD),
        ('th32ProcessID', wintypes.DWORD), ('th32DefaultHeapID', ctypes.c_size_t),
        ('th32ModuleID', wintypes.DWORD), ('cntThreads', wintypes.DWORD),
        ('th32ParentProcessID', wintypes.DWORD), ('pcPriClassBase', wintypes.LONG),
        ('dwFlags', wintypes.DWORD), ('szExeFile', wintypes.WCHAR * 260),
    ]


class DesktopProbe:
    def __init__(self, owned_pids):
        self.owned_pids = owned_pids
        self.user32 = ctypes.WinDLL('user32', use_last_error=True)
        self.kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        self.user32.GetForegroundWindow.restype = wintypes.HWND
        self.user32.IsWindowVisible.argtypes = [wintypes.HWND]
        self.user32.GetWindowThreadProcessId.argtypes = [
            wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        self.callback_type = ctypes.WINFUNCTYPE(
            wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        self.user32.EnumWindows.argtypes = [self.callback_type, wintypes.LPARAM]
        self.kernel32.OpenProcess.argtypes = [
            wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.kernel32.OpenProcess.restype = wintypes.HANDLE
        self.kernel32.GetExitCodeProcess.argtypes = [
            wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        self.kernel32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD)]
        self.kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        self.kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        for name in ('Process32FirstW', 'Process32NextW'):
            getattr(self.kernel32, name).argtypes = [
                wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
        self.baseline = self.foreground()
        self.samples = 0
        self.changes = []
        self.windows = set()

    def foreground(self):
        return int(self.user32.GetForegroundWindow() or 0)

    def sample(self):
        self.samples += 1
        self.find_descendants()
        current = self.foreground()
        if current != self.baseline and current not in self.changes:
            self.changes.append(current)

        @self.callback_type
        def visitor(hwnd, _):
            if self.user32.IsWindowVisible(hwnd):
                pid = wintypes.DWORD()
                self.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                self.windows.add((int(hwnd), pid.value))
            return True

        if not self.user32.EnumWindows(visitor, 0):
            raise ctypes.WinError(ctypes.get_last_error())

    def find_descendants(self):
        handle = self.kernel32.CreateToolhelp32Snapshot(2, 0)
        if handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            entry = ProcessEntry()
            entry.dwSize = ctypes.sizeof(entry)
            found = self.kernel32.Process32FirstW(handle, ctypes.byref(entry))
            parents = {}
            while found:
                parents[entry.th32ProcessID] = entry.th32ParentProcessID
                found = self.kernel32.Process32NextW(handle, ctypes.byref(entry))
            while True:
                children = {pid for pid, parent in parents.items()
                            if parent in self.owned_pids}
                if children.issubset(self.owned_pids):
                    break
                self.owned_pids.update(children)
        finally:
            self.kernel32.CloseHandle(handle)

    def process(self, pid):
        handle = self.kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return None
        try:
            code = wintypes.DWORD()
            size = wintypes.DWORD(32768)
            image = ctypes.create_unicode_buffer(size.value)
            if not self.kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                raise ctypes.WinError(ctypes.get_last_error())
            if code.value != 259:
                return {'pid': pid, 'image': None, 'active': False, 'exitCode': code.value}
            if not self.kernel32.QueryFullProcessImageNameW(
                    handle, 0, image, ctypes.byref(size)):
                if (self.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
                        and code.value != 259):
                    return {'pid': pid, 'image': None, 'active': False, 'exitCode': code.value}
                raise ctypes.WinError(ctypes.get_last_error())
            return {'pid': pid, 'image': image.value,
                    'active': code.value == 259, 'exitCode': code.value}
        finally:
            self.kernel32.CloseHandle(handle)

    def evidence(self, owned_pids):
        return {
            'before': self.baseline,
            'after': self.foreground(),
            'samples': self.samples,
            'changedHandles': self.changes,
            'unchanged': not self.changes and self.foreground() == self.baseline,
            'ownedVisibleWindows': [
                {'hwnd': hwnd, 'pid': pid}
                for hwnd, pid in sorted(self.windows) if pid in owned_pids],
            'ownedProcessIds': sorted(owned_pids),
            'method': 'GetForegroundWindow + EnumWindows/IsWindowVisible; no input, activation, or capture',
        }


def invoke_setup(action, probe, owned_pids, cadence=None):
    script = ROOT / 'tools' / 'setup-collector-schedule.ps1'
    command = (
        "$ErrorActionPreference='Stop'; $ProgressPreference='SilentlyContinue'; "
        "[Console]::OutputEncoding=New-Object Text.UTF8Encoding($false); "
        "& '" + str(script).replace("'", "''") + "' -Action " + action)
    if cadence is not None:
        if cadence not in ('Hourly', 'TwiceDaily') or action not in ('Install', 'Plan'):
            raise ValueError('Cadence is only valid for Install or Plan.')
        command += ' -Cadence ' + cadence
    encoded = base64.b64encode(command.encode('utf-16-le')).decode('ascii')
    with subprocess.Popen(
            ['powershell.exe', '-NoProfile', '-NonInteractive', '-WindowStyle',
             'Hidden', '-EncodedCommand', encoded],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW) as process:
        owned_pids.add(process.pid)
        while True:
            probe.sample()
            try:
                stdout, stderr = process.communicate(timeout=0.05)
                break
            except subprocess.TimeoutExpired:
                pass
        if process.returncode:
            raise RuntimeError(stderr.decode('mbcs', errors='replace'))
    return json.loads(stdout.decode('utf-8-sig'))


def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8-sig'))
    except (FileNotFoundError, PermissionError, json.JSONDecodeError):
        # A concurrent atomic replacement can briefly deny reads on Windows.
        return None


def verify(action, output, cadence=None):
    owned_pids = {os.getpid()}
    probe = DesktopProbe(owned_pids)
    evidence = {'operation': action,
                'startedAt': dt.datetime.now(dt.timezone.utc).isoformat()}
    result = 1
    try:
        before = invoke_setup('Status', probe, owned_pids)
        evidence['before'] = before
        if probe.changes:
            raise RuntimeError('Foreground changed before mutation; operation cancelled.')
        old_run_id = None
        if action == 'Run':
            if not before['Installed'] or before['State'] == 'Running':
                raise RuntimeError('An installed, idle owned task is required.')
            old_state = read_json(before['Config']['statusPath'])
            old_run_id = old_state.get('runId') if old_state else None
        evidence['operationResult'] = invoke_setup(action, probe, owned_pids, cadence)
        if probe.changes:
            raise RuntimeError('Foreground changed during operation; no further operations.')
        if action == 'Run':
            config = evidence['operationResult']['Config']
            deadline = time.monotonic() + 630
            observed_active = None
            while time.monotonic() < deadline:
                probe.sample()
                if probe.changes:
                    raise RuntimeError('Foreground changed; monitoring stopped without further operations.')
                state = read_json(config['statusPath'])
                if state and state.get('runId') != old_run_id:
                    owned_pids.add(state['pid'])
                    process = probe.process(state['pid'])
                    if process and process['active']:
                        observed_active = process
                        evidence['observedActiveProcess'] = process
                    if state['status'] in ('finished', 'failed') and (
                            process is None or not process['active']):
                        evidence['launcherStatus'] = state
                        evidence['observedActiveProcess'] = observed_active
                        evidence['processAtCompletion'] = process
                        evidence['report'] = read_json(config['reportPath'])
                        break
                time.sleep(0.05)
            else:
                raise TimeoutError('Task did not finish within verification deadline.')
        evidence['after'] = invoke_setup('Status', probe, owned_pids)
        if action == 'Run':
            launcher_code = evidence['launcherStatus']['exitCode']
            # Scheduler metadata may settle shortly after the process exits.
            for _ in range(8):
                if evidence['after']['State'] != 'Running':
                    break
                time.sleep(0.25)
                evidence['after'] = invoke_setup('Status', probe, owned_pids)
            evidence['exitCodesMatch'] = (
                launcher_code == evidence['after']['LastTaskResult'])
            result = 0 if evidence['exitCodesMatch'] else 1
        else:
            result = 0 if evidence['after']['Installed'] else 1
    except Exception as error:
        evidence['error'] = str(error)
    finally:
        probe.sample()
        evidence['foreground'] = probe.evidence(owned_pids)
        evidence['finishedAt'] = dt.datetime.now(dt.timezone.utc).isoformat()
        if not evidence['foreground']['unchanged'] or evidence['foreground']['ownedVisibleWindows']:
            result = 1
        evidence['verificationExitCode'] = result
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + '\n',
                          encoding='utf-8')
    print(json.dumps({'evidence': str(output), 'verificationExitCode': result,
                      'foreground': evidence['foreground']}, ensure_ascii=False))
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--action', choices=('Install', 'Run'), required=True)
    parser.add_argument('--cadence', choices=('Hourly', 'TwiceDaily'))
    parser.add_argument('--output', type=Path, required=True)
    arguments = parser.parse_args()
    if os.name != 'nt':
        parser.error('Windows is required.')
    if arguments.cadence and arguments.action != 'Install':
        parser.error('--cadence is only valid with --action Install.')
    sys.exit(verify(arguments.action, arguments.output, arguments.cadence))
