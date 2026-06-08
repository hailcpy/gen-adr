"""
Tests for scope edge-cases (issue #10):

  Sub-bug A: bad since:<ref> must return a structured halt, not raise RuntimeError.
  Sub-bug B: module scope must expose boundary-crossing file changes via --full-diff.
"""
import analyze
from conftest import repo


# --- Sub-bug A: bad since:<ref> --------------------------------------------------

def test_bad_since_ref_returns_halt():
    result = analyze.analyze(repo("repo_squash"), history_scope="since:nope-not-a-ref")
    assert result.get("halt") == "bad_ref"
    assert result["candidates"] == []


def test_valid_since_ref_works():
    result = analyze.analyze(repo("repo_squash"), history_scope="since:HEAD~3")
    assert result.get("halt") is None
    assert "candidates" in result


def test_since_ref_with_invalid_history_does_not_raise():
    try:
        result = analyze.analyze(repo("repo_squash"), history_scope="since:does-not-exist-abc123")
    except RuntimeError:
        raise AssertionError("RuntimeError raised for bogus ref; expected structured halt")
    assert result.get("halt") == "bad_ref"


def test_since_ref_from_unrelated_branch_returns_halt(tmp_path):
    """A ref that exists but is NOT an ancestor of HEAD (e.g. an orphan branch)
    must return ref_not_ancestor instead of silently analyzing an empty range
    or pulling in misleading commits.
    """
    import os, subprocess
    r = str(tmp_path)
    def sh(*args, env=None):
        env_full = {**os.environ, **(env or {})}
        subprocess.run(list(args), cwd=r, check=True, capture_output=True, env=env_full)
    date_env = lambda d: {
        "GIT_AUTHOR_DATE": f"{d}T12:00:00", "GIT_COMMITTER_DATE": f"{d}T12:00:00",
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
    }
    sh("git", "init", "-q", "-b", "main")
    sh("git", "config", "user.email", "t@t")
    sh("git", "config", "user.name", "t")
    with open(os.path.join(r, "a.txt"), "w") as f: f.write("a\n")
    sh("git", "add", "-A", env=date_env("2024-01-01"))
    sh("git", "commit", "-qm", "main commit", env=date_env("2024-01-01"))

    # orphan branch with its own commit — exists but is not an ancestor of HEAD
    sh("git", "checkout", "--orphan", "other")
    sh("git", "rm", "-rf", "--quiet", ".")
    with open(os.path.join(r, "b.txt"), "w") as f: f.write("b\n")
    sh("git", "add", "-A", env=date_env("2024-01-02"))
    sh("git", "commit", "-qm", "orphan commit", env=date_env("2024-01-02"))
    other_sha = subprocess.check_output(
        ["git", "-C", r, "rev-parse", "HEAD"], text=True).strip()
    sh("git", "checkout", "main")

    result = analyze.analyze(r, history_scope=f"since:{other_sha}")
    assert result.get("halt") == "ref_not_ancestor", result
    assert result.get("candidates") == []


# --- Sub-bug B: module scope exposes shared-path files via --full-diff -----------

def test_module_scope_exposes_shared_path_files():
    """Commits A and B both touch lib/bus.ts across the src/payments boundary.

    Before the --full-diff fix, Commit.files for each only showed src/payments/*,
    so A and B couldn't cluster on the shared file. After the fix they share
    lib/bus.ts and must land in the same candidate.
    """
    result = analyze.analyze(
        repo("repo_module_shared"),
        code_scope="module:src/payments",
    )
    assert result.get("halt") is None

    cands = result["candidates"]
    # Find the candidate that covers the two boundary-crossing commits
    shared_bus_cands = [
        c for c in cands
        if any("shared bus" in s for s in c["subjects"])
    ]
    assert len(shared_bus_cands) >= 1, "no candidate contains the boundary-crossing commits"

    # Both #1 and #2 commits must land in exactly ONE candidate
    c1_home = next(
        (c for c in cands if any("(#1)" in s for s in c["subjects"])), None
    )
    c2_home = next(
        (c for c in cands if any("(#2)" in s for s in c["subjects"])), None
    )
    assert c1_home is not None, "commit #1 (foo via shared bus) not found in any candidate"
    assert c2_home is not None, "commit #2 (bar via shared bus) not found in any candidate"
    assert c1_home["id"] == c2_home["id"], (
        f"boundary-crossing commits should cluster together; "
        f"#1 in {c1_home['id']}, #2 in {c2_home['id']}"
    )
    assert "lib/bus.ts" in c1_home["files"], (
        f"lib/bus.ts must appear in candidate files; got {c1_home['files']}"
    )


def test_repo_scope_unchanged():
    """Repo-scope (no pathspec) must not pass --full-diff and must still work."""
    result = analyze.analyze(repo("repo_squash"))
    assert result.get("halt") is None
    assert len(result["candidates"]) > 0
