import asyncio
import io
import json
import sys
import threading
from types import SimpleNamespace

import pytest
from PIL import Image

from immich_tagger.health_server import HealthServer
from immich_tagger.models import RunResult
from immich_tagger.scheduler import Scheduler
from immich_tagger.tagging_engine import TaggingEngineError, WD14TaggingEngine


def test_model_adapter_vocabulary_cache_thresholds_and_alpha(settings, tmp_path, monkeypatch):
    labels = tmp_path / "tags.csv"
    labels.write_text("name,category\ngeneral,9\nblue_hair,0\nhatsune_miku,4\n")
    captured = {}
    class FakeTagger:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.model = SimpleNamespace(num_classes=3)

        def tag(self, image, **kwargs):
            captured.update(kwargs)
            captured["mode"] = image.mode
            captured["labels"] = self.labels
            return SimpleNamespace(general_tag_data={"blue_hair": .7},
                                   character_tag_data={"hatsune_miku": .96},
                                   rating_data={"general": .8, "sensitive": .2})
    def download(repo, filename, **kwargs):
        assert repo == settings.model_repo and filename == "selected_tags.csv"
        assert kwargs["cache_dir"] == settings.model_cache_dir
        return labels
    monkeypatch.setitem(sys.modules, "wdtagger", SimpleNamespace(Tagger=FakeTagger, LabelData=SimpleNamespace))
    monkeypatch.setitem(sys.modules, "huggingface_hub", SimpleNamespace(hf_hub_download=download))
    settings.general_threshold = .5
    settings.character_threshold = .94
    engine = WD14TaggingEngine(settings)
    assert not captured
    data = io.BytesIO()
    Image.new("RGBA", (2, 3), (0, 0, 255, 0)).save(data, format="PNG")
    predictions = engine.predict_tags(data.getvalue())
    assert captured["general_threshold"] == .5
    assert captured["character_threshold"] == .94
    assert captured["cache_dir"] == settings.model_cache_dir
    assert captured["mode"] == "RGBA"
    assert captured["labels"].character == [2]
    assert [(p.name, p.confidence) for p in predictions] == [("hatsune_miku", .96), ("general", .8), ("blue_hair", .7)]


def test_bad_model_vocabulary_fails_before_inference(settings, tmp_path, monkeypatch):
    labels = tmp_path / "tags.csv"
    labels.write_text("name,category\ngeneral,9\n")
    monkeypatch.setitem(sys.modules, "wdtagger", SimpleNamespace(
        Tagger=lambda **_: SimpleNamespace(model=SimpleNamespace(num_classes=2)), LabelData=SimpleNamespace))
    monkeypatch.setitem(sys.modules, "huggingface_hub", SimpleNamespace(hf_hub_download=lambda *a, **k: labels))
    with pytest.raises(TaggingEngineError, match="vocabulary"):
        WD14TaggingEngine(settings).prepare()


def test_scheduler_keeps_health_responsive_and_uses_same_processor(settings):
    settings.enable_scheduler = False
    entered, release = threading.Event(), threading.Event()
    class Worker:
        def __init__(self):
            self.settings = settings
            self.cancelled = threading.Event()
            self.last_error = None
            self.calls = 0

        def run(self, **kwargs):
            self.calls += 1
            assert kwargs["limit"] == 3
            entered.set()
            assert release.wait(timeout=5)
            return RunResult(processed=3)

        def get_metrics(self):
            return {"last_error": self.last_error, "running": entered.is_set() and not release.is_set()}

    worker = Worker()
    async def check():
        scheduler = Scheduler(worker)
        health = HealthServer(worker)
        task = asyncio.create_task(scheduler.start(limit=3))
        try:
            for _ in range(200):
                if entered.is_set():
                    break
                await asyncio.sleep(.005)
            assert entered.is_set()
            response = await health.health(None)
            assert response.status == 200
            assert json.loads(response.body)["running"] is True
            worker.last_error = "one failed"
            assert (await health.health(None)).status == 503
        finally:
            release.set()
            assert await task == 0
        scheduler.stop()
        assert worker.cancelled.is_set()
        assert worker.calls == 1
    asyncio.run(check())
