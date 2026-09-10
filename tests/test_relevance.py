import importlib.util
from pathlib import Path

MODULE = Path(__file__).resolve().parents[1] / "scripts" / "fetch_tenders.py"
spec = importlib.util.spec_from_file_location("fetch_tenders", MODULE)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def test_cloud_software_is_high_relevance():
    score, matched = mod.relevance("Cloud software platform and hosting", "SaaS implementation and support", ["72000000"])
    assert score >= 60
    assert matched


def test_unrelated_works_is_filtered_low():
    score, _ = mod.relevance("Road construction works", "Resurfacing and building works", ["45200000"])
    assert score < 12
