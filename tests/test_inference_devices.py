import io
import sys
from types import SimpleNamespace

import pytest
from PIL import Image, UnidentifiedImageError
from pydantic import ValidationError

from app.progress import ProgressState
from app.tagging_engine import DeepDanbooruTaggingEngine, InferenceBackendError, TaggingEngineError, WD14TaggingEngine
from tests.support.inference import install_fake_wd


@pytest.fixture
def backend(tmp_path, monkeypatch):
    labels = tmp_path / "labels.csv"
    labels.write_text("name,category\ngeneral,9\nblue_hair,0\nhatsune_miku,4\n")
    def install(**kwargs):
        return install_fake_wd(monkeypatch, labels, **kwargs)
    return install


def image_bytes():
    data = io.BytesIO()
    Image.new("RGBA", (8, 6), (0, 0, 255, 0)).save(data, format="PNG")
    return data.getvalue()


@pytest.mark.parametrize("requested,cuda,mps,expected", [
    ("auto", True, True, "cuda"), ("auto", False, True, "mps"), ("auto", False, False, "cpu"),
    ("cpu", True, True, "cpu"), ("mps", True, True, "mps"), ("cuda", True, False, "cuda"),
])
def test_device_selected_before_first_transfer_and_inputs_follow(settings, backend, requested, cuda, mps, expected):
    _, captured = backend(cuda=cuda, mps=mps)
    settings.tagging_device = requested
    settings.tagging_cpu_threads = 2
    engine = WD14TaggingEngine(settings)
    engine.progress = ProgressState(settings)
    assert engine.progress.snapshot()["inference"] is None
    engine.prepare()
    engine.predict_tags(image_bytes())
    assert captured["loaded_devices"] == [expected]
    assert captured["inputs"] == [expected, expected]
    assert engine.runtime_info == {"device": expected, "precision": "float32", "cpu_threads": 2,
                                   "model_repo": settings.model_repo}
    assert engine.progress.snapshot()["inference"] == engine.runtime_info
    engine.progress.reset()
    engine.prepare()
    assert engine.progress.snapshot()["inference"] == engine.runtime_info


@pytest.mark.parametrize("device", ["mps", "cuda"])
def test_explicit_unavailable_device_fails_before_loading(settings, backend, device):
    _, captured = backend()
    settings.tagging_device = device
    engine = WD14TaggingEngine(settings)
    with pytest.raises(InferenceBackendError, match="unavailable"):
        engine.prepare()
    assert not captured["loaded_devices"]
    assert engine.tagger is None and engine.runtime_info is None


@pytest.mark.parametrize("count", [0, -1])
def test_invalid_thread_count(settings, count):
    with pytest.raises(ValidationError):
        settings.tagging_cpu_threads = count


@pytest.mark.parametrize("error", [RuntimeError("device lost"), NotImplementedError("operator"), RuntimeError("out of memory")])
def test_backend_failure_invalidates_model_and_next_run_reloads(settings, backend, error):
    _, captured = backend(mps=True)
    engine = WD14TaggingEngine(settings)
    engine.progress = ProgressState(settings)
    engine.prepare()
    engine.tagger.model.error = error
    with pytest.raises(InferenceBackendError, match="task paused"):
        engine.predict_tags(image_bytes())
    assert engine.tagger is None and engine.progress.snapshot()["inference"] is None
    settings.tagging_device = "cpu"
    assert engine.predict_tags(image_bytes())
    assert captured["loaded_devices"] == ["mps", "cpu"]


@pytest.mark.parametrize("output", [
    SimpleNamespace(ndim=2, shape=(1, 3), dtype="float32", finite=False),
    SimpleNamespace(ndim=2, shape=(1, 2), dtype="float32", finite=True),
    SimpleNamespace(ndim=2, shape=(1, 3), dtype="float16", finite=True),
])
def test_raw_logits_checked_before_thresholding(settings, backend, output):
    backend()
    engine = WD14TaggingEngine(settings)
    engine.prepare()
    engine.tagger.model.output = output
    with pytest.raises(InferenceBackendError):
        engine.predict_tags(image_bytes())
    assert engine.tagger is None


def test_nan_probability_and_warmup_vocabulary_rejected(settings, backend):
    _, captured = backend()
    tagger_type = sys.modules["wdtagger"].Tagger
    tagger_type.tag = lambda *args, **kwargs: SimpleNamespace(
        general_tag_data={"blue_hair": float("nan")}, character_tag_data={}, rating_data={})
    engine = WD14TaggingEngine(settings)
    with pytest.raises(InferenceBackendError, match="finite"):
        engine.prepare()
    assert engine.tagger is None
    tagger_type.tag = lambda *args, **kwargs: SimpleNamespace(
        general_tag_data={"blue_hair": .7}, character_tag_data={}, rating_data={})
    with pytest.raises(InferenceBackendError, match="complete tag vocabulary"):
        engine.prepare()
    assert len(captured["instances"]) == 2


def test_bad_image_keeps_backend_ready(settings, backend):
    backend()
    engine = WD14TaggingEngine(settings)
    engine.prepare()
    with pytest.raises(UnidentifiedImageError):
        engine.predict_tags(b"not an image")
    assert engine.tagger is not None


def test_deepdanbooru_rejects_device_without_loading_tensorflow(settings):
    settings.tagging_device = "mps"
    with pytest.raises(TaggingEngineError, match="only by WD14"):
        DeepDanbooruTaggingEngine(settings).prepare()
