"""Lazy model adapters. API-only commands never import ML dependencies."""
import csv
import io
import logging
import math
import os
from pathlib import Path
from contextlib import nullcontext

from .models import TagPrediction


class TaggingEngineError(RuntimeError):
    pass


class InferenceBackendError(TaggingEngineError):
    """A broken inference backend must pause the queue, not fail individual assets."""


def select_device(torch, requested):
    cuda = torch.cuda.is_available() if requested in ("auto", "cuda") else False
    mps = (torch.backends.mps.is_available()
           if requested in ("auto", "mps") and hasattr(torch.backends, "mps") else False)
    selected = ("cuda" if cuda else "mps" if mps else "cpu") if requested == "auto" else requested
    if (selected == "cuda" and not cuda) or (selected == "mps" and not mps):
        raise InferenceBackendError(f"TAGGING_DEVICE={selected} is unavailable; set TAGGING_DEVICE=cpu to resume")
    return torch.device(selected)


class WD14TaggingEngine:
    def __init__(self, settings):
        self.settings = settings
        self.progress = None
        self.tagger = None
        self.runtime_info = None
        self.device = None

    def _publish_runtime(self):
        if self.progress:
            self.progress.update(inference=self.runtime_info)

    def _invalidate(self):
        self.tagger = None
        self.runtime_info = None
        self._publish_runtime()

    def _backend_error(self, error):
        self._invalidate()
        return InferenceBackendError(
            f"WD14 inference on {self.device} failed: {error}; "
            "task paused, set TAGGING_DEVICE=cpu to resume")

    def prepare(self):
        if self.tagger is not None:
            self._publish_runtime()
            return
        os.environ["HF_HUB_CACHE"] = str(self.settings.model_cache_dir.resolve())
        try:
            with self.progress.operation("import_model") if self.progress else nullcontext():
                import torch
                from wdtagger import LabelData, Tagger
                from huggingface_hub import hf_hub_download
            self.device = self.settings.tagging_device
            self.device = select_device(torch, self.settings.tagging_device)
            if self.settings.tagging_cpu_threads is not None:
                torch.set_num_threads(self.settings.tagging_cpu_threads)

            # wdtagger 0.16.0 chooses CUDA inside __init__; replace that choice before loading.
            device = self.device
            class DeviceTagger(Tagger):
                def _load_model(self, model_repo, cache_dir):
                    self.torch_device = device
                    super()._load_model(model_repo, cache_dir)

            with self.progress.operation("load_model") if self.progress else nullcontext():
                tagger = DeviceTagger(model_repo=self.settings.model_repo, cache_dir=self.settings.model_cache_dir)
            # wdtagger bundles one vocabulary; alternate models need their own ordered labels.
            with self.progress.operation("load_labels") if self.progress else nullcontext():
                label_file = hf_hub_download(self.settings.model_repo, "selected_tags.csv",
                                             cache_dir=self.settings.model_cache_dir)
            with open(label_file, encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            expected = getattr(tagger.model, "num_classes", len(rows))
            if (len(rows) != expected or len({row["name"] for row in rows}) != len(rows)
                    or any(row["category"] not in ("0", "4", "9") for row in rows)):
                raise TaggingEngineError("Model output size does not match its tag vocabulary")
            tagger.labels = LabelData(
                names=[row["name"] for row in rows],
                rating=[i for i, row in enumerate(rows) if row["category"] == "9"],
                general=[i for i, row in enumerate(rows) if row["category"] == "0"],
                character=[i for i, row in enumerate(rows) if row["category"] == "4"],
            )
            original_forward = tagger.model.forward
            def checked_forward(*args, **kwargs):
                # Check every logit: thresholding in wdtagger would silently discard NaNs.
                output = original_forward(*args, **kwargs)
                if output.ndim != 2 or output.shape != (1, expected):
                    raise InferenceBackendError("Model output size does not match its tag vocabulary")
                if output.dtype != torch.float32 or not torch.isfinite(output).all().item():
                    raise InferenceBackendError("Model output must contain finite FP32 logits")
                return output

            # wdtagger calls forward directly, so ordinary Module forward hooks do not run.
            tagger.model.forward = checked_forward
            from PIL import Image
            with self.progress.operation("warmup") if self.progress else nullcontext():
                with Image.new("RGB", (448, 448), "white") as image:
                    result = tagger.tag(image, general_threshold=-1.0, character_threshold=-1.0)
                scores = self._scores(result)
                if set(scores) != set(tagger.labels.names):
                    raise InferenceBackendError("Warmup output does not match the complete tag vocabulary")
            self.tagger = tagger
            self.runtime_info = {"device": str(self.device), "precision": "float32",
                                 "cpu_threads": torch.get_num_threads(), "model_repo": self.settings.model_repo}
            self._publish_runtime()
            logging.getLogger(__name__).info("[模型] WD14 ready: %s", self.runtime_info)
        except (RuntimeError, NotImplementedError) as error:
            raise self._backend_error(error) from error
        except Exception as error:
            self._invalidate()
            raise TaggingEngineError(f"Cannot load WD14: {error}") from error

    @staticmethod
    def _scores(result):
        scores = {}
        for attribute in ("general_tag_data", "character_tag_data", "rating_data"):
            values = getattr(result, attribute, None)
            if not isinstance(values, dict):
                raise InferenceBackendError("Unsupported wdtagger result format")
            for name, score in values.items():
                try:
                    score = float(score)
                except (TypeError, ValueError) as error:
                    raise InferenceBackendError("Unsupported model probability") from error
                if not math.isfinite(score) or not 0 <= score <= 1:
                    raise InferenceBackendError("Model output must contain finite probabilities in [0, 1]")
                scores[name] = score
        return scores

    def _predict(self, image_data, *, full_scores=False):
        if self.tagger is None:
            self.prepare()
        from PIL import Image, ImageOps
        with Image.open(io.BytesIO(image_data)) as image:
            # Preserve alpha for wdtagger's white compositing and honor EXIF orientation.
            oriented = ImageOps.exif_transpose(image)
            try:
                result = self.tagger.tag(
                    oriented,
                    general_threshold=-1.0 if full_scores else self.settings.effective_general_threshold,
                    character_threshold=-1.0 if full_scores else self.settings.character_threshold,
                )
                self._scores(result)
            except (RuntimeError, NotImplementedError) as error:
                raise self._backend_error(error) from error
            finally:
                if oriented is not image:
                    oriented.close()
        return result

    def predict_scores(self, image_data):
        """Complete probabilities for local correctness comparisons, including ratings."""
        return self._scores(self._predict(image_data, full_scores=True))

    def predict_tags(self, image_data):
        result = self._predict(image_data)
        predictions = []
        for kind, attribute in (("general", "general_tag_data"), ("character", "character_tag_data")):
            values = getattr(result, attribute, None)
            if not isinstance(values, dict):
                raise TaggingEngineError("Unsupported wdtagger result format")
            predictions.extend(TagPrediction(name=name, confidence=float(score), kind=kind)
                               for name, score in values.items())
        if not isinstance(result.rating_data, dict):
            raise TaggingEngineError("Unsupported rating result")
        if result.rating_data:
            name, score = max(result.rating_data.items(), key=lambda item: item[1])
            predictions.append(TagPrediction(name=name, confidence=float(score), kind="rating"))
        return sorted(predictions, key=lambda prediction: prediction.confidence, reverse=True)


class DeepDanbooruTaggingEngine:
    def __init__(self, settings):
        self.settings = settings
        self.progress = None
        self.model = None

    def prepare(self):
        if self.settings.tagging_device != "auto":
            raise TaggingEngineError("TAGGING_DEVICE is supported only by WD14; use auto for DeepDanbooru")
        if self.model is None:
            try:
                with self.progress.operation("import_model") if self.progress else nullcontext():
                    import deepdanbooru as dd
                path = self.settings.deepdanbooru_project_dir
                if path is None or not Path(path).is_dir():
                    raise TaggingEngineError("DeepDanbooru requires DEEPDANBOORU_PROJECT_DIR")
                with self.progress.operation("load_model") if self.progress else nullcontext():
                    self.model, self.tags = dd.project.load_project(str(path), compile_model=False)
            except Exception as error:
                raise TaggingEngineError(f"Cannot load DeepDanbooru: {error}") from error

    def predict_tags(self, image_data):
        self.prepare()
        import deepdanbooru as dd
        import numpy as np
        from PIL import Image, ImageOps
        height, width = self.model.input_shape[1:3]
        with Image.open(io.BytesIO(image_data)) as image:
            image = ImageOps.exif_transpose(image).convert("RGBA")
            background = Image.new("RGBA", image.size, "white")
            background.alpha_composite(image)
            values = np.asarray(background.convert("RGB"))
        values = dd.data.transform_and_pad_image(values, width, height) / 255.0
        scores = self.model.predict(np.expand_dims(values, 0), verbose=0)[0]
        return [TagPrediction(name=name, confidence=float(score))
                for name, score in zip(self.tags, scores)
                if float(score) >= self.settings.effective_general_threshold]


def create_tagging_engine(settings):
    if settings.tagging_model == "wd14":
        return WD14TaggingEngine(settings)
    return DeepDanbooruTaggingEngine(settings)
