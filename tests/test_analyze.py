"""
Deterministic eval suite for scripts/analyze.py.

No Claude, no network — pure unit tests over synthetic fixtures with a known
answer key (fixtures/labels.json). Covers:
  - strategy detection
  - cross-PR clustering (the headline behavior)
  - classification precision / recall / F1
  - failure modes (shallow halt)
  - scope-aware output paths
  - documented clustering limitation (cross-module, no shared file)
"""
import analyze
from conftest import repo


# --- helpers -----------------------------------------------------------------

def candidates(name, history="full", code="repo"):
    return analyze.analyze(repo(name), history, code)["candidates"]


def candidate_with(cands, marker):
    """Return the candidate whose commit subjects contain `marker`, or None."""
    for c in cands:
        if any(marker in s for s in c["subjects"]):
            return c
    return None


def classification_of(cands, marker):
    c = candidate_with(cands, marker)
    return c["classification"] if c else None


# --- strategy detection ------------------------------------------------------

def test_strategy_detection(labels):
    for name in ("repo_squash", "repo_linear", "repo_crossmod"):
        if "strategy" in labels[name]:
            m = analyze.analyze(repo(name))
            assert m["strategy"] == labels[name]["strategy"], name


# --- clustering: the headline behavior ---------------------------------------

def test_cross_pr_clustering(labels):
    """Non-adjacent PRs that form one decision must land in one candidate."""
    cands = candidates("repo_squash")
    for cluster in labels["repo_squash"]["clusters"]:
        homes = {id(candidate_with(cands, m)) for m in cluster}
        assert None not in [candidate_with(cands, m) for m in cluster], \
            f"some markers in {cluster} not found"
        assert len(homes) == 1, \
            f"markers {cluster} should share ONE candidate, found {len(homes)}"


def test_linear_affinity_clustering(labels):
    cands = candidates("repo_linear")
    for cluster in labels["repo_linear"]["clusters"]:
        homes = {id(candidate_with(cands, m)) for m in cluster}
        assert len(homes) == 1, f"{cluster} should share one candidate"


def test_no_overclustering(labels):
    """Unrelated noise must NOT get pulled into a decision candidate."""
    cands = candidates("repo_squash")
    kafka = candidate_with(cands, "(#12)")
    # the test-only and bump commits must not be inside the kafka candidate
    assert not any("(#14)" in s for s in kafka["subjects"])
    assert not any("(#15)" in s for s in kafka["subjects"])


# --- classification precision / recall ---------------------------------------

def _pr_f1(cands, arch_markers, skip_markers):
    tp = fp = fn = 0
    for m in arch_markers:
        cls = classification_of(cands, m)
        if cls == "architectural":
            tp += 1
        else:
            fn += 1
    for m in skip_markers:
        cls = classification_of(cands, m)
        if cls == "architectural":
            fp += 1
    precision = tp / (tp + fp) if (tp + fp) else 1.0
    recall = tp / (tp + fn) if (tp + fn) else 1.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    return precision, recall, f1


def test_classification_precision_recall(labels):
    """On clean fixtures precision must be perfect (no fabricated decisions)
    and recall must be high."""
    cands = candidates("repo_squash")
    p, r, f1 = _pr_f1(
        cands,
        labels["repo_squash"]["architectural_markers"],
        labels["repo_squash"]["skip_markers"],
    )
    # precision is the gate: a false ADR is worse than a missed one
    assert p == 1.0, f"precision {p} — noise classified as architectural"
    assert r >= 0.8, f"recall {r} too low"


def test_noise_is_skipped(labels):
    cands = candidates("repo_squash")
    for m in labels["repo_squash"]["skip_markers"]:
        assert classification_of(cands, m) == "skip", f"{m} should be skipped"


def test_evidenced_swap_is_architectural():
    """moment -> date-fns swap (dep delete + add) must be flagged."""
    cands = candidates("repo_squash")
    assert classification_of(cands, "(#30)") == "architectural"


# --- failure modes -----------------------------------------------------------

def test_shallow_clone_halts(labels):
    m = analyze.analyze(repo("repo_shallow"))
    assert m.get("halt") == "shallow_clone"
    assert m["candidates"] == []


# --- scope-aware output paths ------------------------------------------------

def test_module_scope_output_paths(labels):
    for module, expected in labels["repo_modules"]["module_scope"].items():
        m = analyze.analyze(repo("repo_modules"), code_scope=f"module:{module}")
        assert m["preflight"]["output_dir"] == expected
        for c in m["candidates"]:
            assert c["output_dir"] == expected


def test_repo_scope_default_path(labels):
    m = analyze.analyze(repo("repo_modules"))
    assert m["preflight"]["output_dir"] == labels["repo_modules"]["repo_scope_output"]


def test_module_scope_isolates_commits():
    """module:src/payments must not surface the auth commit."""
    cands = candidates("repo_modules", code="module:src/payments")
    assert candidate_with(cands, "(#1)") is not None      # stripe/payments
    assert candidate_with(cands, "(#2)") is None           # jose/auth excluded


# --- documented limitation (xfail) -------------------------------------------

import pytest


@pytest.mark.xfail(reason="deterministic clustering cannot merge cross-module "
                          "decisions with no shared files; v2-embeddings case",
                   strict=True)
def test_crossmodule_clustering_is_a_known_gap(labels):
    cands = candidates("repo_crossmod")
    markers = labels["repo_crossmod"]["known_limitation"]["should_cluster"]
    homes = {id(candidate_with(cands, m)) for m in markers}
    # IDEAL behavior (currently fails): both grpc PRs in one candidate.
    assert len(homes) == 1
