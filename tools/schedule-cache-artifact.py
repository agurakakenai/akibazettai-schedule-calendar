"""Download only an encrypted cache artifact from this repository's main workflow."""
import argparse
import datetime as dt
import json
import os
from pathlib import Path
import subprocess
import sys


REPOSITORY = 'agurakakenai/akibazettai-schedule-calendar'
WORKFLOW = '.github/workflows/deploy-pages.yml'
ARTIFACT_NAME = 'schedule-evidence-encrypted-v1'
MAX_BYTES = 65 * 1024 * 1024


def api(endpoint, *, binary=False):
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith(('AZURE_', 'SCHEDULE_EVIDENCE_'))}
    result = subprocess.run(
        ['gh', 'api', endpoint], capture_output=True, env=environment, timeout=90,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if result.returncode:
        raise ValueError('evidence_artifact_api_failed')
    if len(result.stdout) > MAX_BYTES:
        raise ValueError('evidence_artifact_too_large')
    if binary:
        return result.stdout
    try:
        return json.loads(result.stdout)
    except (ValueError, UnicodeError):
        raise ValueError('invalid_evidence_artifact_response') from None


def stamp(value):
    result = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('invalid_evidence_artifact_time')
    return result


def find_archive(*, request=api, now=None):
    now = now or dt.datetime.now(dt.timezone.utc)
    endpoint = f'repos/{REPOSITORY}/actions/artifacts?name={ARTIFACT_NAME}&per_page=100'
    response = request(endpoint)
    total = response.get('total_count', len(response['artifacts']))
    if type(total) is not int or not 0 <= total <= 1000:
        raise ValueError('evidence_artifact_listing_limit')
    entries = list(response['artifacts'])
    for page in range(2, (total + 99) // 100 + 1):
        entries.extend(request(f'{endpoint}&page={page}')['artifacts'])
    candidates = []
    for artifact in entries:
        run = artifact.get('workflow_run') or {}
        run_id = run.get('id')
        if (type(run_id) is not int or type(artifact.get('id')) is not int
                or artifact.get('name') != ARTIFACT_NAME
                or run.get('head_branch') != 'main' or artifact.get('expired') is not False
                or not 0 < artifact.get('size_in_bytes', 0) <= MAX_BYTES):
            continue
        created = stamp(artifact['created_at'])
        if not dt.timedelta(0) <= now - created < dt.timedelta(days=7):
            continue
        candidates.append((created, artifact['id'], run_id))
    for _, artifact_id, run_id in sorted(candidates, reverse=True):
        run = request(f'repos/{REPOSITORY}/actions/runs/{run_id}')
        if (run.get('head_repository', {}).get('full_name') != REPOSITORY
                or run.get('head_branch') != 'main' or run.get('path') != WORKFLOW
                or run.get('event') not in ('schedule', 'workflow_dispatch', 'push')
                or run.get('status') != 'completed'):
            continue
        return artifact_id
    return None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        target = args.output.absolute()
        root = Path(__file__).resolve().parents[1]
        if (target.name != 'schedule-evidence.zip' or target.exists()
                or target.resolve() != target or target.is_relative_to(root)
                or any(parent.is_symlink() for parent in target.parents)):
            raise ValueError('invalid_evidence_artifact_output')
        artifact_id = find_archive()
        if artifact_id is None:
            print('No retained encrypted schedule artifact is available.')
            return 0
        data = api(f'repos/{REPOSITORY}/actions/artifacts/{artifact_id}/zip', binary=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open('xb') as stream:
            stream.write(data)
        print(f'Encrypted schedule artifact {artifact_id} downloaded; authentication is required before use.')
        return 0
    except (KeyError, TypeError, ValueError, OSError, subprocess.TimeoutExpired) as exc:
        reason = str(exc) if isinstance(exc, ValueError) else 'evidence_artifact_download_failed'
        print(reason, file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
