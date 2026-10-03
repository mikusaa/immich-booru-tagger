"""Small stand-ins for the pinned wdtagger loading and inference contract."""
from types import SimpleNamespace
import sys


def fake_torch(*, cuda=False, mps=False):
    threads = [4]
    def set_threads(count):
        threads[0] = count
    return SimpleNamespace(
        cuda=SimpleNamespace(is_available=lambda: cuda),
        backends=SimpleNamespace(mps=SimpleNamespace(is_available=lambda: mps)),
        device=lambda name: SimpleNamespace(type=name, __str__=lambda: name),
        float32="float32", set_num_threads=set_threads, get_num_threads=lambda: threads[0],
        isfinite=lambda output: SimpleNamespace(all=lambda: SimpleNamespace(item=lambda: output.finite)),
    )


def install_fake_wd(monkeypatch, labels, *, cuda=False, mps=False):
    torch = fake_torch(cuda=cuda, mps=mps)
    class Device:
        def __init__(self, name):
            self.type = name
        def __str__(self):
            return self.type
    torch.device = Device
    captured = {"loaded_devices": [], "inputs": [], "instances": []}
    class Model:
        num_classes = 3
        def __init__(self, device):
            self.device = device
            self.error = None
            self.output = SimpleNamespace(ndim=2, shape=(1, 3), dtype="float32", finite=True)
        def forward(self, inputs):
            assert inputs.device.type == self.device.type
            captured["inputs"].append(inputs.device.type)
            if self.error:
                raise self.error
            return self.output
    class Tagger:
        def __init__(self, model_repo, cache_dir):
            self.torch_device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
            self._load_model(model_repo, cache_dir)
            captured["instances"].append(self)
        def _load_model(self, model_repo, cache_dir):
            captured["loaded_devices"].append(self.torch_device.type)
            self.model = Model(self.torch_device)
        def tag(self, image, **kwargs):
            self.model.forward(SimpleNamespace(device=self.torch_device))
            return SimpleNamespace(general_tag_data={"blue_hair": .7},
                                   character_tag_data={"hatsune_miku": .96}, rating_data={"general": .8})
    monkeypatch.setitem(sys.modules, "torch", torch)
    monkeypatch.setitem(sys.modules, "wdtagger", SimpleNamespace(Tagger=Tagger, LabelData=SimpleNamespace))
    monkeypatch.setitem(sys.modules, "huggingface_hub",
                        SimpleNamespace(hf_hub_download=lambda *args, **kwargs: labels))
    return torch, captured
