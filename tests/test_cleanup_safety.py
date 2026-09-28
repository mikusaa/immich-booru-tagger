import json
from pathlib import Path

import pytest

from app.cleanup_queues import CleanupQueues
from app.cleanup_state import CleanupSafetyError
from app.english_cleanup import EnglishTagCleaner
from app.models import Asset
from app.processor import ImmichAutoTagger
from app.task_store import TaskStore
from tests.support.fakes import ALBUM, FakeImmich, asset, bilingual_asset, paths, tag


@pytest.fixture(autouse=True)
def no_async_jobs(monkeypatch):
    monkeypatch.setattr(CleanupQueues, "poll_interval", 0)
    monkeypatch.setattr(EnglishTagCleaner, "_wait_for_readback", lambda *a: None)
    monkeypatch.setattr(EnglishTagCleaner, "_wait_between_removals", lambda *a: None)


def cleaner(settings, server, *, maintenance=True, dry_run=False, scope="catalog"):
    settings.cleanup_admin_api_key = "admin-key"
    return EnglishTagCleaner(settings, cleanup_scope=scope, maintenance=maintenance,
                             dry_run=dry_run, confirm_cleanup_english=not dry_run, client_factory=server.factory)


def lose_tags_during_flush(settings, server):
    handle = server.handle
    def losing(request):
        response = handle(request)
        if (request.method == "PUT" and request.url.path.endswith("/metadataExtraction")
                and not json.loads(request.content)["isPaused"]):
            server.assets["0"]["tags"] = []
        return response
    server.handle = losing
    worker = cleaner(settings, server)
    try:
        with pytest.raises(CleanupSafetyError, match="保留标签缺失") as raised:
            worker.run(limit=1)
        assert "属性/蓝发（tag-属性/蓝发）" in str(raised.value)
        assert "manual（tag-manual）" in str(raised.value)
    finally:
        worker.close()
        server.handle = handle
    assert paths(server) == set()
    assert not (settings.state_dir / "cleanup-queues.json").exists()
    pending = settings.state_dir / "cleanup-pending.json"
    assert pending.exists()
    return pending


@pytest.mark.parametrize("change", ["same", "limit", "scope", "album", "failure_limit", "dry_run"])
def test_lost_tags_cannot_be_skipped_on_resume_or_rescan(settings, change):
    server = FakeImmich([bilingual_asset(0), bilingual_asset(1)])
    pending = lose_tags_during_flush(settings, server)
    before = pending.read_bytes()
    checkpoint = TaskStore.snapshot(settings.state_dir)
    if change == "album":
        settings.immich_include_album_ids = [ALBUM]
        server.albums[ALBUM] = {"assets": [{"id": "1"}]}
    if change == "failure_limit":
        settings.failure_timeout = 1
    worker = cleaner(settings, server, scope="recorded" if change == "scope" else "catalog",
                     dry_run=change == "dry_run")
    server.calls.clear()
    try:
        with pytest.raises(CleanupSafetyError, match="保留标签缺失"):
            worker.run(limit=2 if change == "limit" else 1)
        assert pending.read_bytes() == before
        assert TaskStore.snapshot(settings.state_dir) == checkpoint
        assert all(method == "GET" for method, _, _ in server.calls)
        assert not any(path == "/api/search/metadata" for _, path, _ in server.calls)
    finally:
        worker.close()


@pytest.mark.parametrize("backfill", [False, True])
def test_regular_writes_block_until_pending_baseline_is_verified(settings, backfill):
    server = FakeImmich([bilingual_asset(0)])
    lose_tags_during_flush(settings, server)
    worker = ImmichAutoTagger(settings, client_factory=server.factory)
    server.calls.clear()
    try:
        with pytest.raises(RuntimeError, match="cleanup-pending.json"):
            worker.run(backfill=backfill)
        assert not server.calls
    finally:
        worker.close()


def test_manual_protected_tag_recovery_allows_resume_and_archives_baseline(settings):
    server = FakeImmich([bilingual_asset(0)])
    pending = lose_tags_during_flush(settings, server)
    baseline = json.loads(pending.read_text())
    # Simulate repairing the server and restoring ONLY the exact protected tags.
    server.assets["0"]["tags"] = [tag(baseline["tags"][tid]) for tid in baseline["protected"]]
    worker = cleaner(settings, server)
    server.calls.clear()
    try:
        worker.run(limit=1)
        assert TaskStore.snapshot(settings.state_dir)["progress"]["task_status"] == "completed"
        assert not pending.exists()
        assert all(m == "GET" for m, _, _ in server.calls)
        history = json.loads((settings.state_dir / "cleanup-baselines.jsonl").read_text())
        assert history["tags"] == baseline["tags"]
        assert history["outcome"] == "recovered"
        assert history["remaining_english"] == []
    finally:
        worker.close()


@pytest.mark.parametrize("fault", ["json", "server", "account", "protected", "planned"])
def test_invalid_pending_baseline_blocks_before_any_remote_calls(settings, fault):
    server = FakeImmich([bilingual_asset(0)])
    pending = lose_tags_during_flush(settings, server)
    state = json.loads(pending.read_text())
    if fault == "json":
        pending.write_text("{")
    else:
        state[fault] = [] if fault == "protected" else ({} if fault == "planned" else "foreign")
        pending.write_text(json.dumps(state))
    worker = cleaner(settings, server)
    server.calls.clear()
    try:
        with pytest.raises(CleanupSafetyError, match="基线损坏或服务/账号不匹配"):
            worker.run()
        assert not server.calls
        assert pending.exists()
    finally:
        worker.close()


@pytest.mark.parametrize("maintenance", [False, True])
def test_full_baseline_is_durable_before_first_delete(settings, maintenance):
    server = FakeImmich([bilingual_asset(0)])
    original_tags = paths(server)
    handle = server.handle
    def checked(request):
        if request.method == "DELETE":
            raw = (settings.state_dir / "cleanup-pending.json").read_text()
            assert "test-key" not in raw and "admin-key" not in raw
            baseline = json.loads(raw)
            assert set(baseline["tags"].values()) == original_tags
            assert {baseline["tags"][tid] for tid in baseline["protected"]} == original_tags - {"blue_hair"}
        return handle(request)
    server.handle = checked
    worker = cleaner(settings, server, maintenance=maintenance)
    try:
        assert worker.run().processed == 1
        assert not (settings.state_dir / "cleanup-pending.json").exists()
        assert json.loads((settings.state_dir / "cleanup-baselines.jsonl").read_text())["tags"]
    finally:
        worker.close()


@pytest.mark.parametrize("failure", ["snapshot", "archive"])
def test_disk_write_failure_keeps_task_uncommitted(settings, monkeypatch, failure):
    server = FakeImmich([bilingual_asset(0)])
    original_open = Path.open
    def failed(path, *args, **kwargs):
        name = "cleanup-pending.tmp" if failure == "snapshot" else "cleanup-baselines.jsonl"
        if path.name == name:
            raise OSError("disk full")
        return original_open(path, *args, **kwargs)
    monkeypatch.setattr(Path, "open", failed)
    worker = cleaner(settings, server)
    try:
        with pytest.raises(CleanupSafetyError):
            worker.run()
        assert TaskStore.snapshot(settings.state_dir)["progress"]["completed"] == 0
        if failure == "snapshot":
            assert not any(m == "DELETE" for m, _, _ in server.calls)
        else:
            assert (settings.state_dir / "cleanup-pending.json").exists()
        assert not (settings.state_dir / "cleanup-queues.json").exists()
    finally:
        worker.close()


def test_sidecar_backlog_and_each_delete_are_drained_separately(settings):
    server = FakeImmich([bilingual_asset(0)])
    server.assets["0"]["tags"].append(tag("general/blue_hair"))
    sidecar = server.queues["sidecar"]["statistics"]
    sidecar["waiting"] = 2  # Existing jobs must finish before the first deletion.
    drains = []
    handle = server.handle
    def jobs(request):
        if request.method == "DELETE":
            assert sidecar["waiting"] == 0
            assert all(q["isPaused"] for q in server.queues.values())
            sidecar["waiting"] += 1
        response = handle(request)
        if (request.method == "PUT" and request.url.path.endswith("/sidecar")
                and not json.loads(request.content)["isPaused"]):
            if sidecar["waiting"]:
                assert server.queues["metadataExtraction"]["isPaused"]
            drains.append(sidecar["waiting"])
            sidecar["waiting"] = 0
        return response
    server.handle = jobs
    worker = cleaner(settings, server)
    try:
        assert worker.run().processed == 1
        assert drains[:3] == [2, 1, 1]
    finally:
        worker.close()


@pytest.mark.parametrize("maintenance", [False, True])
def test_initially_empty_asset_remains_a_skip_without_remote_writes(settings, maintenance):
    server = FakeImmich([asset(0)])
    worker = cleaner(settings, server, maintenance=maintenance)
    try:
        assert worker.process_asset(worker.clients[0], Asset(id="0", type="IMAGE")).status == "skipped"
        assert not server.writes
        assert not settings.state_dir.exists()
    finally:
        worker.close()


def test_unreadable_pending_asset_is_not_silently_resolved(settings):
    server = FakeImmich([bilingual_asset(0)])
    pending = lose_tags_during_flush(settings, server)
    del server.assets["0"]
    worker = cleaner(settings, server)
    try:
        with pytest.raises(CleanupSafetyError, match="无法复核图片"):
            worker.run()
        assert pending.exists()
    finally:
        worker.close()
