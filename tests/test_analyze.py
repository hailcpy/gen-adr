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


# --- co-change coupling unit tests -------------------------------------------

def test_build_coupling_jaccard_math():
    """Jaccard formula: shared / (a + b - shared)."""
    # Manually construct the same structure build_coupling would produce.
    # File A: 10 revisions, File B: 8 revisions, shared: 6
    # Jaccard = 6 / (10 + 8 - 6) = 6/12 = 0.5
    file_revs = {"a.py": 10, "b.py": 8}
    pair_revs = {frozenset({"a.py", "b.py"}): 6}

    coupling = {}
    for pair, shared in pair_revs.items():
        if shared < analyze.COUPLING_MIN_SHARED:
            continue
        fa, fb = tuple(pair)
        denom = file_revs[fa] + file_revs[fb] - shared
        score = shared / denom if denom else 0.0
        if score >= analyze.COUPLING_MIN_SCORE:
            coupling[pair] = score

    assert frozenset({"a.py", "b.py"}) in coupling
    assert abs(coupling[frozenset({"a.py", "b.py"})] - 0.5) < 1e-9


def test_build_coupling_min_shared_gate():
    """Pairs with fewer than COUPLING_MIN_SHARED co-appearances are excluded."""
    file_revs = {"x.py": 10, "y.py": 10}
    pair_revs = {frozenset({"x.py", "y.py"}): analyze.COUPLING_MIN_SHARED - 1}

    coupling = {}
    for pair, shared in pair_revs.items():
        if shared < analyze.COUPLING_MIN_SHARED:
            continue
        fa, fb = tuple(pair)
        denom = file_revs[fa] + file_revs[fb] - shared
        score = shared / denom if denom else 0.0
        if score >= analyze.COUPLING_MIN_SCORE:
            coupling[pair] = score

    assert len(coupling) == 0


def test_shares_coupling_substitutes_for_topic():
    """file_overlap + coupling should merge even without topic-token overlap."""
    # coupling keys use full repo-relative paths, same as git log --name-status
    coupling = {frozenset({"services/cache/cache.py", "services/cache/cache_config.yaml"}): 0.75}
    assert analyze._shares(
        {"services/cache"}, {"services/cache"},        # dir overlap
        {"redis"}, {"memcached"},                      # NO topic overlap
        {"services/cache/cache.py"}, {"services/cache/cache_config.yaml"},
        coupling=coupling,
    )


def test_shares_coupling_substitutes_for_file_overlap():
    """topic_overlap + coupling should merge even without shared files/dirs."""
    coupling = {frozenset({"src/auth.py", "src/session.py"}): 0.60}
    assert analyze._shares(
        {"src/auth"}, {"src/session"},                 # NO dir overlap (different deep dirs)
        {"auth", "token"},  {"auth", "session"},       # topic overlap: "auth"
        {"src/auth.py"}, {"src/session.py"},
        coupling=coupling,
    )


def test_shares_coupling_alone_does_not_merge():
    """Coupling alone (without any other signal) must NOT trigger a merge."""
    coupling = {frozenset({"a.py", "b.py"}): 0.90}
    assert not analyze._shares(
        {"module/a"}, {"module/b"},                    # different deep dirs
        {"alpha"},    {"beta"},                        # no topic overlap
        {"a.py"},     {"b.py"},
        coupling=coupling,
    )


def test_shares_no_coupling_unchanged():
    """Without coupling, original AND-gate behaviour is preserved."""
    # file overlap + topic overlap → merge
    assert analyze._shares(
        {"src"}, {"src"}, {"redis"}, {"redis"},
        {"src/cache.py"}, {"src/cache.py"},
        coupling=None,
    )
    # file overlap only → no merge
    assert not analyze._shares(
        {"src"}, {"src"}, {"foo"}, {"bar"},
        {"src/cache.py"}, {"src/cache.py"},
        coupling=None,
    )


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


# --- module-relative dir overlap ---------------------------------------------

def _commit(sha, subject, *paths):
    return analyze.Commit(
        sha=sha, author="a", date="2024-01-01", parents=[], subject=subject,
        files=[analyze.FileChange(status="M", path=p) for p in paths],
    )


def test_module_root_does_not_bind_under_module_scope():
    """Two commits both living directly in the module root must NOT dir-overlap.

    Without the relative-depth shift, every file under a depth-2 module shares
    the `services/fetch_findings` prefix at absolute depth 2, so file_overlap
    fires for every pair and the whole module collapses into one mega-cluster.
    """
    a = _commit("a", "add rapid7", "services/fetch_findings/vendor.py")
    b = _commit("b", "add sentinelone", "services/fetch_findings/services.py")

    repo_cfg = analyze.Config()  # no module scope → coarse prefix still binds
    assert a.dirs_deep(repo_cfg) & b.dirs_deep(repo_cfg)

    mod_cfg = analyze.Config(module_prefix="services/fetch_findings")
    assert not (a.dirs_deep(mod_cfg) & b.dirs_deep(mod_cfg)), \
        "module root must not bind two commits under module scope"


def test_first_subdir_inside_module_binds():
    """Relative depth 1 (the first subdir inside the module) is the bind unit."""
    a = _commit("a", "stream a", "services/fetch_findings/correlation/engine.py")
    b = _commit("b", "stream b", "services/fetch_findings/correlation/state.py")
    cfg = analyze.Config(module_prefix="services/fetch_findings")
    assert a.dirs_deep(cfg) & b.dirs_deep(cfg) == {"services/fetch_findings/correlation"}


def test_module_scope_no_mega_cluster():
    """End-to-end: distinct subsystems inside one deep module stay distinct.

    repo_deepmodule packs correlation + streaming under services/findings, all
    sharing the module prefix and the "findings" topic token. Pre-fix this
    collapsed into one mega-cluster; now it must split by subsystem.
    """
    cands = candidates("repo_deepmodule", code="module:services/findings")
    assert len(cands) == 2, \
        f"expected 2 subsystem candidates, got {len(cands)}"
    corr = candidate_with(cands, "correlation")
    stream = candidate_with(cands, "streaming")
    assert corr is not None and stream is not None
    assert id(corr) != id(stream), "correlation and streaming must not merge"
