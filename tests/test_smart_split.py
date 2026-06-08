"""
Tests for smart oversized-cluster splitting (issue #8):

  Replace the naive date-window slice with a greedy overlap-density peel, so a
  cohesive decision that happens to share a date window with unrelated commits
  isn't cut in half just to satisfy cfg.cluster_cap.
"""
import os
import subprocess

import analyze
from analyze import Commit, FileChange, Config


def _commit(sha, date, subject, *paths):
    return Commit(
        sha=sha, author="a", date=date, parents=[], subject=subject,
        files=[FileChange(status="M", path=p) for p in paths],
    )


def _ids(group):
    return sorted(c.sha for c in group)


# --- split_oversized=False sanity check --------------------------------------

def test_split_oversized_off_keeps_one_group():
    commits = [
        _commit(f"s{i}", f"2024-01-{i + 1:02d}", f"change {i}", "lib/strong.ts")
        for i in range(10)
    ]
    cfg = Config(cluster_cap=5, split_oversized=False)

    # exercise the real cluster() path: with split_oversized off, the oversized
    # group must stay intact and carry the WARN signal.
    chunks = [commits]
    chunk_ranges = {0: None}
    clusters, cluster_ranges, split_reasons = analyze.cluster(
        chunks, cfg, chunk_ranges=chunk_ranges,
    )
    assert len(clusters) == 1
    assert len(clusters[0]) == 10
    assert split_reasons.get(0) is None

    score, cls, signals = analyze.classify(clusters[0], cfg)
    assert any("WARN: oversized cluster" in s for s in signals)
    assert not any("INFO: split from oversized cluster" in s for s in signals)


# --- greedy overlap-density peel keeps cohesive sub-clusters together --------

def _interleaved_strong_other():
    strong = [_commit(f"strong{i}", f"2024-01-{2 * i + 1:02d}",
                      f"strong change {i}", "lib/strong.ts") for i in range(5)]
    other = [_commit(f"other{i}", f"2024-01-{2 * i + 2:02d}",
                     f"other change {i}", "lib/other.ts") for i in range(5)]
    merged = sorted(strong + other, key=lambda c: c.date)
    return merged, strong, other


def test_smart_split_keeps_strongest_subcluster_together():
    merged, strong, other = _interleaved_strong_other()
    cfg = Config(cluster_cap=5, split_oversized=True)
    spec = analyze.build_specificity(merged, cfg)

    sub_clusters, reasons = analyze._split_oversized(merged, cfg, spec)

    assert len(sub_clusters) == 2
    got = sorted(_ids(g) for g in sub_clusters)
    want = sorted([_ids(strong), _ids(other)])
    assert got == want, got
    assert all("overlap-density peel" in r for r in reasons)


def test_smart_split_integration_keeps_strongest_subcluster_together(tmp_path):
    """Full analyze() pipeline: build a tmp git repo where 10 commits interleave
    by date across two unrelated cohesive groups of 5, with cluster_cap=5 and
    split_oversized=True. The naive date-slicer would produce two 5-commit
    windows that mix both groups; the overlap-density peel must keep each
    group intact.
    """
    r = str(tmp_path)

    def sh(*args, env=None):
        env_full = {**os.environ, **(env or {})}
        subprocess.run(list(args), cwd=r, check=True, capture_output=True, env=env_full)

    def date_env(d):
        return {
            "GIT_AUTHOR_DATE": f"{d}T12:00:00", "GIT_COMMITTER_DATE": f"{d}T12:00:00",
            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
        }

    sh("git", "init", "-q", "-b", "main")
    sh("git", "config", "user.email", "t@t")
    sh("git", "config", "user.name", "t")

    mod = os.path.join(r, "lib", "mod")
    os.makedirs(mod, exist_ok=True)
    # `shared.ts` is touched by every commit — it's what merges all 11 commits
    # into ONE oversized cluster (the chunk/cluster signal). `strong.ts` /
    # `other.ts` are each touched only by their own cohesive half — that's the
    # stronger, more specific overlap the peel must use to keep each half
    # together rather than slicing by date.
    for fn in ("shared.ts", "strong.ts", "other.ts"):
        with open(os.path.join(mod, fn), "w") as f:
            f.write(f"// {fn}\n")
    sh("git", "add", "-A", env=date_env("2023-12-01"))
    sh("git", "commit", "-qm", "seed files", env=date_env("2023-12-01"))

    # interleave by date: strong, other, strong, other, ...
    day = 1
    for i in range(5):
        d = f"2024-01-{day:02d}"
        with open(os.path.join(mod, "shared.ts"), "a") as f:
            f.write(f"s{i}\n")
        with open(os.path.join(mod, "strong.ts"), "a") as f:
            f.write(f"line {i}\n")
        sh("git", "add", "-A", env=date_env(d))
        sh("git", "commit", "-qm", f"strongwidget tweak {i}", env=date_env(d))
        day += 1

        d = f"2024-01-{day:02d}"
        with open(os.path.join(mod, "shared.ts"), "a") as f:
            f.write(f"o{i}\n")
        with open(os.path.join(mod, "other.ts"), "a") as f:
            f.write(f"line {i}\n")
        sh("git", "add", "-A", env=date_env(d))
        sh("git", "commit", "-qm", f"otherwidget tweak {i}", env=date_env(d))
        day += 1

    cfg = Config(cluster_cap=5, split_oversized=True)
    result = analyze.analyze(r, cfg=cfg)
    assert result.get("halt") is None

    cands = result["candidates"]
    split_cands = [c for c in cands
                   if any("split from oversized cluster" in s for s in c["signals"])]
    # the 11 commits merge into one oversized cluster (all share shared.ts),
    # which the peel splits into: the 5 "strong" commits, the 5 "other"
    # commits, and the lone seed commit.
    assert len(split_cands) == 3, [c["subjects"] for c in cands]

    sized_5 = [c for c in split_cands if len(c["commits"]) == 5]
    assert len(sized_5) == 2
    for c in sized_5:
        subjects = " ".join(c["subjects"])
        # each split sub-cluster must be cohesive: either all "strongwidget" or
        # all "otherwidget" commits — never a date-windowed mix of both.
        assert ("strongwidget" in subjects) != ("otherwidget" in subjects), c["subjects"]

    singleton = [c for c in split_cands if len(c["commits"]) == 1]
    assert len(singleton) == 1
    assert singleton[0]["subjects"] == ["seed files"]


# --- singletons in the remainder ---------------------------------------------

def test_smart_split_singletons_in_remainder():
    strong = [_commit(f"strong{i}", f"2024-01-{i + 1:02d}", f"strong change {i}",
                      "lib/strong.ts") for i in range(4)]
    loners = [
        _commit("lone1", "2024-01-05", "adjust gamma routine", "lib/lonely1.ts"),
        _commit("lone2", "2024-01-06", "polish delta widget", "lib/lonely2.ts"),
    ]
    commits = sorted(strong + loners, key=lambda c: c.date)
    cfg = Config(cluster_cap=3, split_oversized=True)
    spec = analyze.build_specificity(commits, cfg)

    sub_clusters, reasons = analyze._split_oversized(commits, cfg, spec)

    sizes = sorted(len(g) for g in sub_clusters)
    assert sizes == [1, 1, 1, 3], sizes

    big = next(g for g in sub_clusters if len(g) == 3)
    assert _ids(big) == sorted(c.sha for c in strong[:3])

    singleton_shas = sorted(g[0].sha for g in sub_clusters if len(g) == 1)
    assert singleton_shas == sorted(["strong3", "lone1", "lone2"])


# --- determinism --------------------------------------------------------------

def test_smart_split_deterministic():
    merged, _, _ = _interleaved_strong_other()
    cfg = Config(cluster_cap=5, split_oversized=True)
    spec = analyze.build_specificity(merged, cfg)

    first, first_reasons = analyze._split_oversized(list(merged), cfg, spec)
    second, second_reasons = analyze._split_oversized(list(merged), cfg, spec)

    assert [_ids(g) for g in first] == [_ids(g) for g in second]
    assert first_reasons == second_reasons


# --- specificity filters out hot files ---------------------------------------

def test_smart_split_with_specificity_filters_hot_files():
    """A file present in every commit is not 'specific' once a df cap is set,
    so it must not drive the split. With no other shared signal, the peel
    should fall back to singletons rather than grouping by the hot file."""
    subjects = ["adjust gamma", "polish delta", "tweak epsilon", "refine zeta",
                "patch theta", "rework iota", "trim kappa", "shift lambda",
                "bump nu", "tune xi"]
    commits = [
        _commit(f"c{i}", f"2024-01-{i + 1:02d}", f"{subjects[i]} routine",
                "lib/hot.ts", f"lib/unique{i}.ts")
        for i in range(10)
    ]
    cfg = Config(cluster_cap=5, split_oversized=True, file_df_max=3, token_df_max=3)
    spec = analyze.build_specificity(commits, cfg)
    assert spec is not None

    # sanity: hot.ts is shared by all 10 commits, so it's filtered as non-specific
    assert spec.fdf["lib/hot.ts"] == 10
    assert "lib/hot.ts" not in spec.specific_files({"lib/hot.ts"})

    sub_clusters, reasons = analyze._split_oversized(commits, cfg, spec)

    # no two commits share a specific file or topic token -> all singletons
    assert all(len(g) == 1 for g in sub_clusters)
    assert len(sub_clusters) == 10

    # without the spec filter (None), the hot file WOULD bind everything into
    # one big overlap-connected group instead of singletons.
    no_spec_clusters, _ = analyze._split_oversized(commits, cfg, None)
    assert any(len(g) > 1 for g in no_spec_clusters)
