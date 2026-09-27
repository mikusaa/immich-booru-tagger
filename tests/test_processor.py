import json

import pytest

from immich_tagger.processor import ImmichAutoTagger
from immich_tagger.models import TagPrediction
from immich_tagger.state import writer_lock
from immich_tagger.tagging_engine import TaggingEngineError
from conftest import FakeEngine, FakeImmich, asset, tag


def processor(settings, server, *, dry_run=False, engine=None):
    return ImmichAutoTagger(settings, client_factory=server.factory, dry_run=dry_run,
                            engine_factory=lambda _: engine or FakeEngine())


@pytest.mark.parametrize("legacy", [False, True])
def test_snapshot_before_writes_no_pagination_loss_and_idempotent(settings, legacy):
    settings.batch_size = 2
    server = FakeImmich([asset(i) for i in range(7)], legacy=legacy)
    server.assets["0"]["tags"] = [tag("manual")]
    worker = processor(settings, server)
    try:
        assert worker.run().processed == 7
        searches = [i for i, c in enumerate(server.calls) if c[0] == "POST"]
        first_write = next(i for i, c in enumerate(server.calls) if c[0] == "PUT")
        assert max(searches) < first_write
        for item in server.assets.values():
            paths = {t["value"] for t in item["tags"]}
            assert {"blue_hair", "hatsune_miku", "auto:processed"}.issubset(paths)
            assert any(p.startswith("属性/") for p in paths)
            assert any(p.startswith("角色/") for p in paths)
            assert not any(p.startswith("zh/") for p in paths)
        assert "manual" in {t["value"] for t in server.assets["0"]["tags"]}
        writes = len(server.writes)
        assert worker.run().attempted == 0
        assert len(server.writes) == writes
        assert worker.get_metrics()["last_run"]["attempted"] == 0
        journal = [json.loads(line) for line in (settings.state_dir / "assignments.jsonl").read_text().splitlines()]
        assert len(journal) == 7
        assert "test-key" not in (settings.state_dir / "assignments.jsonl").read_text()
    finally:
        worker.close()


@pytest.mark.parametrize("kwargs,expected", [({"limit": 3}, 3), ({"single": True}, 2),
                                             ({"max_cycles": 2, "limit": 3}, 3)])
def test_total_limit_across_accounts(settings, kwargs, expected):
    settings = settings.model_copy(update={"immich_api_key": "", "immich_api_keys": ["one", "two"], "batch_size": 2})
    servers = {"one": FakeImmich([asset(0)]), "two": FakeImmich([asset(i) for i in range(1, 5)])}
    def factory(settings, account, **options):
        return servers[account["api_key"]].factory(settings, account, **options)
    worker = ImmichAutoTagger(settings, client_factory=factory, engine_factory=lambda _: FakeEngine())
    try:
        assert worker.run(**kwargs).attempted == expected
    finally:
        worker.close()


@pytest.mark.parametrize("english", [False, True])
def test_dry_run_no_writes_or_state_and_no_marker(settings, english, caplog):
    settings.english_tags_enabled = english
    caplog.set_level("INFO")

    class Engine(FakeEngine):
        def predict_tags(self, image):
            return super().predict_tags(image) + [TagPrediction(name="unknown_tag", confidence=.8)]

    server = FakeImmich([asset(0)])
    worker = processor(settings, server, dry_run=True, engine=Engine())
    try:
        assert worker.run(limit=1).planned == 1
        assert not server.writes
        assert not settings.state_dir.exists()
        assert not server.tags
        preview = next(record.message for record in caplog.records if "计划添加：" in record.message)
        paths, _ = json.JSONDecoder().raw_decode(preview.split("计划添加：", 1)[1])
        assert "属性/蓝发" in paths
        assert ("blue_hair" in paths) is english
        assert ("hatsune_miku" in paths) is english
        assert "unknown_tag" in paths
        assert "auto:processed" not in paths
    finally:
        worker.close()


@pytest.mark.parametrize("english,chinese", [(True, True), (False, True), (True, False)])
def test_selected_languages_missing_translations_and_existing_tags(settings, tmp_path, english, chinese):
    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps({"tags": {
        "blue_hair": {"zh": "蓝发", "kind": "general"},
        "hatsune_miku": {"zh": "", "kind": "character"},
        "general": {"zh": "全年龄", "kind": "rating"},
    }}))
    settings.translation_file = catalog
    settings.english_tags_enabled = english
    settings.translations_enabled = chinese

    class Engine(FakeEngine):
        def predict_tags(self, image):
            return super().predict_tags(image) + [
                TagPrediction(name="unknown_tag", confidence=.8),
                TagPrediction(name="general", confidence=.9, kind="rating"),
            ]

    existing = {"preexisting_english_tag", "手工标签"}
    server = FakeImmich([asset(0, tags=list(map(tag, existing)))])
    worker = processor(settings, server, engine=Engine())
    try:
        assert worker.run().processed == 1
        expected = existing | {"auto:processed", "hatsune_miku", "unknown_tag"}
        if english:
            expected |= {"blue_hair", "general"}
        if chinese:
            expected |= {"属性/蓝发", "评级/全年龄"}
            assert set(worker.catalog.missing) == {"hatsune_miku", "unknown_tag"}
        else:
            assert worker._catalog is None
        assert {t["value"] for t in server.assets["0"]["tags"]} == expected
        journal = json.loads((settings.state_dir / "assignments.jsonl").read_text())
        assert set(journal["added"]) == expected - existing - {"auto:processed"}
        writes = len(server.writes)
        assert worker.run().attempted == 0
        assert len(server.writes) == writes
    finally:
        worker.close()


def test_chinese_preferred_without_translations_falls_back_to_english(settings, tmp_path):
    settings.english_tags_enabled = False
    catalog = tmp_path / "catalog.json"
    catalog.write_text('{"tags": {}}')
    settings.translation_file = catalog
    server = FakeImmich([asset(0)])
    worker = processor(settings, server)
    try:
        assert worker.run().processed == 1
        assert {t["value"] for t in server.assets["0"]["tags"]} == {"auto:processed", "blue_hair", "hatsune_miku"}
        assert set(worker.catalog.missing) == {"blue_hair", "hatsune_miku"}
    finally:
        worker.close()


@pytest.mark.parametrize("failure", ["incomplete_upsert", "drop_assignment"])
def test_partial_write_never_marks_processed_and_retry_resets_failures(settings, failure):
    server = FakeImmich([asset(0)])
    setattr(server, failure, True)
    worker = processor(settings, server)
    try:
        assert worker.run().failed == 1
        assert "auto:processed" not in server.tags
        tracker = worker.failure_tracker(worker.clients[0])
        assert tracker.failures["0"]["attempts"] == 1
        setattr(server, failure, False)
        assert worker.run().processed == 1
        assert not worker.failure_tracker(worker.clients[0]).failures
    finally:
        worker.close()


def test_permanent_failures_do_not_block_later_pages(settings):
    settings.batch_size = 1
    settings.failure_timeout = 1
    server = FakeImmich([asset(i) for i in range(3)], legacy=True)
    worker = processor(settings, server)
    try:
        worker.failure_tracker(worker.clients[0]).record_failure("0")
        result = worker.run()
        assert result.processed == 2
        assert result.skipped == 1
        assert not server.assets["0"]["tags"]
    finally:
        worker.close()


@pytest.mark.parametrize("existing_chinese", [[], ["zh/属性/蓝发", "zh/角色/初音未来"]])
@pytest.mark.parametrize("english", [False, True])
def test_backfill_without_model_or_download_and_repeat_safe(settings, existing_chinese, english):
    settings.english_tags_enabled = english
    server = FakeImmich([asset(0, tags=[tag("blue_hair"), tag("hatsune_miku"), tag("auto:processed"),
                                      tag("unknown_manual_tag"), *map(tag, existing_chinese)])])
    def no_engine(_):
        raise AssertionError("Backfill must not initialize a model")
    worker = ImmichAutoTagger(settings, client_factory=server.factory, engine_factory=no_engine)
    try:
        assert worker.run(backfill=True).processed == 1
        assert worker.run(backfill=True).skipped == 1
        assert not any(c[1].endswith("thumbnail") for c in server.calls)
        assert len(server.writes) == 2
        assert set(worker.catalog.missing) == {"unknown_manual_tag"}
        paths = {t["value"] for t in server.assets["0"]["tags"]}
        assert "属性/蓝发" in paths
        assert set(existing_chinese).issubset(paths)
    finally:
        worker.close()


def test_model_loading_error_aborts_without_poisoning_asset_failures(settings):
    class BrokenEngine(FakeEngine):
        def prepare(self):
            raise TaggingEngineError("No model available")
    server = FakeImmich([asset(i) for i in range(3)])
    worker = processor(settings, server, engine=BrokenEngine())
    try:
        with pytest.raises(TaggingEngineError):
            worker.run()
        assert not server.writes
        assert not worker.failure_tracker(worker.clients[0]).failures
        assert worker.last_error == "No model available"
    finally:
        worker.close()


def test_changing_output_language_starts_new_run(settings):
    class BrokenEngine(FakeEngine):
        def prepare(self):
            raise TaggingEngineError("No model available")

    server = FakeImmich([asset(0)])
    worker = processor(settings, server, engine=BrokenEngine())
    try:
        with pytest.raises(TaggingEngineError):
            worker.run()
        previous_run_id = worker.get_metrics()["progress"]["run_id"]
        assert worker.has_pending_run()
    finally:
        worker.close()
    settings.english_tags_enabled = False
    worker = processor(settings, server)
    try:
        assert not worker.has_pending_run()
        assert worker.run().processed == 1
        assert worker.get_metrics()["progress"]["run_id"] != previous_run_id
        paths = {t["value"] for t in server.assets["0"]["tags"]}
        assert "属性/蓝发" in paths
        assert "blue_hair" not in paths
        assert "hatsune_miku" not in paths
    finally:
        worker.close()


def test_backfill_handles_raw_rating_and_prefixed_tags(settings):
    server = FakeImmich([asset(0, tags=[tag("general"), tag("general/blue_hair"),
                                      tag("character/hatsune_miku")])])
    worker = processor(settings, server)
    try:
        assert worker.run(backfill=True).processed == 1
        paths = {t["value"] for t in server.assets["0"]["tags"]}
        assert "评级/全年龄" in paths
        assert any(p.startswith("角色/") for p in paths)
        assert any(p.startswith("属性/") for p in paths)
        assert "auto:processed" not in paths
        assert worker._engine is None
    finally:
        worker.close()


def test_shared_state_lock_prevents_concurrent_writers(settings):
    with writer_lock(settings.state_dir):
        with pytest.raises(RuntimeError, match="Another tagger"):
            with writer_lock(settings.state_dir):
                pytest.fail("Lock was not exclusive")


def test_cancellation_is_not_reset_by_queued_run(settings):
    server = FakeImmich([asset(0)])
    worker = processor(settings, server)
    worker.cancelled.set()
    try:
        assert worker.run().attempted == 0
        assert not server.writes
        assert worker._engine is None
    finally:
        worker.close()
