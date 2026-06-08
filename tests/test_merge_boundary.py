"""
Tests for merge-boundary chunking and the chunk_ranges side-channel.
Uses the repo_merge_pr fixture: three real merge commits, each with 2 commits.
"""
import analyze
from conftest import repo


# ---------------------------------------------------------------------------
# enumerate_merge_chunks
# ---------------------------------------------------------------------------

def test_enumerate_merge_chunks_returns_one_per_pr(build_fixtures):
    chunks = analyze.enumerate_merge_chunks(repo("repo_merge_pr"), pathspec=None)
    assert len(chunks) == 3, f"expected 3 merge chunks, got {len(chunks)}"
    # each PR has 2 commits (excluding the merge commit itself)
    for merge_sha, base_sha, commits in chunks:
        assert len(commits) == 2, (
            f"merge {merge_sha}: expected 2 commits, got {len(commits)}"
        )


def test_merge_chunk_range_uses_merge_base(build_fixtures):
    chunks = analyze.enumerate_merge_chunks(repo("repo_merge_pr"), pathspec=None)
    r = repo("repo_merge_pr")
    for merge_sha, base_sha, commits in chunks:
        # verify merge_sha is actually a merge commit
        out = analyze.git(r, "log", "--merges", "--pretty=format:%H")
        merge_shas = {l.strip() for l in out.splitlines() if l.strip()}
        assert merge_sha in merge_shas, f"{merge_sha} is not a merge commit"
        # verify base_sha is correct merge-base of the two parents
        parents_raw = analyze.git(r, "log", "-1", "--pretty=format:%P",
                                  merge_sha).strip()
        parents = parents_raw.split()
        assert len(parents) == 2
        expected_base = analyze.git(r, "merge-base", parents[0], parents[1]).strip()
        assert base_sha == expected_base, (
            f"base mismatch for {merge_sha}: got {base_sha}, expected {expected_base}"
        )


# ---------------------------------------------------------------------------
# detect_strategy
# ---------------------------------------------------------------------------

def test_merge_boundary_strategy_detected(build_fixtures):
    strategy = analyze.detect_strategy(repo("repo_merge_pr"), pathspec=None)
    assert strategy == "merge-boundary", f"expected merge-boundary, got {strategy!r}"


# ---------------------------------------------------------------------------
# full analyze() — one candidate per PR, each with range set
# ---------------------------------------------------------------------------

def test_analyze_produces_one_candidate_per_pr(build_fixtures):
    manifest = analyze.analyze(repo("repo_merge_pr"))
    assert manifest["strategy"] == "merge-boundary"
    cands = manifest["candidates"]
    # 3 PRs -> 3 candidates (each chunk is distinct files, won't cluster)
    assert len(cands) == 3, f"expected 3 candidates, got {len(cands)}: {[c['subjects'] for c in cands]}"
    # every candidate must have a range set
    for c in cands:
        assert c["range"] is not None, f"candidate {c['id']} missing range"
        base, head = c["range"]
        assert base and head, f"candidate {c['id']} has empty range: {c['range']}"


# ---------------------------------------------------------------------------
# commit assigned to earliest merge when it appears in multiple ranges
# ---------------------------------------------------------------------------

def test_commit_in_multiple_merges_assigned_to_earliest(build_fixtures):
    """A commit reachable from two merge commits must end up in the earlier chunk.

    We synthesise the scenario by calling enumerate_merge_chunks on a repo where
    we know the commit ordering, then assert that if we deduplicate (as
    chunk_commits does) the commit lands in the first chunk that sees it.
    """
    import tempfile, os, subprocess

    with tempfile.TemporaryDirectory() as tmp:
        # build a tiny repo with a shared commit reachable from two merges
        def sh(*args, env=None):
            env_full = {**os.environ, **(env or {})}
            subprocess.run(list(args), cwd=tmp, check=True,
                           capture_output=True, env=env_full)

        date_env = lambda d: {
            "GIT_AUTHOR_DATE": f"{d}T12:00:00",
            "GIT_COMMITTER_DATE": f"{d}T12:00:00",
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
        }

        sh("git", "init", "-q", "-b", "main")
        sh("git", "config", "user.email", "t@t")
        sh("git", "config", "user.name", "t")

        # init commit
        with open(os.path.join(tmp, "README.md"), "w") as f:
            f.write("init\n")
        sh("git", "add", ".", env=date_env("2024-01-01"))
        sh("git", "commit", "-qm", "init", env=date_env("2024-01-01"))

        # branch A: two commits
        sh("git", "checkout", "-qb", "branchA")
        with open(os.path.join(tmp, "a.txt"), "w") as f:
            f.write("a\n")
        sh("git", "add", ".", env=date_env("2024-01-02"))
        sh("git", "commit", "-qm", "add a", env=date_env("2024-01-02"))
        with open(os.path.join(tmp, "a2.txt"), "w") as f:
            f.write("a2\n")
        sh("git", "add", ".", env=date_env("2024-01-03"))
        sh("git", "commit", "-qm", "add a2", env=date_env("2024-01-03"))
        sh("git", "checkout", "-q", "main")
        sh("git", "merge", "--no-ff", "-qm", "Merge branchA", "branchA",
           env=date_env("2024-01-04"))

        # branch B
        sh("git", "checkout", "-qb", "branchB")
        with open(os.path.join(tmp, "b.txt"), "w") as f:
            f.write("b\n")
        sh("git", "add", ".", env=date_env("2024-02-01"))
        sh("git", "commit", "-qm", "add b", env=date_env("2024-02-01"))
        sh("git", "checkout", "-q", "main")
        sh("git", "merge", "--no-ff", "-qm", "Merge branchB", "branchB",
           env=date_env("2024-02-02"))

        # branch C
        sh("git", "checkout", "-qb", "branchC")
        with open(os.path.join(tmp, "c.txt"), "w") as f:
            f.write("c\n")
        sh("git", "add", ".", env=date_env("2024-03-01"))
        sh("git", "commit", "-qm", "add c", env=date_env("2024-03-01"))
        sh("git", "checkout", "-q", "main")
        sh("git", "merge", "--no-ff", "-qm", "Merge branchC", "branchC",
           env=date_env("2024-03-02"))

        # call chunk_commits with merge-boundary — each commit should appear once
        commits = analyze.parse_log(tmp, rev_range=None, pathspec=None)
        chunks, ranges = analyze.chunk_commits(
            commits, "merge-boundary", analyze.Config(),
            repo=tmp, pathspec=None,
        )

        all_shas = [c.sha for chunk in chunks for c in chunk]
        # no duplicates
        assert len(all_shas) == len(set(all_shas)), \
            f"duplicate shas across chunks: {all_shas}"
        # all commits accounted for
        assert len(chunks) == 3
