"""
Performance regression test for issue #7 (inverted-index candidate generation):

  `cluster()` and `_affinity_groups()` used to score every pair of n entities
  (O(n^2) `_shares` calls). On a 500-chunk history that's 124,750 calls. The
  inverted-index candidate generator (`_candidate_pairs`) should only surface
  pairs that actually share a file/token/dir/community/coupling edge, so the
  number of `_shares_with_reasons` calls stays far below n*(n-1)/2.
"""
import analyze
from analyze import Commit, FileChange, Config


def _commit(sha, date, subject, *paths):
    return Commit(
        sha=sha, author="a", date=date, parents=[], subject=subject,
        files=[FileChange(status="M", path=p) for p in paths],
    )


def _synthetic_commits(n: int) -> list[Commit]:
    """n commits, each touching 2-3 files from its own small file group, with
    a group-distinctive subject token — no globally hot file or word, so the
    candidate set stays sparse."""
    commits = []
    for i in range(n):
        group = i // 3  # 3 commits per file group
        paths = [f"lib/group{group}/file{k}.ts" for k in range(2)]
        if i % 3 == 1:
            paths.append(f"lib/group{group}/extra{i}.ts")
        commits.append(_commit(
            f"c{i:04d}", f"2024-01-{(i % 28) + 1:02d}",
            f"widget{group}", *paths,
        ))
    return commits


def test_cluster_pair_count_bounded_on_synth_500():
    """500 synthetic commits/chunks should not require 124,750 _shares calls."""
    n = 500
    commits = _synthetic_commits(n)
    cfg = Config(cluster_cap=10_000, split_oversized=False)

    chunks = [[c] for c in commits]
    chunk_ranges = {i: None for i in range(n)}

    calls = {"count": 0}
    real = analyze._shares_with_reasons

    def counting_wrapper(*args, **kwargs):
        calls["count"] += 1
        return real(*args, **kwargs)

    orig = analyze._shares_with_reasons
    analyze._shares_with_reasons = counting_wrapper
    try:
        clusters, _, _, _ = analyze.cluster(chunks, cfg, chunk_ranges=chunk_ranges)
    finally:
        analyze._shares_with_reasons = orig

    naive = n * (n - 1) // 2
    assert calls["count"] < 5000, (
        f"_shares_with_reasons called {calls['count']} times "
        f"(naive O(n^2) would be {naive})"
    )
    assert calls["count"] < naive
    assert len(clusters) > 1
