import pytest

from tools.benchmark_inference import compare_scores, memory_plateau, parse_arguments


def test_comparison_requires_probability_tolerance_and_same_rating():
    names, kinds = ["general", "sensitive", "blue_hair", "character"], ["rating", "rating", "general", "character"]
    baseline = [[.8, .2, .3501, .9001]]
    nearby = [[.8001, .1999, .3499, .8999]]
    report = compare_scores(baseline, nearby, names, kinds, .35, .90)
    assert report["passed"] and len(report["label_changes"]) == 2
    assert not compare_scores(baseline, [[.8, .2, .4, .9001]], names, kinds, .35, .90)["passed"]
    assert not compare_scores([[.5, .5001, .4, .95]], [[.5001, .5, .4, .95]], names, kinds, .35, .9)["passed"]
    with pytest.raises(ValueError, match="finite"):
        compare_scores(baseline, [[.8, .2, float("nan"), .9]], names, kinds, .35, .9)


def test_memory_plateau_ignores_initial_cache_but_rejects_sustained_growth():
    def samples(growing):
        return [{"rss_bytes": 100 * 1024 ** 2 + (i * 1024 ** 2 if growing else 0),
                 "mps_allocated_bytes": 40 * 1024 ** 2,
                 "mps_driver_bytes": (200 if i < 100 else 80) * 1024 ** 2} for i in range(1000)]
    assert memory_plateau(samples(False))["passed"]
    assert not memory_plateau(samples(True))["passed"]


def test_benchmark_cli_requires_directory_and_enough_iterations(tmp_path):
    args = ["--images", str(tmp_path), "--output", str(tmp_path / "report.json")]
    assert parse_arguments(args).iterations == 100
    with pytest.raises(SystemExit):
        parse_arguments(args + ["--iterations", "99"])
    with pytest.raises(SystemExit):
        parse_arguments(args + ["--stability"])
    with pytest.raises(SystemExit):
        parse_arguments(args + ["--reference", str(tmp_path / "report.json")])
