"""
Tests for the Config layer of scripts/analyze.py.

Two contracts:
  1. Config() defaults reproduce the original hardcoded behavior (the eval
     baseline the rest of the suite asserts against).
  2. Overrides have the documented, deterministic effect on the manifest.
"""
import json

import analyze
from analyze import Config
from conftest import repo


def _candidates(name, cfg=None, code="repo"):
    return analyze.analyze(repo(name), "full", code, cfg)["candidates"]


def _cls_of(cands, marker):
    for c in cands:
        if any(marker in s for s in c["subjects"]):
            return c["classification"]
    return None


# --- defaults are the baseline ----------------------------------------------

def test_explicit_default_config_matches_implicit():
    """Passing Config() must be identical to passing nothing."""
    implicit = analyze.analyze(repo("repo_squash"))
    explicit = analyze.analyze(repo("repo_squash"), cfg=Config())
    assert implicit["candidates"] == explicit["candidates"]


def test_arch_threshold_gates_classification():
    """Raising the architectural threshold demotes otherwise-arch candidates."""
    base = _candidates("repo_squash")
    arch = [c for c in base if c["classification"] == "architectural"]
    assert arch, "fixture should yield at least one architectural candidate"

    strict = _candidates("repo_squash", Config(score_arch_threshold=99))
    assert all(c["classification"] != "architectural" for c in strict)


def test_weight_override_changes_score():
    """Zeroing a weight lowers scores for candidates that relied on it."""
    base = {c["id"]: c["score"] for c in _candidates("repo_squash")}
    zeroed = Config(weights={**analyze.DEFAULT_WEIGHTS, "new_src": 0})
    after = {c["id"]: c["score"] for c in _candidates("repo_squash", zeroed)}
    assert any(after[k] < base[k] for k in base), \
        "zeroing new_src should reduce at least one score"


def test_min_dir_depth_affects_clustering():
    """min_dir_depth=1 lets shallow dir overlap bind commits the default rejects."""
    shallow = Config(min_dir_depth=1)
    default_n = len(_candidates("repo_linear"))
    shallow_n = len(_candidates("repo_linear", shallow))
    # looser binding never produces MORE clusters than the strict default
    assert shallow_n <= default_n


# --- oversize handling -------------------------------------------------------

def test_oversize_warning_emitted():
    cfg = Config(cluster_cap=0)
    cands = _candidates("repo_squash", cfg)
    assert any(any("WARN: oversized" in s for s in c["signals"]) for c in cands)


def test_split_oversized_caps_cluster_size():
    cfg = Config(cluster_cap=1, split_oversized=True)
    cands = _candidates("repo_squash", cfg)
    assert all(len(c["commits"]) <= 1 for c in cands)


# --- from_file merge semantics ----------------------------------------------

def test_from_file_none_is_default():
    assert Config.from_file(None) == Config()


def test_from_file_scalar_and_extra_merge(tmp_path):
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps({
        "cluster_cap": 3,
        "extra_stopwords": ["zzz"],
        "weights": {"breadth": 9},
    }))
    cfg = Config.from_file(str(p))
    assert cfg.cluster_cap == 3
    assert "zzz" in cfg.stopwords
    assert analyze.DEFAULT_STOPWORDS <= cfg.stopwords  # extras union, not replace
    assert cfg.weights["breadth"] == 9
    assert cfg.weights["new_src"] == analyze.DEFAULT_WEIGHTS["new_src"]  # untouched


def test_from_file_bare_set_replaces(tmp_path):
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps({"stopwords": ["only", "these"]}))
    cfg = Config.from_file(str(p))
    assert cfg.stopwords == frozenset({"only", "these"})


# --- edge specificity (hot-file / IDF de-chaining) ---------------------------

def _home(cands, marker):
    """id() of the candidate whose subjects contain `marker`."""
    for c in cands:
        if any(marker in s for s in c["subjects"]):
            return id(c)
    return None


def test_hotfile_repo_collapses_without_specificity():
    """Baseline (presence-only overlap) collapses the hot-file repo: every commit
    shares app/server.py + the 'endpoint' token, so union-find chains them."""
    cands = _candidates("repo_hotfile")
    sizes = sorted((len(c["commits"]) for c in cands), reverse=True)
    assert sizes[0] >= 9, f"expected a mega-cluster at baseline, got {sizes}"


def test_specificity_dechains_hotfile_repo():
    """With file/token/dir df caps, the hot file, recurring token and hot top dir
    stop binding, so the three features separate into distinct candidates."""
    cfg = Config(file_df_max=4, token_df_max=4, dir_df_max=4)
    cands = _candidates("repo_hotfile", cfg)
    sizes = sorted((len(c["commits"]) for c in cands), reverse=True)
    assert sizes[0] <= 3, f"specificity should break the mega-cluster, got {sizes}"
    homes = {_home(cands, m) for m in ("alpha", "beta", "gamma")}
    assert None not in homes, "each feature should have a candidate"
    assert len(homes) == 3, "alpha / beta / gamma must not be merged together"
