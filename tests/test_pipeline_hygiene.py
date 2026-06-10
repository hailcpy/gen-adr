"""
Tests for issue #39: build_coupling's per-commit changeset cap
(coupling_max_changeset, config-overridable) and detect_strategy's rev_range
scoping.

Also tests for issue #38: mixed-strategy affinity-grouping of leftover
linear commits, and the --strategy CLI override that bypasses detection.
"""
import os
import tempfile

import analyze
from analyze import Config
from test_diff_content import _sh, _init_repo, _commit, _date_env


# ---------------------------------------------------------------------------
# build_coupling: changeset cap
# ---------------------------------------------------------------------------

def test_build_coupling_caps_mega_commit_changeset():
    """A commit touching more files than coupling_max_changeset contributes no
    pairs under the default cap (30), but does once the cap is raised."""
    with tempfile.TemporaryDirectory() as tmp:
        _init_repo(tmp)
        with open(os.path.join(tmp, "x.py"), "w") as f:
            f.write("x\n")
        with open(os.path.join(tmp, "y.py"), "w") as f:
            f.write("y\n")
        _commit(tmp, "init", "2024-01-01")

        # 4 more commits touching x.py and y.py together -> pair_revs == 5,
        # file_revs[x] == file_revs[y] == 5
        for i in range(2, 6):
            with open(os.path.join(tmp, "x.py"), "a") as f:
                f.write(f"x{i}\n")
            with open(os.path.join(tmp, "y.py"), "a") as f:
                f.write(f"y{i}\n")
            _commit(tmp, f"chore: edit {i}", f"2024-01-0{i}")

        # mega commit: touches x.py, y.py, plus 98 new files (100 total)
        with open(os.path.join(tmp, "x.py"), "a") as f:
            f.write("xmega\n")
        with open(os.path.join(tmp, "y.py"), "a") as f:
            f.write("ymega\n")
        for i in range(98):
            with open(os.path.join(tmp, f"gen_{i}.py"), "w") as f:
                f.write("g\n")
        _commit(tmp, "chore: vendor dump", "2024-01-06")

        key = frozenset({"x.py", "y.py"})

        # default cap (30) skips the 100-file mega commit for pair enumeration:
        # pair_revs == 5, file_revs == 6 each -> denom = 6+6-5 = 7
        default_coupling = analyze.build_coupling(tmp)
        assert key in default_coupling
        assert abs(default_coupling[key] - 5 / 7) < 1e-9

        # raising the cap lets the mega commit contribute a pair too:
        # pair_revs == 6, file_revs == 6 each -> denom = 6
        loose_coupling = analyze.build_coupling(tmp, cfg=Config(coupling_max_changeset=200))
        assert key in loose_coupling
        assert abs(loose_coupling[key] - 1.0) < 1e-9


# ---------------------------------------------------------------------------
# detect_strategy: rev_range scoping
# ---------------------------------------------------------------------------

def test_detect_strategy_scoped_to_rev_range():
    """A repo whose merge-heavy history predates a switch to squash merges is
    classified by the scoped window, not by dead full history."""
    with tempfile.TemporaryDirectory() as tmp:
        _init_repo(tmp)
        with open(os.path.join(tmp, "README.md"), "w") as f:
            f.write("init\n")
        _commit(tmp, "init", "2024-01-01")

        for i in range(3):
            branch = f"feature{i}"
            _sh("git", "checkout", "-q", "-b", branch, cwd=tmp)
            with open(os.path.join(tmp, f"feat{i}.txt"), "w") as f:
                f.write("x\n")
            _commit(tmp, f"feat: feature {i}", f"2024-01-0{i + 2}")
            _sh("git", "checkout", "-q", "main", cwd=tmp)
            _sh("git", "merge", "--no-ff", "-q", "-m", f"Merge feature {i}", branch,
                cwd=tmp, env=_date_env(f"2024-01-0{i + 2}"))

        _sh("git", "tag", "v1", cwd=tmp)

        # post-v1: a single squash-style commit, no more merges
        with open(os.path.join(tmp, "squash.txt"), "w") as f:
            f.write("y\n")
        _commit(tmp, "feat: add widget (#42)", "2024-02-01")

        full = analyze.detect_strategy(tmp, pathspec=None)
        assert full == "mixed"  # >=3 merges AND a squash-style subject overall

        scoped = analyze.detect_strategy(tmp, pathspec=None, rev_range="v1..HEAD")
        assert scoped == "squash-boundary"  # no merges in this window


def test_merge_chunk_enumeration_scoped_to_rev_range():
    """A merge from before the `since:<ref>` window must not leak its commits
    (or files) into the candidate set, even when later merges select
    merge-boundary chunking for the scoped run."""
    with tempfile.TemporaryDirectory() as tmp:
        _init_repo(tmp)
        with open(os.path.join(tmp, "README.md"), "w") as f:
            f.write("init\n")
        _commit(tmp, "init", "2024-01-01")

        # pre-v1 merge bringing in src/old.py
        os.makedirs(os.path.join(tmp, "src"), exist_ok=True)
        _sh("git", "checkout", "-q", "-b", "old-feature", cwd=tmp)
        with open(os.path.join(tmp, "src", "old.py"), "w") as f:
            f.write("old\n")
        _commit(tmp, "feat: old feature", "2024-01-02")
        _sh("git", "checkout", "-q", "main", cwd=tmp)
        _sh("git", "merge", "--no-ff", "-q", "-m", "Merge old feature", "old-feature",
            cwd=tmp, env=_date_env("2024-01-02"))

        _sh("git", "tag", "v1", cwd=tmp)

        # post-v1: 3 merges, no squash-style subjects -> merge-boundary
        for i in range(3):
            branch = f"feature{i}"
            _sh("git", "checkout", "-q", "-b", branch, cwd=tmp)
            with open(os.path.join(tmp, "src", f"new{i}.py"), "w") as f:
                f.write("new\n")
            _commit(tmp, f"feat: new feature {i}", f"2024-02-0{i + 1}")
            _sh("git", "checkout", "-q", "main", cwd=tmp)
            _sh("git", "merge", "--no-ff", "-q", "-m", f"Merge feature {i}", branch,
                cwd=tmp, env=_date_env(f"2024-02-0{i + 1}"))

        scoped = analyze.detect_strategy(tmp, pathspec=None, rev_range="v1..HEAD")
        assert scoped == "merge-boundary"

        result = analyze.analyze(tmp, history_scope="since:v1")
        assert result["strategy"] == "merge-boundary"

        all_files = {f for c in result["candidates"] for f in c["files"]}
        assert "src/old.py" not in all_files


# ---------------------------------------------------------------------------
# issue #38: mixed strategy groups leftover linear commits; --strategy override
# ---------------------------------------------------------------------------

def _build_merge_heavy_repo_with_linear_tail(tmp):
    """3 squash-referenced merges, then 4 related linear commits all touching
    src/widget.py with a shared "widget" topic token — none covered by a
    merge range."""
    _init_repo(tmp)
    with open(os.path.join(tmp, "README.md"), "w") as f:
        f.write("init\n")
    _commit(tmp, "init", "2024-01-01")

    for i in range(3):
        branch = f"feature{i}"
        _sh("git", "checkout", "-q", "-b", branch, cwd=tmp)
        with open(os.path.join(tmp, f"feat{i}.txt"), "w") as f:
            f.write("x\n")
        _commit(tmp, f"feat: feature {i} (#{i + 1})", f"2024-01-0{i + 2}")
        _sh("git", "checkout", "-q", "main", cwd=tmp)
        _sh("git", "merge", "--no-ff", "-q", "-m", f"Merge feature {i}", branch,
            cwd=tmp, env=_date_env(f"2024-01-0{i + 2}"))

    os.makedirs(os.path.join(tmp, "src"), exist_ok=True)
    for i in range(4):
        with open(os.path.join(tmp, "src", "widget.py"), "a") as f:
            f.write(f"line {i}\n")
        _commit(tmp, f"feat: widget step {i}", f"2024-02-0{i + 1}")


def test_mixed_strategy_groups_leftover_linear_commits():
    """Issue #38: a run of related linear commits not covered by any merge
    range affinity-groups into one chunk, not 4 singletons."""
    with tempfile.TemporaryDirectory() as tmp:
        _build_merge_heavy_repo_with_linear_tail(tmp)

        strategy = analyze.detect_strategy(tmp, pathspec=None)
        assert strategy == "mixed"

        cfg = Config()
        commits = analyze.parse_log(tmp, rev_range=None, pathspec=None)
        coupling = analyze.build_coupling(tmp, cfg=cfg)
        communities = analyze.build_leiden_communities(tmp)
        cfg.file_df_max, cfg.token_df_max, cfg.dir_df_max = \
            analyze.resolve_specificity_caps(cfg, len(commits))
        spec = analyze.build_specificity(commits, cfg)

        chunks, ranges = analyze.chunk_commits(
            commits, strategy, cfg, repo=tmp, pathspec=None,
            coupling=coupling, communities=communities, spec=spec,
        )

        widget_chunks = [c for c in chunks
                         if all("widget" in commit.subject for commit in c)]
        assert len(widget_chunks) == 1, \
            f"expected the 4 widget commits in one chunk, got {widget_chunks}"
        assert len(widget_chunks[0]) == 4


def test_strategy_direct_commit_bypasses_merge_enumeration():
    """Issue #38: --strategy direct-commit on a merge-heavy repo skips merge
    enumeration entirely — every chunk range is None, never a merge range."""
    with tempfile.TemporaryDirectory() as tmp:
        _build_merge_heavy_repo_with_linear_tail(tmp)

        # auto-detection would pick "mixed" (merge-boundary chunks for the
        # 3 features), but the override forces direct-commit chunking.
        assert analyze.detect_strategy(tmp, pathspec=None) == "mixed"

        cfg = Config()
        commits = analyze.parse_log(tmp, rev_range=None, pathspec=None)
        coupling = analyze.build_coupling(tmp, cfg=cfg)
        communities = analyze.build_leiden_communities(tmp)
        cfg.file_df_max, cfg.token_df_max, cfg.dir_df_max = \
            analyze.resolve_specificity_caps(cfg, len(commits))
        spec = analyze.build_specificity(commits, cfg)

        chunks, ranges = analyze.chunk_commits(
            commits, "direct-commit", cfg, repo=tmp, pathspec=None,
            coupling=coupling, communities=communities, spec=spec,
        )

        assert sum(len(c) for c in chunks) == len(commits)
        assert all(r is None for r in ranges.values())


def test_analyze_strategy_override_reported_in_manifest():
    """Issue #38: analyze(..., strategy="direct-commit") overrides detection
    and the override is reflected in manifest["strategy"]."""
    with tempfile.TemporaryDirectory() as tmp:
        _build_merge_heavy_repo_with_linear_tail(tmp)

        auto = analyze.analyze(tmp)
        assert auto["strategy"] == "mixed"

        forced = analyze.analyze(tmp, strategy="direct-commit")
        assert forced["strategy"] == "direct-commit"
