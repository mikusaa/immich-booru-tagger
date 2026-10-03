"""Require reviewed Apple Silicon and isolated Immich evidence before a stable release."""
import argparse
import json
import math
import re
from pathlib import Path


def validate(cpu, mps, integration):
    for report, device in ((cpu, "cpu"), (mps, "mps")):
        if not report.get("passed") or report["inference"]["device"] != device:
            raise ValueError(f"Missing passing {device} report")
        system = report["system"]
        if (not re.fullmatch(r"Apple M[1-9][0-9]*(?: (?:Pro|Max|Ultra))?", system["chip"])
                or system["architecture"] != "arm64"
                or not system["system"].startswith("macOS-")):
            raise ValueError("Stable release requires native macOS ARM64 Apple Silicon evidence")
        if report["inference"]["precision"] != "float32":
            raise ValueError("FP32 evidence required")
        images = report["images"]
        if len(images) < 100 or len({image["sha256"] for image in images}) < 100:
            raise ValueError("At least 100 distinct fixed images required")
    for field in ("system", "model_revision", "images", "general_threshold", "character_threshold"):
        if cpu[field] != mps[field]:
            raise ValueError(f"CPU/MPS evidence mismatch: {field}")
    if any(report["inference"]["model_repo"] != "SmilingWolf/wd-swinv2-tagger-v3" for report in (cpu, mps)):
        raise ValueError("Default WD14 model evidence required")
    comparison = mps["comparison"]
    error = comparison["max_absolute_probability_error"]
    if (not comparison["passed"] or not math.isfinite(error) or not 0 <= error <= 1e-3
            or comparison["threshold_violations"] or comparison["rating_mismatches"]):
        raise ValueError("CPU/MPS correctness acceptance failed")
    if mps["iterations"] < 1000 or not mps["memory_plateau"]["passed"]:
        raise ValueError("1000-iteration MPS stability acceptance required")
    if any(mps["environment"].get(key) != "0" for key in
           ("PYTORCH_ENABLE_MPS_FALLBACK", "PYTORCH_MPS_FAST_MATH")):
        raise ValueError("MPS fallback/fast math must be disabled")
    if (not integration.get("passed") or integration.get("assets", 0) < 2
            or integration.get("failed") != 0
            or integration.get("preview_planned", 0) < integration["assets"]
            or not 0 < integration.get("before_interruption_processed", 0) < integration["assets"]
            or integration.get("resumed_processed") != integration["assets"]
            or not all(integration.get(key) for key in
                       ("manual_tags_preserved", "idempotent", "same_run_id", "delayed_readback"))
            or integration.get("devices") != ["mps", "cpu"]):
        raise ValueError("Isolated Immich preview/write/interruption/CPU resume acceptance required")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=Path("docs/validation"))
    args = parser.parse_args(argv)
    try:
        reports = [json.loads((args.directory / name).read_text()) for name in
                   ("apple-silicon-cpu.json", "apple-silicon-mps.json", "immich-integration.json")]
        validate(*reports)
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        print(f"Stable release acceptance blocked: {error}")
        return 1
    print("Apple Silicon and isolated Immich release acceptance passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
