import json
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

from app.task_store import TaskStore


PROJECT_ROOT = Path(__file__).resolve().parents[1]
HARNESS = 'tests.support.restart_harness'


@pytest.mark.parametrize('stage,stop_signal', [
    ('scan', signal.SIGKILL), ('model', signal.SIGKILL), ('inference', signal.SIGTERM),
    ('inference', signal.SIGKILL), ('tags', signal.SIGKILL), ('marker', signal.SIGKILL),
    ('checkpoint', signal.SIGKILL), ('after_checkpoint', signal.SIGKILL),
])
def test_real_process_interruption_and_reopen(tmp_path, stage, stop_signal):
    with (tmp_path / 'worker.log').open('w') as log:
        child = subprocess.Popen([sys.executable, '-m', HARNESS, str(tmp_path), '--block', stage],
                                 stdout=log, stderr=log, cwd=PROJECT_ROOT)
        try:
            deadline = time.monotonic() + 10
            while not (tmp_path / 'blocked').exists() and child.poll() is None and time.monotonic() < deadline:
                time.sleep(.02)
            assert (tmp_path / 'blocked').exists(), (tmp_path / 'worker.log').read_text()
            child.send_signal(stop_signal)
            child.wait(timeout=5)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)
    before = json.loads((tmp_path / 'remote.json').read_text())
    checkpoint = TaskStore.snapshot(tmp_path / 'state')
    resumed = subprocess.run([sys.executable, '-m', HARNESS, str(tmp_path)], capture_output=True, text=True, timeout=10, cwd=PROJECT_ROOT)
    assert resumed.returncode == 0, resumed.stderr
    after = json.loads((tmp_path / 'remote.json').read_text())
    snapshot = TaskStore.snapshot(tmp_path / 'state')
    assert snapshot['progress']['run_id'] == checkpoint['progress']['run_id']
    assert snapshot['progress']['completed'] == snapshot['progress']['total'] == 3
    assert snapshot['progress']['task_status'] == 'completed'
    assert not after['assets']['3']['tags']
    for identifier in ('0', '1', '2'):
        tags = [t['id'] for t in after['assets'][identifier]['tags']]
        assert 'tag-auto:processed' in tags
        assert len(tags) == len(set(tags))
    if stage != 'scan':
        assert after['searches'] == before['searches']
    else:
        assert after['searches'] > before['searches']
    if stage in ('marker', 'checkpoint', 'after_checkpoint'):
        assert after['inferences'].count('0') == 1
    if stage == 'inference':
        assert after['inferences'].count('0') == 1
        assert after['inferences'].count('1') == (1 if stop_signal == signal.SIGTERM else 2)
    if stage == 'tags':
        writes = [a for a in after['assignments'] if '0' in a['assetIds'] and 'tag-auto:processed' not in a['tagIds']]
        assert len(writes) == 1
    for path in (tmp_path / 'state').glob('*'):
        if path.is_file():
            assert b'restart-test-secret' not in path.read_bytes()


@pytest.mark.parametrize('stage,maintenance', [
    *[(stage, False) for stage in ('scan', 'cleanup-prepared', 'cleanup-deleted', 'checkpoint', 'after_checkpoint')],
    *[(stage, True) for stage in ('queue-intent', 'queue-paused-metadataExtraction', 'queue-paused-sidecar',
                                  'cleanup-deleted', 'queue-resumed-sidecar', 'queue-resumed-metadataExtraction',
                                  'checkpoint', 'after_checkpoint')],
])
def test_cleanup_survives_kill_with_audit_and_no_duplicate_removal(tmp_path, stage, maintenance):
    command = [sys.executable, '-m', HARNESS, str(tmp_path), '--cleanup']
    if maintenance:
        command.append('--maintenance')
    with (tmp_path / 'worker.log').open('w') as log:
        child = subprocess.Popen([*command, '--block', stage], stdout=log, stderr=log, cwd=PROJECT_ROOT)
        try:
            deadline = time.monotonic() + 10
            while not (tmp_path / 'blocked').exists() and child.poll() is None and time.monotonic() < deadline:
                time.sleep(.02)
            assert (tmp_path / 'blocked').exists(), (tmp_path / 'worker.log').read_text()
            child.kill()
            child.wait(timeout=5)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)
    before = json.loads((tmp_path / 'remote.json').read_text())
    previous = TaskStore.snapshot(tmp_path / 'state')
    if maintenance:
        assert previous['progress']['completed'] == (1 if stage == 'after_checkpoint' else 0)
        assert (tmp_path / 'state' / 'cleanup-queues.json').exists() == (stage not in ('checkpoint', 'after_checkpoint'))
    resumed = subprocess.run(command, capture_output=True, text=True, timeout=10, cwd=PROJECT_ROOT)
    assert resumed.returncode == 0, resumed.stderr
    after = json.loads((tmp_path / 'remote.json').read_text())
    snapshot = TaskStore.snapshot(tmp_path / 'state')
    assert snapshot['progress']['run_id'] == previous['progress']['run_id']
    assert snapshot['progress']['task_status'] == 'completed'
    assert snapshot['progress']['completed'] == snapshot['progress']['total'] == 3
    if stage != 'scan':
        assert after['searches'] == before['searches']
    assert not after['inferences'] and not after['assignments']
    assert len(after['removals']) == 3
    if maintenance:
        assert after['queues']['sidecar']['isPaused'] is True
        assert after['queues']['metadataExtraction']['isPaused'] is False
        assert not (tmp_path / 'state' / 'cleanup-queues.json').exists()
    for identifier in ('0', '1', '2'):
        assert {t['value'] for t in after['assets'][identifier]['tags']} == {'属性/蓝发', 'auto:processed', 'manual'}
    assert 'blue_hair' in {t['value'] for t in after['assets']['3']['tags']}
    journal = [json.loads(line) for line in (tmp_path / 'state' / 'cleanup.jsonl').read_text().splitlines()]
    for identifier in ('0', '1', '2'):
        assert any(e['asset_id'] == identifier and e['status'] == 'prepared' for e in journal)
    for path in (tmp_path / 'state').glob('*'):
        if path.is_file():
            assert b'restart-test-secret' not in path.read_bytes()
            assert b'restart-admin-secret' not in path.read_bytes()
