"""
Tests for issue #39: build_coupling's per-commit changeset cap
(coupling_max_changeset, config-overridable) and detect_strategy's rev_range
scoping.
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
