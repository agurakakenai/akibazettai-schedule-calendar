"""Console-less, same-process entry point for the per-user scheduled collector."""
import argparse
import contextlib
import datetime as dt
import importlib.util
import json
import os
from pathlib import Path
import sys
import uuid


OWNER = 'akibazettai-schedule-calendar:scheduled-collector:v1'
CONFIG_NAME = 'collector-config.json'


def timestamp():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')


def atomic_json(path, value):
    staging = path.with_name(path.name + '.' + uuid.uuid4().hex + '.writing')
    try:
        with staging.open('x', encoding='utf-8', newline='\n') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        staging.replace(path)
    finally:
        staging.unlink(missing_ok=True)


def load_config(path):
    config = json.loads(path.read_text(encoding='utf-8-sig'))
    if (config.get('owner') != OWNER or config.get('schemaVersion') != 1
            or not config.get('installationId') or not config.get('userSid')):
        raise ValueError('unowned_config')
    base = path.resolve().parent
    if Path(config['dataRoot']).resolve() != base:
        raise ValueError('incorrect_data_root')
    root = Path(config['repoRoot'])
    if not root.is_absolute():
        raise ValueError('absolute_repo_root_required')
    for key, name in (
            ('snapshotPath', 'observed-shifts.json'),
            ('reportPath', 'last-run.json'),
            ('statusPath', 'launcher-status.json'),
            ('logPath', 'collector.log')):
        if Path(config[key]).resolve() != base / name:
            raise ValueError('incorrect_' + key)
    if Path(config['publishPath']).resolve() != (
            root / 'data' / 'observed-shifts.json').resolve():
        raise ValueError('incorrect_publish_path')
    return config


def collector_arguments(config):
    return ['--once', '--days', '2', '--max-posts', '20',
            '--snapshot', config['snapshotPath'],
            '--publish', config['publishPath'],
            '--report', config['reportPath']]


def invoke_collector(config):
    root = Path(config['repoRoot'])
    script = root / 'tools' / 'collect-shifts.py'
    if not script.is_file():
        raise FileNotFoundError('collector_source_missing_reregister_required')
    spec = importlib.util.spec_from_file_location('scheduled_shift_collector', script)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    previous_cwd = Path.cwd()
    previous_path = sys.path[:]
    try:
        os.chdir(root)
        sys.path.insert(0, str(script.parent))
        spec.loader.exec_module(module)
        return module.main(collector_arguments(config))
    finally:
        os.chdir(previous_cwd)
        sys.path[:] = previous_path
        sys.modules.pop(spec.name, None)


def exit_code(value):
    if value is None:
        return 0
    if isinstance(value, int) and 0 <= value <= 0xFFFFFFFF:
        return value
    return 4


def run(config_path):
    # Do not write anything until the adjacent configuration proves ownership.
    try:
        config = load_config(config_path)
    except Exception:
        return 4
    status_path = Path(config['statusPath'])
    log_path = Path(config['logPath'])
    state = {
        'owner': OWNER,
        'installationId': config['installationId'],
        'runId': str(uuid.uuid4()),
        'pid': os.getpid(),
        'executable': sys.executable,
        'repoRoot': config['repoRoot'],
        'startedAt': timestamp(),
        'finishedAt': None,
        'status': 'running',
        'exitCode': None,
        'reportPath': config['reportPath'],
    }
    result = 4
    try:
        if log_path.exists() and log_path.stat().st_size > 5 * 1024 * 1024:
            log_path.replace(log_path.with_name('collector.previous.log'))
        with log_path.open('a', encoding='utf-8', buffering=1, newline='\n') as log:
            with contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
                print(json.dumps({'event': 'launcher-start', **state}))
                try:
                    atomic_json(status_path, state)
                    result = exit_code(invoke_collector(config))
                except SystemExit as error:
                    result = exit_code(error.code)
                except BaseException as error:
                    state['errorType'] = type(error).__name__
                    print(json.dumps({'event': 'launcher-error',
                                      'errorType': type(error).__name__}))
                    result = 4
                finally:
                    state.update(finishedAt=timestamp(), status='finished',
                                 exitCode=result)
                    atomic_json(status_path, state)
                    print(json.dumps({'event': 'launcher-finish', **state}))
    except BaseException:
        result = 4
        state.update(finishedAt=timestamp(), status='failed', exitCode=result,
                     errorType='LauncherIOError')
        try:
            atomic_json(status_path, state)
        except Exception:
            pass
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path,
                        default=Path(__file__).with_name(CONFIG_NAME))
    args = parser.parse_args(argv)
    return run(args.config)


if __name__ == '__main__':
    sys.exit(main())
