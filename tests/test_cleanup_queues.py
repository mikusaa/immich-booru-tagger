import json
import logging

import httpx
import pytest

from conftest import FakeImmich, tag
from immich_tagger.cleanup import EnglishTagCleaner
from immich_tagger.cleanup_queues import CleanupQueues, QueueMaintenanceError, restore_cleanup_queues
from immich_tagger.immich_client import ImmichAPIError
from immich_tagger.progress import ProgressState
from immich_tagger.main import main, parse_arguments
from immich_tagger.processor import ImmichAutoTagger
from immich_tagger.state import writer_lock
from immich_tagger.task_store import TaskStore
from test_cleanup import bilingual_asset, paths


@pytest.fixture(autouse=True)
def fast_polls(monkeypatch):
    monkeypatch.setattr(CleanupQueues, 'poll_interval', 0)


def worker_for(settings, server, **kwargs):
    settings.cleanup_admin_api_key = 'admin-only-secret'
    return EnglishTagCleaner(settings, cleanup_scope='catalog', dry_run=False,
                             confirm_cleanup_english=True, maintenance=True,
                             client_factory=server.factory, **kwargs)


def writes(server):
    return [(path.rsplit('/', 1)[-1], body['isPaused']) for method, path, body in server.calls
            if method == 'PUT' and path.startswith('/api/queues/')]


@pytest.mark.parametrize('paused', [False, True])
def test_each_asset_flushes_before_checkpoint_and_preserves_original_pauses(settings, paused):
    server = FakeImmich([bilingual_asset(0), bilingual_asset(1)])
    server.queues['sidecar']['isPaused'] = paused
    handle = server.handle
    credentials = []
    def checked(request):
        credentials.append((request.url.path, request.headers['X-API-Key']))
        if request.method == 'DELETE':
            assert all(q['isPaused'] for q in server.queues.values())
            state = json.loads((settings.state_dir / 'cleanup-queues.json').read_text())
            assert state['phase'] == 'cleaning'
            assert 'admin-only-secret' not in json.dumps(state)
        if request.method == 'PUT' and request.url.path.endswith('/metadataExtraction'):
            if json.loads(request.content)['isPaused'] is False:
                assert server.queues['sidecar']['isPaused'] is False
                # Current asset has not been committed while jobs are resumed.
                completed = TaskStore.snapshot(settings.state_dir)['progress']['completed']
                deleted = sum(m == 'DELETE' for m, _, _ in server.calls)
                assert completed < deleted
        return handle(request)
    server.handle = checked
    worker = worker_for(settings, server)
    try:
        assert worker.run().processed == 2
        assert server.queues['sidecar']['isPaused'] is paused
        assert not server.queues['metadataExtraction']['isPaused']
        assert not worker._queues.path.exists()
        assert sum(n == 'sidecar' and p is False for n, p in writes(server)) >= 2
        assert all(key == ('admin-only-secret' if path.startswith('/api/queues/') else 'test-key')
                   for path, key in credentials)
    finally:
        worker.close()


@pytest.mark.parametrize('method', ['GET', 'PUT'])
def test_permission_denial_never_deletes_assets(settings, method):
    server = FakeImmich([bilingual_asset(0)])
    handle = server.handle
    def denied(request):
        if request.url.path.startswith('/api/queues/') and request.method == method:
            return httpx.Response(403)
        return handle(request)
    server.handle = denied
    worker = worker_for(settings, server)
    try:
        with pytest.raises((ImmichAPIError, QueueMaintenanceError)):
            worker.run()
        assert not any(m == 'DELETE' for m, _, _ in server.calls)
        assert 'blue_hair' in paths(server)
    finally:
        worker.close()


def test_writer_lock_precedes_all_queue_calls(settings):
    server = FakeImmich([bilingual_asset(0)])
    worker = worker_for(settings, server)
    try:
        with writer_lock(settings.state_dir):
            with pytest.raises(RuntimeError, match='Another tagger'):
                worker.run()
        assert not server.calls
    finally:
        worker.close()


@pytest.mark.parametrize('change', ['english', 'protected', 'failed_job'])
def test_async_flush_failure_never_commits_success(settings, change):
    server = FakeImmich([bilingual_asset(0)])
    handle = server.handle
    def changed(request):
        response = handle(request)
        if (request.method == 'PUT' and request.url.path.endswith('/metadataExtraction')
                and json.loads(request.content)['isPaused'] is False):
            if change == 'english':
                server.assets['0']['tags'].append(tag('blue_hair'))
            elif change == 'protected':
                server.assets['0']['tags'] = [tag('auto:processed')]
            else:
                server.queues['metadataExtraction']['statistics']['failed'] += 1
        return response
    server.handle = changed
    worker = worker_for(settings, server)
    try:
        if change == 'english':
            result = worker.run()
            assert result.failed == 1 and result.processed == 0
        else:
            with pytest.raises(QueueMaintenanceError):
                worker.run()
        snap = TaskStore.snapshot(settings.state_dir)
        assert snap['last_run']['processed'] == 0
        assert all(not q['isPaused'] for q in server.queues.values())
    finally:
        worker.close()


def test_cancellation_during_delete_restores_queues_and_keeps_asset_pending(settings):
    server = FakeImmich([bilingual_asset(0)])
    server.assets['0']['tags'].append(tag('general/blue_hair'))
    worker = worker_for(settings, server)
    original = worker.clients[0].untag_single_asset
    def cancel(*args):
        original(*args)
        worker.cancelled.set()
    worker.clients[0].untag_single_asset = cancel
    try:
        worker.run()
        assert not worker._queues.path.exists()
        assert all(not q['isPaused'] for q in server.queues.values())
        snapshot = TaskStore.snapshot(settings.state_dir)
        assert snapshot['progress']['task_status'] == 'paused'
        assert snapshot['progress']['completed'] == 0
        assert snapshot['last_run']['failed'] == 0
        assert 'general/blue_hair' in paths(server)
    finally:
        worker.close()


def test_timeout_retains_recoverable_state_and_does_not_claim_success(settings):
    settings.cleanup_queue_timeout = .001
    server = FakeImmich([bilingual_asset(0)])
    server.queues['sidecar']['statistics']['active'] = 1
    worker = worker_for(settings, server)
    try:
        with pytest.raises(QueueMaintenanceError, match='restore-cleanup-queues'):
            worker.run()
        assert worker._queues.path.exists()
        assert not any(m == 'DELETE' for m, _, _ in server.calls)
        server.queues['sidecar']['statistics']['active'] = 0
        assert worker._queues.recover()
        assert all(not q['isPaused'] for q in server.queues.values())
    finally:
        worker.close()


@pytest.mark.parametrize('mismatch', ['server', 'corrupt'])
def test_recovery_rejects_foreign_or_corrupt_record_without_remote_calls(settings, mismatch):
    server = FakeImmich([bilingual_asset(0)])
    worker = worker_for(settings, server)
    settings.state_dir.mkdir()
    worker._queues.path.write_text('{' if mismatch == 'corrupt' else json.dumps({
        'version': 1, 'server': 'another-server', 'queues': {}}))
    try:
        with pytest.raises(QueueMaintenanceError, match='损坏或服务地址不匹配'):
            worker.run()
        assert not server.calls
    finally:
        worker.close()


def test_default_cleaner_and_regular_writer_refuse_pending_maintenance(settings):
    server = FakeImmich([bilingual_asset(0)])
    settings.state_dir.mkdir()
    (settings.state_dir / 'cleanup-queues.json').write_text('{}')
    for worker in (ImmichAutoTagger(settings, client_factory=server.factory),
                   EnglishTagCleaner(settings, cleanup_scope='catalog', dry_run=False,
                                     confirm_cleanup_english=True, client_factory=server.factory)):
        try:
            with pytest.raises(RuntimeError, match='restore-cleanup-queues'):
                worker.run()
            assert not server.calls
        finally:
            worker.close()


def test_restore_command_needs_no_catalog_or_asset_account_read(settings, monkeypatch):
    server = FakeImmich([bilingual_asset(0)])
    worker = worker_for(settings, server)
    state = {'version': 1, 'server': worker._queues.server, 'phase': 'cleaning',
             'queues': {n: {'isPaused': False, 'failed': 0} for n in server.queues}}
    worker._queues._save(state)
    for queue in server.queues.values():
        queue['isPaused'] = True
    worker.close()
    settings.translation_file = settings.state_dir / 'does-not-exist.json'
    monkeypatch.setattr('immich_tagger.main.Settings', lambda **_: settings)
    monkeypatch.setattr('immich_tagger.immich_client.ImmichClient', server.factory)
    assert main(['--restore-cleanup-queues']) == 0
    assert not (settings.state_dir / 'cleanup-queues.json').exists()
    assert all(p.startswith('/api/queues/') for _, p, _ in server.calls)
    assert 'blue_hair' in paths(server)


def test_cli_maintenance_and_recovery_guards():
    for args in (['--cleanup-maintenance'], ['--mode', 'cleanup-english', '--cleanup-maintenance'],
                 ['--restore-cleanup-queues', '--dry-run'],
                 ['--restore-cleanup-queues', '--mode', 'cleanup-english', '--confirm-cleanup-english']):
        with pytest.raises(SystemExit):
            parse_arguments(args)


def test_lost_pause_response_restores_both_queues_before_any_delete(settings):
    server = FakeImmich([bilingual_asset(0)])
    handle = server.handle
    lost = False
    def uncertain(request):
        nonlocal lost
        response = handle(request)
        if request.method == 'PUT' and request.url.path.endswith('/sidecar') and not lost:
            lost = True
            raise httpx.ReadTimeout('response lost after pause applied', request=request)
        return response
    server.handle = uncertain
    worker = worker_for(settings, server)
    try:
        assert worker.run().failed == 1
        assert not any(m == 'DELETE' for m, _, _ in server.calls)
        assert all(not q['isPaused'] for q in server.queues.values())
        assert not worker._queues.path.exists()
    finally:
        worker.close()


def test_cleanup_log_exposes_actual_wait_tag_and_whole_asset_time(settings, monkeypatch, caplog):
    clock = [100.0]
    monkeypatch.setattr('immich_tagger.progress.time.monotonic', lambda: clock[0])
    progress = ProgressState(settings)
    progress.phase('processing', '开始清理')
    progress.update(total=1, cleanup_started=100, cleanup_tag='blue_hair', cleanup_index=2,
                    cleanup_total=15, cleanup_attempt=1)
    caplog.set_level(logging.INFO, logger='progress')
    with progress.operation('settle_tags'):
        clock[0] = 135.0
        progress.report()
    assert '等待标签异步回写' in caplog.text
    assert '标签核对 2/15' in caplog.text
    assert '当前标签：blue_hair' in caplog.text
    assert '本张累计耗时 35 秒' in caplog.text
    assert 'cleanup_started' not in progress.snapshot()
