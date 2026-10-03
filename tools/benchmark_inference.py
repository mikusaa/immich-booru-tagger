"""Local inference timing, full-score comparison, and memory sampling; no Immich calls."""
import argparse
import hashlib
import importlib.metadata
import json
import math
import os
import platform
import resource
import statistics
import subprocess
import sys
import time
from pathlib import Path

from app.config import Settings
from app.tagging_engine import WD14TaggingEngine


def positive_integer(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be positive")
    return number


def parse_arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--images", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--device", choices=("auto", "cpu", "mps", "cuda"), default="auto")
    parser.add_argument("--cpu-threads", type=positive_integer)
    parser.add_argument("--iterations", type=positive_integer, default=100)
    parser.add_argument("--model-repo", default="SmilingWolf/wd-swinv2-tagger-v3")
    parser.add_argument("--model-cache", type=Path, default=Path("models"))
    parser.add_argument("--general-threshold", type=float, default=.35)
    parser.add_argument("--character-threshold", type=float, default=.90)
    parser.add_argument("--reference", type=Path, help="CPU JSON report for the same corpus and model snapshot")
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--stability", action="store_true", help="Require 1000 iterations and check memory plateau")
    args = parser.parse_args(argv)
    if args.iterations < (1000 if args.stability else 100):
        parser.error("at least 100 iterations required (1000 for --stability)")
    if not args.images.is_dir():
        parser.error("--images must be a directory")
    if args.output.suffix != ".json":
        parser.error("--output must end in .json")
    if args.reference and args.reference.resolve() == args.output.resolve():
        parser.error("--output must not overwrite --reference")
    return args


def system_info():
    info = {"system": platform.platform(), "architecture": platform.machine(),
            "chip": platform.processor(), "memory_bytes": None}
    if sys.platform == "darwin":
        for key, name in (("machdep.cpu.brand_string", "chip"), ("hw.memsize", "memory_bytes")):
            try:
                value = subprocess.check_output(["sysctl", "-n", key], text=True).strip()
                info[name] = int(value) if name == "memory_bytes" else value
            except (OSError, subprocess.CalledProcessError):
                pass
    else:
        try:
            info["memory_bytes"] = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
        except (OSError, ValueError):
            pass
    return info


def memory_sample(torch, device):
    rss = None
    try:
        if sys.platform == "darwin":
            rss = int(subprocess.check_output(["ps", "-o", "rss=", "-p", str(os.getpid())], text=True)) * 1024
        else:
            rss = int(Path("/proc/self/statm").read_text().split()[1]) * os.sysconf("SC_PAGE_SIZE")
    except (OSError, ValueError, subprocess.CalledProcessError):
        pass
    return {"rss_bytes": rss,
            "mps_allocated_bytes": torch.mps.current_allocated_memory() if device == "mps" else None,
            "mps_driver_bytes": torch.mps.driver_allocated_memory() if device == "mps" else None}


def memory_plateau(samples):
    checks = {}
    # Ignore initial graph/cache growth; compare windows in the second half of the run.
    tail = samples[len(samples) // 2:]
    window = min(100, len(tail) // 2)
    for key in ("rss_bytes", "mps_allocated_bytes", "mps_driver_bytes"):
        first = [s[key] for s in tail[:window] if s[key] is not None]
        last = [s[key] for s in tail[-window:] if s[key] is not None]
        if not first or not last:
            continue
        start, end = statistics.median(first), statistics.median(last)
        allowance = max(32 * 1024 ** 2, start * .1)
        checks[key] = {"start_bytes": start, "end_bytes": end,
                       "growth_bytes": end - start, "allowance_bytes": allowance,
                       "passed": end - start <= allowance}
    return {"passed": "rss_bytes" in checks and all(c["passed"] for c in checks.values()), "checks": checks}


def compare_scores(reference, current, names, categories, general_threshold, character_threshold):
    if (not len(current) or len(reference) != len(current) or len(names) != len(categories)
            or any(len(row) != len(names) for row in reference)
            or any(len(row) != len(names) for row in current)):
        raise ValueError("Score shape mismatch")
    changes = []
    violations = []
    maximum = 0.0
    ratings = [i for i, kind in enumerate(categories) if kind == "rating"]
    for row in range(len(current)):
        for column, kind in enumerate(categories):
            cpu, candidate = float(reference[row][column]), float(current[row][column])
            if not all(math.isfinite(value) for value in (cpu, candidate)):
                raise ValueError("Scores must be finite")
            maximum = max(maximum, abs(cpu - candidate))
            if kind == "rating":
                continue
            threshold = character_threshold if kind == "character" else general_threshold
            if (cpu > threshold) != (candidate > threshold):
                change = {"image_index": row, "tag": names[column], "kind": kind,
                          "cpu": cpu, "candidate": candidate,
                          "threshold": threshold}
                changes.append(change)
                if abs(cpu - threshold) > 1e-3:
                    violations.append(change)
    rating_mismatches = []
    if ratings:
        for row in range(len(current)):
            a = max(ratings, key=lambda column: reference[row][column])
            b = max(ratings, key=lambda column: current[row][column])
            if a != b:
                rating_mismatches.append({"image_index": row, "cpu": names[a], "candidate": names[b]})
    return {"passed": maximum <= 1e-3 and not violations and not rating_mismatches,
            "max_absolute_probability_error": maximum, "tolerance": 1e-3,
            "label_changes": changes, "threshold_violations": violations,
            "rating_mismatches": rating_mismatches}


def benchmark(args):
    if args.offline:
        os.environ["HF_HUB_OFFLINE"] = "1"
    settings = Settings(_env_file=None, immich_base_url="http://unused", immich_api_key="local-benchmark",
                        immich_api_keys=[], immich_libraries={}, tagging_model="wd14",
                        tagging_device=args.device, tagging_cpu_threads=args.cpu_threads,
                        model_repo=args.model_repo, model_cache_dir=args.model_cache,
                        general_threshold=args.general_threshold, character_threshold=args.character_threshold)
    extensions = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff", ".gif"}
    images = sorted(p for p in args.images.rglob("*") if p.is_file() and p.suffix.lower() in extensions)
    if not images:
        raise ValueError("No supported images found")
    manifest = [{"path": p.relative_to(args.images).as_posix(),
                 "sha256": hashlib.sha256(p.read_bytes()).hexdigest()} for p in images]
    engine = WD14TaggingEngine(settings)
    started = time.perf_counter()
    engine.prepare()
    cold_start = time.perf_counter() - started
    import numpy as np
    import torch
    from huggingface_hub import snapshot_download
    snapshot = Path(snapshot_download(args.model_repo, cache_dir=args.model_cache, local_files_only=True))
    device = engine.runtime_info["device"]
    def synchronize():
        if device == "mps":
            torch.mps.synchronize()
        elif device.startswith("cuda"):
            torch.cuda.synchronize()
    for index in range(5):
        engine.predict_tags(images[index % len(images)].read_bytes())
    synchronize()
    timings, memory = [], []
    for index in range(args.iterations):
        data = images[index % len(images)].read_bytes()
        synchronize()
        started = time.perf_counter()
        engine.predict_tags(data)
        synchronize()
        timings.append(time.perf_counter() - started)
        memory.append(memory_sample(torch, device))
    names = engine.tagger.labels.names
    rating_indices, character_indices = set(engine.tagger.labels.rating), set(engine.tagger.labels.character)
    categories = ["rating" if i in rating_indices else
                  "character" if i in character_indices else "general" for i in range(len(names))]
    score_rows = []
    for image in images:
        values = engine.predict_scores(image.read_bytes())
        score_rows.append([values[name] for name in names])
    scores = np.asarray(score_rows, dtype=np.float32)
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    report = {"schema_version": 1, "system": system_info(), "inference": engine.runtime_info,
              "versions": {name: importlib.metadata.version(name) for name in
                           ("torch", "torchvision", "timm", "wdtagger", "Pillow", "numpy")},
              "model_revision": snapshot.name, "images": manifest, "image_count": len(images),
              "general_threshold": args.general_threshold, "character_threshold": args.character_threshold,
              "iterations": args.iterations, "warmup_iterations": 5, "cold_start_seconds": cold_start,
              "timing_includes": "decode, preprocessing, inference, CPU transfer and tag filtering; excludes file I/O",
              "p50_seconds": float(np.percentile(timings, 50)), "p95_seconds": float(np.percentile(timings, 95)),
              "images_per_second": len(timings) / sum(timings),
              "peak_rss_bytes": peak if sys.platform == "darwin" else peak * 1024,
              "memory_samples": memory, "memory_plateau": memory_plateau(memory),
              "environment": {name: os.environ.get(name, "0") for name in
                              ("PYTORCH_ENABLE_MPS_FALLBACK", "PYTORCH_MPS_FAST_MATH")}}
    passed = not args.stability or report["memory_plateau"]["passed"]
    if args.reference:
        reference = json.loads(args.reference.read_text())
        if reference["inference"]["device"] != "cpu":
            raise ValueError("Reference must be a CPU report")
        for key in ("model_revision", "images", "general_threshold", "character_threshold"):
            if reference[key] != report[key]:
                raise ValueError(f"Reference mismatch: {key}")
        if reference["inference"]["model_repo"] != settings.model_repo:
            raise ValueError("Reference model mismatch")
        with np.load(args.reference.parent / reference["scores_file"], allow_pickle=False) as baseline:
            if baseline["names"].tolist() != names or baseline["categories"].tolist() != categories:
                raise ValueError("Reference vocabulary mismatch")
            reference_scores = baseline["scores"]
            if reference_scores.shape != scores.shape:
                raise ValueError("Reference score shape mismatch")
            report["comparison"] = compare_scores(reference_scores, scores, names, categories,
                                                   args.general_threshold, args.character_threshold)
            passed = passed and report["comparison"]["passed"]
    if args.stability and device == "mps":
        passed = passed and all(value == "0" for value in report["environment"].values())
    report["passed"] = passed
    args.output.parent.mkdir(parents=True, exist_ok=True)
    scores_file = args.output.with_suffix(".scores.npz")
    np.savez_compressed(scores_file, names=np.asarray(names), categories=np.asarray(categories), scores=scores)
    report["scores_file"] = scores_file.name
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return report


def main(argv=None):
    args = parse_arguments(argv)
    try:
        report = benchmark(args)
    except Exception as error:
        print(f"Benchmark failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps({"output": str(args.output), "passed": report["passed"],
                      "device": report["inference"]["device"], "images_per_second": report["images_per_second"]}))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
