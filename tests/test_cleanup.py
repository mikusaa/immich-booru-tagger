import json

import httpx
import pytest

from conftest import ALBUM, LIBRARY_A, LIBRARY_B, FakeImmich, asset, tag
from immich_tagger.cleanup import EnglishTagCleaner
from immich_tagger.immich_client import ImmichAPIError
from immich_tagger.main import main, parse_arguments
from immich_tagger.processor import ImmichAutoTagger, StateWriteError
from immich_tagger.state import account_scope, record_assignment
from immich_tagger.task_store import TaskStore


def bilingual_asset(number, **values):
    return asset(number, tags=list(map(tag, ["blue_hair", "属性/蓝发", "auto:processed", "manual"])), **values)


def cleaner(settings, server, *, scope="catalog", dry_run=False, **kwargs):
    def no_engine(_):
        raise AssertionError("Cleanup must not load a model")
    worker = EnglishTagCleaner(settings, cleanup_scope=scope, dry_run=dry_run,
                               confirm_cleanup_english=not dry_run, client_factory=server.factory,
                               engine_factory=no_engine, **kwargs)
    worker._wait_for_readback = lambda attempt: None
    worker._wait_between_removals = lambda: None
    return worker


def maintenance_cleaner(settings, server, *, scope="catalog"):
    settings.cleanup_admin_api_key = "admin-key"
    return cleaner(settings, server, scope=scope, maintenance=True)


def record(settings, asset_id="0", paths=("blue_hair",), account=None):
    account = account or settings.get_library_config()[0]
    record_assignment(settings.state_dir, account_scope(settings, account), asset_id, list(paths))


def paths(server, asset_id="0"):
    return {t["value"] for t in server.assets[asset_id]["tags"]}


@pytest.mark.parametrize("scope", ["recorded", "catalog"])
def test_cleanup_modes_and_protected_tags(settings, tmp_path, scope):
    settings.processed_tag_name = "blue_eyes"  # Even a dictionary entry used as marker is protected.
    settings.tag_language_mode = "english"  # Explicit maintenance loads its own dictionary.
    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps({"tags": {
        "blue_hair": {"kind": "general", "zh": "蓝发"},
        "blue_eyes": {"kind": "general", "zh": "蓝眼"},
        "long_hair": {"kind": "general", "zh": "长发"},
        "short_hair": {"kind": "general", "zh": "短发"},
        "general": {"kind": "rating", "zh": "全年龄"},
        "hatsune_miku": {"kind": "character", "zh": "初音未来"},
        "disabled": {"kind": "general", "zh": ""},
    }}))
    settings.translation_file = catalog
    originals = {"blue_hair", "属性/蓝发", "general/blue_hair", "long_hair", "属性/长发",
                 "blue_eyes", "属性/蓝眼", "short_hair", "zh/属性/短发", "zh/blue_hair",
                 "general", "评级/全年龄", "character/hatsune_miku", "角色/初音未来",
                 "disabled", "unknown", "手工", "auto:processed"}
    server = FakeImmich([asset(0, tags=list(map(tag, originals)))])
    record(settings, paths=originals - {"long_hair"})
    worker = cleaner(settings, server, scope=scope)
    try:
        assert worker.run().processed == 1
        expected = {"blue_hair", "general/blue_hair", "general", "character/hatsune_miku"}
        if scope == "catalog":
            expected.add("long_hair")
        assert paths(server) == originals - expected
        assert set(server.tags) == originals  # Global tag objects are untouched.
        deletes = [c for c in server.writes if c[0] == "DELETE"]
        assert len(deletes) == len(expected)
        assert all(c[1].startswith("/api/tags/") and c[1].endswith("/assets") for c in deletes)
        assert all(c[2] == {"ids": ["0"]} for c in deletes)
        assert not any(c[1].endswith("/thumbnail") for c in server.calls)
        assert worker._engine is None
        journal = [json.loads(line) for line in (settings.state_dir / "cleanup.jsonl").read_text().splitlines()]
        assert len(journal) == 2 * len(expected)
        for prepared, confirmed in zip(journal[::2], journal[1::2]):
            assert (prepared["status"], confirmed["status"]) == ("prepared", "confirmed")
            assert prepared["operation_id"] == confirmed["operation_id"]
        assert {e["path"] for e in journal} == expected
        assert "test-key" not in (settings.state_dir / "cleanup.jsonl").read_text()
        writes = len(server.writes)
        assert worker.run().processed == 0
        assert len(server.writes) == writes
    finally:
        worker.close()


@pytest.mark.parametrize("scope", ["recorded", "catalog"])
def test_preview_has_no_remote_or_state_writes(settings, scope, caplog):
    if scope == "recorded":
        record(settings)
    before = {p.name: p.read_bytes() for p in settings.state_dir.glob("*")}
    server = FakeImmich([bilingual_asset(0)])
    worker = cleaner(settings, server, scope=scope, dry_run=True)
    caplog.set_level("INFO")
    try:
        assert worker.run().planned == 1
        assert not server.writes
        assert {p.name: p.read_bytes() for p in settings.state_dir.glob("*")} == before
        if scope == "catalog":
            assert not settings.state_dir.exists()
        assert any("计划移除：blue_hair｜已有中文：属性/蓝发" in r.message for r in caplog.records)
    finally:
        worker.close()


def test_recorded_evidence_is_account_specific_and_does_not_search(settings):
    server = FakeImmich([bilingual_asset(0), bilingual_asset(1), bilingual_asset(2)])
    record(settings, "0")
    record(settings, "1", account={"api_key": "different-key"})
    record(settings, "2", paths=["manual"])
    worker = cleaner(settings, server, scope="recorded")
    try:
        assert worker.run().processed == 1
        assert "blue_hair" not in paths(server)
        assert "blue_hair" in paths(server, "1") & paths(server, "2")
        assert not any(c[1] == "/api/search/metadata" for c in server.calls)
    finally:
        worker.close()


@pytest.mark.parametrize("content", [None, "{broken", '{"account":"x","asset_id":"0","added":"blue_hair"}'])
def test_missing_or_invalid_journal_stops_before_remote_writes(settings, content):
    if content is not None:
        settings.state_dir.mkdir()
        (settings.state_dir / "assignments.jsonl").write_text(content)
    server = FakeImmich([bilingual_asset(0)])
    worker = cleaner(settings, server, scope="recorded")
    try:
        with pytest.raises(ValueError, match="assignments.jsonl"):
            worker.run()
        assert not server.calls
        assert "blue_hair" in paths(server)
    finally:
        worker.close()


@pytest.mark.parametrize("scope", ["recorded", "catalog"])
def test_cleanup_scope_and_unavailable_assets(settings, scope):
    settings.immich_include_library_ids = [LIBRARY_A, LIBRARY_B]
    settings.immich_exclude_library_ids = [LIBRARY_B]
    settings.immich_include_album_ids = [ALBUM]
    server = FakeImmich([bilingual_asset(0, libraryId=LIBRARY_A), bilingual_asset(1, libraryId=LIBRARY_B),
                        bilingual_asset(2, libraryId=LIBRARY_A), bilingual_asset(3, libraryId=LIBRARY_A, type="VIDEO"),
                        bilingual_asset(4, libraryId=LIBRARY_A, isOffline=True),
                        bilingual_asset(5, libraryId=LIBRARY_A, isTrashed=True), bilingual_asset(6)])
    server.ignore_filters = True
    server.albums[ALBUM] = {"assets": [{"id": str(i)} for i in (0, 1, 3, 4, 5, 6)]}
    for i in range(8):
        record(settings, str(i))
    worker = cleaner(settings, server, scope=scope)
    try:
        assert worker.run().processed == 1
        assert "blue_hair" not in paths(server)
        for i in range(1, 7):
            assert "blue_hair" in paths(server, str(i))
    finally:
        worker.close()


@pytest.mark.parametrize("scope", ["recorded", "catalog"])
def test_resume_cleanup_without_rescan_and_without_scheduler_resume(settings, scope):
    server = FakeImmich([bilingual_asset(i) for i in range(4)])
    for i in range(4):
        record(settings, str(i))
    worker = cleaner(settings, server, scope=scope)
    original = worker.clients[0].untag_single_asset
    def remove(*args):
        original(*args)
        worker.cancelled.set()
    worker.clients[0].untag_single_asset = remove
    try:
        assert worker.run(limit=3).processed == 1
    finally:
        worker.close()
    saved = TaskStore.snapshot(settings.state_dir)
    searches = sum(c[1] == "/api/search/metadata" for c in server.calls)
    scheduled = ImmichAutoTagger(settings, client_factory=server.factory)
    try:
        assert not scheduled.has_pending_run()
    finally:
        scheduled.close()
    worker = cleaner(settings, server, scope=scope)
    try:
        assert worker.has_pending_run(limit=3)
        assert worker.run(limit=3).processed == 3
        assert worker.get_metrics()["progress"]["resumed"]
        assert worker.get_metrics()["progress"]["run_id"] == saved["progress"]["run_id"]
        assert sum(c[1] == "/api/search/metadata" for c in server.calls) == searches
        assert "blue_hair" in paths(server, "3")
    finally:
        worker.close()


@pytest.mark.parametrize("change", ["translation", "album", "offline", "deleted"])
def test_resume_rechecks_chinese_and_scope(settings, change):
    settings.immich_include_album_ids = [ALBUM]
    server = FakeImmich([bilingual_asset(i) for i in range(2)])
    server.albums[ALBUM] = {"assets": [{"id": "0"}, {"id": "1"}]}
    worker = cleaner(settings, server)
    original = worker.clients[0].untag_single_asset
    def remove(*args):
        original(*args)
        worker.cancelled.set()
    worker.clients[0].untag_single_asset = remove
    try:
        worker.run()
    finally:
        worker.close()
    if change == "translation":
        server.assets["1"]["tags"] = [tag("blue_hair")]
    elif change == "album":
        server.albums[ALBUM] = {"assets": [{"id": "0"}]}
    elif change == "offline":
        server.assets["1"]["isOffline"] = True
    else:
        del server.assets["1"]
    worker = cleaner(settings, server)
    try:
        assert worker.run().skipped == 1
        assert len(server.writes) == 1
    finally:
        worker.close()


def test_rechecks_translation_immediately_before_delete(settings):
    server = FakeImmich([bilingual_asset(0)])
    worker = cleaner(settings, server)
    original = worker.clients[0].get_asset
    calls = 0
    def get_asset(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            server.assets["0"]["tags"] = [tag("blue_hair")]
        return original(*args, **kwargs)
    worker.clients[0].get_asset = get_asset
    try:
        assert worker.run().skipped == 1
        assert not server.writes
    finally:
        worker.close()


def test_removal_readback_failure_retries_on_next_explicit_run(settings):
    server = FakeImmich([bilingual_asset(0)])
    server.drop_removal = True
    worker = cleaner(settings, server)
    try:
        assert worker.run().failed == 1
        assert worker.failure_tracker(worker.clients[0]).failures["0"]["attempts"] == 1
        normal = ImmichAutoTagger(settings, client_factory=server.factory)
        try:
            assert not normal.failure_tracker(normal.clients[0]).failures
        finally:
            normal.close()
        server.drop_removal = False
        assert worker.run().processed == 1
        assert not worker.failure_tracker(worker.clients[0]).failures
        assert worker.run().attempted == 0
        assert len(server.writes) == 2
    finally:
        worker.close()


def test_journal_write_failure_before_delete_stops_run(settings, monkeypatch):
    server = FakeImmich([bilingual_asset(0)])
    def fail(*args, **kwargs):
        raise OSError("disk full")
    monkeypatch.setattr("immich_tagger.cleanup.record_cleanup", fail)
    worker = cleaner(settings, server)
    try:
        with pytest.raises(StateWriteError):
            worker.run()
        assert not server.writes
        assert TaskStore.snapshot(settings.state_dir)["progress"]["task_status"] == "paused"
        assert not worker.failure_tracker(worker.clients[0]).failures
    finally:
        worker.close()


def test_cleanup_default_preview_and_explicit_confirmation(settings):
    args = parse_arguments(["--mode", "cleanup-english"])
    assert args.dry_run and args.cleanup_scope == "recorded"
    args = parse_arguments(["--mode", "cleanup-english", "--confirm-cleanup-english"])
    assert not args.dry_run
    with pytest.raises(ValueError, match="confirm-cleanup-english"):
        EnglishTagCleaner(settings, dry_run=False)
    for flags in (["--confirm-cleanup-english"], ["--cleanup-scope", "catalog"],
                  ["--mode", "cleanup-english", "--dry-run", "--confirm-cleanup-english"]):
        with pytest.raises(SystemExit):
            parse_arguments(flags)


@pytest.mark.parametrize("confirm", [False, True])
def test_cli_selects_cleanup_and_preview_safety(settings, monkeypatch, confirm):
    server = FakeImmich([bilingual_asset(0)])
    monkeypatch.setattr("immich_tagger.main.Settings", lambda **_: settings)
    factory = EnglishTagCleaner
    monkeypatch.setattr(factory, "_wait_for_readback", lambda *args: None)
    monkeypatch.setattr(factory, "_wait_between_removals", lambda *args: None)
    monkeypatch.setattr("immich_tagger.cleanup.EnglishTagCleaner",
                        lambda *a, **kw: factory(*a, **kw, client_factory=server.factory))
    async def start(_):
        pass
    monkeypatch.setattr("immich_tagger.health_server.HealthServer.start", start)
    flags = ["--mode", "cleanup-english", "--cleanup-scope", "catalog"]
    if confirm:
        flags += ["--confirm-cleanup-english"]
    assert main(flags) == 0
    assert bool(server.writes) is confirm
    assert ("blue_hair" in paths(server)) is not confirm


def test_client_removal_guard_and_readback(settings):
    server = FakeImmich([bilingual_asset(0)])
    with server.factory(settings, settings.get_library_config()[0], dry_run=True) as client:
        with pytest.raises(ImmichAPIError, match="Dry run"):
            client.untag_single_asset("0", "tag-blue_hair")
    assert not server.writes
    server.drop_removal = True
    with server.factory(settings, settings.get_library_config()[0]) as client:
        with pytest.raises(ImmichAPIError, match="removal incomplete"):
            client.untag_single_asset("0", "tag-blue_hair")


def test_maintenance_cleanup_coordinates_queues_and_restores_state(settings):
    server = FakeImmich([bilingual_asset(0)])
    worker = maintenance_cleaner(settings, server)
    try:
        result = worker.run()
        assert result.processed == 1
        assert paths(server) == {"属性/蓝发", "auto:processed", "manual"}
        assert all(not queue["isPaused"] for queue in server.queues.values())
        assert not (settings.state_dir / "cleanup-queues.json").exists()
        assert any(call[1].startswith("/api/queues/") for call in server.writes)
    finally:
        worker.close()


def test_maintenance_rejects_missing_admin_key(settings):
    server = FakeImmich([bilingual_asset(0)])
    with pytest.raises(ValueError, match="CLEANUP_ADMIN_API_KEY"):
        EnglishTagCleaner(settings, cleanup_scope="catalog", dry_run=False,
                          confirm_cleanup_english=True, maintenance=True,
                          client_factory=server.factory)


@pytest.mark.parametrize("status", [401, 403])
def test_cleanup_permission_failure_stops_whole_run(settings, status):
    server = FakeImmich([bilingual_asset(0), bilingual_asset(1)])
    handle = server.handle
    def denied(request):
        if request.method == "DELETE":
            return httpx.Response(status)
        return handle(request)
    server.handle = denied
    worker = cleaner(settings, server)
    try:
        with pytest.raises(ImmichAPIError):
            worker.run()
        assert not worker.failure_tracker(worker.clients[0]).failures
        assert TaskStore.snapshot(settings.state_dir)["progress"]["task_status"] == "paused"
    finally:
        worker.close()


def test_partial_removal_only_retries_remaining_tag(settings):
    server = FakeImmich([asset(0, tags=list(map(tag, ["blue_hair", "属性/蓝发", "general", "评级/全年龄"])))])
    handle = server.handle
    failed = False
    def partial(request):
        nonlocal failed
        if request.method == "DELETE" and request.url.path == "/api/tags/tag-general/assets" and not failed:
            failed = True
            return httpx.Response(500)
        return handle(request)
    server.handle = partial
    worker = cleaner(settings, server)
    try:
        assert worker.run().failed == 1
        assert paths(server) == {"属性/蓝发", "general", "评级/全年龄"}
        assert worker.run().processed == 1
        assert paths(server) == {"属性/蓝发", "评级/全年龄"}
        assert len([c for c in server.calls if c[0] == "DELETE" and c[1].endswith("tag-blue_hair/assets")]) == 1
    finally:
        worker.close()


@pytest.mark.parametrize("change", ["scope", "record", "translation"])
def test_cleanup_changed_evidence_or_policy_rescans(settings, tmp_path, change):
    server = FakeImmich([bilingual_asset(i) for i in range(2)])
    record(settings, "0")
    record(settings, "1")
    worker = cleaner(settings, server, scope="recorded")
    original = worker.clients[0].untag_single_asset
    def remove(*args):
        original(*args)
        worker.cancelled.set()
    worker.clients[0].untag_single_asset = remove
    try:
        worker.run()
    finally:
        worker.close()
    previous = TaskStore.snapshot(settings.state_dir)["progress"]["run_id"]
    if change == "record":
        (settings.state_dir / "assignments.jsonl").write_text("")
    elif change == "translation":
        override = tmp_path / "overrides.json"
        override.write_text(json.dumps({"blue_hair": {"kind": "general", "zh": ""}}))
        settings.translation_overrides = override
    worker = cleaner(settings, server, scope="catalog" if change == "scope" else "recorded")
    try:
        assert not worker.has_pending_run()
        assert worker.run().processed == (1 if change == "scope" else 0)
        assert worker.get_metrics()["progress"]["run_id"] != previous
        assert ("blue_hair" in paths(server, "1")) is (change != "scope")
    finally:
        worker.close()


def test_failure_limit_and_cli_reset_include_cleanup(settings, monkeypatch, capsys):
    settings.failure_timeout = 1
    server = FakeImmich([bilingual_asset(0)])
    server.drop_removal = True
    worker = cleaner(settings, server)
    try:
        assert worker.run().failed == 1
        server.drop_removal = False
        assert worker.run().processed == 0
    finally:
        worker.close()
    monkeypatch.setattr("immich_tagger.main.Settings", lambda **_: settings)
    assert main(["--show-failures"]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert "cleanup-catalog" in summary["User_1"] and "cleanup-recorded" in summary["User_1"]
    assert main(["--reset-failure", "0"]) == 0
    worker = cleaner(settings, server)
    try:
        assert worker.run().processed == 1
    finally:
        worker.close()


@pytest.mark.parametrize("reappear_always", [False, True])
def test_whole_asset_readback_retries_async_tag_reappearance(settings, reappear_always):
    settings.max_retries = 2
    server = FakeImmich([bilingual_asset(0)])
    worker = cleaner(settings, server)
    attempts = []
    def asynchronous_write(attempt):
        attempts.append(attempt)
        if attempt == 0 or reappear_always:
            server.assets["0"]["tags"].append(tag("blue_hair"))
    worker._wait_for_readback = asynchronous_write
    try:
        result = worker.run()
        if reappear_always:
            assert result.failed == 1 and result.processed == 0
            assert attempts == [0, 1, 2]
            assert "blue_hair" in paths(server)
        else:
            assert result.processed == 1 and not result.failed
            assert attempts == [0, 1, 1]
            assert "blue_hair" not in paths(server)
        assert len(server.writes) == (3 if reappear_always else 2)
        assert {"属性/蓝发", "auto:processed", "manual"}.issubset(paths(server))
    finally:
        worker.close()


def test_second_delayed_read_catches_late_metadata_reappearance(settings):
    settings.max_retries = 1
    server = FakeImmich([bilingual_asset(0)])
    worker = cleaner(settings, server)
    reads = []
    def late_change(attempt):
        reads.append(attempt)
        if len(reads) == 2:
            server.assets["0"]["tags"].append(tag("blue_hair"))
    worker._wait_for_readback = late_change
    try:
        assert worker.run().processed == 1
        assert reads == [0, 0, 1, 1]
        assert len(server.writes) == 2
        assert "blue_hair" not in paths(server)
    finally:
        worker.close()


def test_readback_retry_rechecks_chinese_before_deleting_again(settings):
    settings.max_retries = 2
    server = FakeImmich([bilingual_asset(0)])
    worker = cleaner(settings, server)
    def external_change(attempt):
        server.assets["0"]["tags"] = [tag("blue_hair"), tag("auto:processed"), tag("manual")]
    worker._wait_for_readback = external_change
    try:
        assert worker.run().skipped == 1
        assert len(server.writes) == 1
        assert "blue_hair" in paths(server)
    finally:
        worker.close()


def test_http_success_with_failed_bulk_item_is_not_cleanup_success(settings):
    server = FakeImmich([bilingual_asset(0)])
    handle = server.handle
    def rejected(request):
        if request.method == "DELETE":
            return httpx.Response(200, json=[{"id": "0", "success": False, "error": "no_permission"}])
        return handle(request)
    server.handle = rejected
    worker = cleaner(settings, server)
    try:
        assert worker.run().failed == 1
        assert "blue_hair" in paths(server)
        records = [json.loads(line) for line in (settings.state_dir / "cleanup.jsonl").read_text().splitlines()]
        assert {r["status"] for r in records} == {"prepared"}
    finally:
        worker.close()
