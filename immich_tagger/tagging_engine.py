"""Lazy model adapters. API-only commands never import ML dependencies."""
import csv
import io
import os
from pathlib import Path

from .models import TagPrediction


class TaggingEngineError(RuntimeError):
    pass


class WD14TaggingEngine:
    def __init__(self, settings):
        self.settings = settings
        self.tagger = None

    def prepare(self):
        if self.tagger is not None:
            return
        os.environ["HF_HUB_CACHE"] = str(self.settings.model_cache_dir.resolve())
        try:
            from wdtagger import LabelData, Tagger
            from huggingface_hub import hf_hub_download
            tagger = Tagger(model_repo=self.settings.model_repo, cache_dir=self.settings.model_cache_dir)
            # wdtagger bundles one vocabulary; alternate models need their own ordered labels.
            label_file = hf_hub_download(self.settings.model_repo, "selected_tags.csv",
                                         cache_dir=self.settings.model_cache_dir)
            with open(label_file, encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            expected = getattr(tagger.model, "num_classes", len(rows))
            if len(rows) != expected:
                raise TaggingEngineError("Model output size does not match its tag vocabulary")
            tagger.labels = LabelData(
                names=[row["name"] for row in rows],
                rating=[i for i, row in enumerate(rows) if row["category"] == "9"],
                general=[i for i, row in enumerate(rows) if row["category"] == "0"],
                character=[i for i, row in enumerate(rows) if row["category"] == "4"],
            )
            self.tagger = tagger
        except Exception as error:
            raise TaggingEngineError(f"Cannot load WD14: {error}") from error

    def predict_tags(self, image_data):
        if self.tagger is None:
            self.prepare()
        from PIL import Image, ImageOps
        with Image.open(io.BytesIO(image_data)) as image:
            # Preserve alpha for wdtagger's white compositing and honor EXIF orientation.
            result = self.tagger.tag(
                ImageOps.exif_transpose(image),
                general_threshold=self.settings.effective_general_threshold,
                character_threshold=self.settings.character_threshold,
            )
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
        self.model = None

    def prepare(self):
        if self.model is None:
            try:
                import deepdanbooru as dd
                path = self.settings.deepdanbooru_project_dir
                if path is None or not Path(path).is_dir():
                    raise TaggingEngineError("DeepDanbooru requires DEEPDANBOORU_PROJECT_DIR")
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
