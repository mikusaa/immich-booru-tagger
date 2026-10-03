"""Offline CPU contract smoke test with real timm/wdtagger and untrained SwinV2 weights."""
import io
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from app.config import Settings
from app.tagging_engine import WD14TaggingEngine


def main():
    import torch
    import timm
    import wdtagger
    from PIL import Image
    torch.set_num_threads(2)
    model = timm.create_model("swinv2_base_window8_256", pretrained=False, img_size=448,
                              window_size=14, num_classes=10861, act_layer="gelu_tanh")
    model.pretrained_cfg.update(input_size=(3, 448, 448), mean=(.5, .5, .5), std=(.5, .5, .5),
                                crop_pct=1.0, interpolation="bicubic")
    labels = Path(wdtagger.__file__).parent / "assets" / "selected_tags.csv"
    data = io.BytesIO()
    Image.new("RGBA", (48, 32), (0, 0, 255, 0)).save(data, format="PNG")
    with tempfile.TemporaryDirectory() as directory:
        settings = Settings(_env_file=None, immich_base_url="http://unused", immich_api_key="test",
                            immich_api_keys=[], immich_libraries={}, tagging_model="wd14",
                            tagging_device="cpu", tagging_cpu_threads=2, model_cache_dir=Path(directory))
        engine = WD14TaggingEngine(settings)
        with (patch("timm.create_model", return_value=model),
              patch("timm.models.load_state_dict_from_hf", return_value=model.state_dict()),
              patch("huggingface_hub.hf_hub_download", return_value=str(labels)),
              patch("torch.cuda.is_available", return_value=True)):
            engine.prepare()
            scores = engine.predict_scores(data.getvalue())
        assert len(scores) == 10861
        assert engine.runtime_info["device"] == "cpu"
        assert next(engine.tagger.model.parameters()).device.type == "cpu"
        print(json.dumps({"passed": True, "weights": "untrained", "classes": len(scores),
                          "inference": engine.runtime_info}))


if __name__ == "__main__":
    main()
