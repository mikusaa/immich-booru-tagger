import asyncio
import json
import logging
import sqlite3
import threading
from datetime import datetime, timezone

import pytest

from conftest import ALBUM, LIBRARY_A, LIBRARY_B, FakeEngine, FakeImmich, asset, tag
from immich_tagger.health_server import HealthServer
from immich_tagger.logging import ZonedFormatter
from immich_tagger.main import main
from immich_tagger.models import AssetProcessingResult
from immich_tagger.processor import ImmichAutoTagger
from immich_tagger.progress import ProgressState
from immich_tagger.scheduler import Scheduler
from immich_tagger.state import writer_lock
from immich_tagger.task_store import TaskStore
from test_processor import processor


def interrupt_after_first(settings, server, **kwargs):
    engine = FakeEngine()
    worker = processor(settings, server, engine=engine)
    original = engine.predict_tags
    def predict(data):
        result = original(data)
        worker.cancelled.set()
        return result
    engine.predict_tags = predict
    result = worker.run(**kwargs)
    worker.close()
    return result


def test_resume_skips_scan_and_retains_total_limit(settings):
    settings.batch_size = 2
    server = FakeImmich([asset(i) for i in range(8)])
    assert interrupt_after_first(settings, server, limit=5).processed == 1
    saved = TaskStore.snapshot(settings.state_dir)
    assert saved['progress']['total'] == 5
    assert saved['progress']['remaining'] == 4
    previous_searches = sum(c[0] == 'POST' for c in server.calls)
    server.assets['new'] = asset('new')
    worker = processor(settings, server)
    try:
        assert worker.get_metrics()['last_run']['processed'] == 1
        assert worker.run(limit=5).processed == 5
        metrics = worker.get_metrics()
        assert metrics['progress']['resumed']
        assert metrics['progress']['session_completed'] == 4
        assert worker.engine.calls == 4
        assert sum(c[0] == 'POST' for c in server.calls) == previous_searches
        assert not server.assets['5']['tags'] and not server.assets['new']['tags']
        assert metrics['progress']['remaining'] == 0
    finally:
        worker.close()


def test_repeated_restarts_do_not_refill_limit(settings):
    server = FakeImmich([asset(i) for i in range(10)])
    for expected in range(1, 5):
        assert interrupt_after_first(settings, server, limit=4).processed == expected
    assert sum(bool(a['tags']) for a in server.assets.values()) == 4
    assert TaskStore.snapshot(settings.state_dir)['progress']['task_status'] == 'completed'


def test_interrupted_scan_revalidates_generation_and_does_not_write(settings):
    settings.batch_size = 2
    server = FakeImmich([asset(i) for i in range(5)])
    worker = processor(settings, server)
    original = worker.clients[0].iter_asset_pages
    def pages(**kwargs):
        for page in original(**kwargs):
            yield page
            worker.cancelled.set()
    worker.clients[0].iter_asset_pages = pages
    try:
        assert worker.run().attempted == 0
        assert not server.writes
        assert TaskStore.snapshot(settings.state_dir)['progress']['scan_complete'] is False
    finally:
        worker.close()
    del server.assets['0']
    server.assets['new'] = asset('new')
    worker = processor(settings, server)
    try:
        assert worker.run().processed == 5
        assert worker.get_metrics()['progress']['total'] == 5
        assert all(a['tags'] for a in server.assets.values())
    finally:
        worker.close()


@pytest.mark.parametrize('change', ['delete', 'library', 'album', 'offline', 'marked'])
def test_resume_checks_current_asset_state(settings, change):
    settings.immich_include_library_ids = [LIBRARY_A]
    settings.immich_include_album_ids = [ALBUM]
    server = FakeImmich([asset(i, libraryId=LIBRARY_A) for i in range(3)])
    server.albums[ALBUM] = {'assets': [{'id': str(i)} for i in range(3)]}
    assert interrupt_after_first(settings, server).processed == 1
    if change == 'delete':
        import httpx
        handle = server.handle
        def missing(request):
            if request.url.path == '/api/assets/1':
                return httpx.Response(404)
            return handle(request)
        server.handle = missing
    elif change == 'library':
        server.assets['1']['libraryId'] = LIBRARY_B
    elif change == 'album':
        server.albums[ALBUM]['assets'] = [{'id': '0'}, {'id': '2'}]
    elif change == 'offline':
        server.assets['1']['isOffline'] = True
    else:
        server.assets['1']['tags'] = [tag(settings.processed_tag_name)]
    worker = processor(settings, server)
    try:
        result = worker.run()
        assert result.processed == 2 and result.skipped == 1
        assert worker.engine.calls == 1
        assert worker.get_metrics()['progress']['completed'] == 3
    finally:
        worker.close()


@pytest.mark.parametrize('change', ['threshold', 'scope', 'limit', 'translation'])
def test_incompatible_configuration_replaces_queue(settings, change, tmp_path):
    server = FakeImmich([asset(i) for i in range(4)])
    interrupt_after_first(settings, server)
    previous_id = TaskStore.snapshot(settings.state_dir)['progress']['run_id']
    kwargs = {}
    if change == 'threshold':
        settings.general_threshold = .7
    elif change == 'scope':
        settings.immich_include_album_ids = [ALBUM]
        server.albums[ALBUM] = {'assets': [{'id': '2'}]}
    elif change == 'limit':
        kwargs['limit'] = 1
    else:
        path = tmp_path / 'overrides.json'
        path.write_text('{"blue_hair":{"zh":"青发","kind":"general"}}')
        settings.translation_overrides = path
    worker = processor(settings, server)
    try:
        worker.run(**kwargs)
        assert not worker.get_metrics()['progress']['resumed']
        with TaskStore(settings.state_dir, readonly=True) as store:
            assert store.get_run(previous_id)['status'] == 'superseded'
    finally:
        worker.close()


def test_operational_settings_do_not_invalidate_queue(settings):
    server = FakeImmich([asset(i) for i in range(3)])
    interrupt_after_first(settings, server)
    settings.batch_size = 1
    settings.log_progress_interval_seconds = 2
    settings.request_timeout = 42
    settings.timezone = 'UTC'
    worker = processor(settings, server)
    try:
        assert worker.run().processed == 3
        assert worker.get_metrics()['progress']['resumed']
    finally:
        worker.close()


def test_failure_checkpoint_is_atomic_and_cannot_count_twice(settings):
    with writer_lock(settings.state_dir), TaskStore(settings.state_dir) as store:
        run, _ = store.begin('inference', 'sig', None)
        generation = store.start_scan(run['id'])
        store.save_page(run['id'], generation, [('acct', 'image', 0)], {}, 0)
        store.finish_scan(run['id'], generation)
        store.mark_processing(run['id'], 'acct', 'image')
        store.db.execute("CREATE TRIGGER break_counter BEFORE UPDATE OF result ON runs BEGIN SELECT RAISE(ABORT, 'disk failure'); END")
        with pytest.raises(sqlite3.IntegrityError):
            store.finish_item(run['id'], 'acct', AssetProcessingResult(asset_id='image', error='bad image'), 'acct', 3)
        assert not store.failures('acct')
        assert json.loads(store.get_run(run['id'])['result'])['attempted'] == 0
        store.db.execute('DROP TRIGGER break_counter')
        assert store.finish_item(run['id'], 'acct', AssetProcessingResult(asset_id='image'), 'acct', 3).failed == 1
        with pytest.raises(RuntimeError, match='已经提交'):
            store.finish_item(run['id'], 'acct', AssetProcessingResult(asset_id='image'), 'acct', 3)
        assert store.failures('acct')['image']['attempts'] == 1


def test_legacy_migration_reset_and_readonly_queries(settings):
    server = FakeImmich()
    worker = processor(settings, server)
    tracker = worker.failure_tracker(worker.clients[0])
    settings.state_dir.mkdir()
    legacy = {'failures': {'42': {'attempts': 3, 'permanently_failed': True, 'last_failed': '2026-01-01'}}}
    tracker.failure_file.write_text(json.dumps(legacy))
    assert tracker.is_permanently_failed('42')
    assert not (settings.state_dir / 'progress.sqlite3').exists()
    tracker.reset_failures()
    assert tracker.failure_file.exists()
    assert not tracker.failures
    with writer_lock(settings.state_dir), TaskStore(settings.state_dir):
        pass
    assert not tracker.failures
    worker.close()


def test_migration_failure_does_not_partially_import(settings):
    settings.state_dir.mkdir()
    (settings.state_dir / 'failures-a.json').write_text(json.dumps({'failures': {'x': {
        'attempts': 2, 'last_failed': 'date', 'permanently_failed': False}}}))
    bad = settings.state_dir / 'failures-b.json'
    bad.write_text('{bad')
    with writer_lock(settings.state_dir):
        with pytest.raises(json.JSONDecodeError):
            TaskStore(settings.state_dir)
        db = sqlite3.connect(settings.state_dir / 'progress.sqlite3')
        assert db.execute('SELECT COUNT(*) FROM failures').fetchone()[0] == 0
        db.close()
        bad.write_text('{"failures": {}}')
        with TaskStore(settings.state_dir) as store:
            assert store.failures('a')['x']['attempts'] == 2


def test_unknown_database_version_is_never_recreated(settings):
    settings.state_dir.mkdir()
    db = sqlite3.connect(settings.state_dir / 'progress.sqlite3')
    db.execute('PRAGMA user_version=99')
    db.close()
    with pytest.raises(RuntimeError, match='版本不兼容'):
        TaskStore(settings.state_dir)
    db = sqlite3.connect(settings.state_dir / 'progress.sqlite3')
    assert db.execute('PRAGMA user_version').fetchone()[0] == 99
    db.close()


def test_status_without_record_does_not_create_directory(settings, monkeypatch, capsys):
    monkeypatch.setattr('immich_tagger.main.Settings', lambda **_: settings)
    assert main(['--progress-status']) == 0
    value = json.loads(capsys.readouterr().out)
    assert value['running'] is None and value['live'] is False
    assert value['progress'] is None
    assert not settings.state_dir.exists()


def test_dry_run_does_not_change_existing_task(settings):
    server = FakeImmich([asset(i) for i in range(3)])
    interrupt_after_first(settings, server)
    before = TaskStore.snapshot(settings.state_dir)
    writes = len(server.writes)
    worker = processor(settings, server, dry_run=True)
    try:
        assert worker.run().planned == 2
        assert TaskStore.snapshot(settings.state_dir) == before
        assert len(server.writes) == writes
    finally:
        worker.close()


def test_full_filtered_pages_have_scan_progress(settings):
    settings.batch_size = 1
    server = FakeImmich([asset(i, isOffline=True) for i in range(3)])
    worker = processor(settings, server)
    try:
        assert worker.run().attempted == 0
        progress = worker.get_metrics()['progress']
        assert progress['scan_pages'] == 3
        assert progress['scan_records'] == 3
        assert progress['scan_skips']['非图片或不可用'] == 3
        assert worker._engine is None
    finally:
        worker.close()


def test_heartbeat_during_blocked_model_health_and_last_progress(settings, caplog):
    settings.log_progress_interval_seconds = 1
    entered, release, reported = threading.Event(), threading.Event(), threading.Event()
    class BlockingEngine(FakeEngine):
        def prepare(self):
            entered.set()
            assert release.wait(5)
            super().prepare()
    class Seen(logging.Handler):
        def emit(self, record):
            if '当前操作' in record.getMessage():
                reported.set()
    handler = Seen()
    logging.getLogger('progress').addHandler(handler)
    server = FakeImmich([asset(0)])
    worker = processor(settings, server, engine=BlockingEngine())
    caplog.set_level(logging.INFO, logger='progress')
    thread = threading.Thread(target=worker.run)
    try:
        thread.start()
        assert entered.wait(3)
        before = worker.get_metrics()['progress']['last_progress_at']
        response = asyncio.run(HealthServer(worker).health(None))
        assert response.status == 200
        assert reported.wait(3)
        assert worker.get_metrics()['progress']['last_progress_at'] == before
        assert worker.get_metrics()['progress']['operation'] == 'load_model'
    finally:
        release.set()
        thread.join(5)
        logging.getLogger('progress').removeHandler(handler)
        worker.close()
    assert not thread.is_alive()


def test_slow_operation_warning_throttled_and_time_zone(settings, monkeypatch, caplog):
    clock = [100.0]
    monkeypatch.setattr('immich_tagger.progress.time.monotonic', lambda: clock[0])
    progress = ProgressState(settings)
    caplog.set_level(logging.INFO, logger='progress')
    with progress.operation('inference'):
        clock[0] += 60
        progress.report()
        clock[0] += 10
        progress.report()
        clock[0] += 50
        progress.report()
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 2
    assert all('仍在等待' in r.getMessage() for r in warnings)
    assert progress.snapshot()['last_progress_at'] is None
    record = logging.LogRecord('test', logging.INFO, '', 0, 'secret-key', (), None)
    record.created = datetime(2026, 9, 27, 9, 21, tzinfo=timezone.utc).timestamp()
    line = ZonedFormatter('Asia/Shanghai', ['secret-key']).format(record)
    assert '17:21:00+08:00' in line and 'secret-key' not in line


def test_scheduler_resumes_once_without_extra_startup_run(settings):
    server = FakeImmich([asset(i) for i in range(2)])
    interrupt_after_first(settings, server)
    settings.run_on_startup = True
    worker = processor(settings, server)
    scheduler = Scheduler(worker)
    original = scheduler.run_once
    calls = []
    async def once(**kwargs):
        calls.append(kwargs)
        result = await original(**kwargs)
        scheduler.stop()
        return result
    scheduler.run_once = once
    try:
        assert asyncio.run(scheduler.start()) == 0
        assert len(calls) == 1
        assert worker.get_metrics()['last_run']['processed'] == 2
    finally:
        worker.close()


def test_authentication_error_keeps_queue_without_asset_failures(settings):
    import httpx
    server = FakeImmich([asset(i) for i in range(2)])
    original = server.handle
    def unauthorized(request):
        if request.url.path.endswith('/thumbnail'):
            return httpx.Response(401, json={'secret': 'test-key'})
        return original(request)
    server.handle = unauthorized
    worker = processor(settings, server)
    try:
        with pytest.raises(Exception, match='401'):
            worker.run()
        assert not worker.failure_tracker(worker.clients[0]).failures
        snapshot = TaskStore.snapshot(settings.state_dir)
        assert snapshot['progress']['remaining'] == 2
        assert 'test-key' not in json.dumps(snapshot)
    finally:
        worker.close()


def test_bad_image_is_per_asset_failure(settings):
    class BadImage(FakeEngine):
        def predict_tags(self, data):
            raise OSError('unsupported image')
    worker = processor(settings, FakeImmich([asset(0), asset(1)]), engine=BadImage())
    try:
        assert worker.run().failed == 2
        assert worker.get_metrics()['progress']['task_status'] == 'completed'
    finally:
        worker.close()


@pytest.mark.parametrize('field', ['log_progress_interval_seconds', 'log_slow_operation_seconds'])
def test_progress_intervals_require_positive_seconds(settings, field):
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        setattr(settings, field, 0)


def test_task_summary_retention_preserves_unfinished_queue(settings):
    with writer_lock(settings.state_dir), TaskStore(settings.state_dir) as store:
        pending, _ = store.begin('backfill', 'pending', None)
        generation = store.start_scan(pending['id'])
        store.save_page(pending['id'], generation, [('account', 'image', 0)], {}, 0)
        store.finish_scan(pending['id'], generation)
        for number in range(105):
            run, _ = store.begin('inference', str(number), None)
            generation = store.start_scan(run['id'])
            store.finish_scan(run['id'], generation)
            store.complete(run['id'])
        assert store.db.execute("SELECT COUNT(*) FROM runs WHERE status='completed'").fetchone()[0] == 100
        assert len(list(store.pending_items(pending['id']))) == 1


def test_corrupt_database_is_not_replaced(settings):
    settings.state_dir.mkdir()
    path = settings.state_dir / 'progress.sqlite3'
    original = b'corrupt sqlite data'
    path.write_bytes(original)
    with pytest.raises(sqlite3.DatabaseError):
        TaskStore(settings.state_dir)
    assert path.read_bytes() == original


def test_backfill_resume_without_model_or_download(settings):
    server = FakeImmich([asset(i, tags=[tag('blue_hair')]) for i in range(3)])
    worker = processor(settings, server)
    original = worker.process_asset
    def first(*args, **kwargs):
        result = original(*args, **kwargs)
        worker.cancelled.set()
        return result
    worker.process_asset = first
    assert worker.run(backfill=True).processed == 1
    worker.close()
    searches = sum(c[0] == 'POST' for c in server.calls)
    worker = processor(settings, server)
    try:
        assert worker.run(backfill=True).processed == 3
        assert worker._engine is None
        assert sum(c[0] == 'POST' for c in server.calls) == searches
        assert not any(c[1].endswith('thumbnail') for c in server.calls)
        assert all('auto:processed' not in {t['value'] for t in a['tags']} for a in server.assets.values())
    finally:
        worker.close()


def test_multi_account_resume_and_shared_limit(settings):
    settings = settings.model_copy(update={'immich_api_key': '', 'immich_api_keys': ['one', 'two']})
    servers = {'one': FakeImmich([asset(0)]), 'two': FakeImmich([asset(i) for i in range(5)])}
    engine = FakeEngine()
    def factory(settings, account, **options):
        return servers[account['api_key']].factory(settings, account, **options)
    worker = ImmichAutoTagger(settings, client_factory=factory, engine_factory=lambda _: engine)
    original = engine.predict_tags
    def predict(image):
        result = original(image)
        worker.cancelled.set()
        return result
    engine.predict_tags = predict
    assert worker.run(limit=3).processed == 1
    worker.close()
    counts = {name: sum(c[0] == 'POST' for c in server.calls) for name, server in servers.items()}
    worker = ImmichAutoTagger(settings, client_factory=factory, engine_factory=lambda _: FakeEngine())
    try:
        assert worker.run(limit=3).processed == 3
        assert worker.get_metrics()['progress']['session_completed'] == 2
        assert sum(bool(a['tags']) for a in servers['two'].assets.values()) == 2
        assert counts == {name: sum(c[0] == 'POST' for c in server.calls) for name, server in servers.items()}
    finally:
        worker.close()


def test_scheduler_resume_disabled_waits_without_running(settings):
    server = FakeImmich([asset(i) for i in range(2)])
    interrupt_after_first(settings, server)
    settings.resume_on_startup = False
    worker = processor(settings, server)
    scheduler = Scheduler(worker)
    original = worker.progress.phase
    def phase(name, message, **kwargs):
        original(name, message, **kwargs)
        if name == 'waiting':
            scheduler.stop()
    worker.progress.phase = phase
    try:
        assert asyncio.run(scheduler.start()) == 0
        assert worker._engine is None
        assert worker.get_metrics()['last_run']['processed'] == 1
        assert worker.get_metrics()['progress']['next_run_at']
    finally:
        worker.close()


def test_health_only_does_not_migrate_or_contact_immich(settings, monkeypatch):
    import signal
    from immich_tagger.main import parse_arguments, run_service
    settings.state_dir.mkdir()
    (settings.state_dir / 'failures-old.json').write_text('{"failures": {}}')
    server = FakeImmich()
    worker = processor(settings, server)
    async def check():
        callbacks = {}
        loop = asyncio.get_running_loop()
        monkeypatch.setattr(loop, 'add_signal_handler', lambda sig, fn: callbacks.update({sig: fn}))
        monkeypatch.setattr(loop, 'remove_signal_handler', lambda sig: None)
        async def start(_):
            callbacks[signal.SIGTERM]()
        async def stop(_):
            pass
        monkeypatch.setattr(HealthServer, 'start', start)
        monkeypatch.setattr(HealthServer, 'stop', stop)
        assert await run_service(worker, parse_arguments(['--mode', 'health-only'])) == 0
    try:
        asyncio.run(check())
        assert not server.calls and worker._engine is None
        assert not (settings.state_dir / 'progress.sqlite3').exists()
    finally:
        worker.close()


def test_retry_logs_are_chinese_and_omit_response_credentials(settings, monkeypatch, caplog):
    import httpx
    settings.max_retries = 1
    attempts = []
    def handle(request):
        attempts.append(1)
        if len(attempts) == 1:
            return httpx.Response(429, headers={'Retry-After': '2'}, text='test-key')
        return httpx.Response(200, json=[])
    monkeypatch.setattr('immich_tagger.immich_client.time.sleep', lambda _: None)
    from immich_tagger.immich_client import ImmichClient
    caplog.set_level(logging.WARNING)
    with ImmichClient(settings, transport=httpx.MockTransport(handle)) as client:
        client.get_all_tags()
    assert '第 1 次重试' in caplog.text
    assert '2 秒后重试' in caplog.text
    assert 'test-key' not in caplog.text


def test_finished_rate_excludes_scheduler_idle_time(settings, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr('immich_tagger.progress.time.monotonic', lambda: clock[0])
    progress = ProgressState(settings)
    clock[0] += 20  # scanning/model preparation
    progress.phase('processing', '开始处理')
    clock[0] += 10
    progress.update(session_completed=20)
    progress.finish()
    assert progress.snapshot()['assets_per_second'] == 2
    progress.phase('waiting', '等待下一轮')
    clock[0] += 3600
    assert progress.snapshot()['assets_per_second'] == 2
    assert progress.snapshot()['session_elapsed_seconds'] == 30
