import copy
from pathlib import Path

import pytest

from scripts.check_release_acceptance import main, validate


@pytest.fixture
def evidence():
    common = {"passed": True, "system": {"chip": "Apple M5", "architecture": "arm64",
                                       "system": "macOS-27.2-arm64-arm-64bit"},
              "images": [{"path": str(i), "sha256": str(i)} for i in range(100)],
              "model_revision": "snapshot", "general_threshold": .35, "character_threshold": .9}
    cpu, mps = copy.deepcopy(common), copy.deepcopy(common)
    cpu["inference"] = {"device": "cpu", "precision": "float32", "model_repo": "SmilingWolf/wd-swinv2-tagger-v3"}
    mps["inference"] = {"device": "mps", "precision": "float32", "model_repo": "SmilingWolf/wd-swinv2-tagger-v3"}
    mps.update(iterations=1000, memory_plateau={"passed": True},
               environment={"PYTORCH_ENABLE_MPS_FALLBACK": "0", "PYTORCH_MPS_FAST_MATH": "0"},
               comparison={"passed": True, "max_absolute_probability_error": 1e-5,
                           "threshold_violations": [], "rating_mismatches": []})
    integration = {"passed": True, "assets": 4, "failed": 0, "manual_tags_preserved": True,
                   "idempotent": True, "same_run_id": True, "devices": ["mps", "cpu"],
                   "preview_planned": 4, "before_interruption_processed": 1, "resumed_processed": 4,
                   "delayed_readback": True}
    return cpu, mps, integration


@pytest.mark.parametrize("chip", ["Apple M1", "Apple M2 Ultra", "Apple M3 Max", "Apple M4 Pro", "Apple M5"])
def test_release_gate_accepts_complete_apple_silicon_evidence(evidence, chip):
    for report in evidence[:2]:
        report["system"]["chip"] = chip
    validate(*evidence)


@pytest.mark.parametrize("case", ["chip", "architecture", "os", "different_chip", "short", "nan", "fallback",
                                 "mismatch", "integration", "duplicates", "model", "preview"])
def test_release_gate_rejects_incomplete_or_wrong_evidence(evidence, case):
    cpu, mps, integration = evidence
    if case == "chip":
        cpu["system"]["chip"] = mps["system"]["chip"] = "Intel Core i7"
    elif case == "architecture":
        cpu["system"]["architecture"] = mps["system"]["architecture"] = "x86_64"
    elif case == "os":
        cpu["system"]["system"] = mps["system"]["system"] = "Linux-arm64"
    elif case == "different_chip":
        mps["system"]["chip"] = "Apple M4"
    elif case == "short":
        mps["iterations"] = 100
    elif case == "nan":
        mps["comparison"]["max_absolute_probability_error"] = float("nan")
    elif case == "fallback":
        mps["environment"]["PYTORCH_ENABLE_MPS_FALLBACK"] = "1"
    elif case == "mismatch":
        mps["model_revision"] = "another snapshot"
    elif case == "integration":
        integration["same_run_id"] = False
    elif case == "duplicates":
        cpu["images"] = [cpu["images"][0]] * 100
    elif case == "model":
        cpu["inference"]["model_repo"] = mps["inference"]["model_repo"] = "alternative"
    elif case == "preview":
        integration["preview_planned"] = 0
    with pytest.raises(ValueError):
        validate(*evidence)


def test_missing_evidence_blocks_release(tmp_path):
    assert main(["--directory", str(tmp_path)]) == 1


def test_archived_m5_evidence_passes_release_gate():
    directory = Path(__file__).resolve().parents[1] / "docs" / "validation"
    assert main(["--directory", str(directory)]) == 0
