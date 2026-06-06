#!/usr/bin/env python3
"""
analyze.py — deterministic analytical pipeline for the adr-generator skill.

Extracts PREFLIGHT -> DETECT -> CHUNK -> CLUSTER -> CLASSIFY out of the skill
prose into a pure function over `git`. Emits a JSON manifest of scored
"decision candidates". The agent then only writes MADR prose + places tags for
each candidate.

Design goals:
  - Deterministic: same repo + scope -> same manifest. No embeddings, no
    temporal proximity, no model calls. This is what makes the skill eval-able.
  - Stdlib only: runs in CI without dependencies.

Usage:
    python3 scripts/analyze.py <repo_path> [--history full|since:<ref>]
                                           [--code repo|module:<path>]
                                           [--json]
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field, asdict
from typing import Optional

try:
    from leiden_signal import build_leiden_communities, commits_share_community
    _LEIDEN_AVAILABLE = True
except ImportError:
    _LEIDEN_AVAILABLE = False
    build_leiden_communities = lambda *_: None  # type: ignore[assignment]
    commits_share_community = lambda *_: False  # type: ignore[assignment]

# --- constants ---------------------------------------------------------------

COUPLING_MIN_SHARED = 5   # Tornhill's empirical minimums from code-maat
COUPLING_MIN_SCORE  = 0.30

DEP_FILES = {
    "package.json", "requirements.txt", "go.mod", "cargo.toml",
    "pom.xml", "build.gradle", "pipfile", "pyproject.toml", "gemfile",
}
SCHEMA_RE = re.compile(r"schema|migration|interface|contract|proto|\.sql$", re.I)
SECURITY_RE = re.compile(r"(^|/)(auth|certs|secrets)/", re.I)
INFRA_RE = re.compile(r"(^|/)(Dockerfile|k8s/|\.github/workflows/)", re.I)
TEST_RE = re.compile(r"(^|/)(tests?|__tests__)/|(_test\.|\.test\.|\.spec\.|_spec\.)", re.I)
DOCS_RE = re.compile(r"\.(md|rst|txt|adoc)$|(^|/)docs?/", re.I)
SRC_PATH_RE = re.compile(r"(^|/)(src|lib|core|app|services|internal|pkg)/", re.I)
PR_REF_RE = re.compile(r"\(#\d+\)")

# topic-token extraction
DEFAULT_STOPWORDS = frozenset({
    "fix", "add", "adds", "added", "update", "updates", "updated", "the", "to",
    "for", "a", "an", "in", "of", "and", "or", "remove", "removes", "removed",
    "merge", "pr", "wip", "chore", "bump", "use", "using", "with", "from",
    "into", "via", "new", "initial", "support", "refactor", "cleanup", "test",
    "tests", "docs", "doc", "lint", "format", "style", "minor", "misc",
    "everywhere", "correct", "comment", "scaffold", "project", "wire",
})
# generic file basenames that should NOT be treated as distinctive topic tokens
# (everyone edits index.ts / store.go / config.* — merging on these is noise)
DEFAULT_GENERIC_BASENAMES = frozenset({
    "index", "main", "mod", "init", "utils", "util", "config", "common",
    "types", "constants", "helpers", "app", "store", "store_test", "lib",
    "setup", "base", "core", "model", "models", "service", "services",
})
# classification score weights, keyed by signal name
DEFAULT_WEIGHTS = {
    "new_src": 3,        # new non-test files in src/lib/core/app paths
    "dep_file": 3,       # dependency manifest changed
    "schema": 3,         # schema/migration/proto changed
    "breadth": 2,        # >5 files across >=2 top-level dirs
    "deletion": 2,       # a file was deleted (replacement pattern)
    "replace_verb": 1,   # message: migrate/replace/adopt/...
    "demote_fix": -1,    # message starts with fix/chore/style/bump
    "demote_test": -2,   # message starts with test/docs/lint/format
}
WORD_RE = re.compile(r"[a-z][a-z0-9]{2,}")

REPLACE_WORDS = re.compile(r"\b(migrate|replace|adopt|switch|introduce|implement)\b", re.I)
DEMOTE_PREFIX = re.compile(r"^(fix|chore|style|bump)\b", re.I)
EXCLUDE_PREFIX = re.compile(r"^(test|docs|lint|format)\b", re.I)


# --- config ------------------------------------------------------------------

@dataclass
class Config:
    """Tunable knobs for the pipeline. Defaults reproduce the original
    hardcoded behavior, so `Config()` is the deterministic eval baseline.

    Load overrides from JSON with `Config.from_file(path)`. Supported keys:
      min_dir_depth, cluster_cap, split_oversized,
      score_arch_threshold, score_borderline_threshold,
      stopwords / extra_stopwords,
      generic_basenames / extra_generic_basenames,
      weights (partial dict, merged over defaults).
    The `extra_*` lists union with the defaults; the bare keys replace them.
    """
    # clustering
    min_dir_depth: int = 2          # dir prefixes shorter than this don't bind
    cluster_cap: int = 8            # commits per cluster before it's "oversized"
    split_oversized: bool = False   # if True, split oversized clusters by date
    # classification
    score_arch_threshold: int = 3       # >= this -> architectural
    score_borderline_threshold: int = 1  # >= this (but < arch) -> borderline
    # token vocabularies
    stopwords: frozenset = DEFAULT_STOPWORDS
    generic_basenames: frozenset = DEFAULT_GENERIC_BASENAMES
    weights: dict = field(default_factory=lambda: dict(DEFAULT_WEIGHTS))

    @classmethod
    def from_file(cls, path: Optional[str]) -> "Config":
        if not path:
            return cls()
        with open(path) as fh:
            raw = json.load(fh)
        cfg = cls()
        for key in ("min_dir_depth", "cluster_cap", "split_oversized",
                    "score_arch_threshold", "score_borderline_threshold"):
            if key in raw:
                setattr(cfg, key, raw[key])
        if "stopwords" in raw:
            cfg.stopwords = frozenset(raw["stopwords"])
        if "extra_stopwords" in raw:
            cfg.stopwords = cfg.stopwords | frozenset(raw["extra_stopwords"])
        if "generic_basenames" in raw:
            cfg.generic_basenames = frozenset(raw["generic_basenames"])
        if "extra_generic_basenames" in raw:
            cfg.generic_basenames = (
                cfg.generic_basenames | frozenset(raw["extra_generic_basenames"])
            )
        if "weights" in raw:
            cfg.weights = {**cfg.weights, **raw["weights"]}
        return cfg


# --- data models -------------------------------------------------------------

@dataclass
class FileChange:
    status: str  # A, M, D, R...
    path: str


@dataclass
class Commit:
    sha: str
    author: str
    date: str  # author date, YYYY-MM-DD
    parents: list[str]
    subject: str
    files: list[FileChange] = field(default_factory=list)

    @property
    def top_dirs(self) -> set[str]:
        """First path component — used for the classification breadth signal."""
        dirs = set()
        for f in self.files:
            parts = f.path.split("/")
            dirs.add(parts[0] if len(parts) > 1 else "")
        return dirs

    def dirs_deep(self, cfg: "Config") -> set[str]:
        """Directory prefixes of depth >= cfg.min_dir_depth, for clustering overlap.

        Depth-1 buckets (`src`, `lib`, `services`) are too coarse to imply two
        commits belong to the same decision, so they are excluded by default.
        `lib/cache` counts; `lib` alone does not.
        """
        out: set[str] = set()
        for f in self.files:
            parts = f.path.split("/")[:-1]  # drop filename
            for i in range(cfg.min_dir_depth, len(parts) + 1):
                out.add("/".join(parts[:i]))
        return out

    @property
    def paths(self) -> set[str]:
        return {f.path for f in self.files}

    def topic_tokens(self, cfg: "Config") -> set[str]:
        toks = {w for w in WORD_RE.findall(self.subject.lower())
                if w not in cfg.stopwords}
        # add new-file basenames (without extension) as topic signal,
        # skipping generic names that would cause spurious merges
        for f in self.files:
            if f.status.startswith("A"):
                base = f.path.split("/")[-1].split(".")[0].lower()
                if (len(base) >= 3 and base not in cfg.stopwords
                        and base not in cfg.generic_basenames):
                    toks.add(base)
        return toks


@dataclass
class Candidate:
    id: str
    commits: list[str]
    subjects: list[str]
    files: list[str]
    top_dirs: list[str]
    topic_tokens: list[str]
    score: int
    signals: list[str]
    classification: str  # architectural | borderline | skip
    title_hint: str
    output_dir: str


# --- git helpers -------------------------------------------------------------

def git(repo: str, *args: str) -> str:
    res = subprocess.run(
        ["git", "-C", repo, *args],
        capture_output=True, text=True,
    )
    if res.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {res.stderr.strip()}")
    return res.stdout


def is_shallow(repo: str) -> bool:
    return git(repo, "rev-parse", "--is-shallow-repository").strip() == "true"


def commit_count(repo: str, rev_range: Optional[str]) -> int:
    args = ["rev-list", "--count"]
    args.append(rev_range if rev_range else "HEAD")
    try:
        return int(git(repo, *args).strip())
    except RuntimeError:
        return 0


def next_adr_number(repo: str, output_dir: str) -> int:
    """Highest existing NNNN-*.md in output_dir + 1 (scope-aware)."""
    import os
    path = os.path.join(repo, output_dir)
    if not os.path.isdir(path):
        return 1
    highest = 0
    for name in os.listdir(path):
        m = re.match(r"^(\d{4})-.*\.md$", name)
        if m:
            highest = max(highest, int(m.group(1)))
    return highest + 1


# --- pipeline ----------------------------------------------------------------

SENTINEL = "__COMMIT__"


def parse_log(repo: str, rev_range: Optional[str], pathspec: Optional[str],
              merges_only: bool = False, no_merges: bool = True) -> list[Commit]:
    fmt = f"{SENTINEL}%H|%an|%ad|%P|%s"
    args = ["log", f"--pretty=format:{fmt}", "--name-status", "--date=short"]
    if merges_only:
        args.append("--merges")
    elif no_merges:
        args.append("--no-merges")
    if rev_range:
        args.append(rev_range)
    if pathspec:
        args += ["--", pathspec]
    out = git(repo, *args)

    commits: list[Commit] = []
    current: Optional[Commit] = None
    for line in out.splitlines():
        if line.startswith(SENTINEL):
            sha, author, date, parents, subject = line[len(SENTINEL):].split("|", 4)
            current = Commit(
                sha=sha, author=author, date=date,
                parents=parents.split() if parents else [],
                subject=subject,
            )
            commits.append(current)
        elif line.strip() and current is not None:
            parts = line.split("\t")
            status = parts[0]
            # rename: "R100\told\tnew" -> treat new path as modify
            path = parts[-1]
            current.files.append(FileChange(status=status, path=path))
    return commits


def detect_strategy(repo: str, pathspec: Optional[str]) -> str:
    merge_count = len([l for l in git(
        repo, "log", "--merges", "--oneline",
        *(["--", pathspec] if pathspec else [])
    ).splitlines() if l.strip()])
    subjects = git(repo, "log", "--no-merges", "--pretty=format:%s",
                   *(["--", pathspec] if pathspec else [])).splitlines()
    squash_count = sum(1 for s in subjects if PR_REF_RE.search(s))

    has_merge = merge_count >= 3
    has_squash = squash_count > 0
    if has_merge and has_squash:
        return "mixed"
    if has_merge:
        return "merge-boundary"
    if has_squash:
        return "squash-boundary"
    return "direct-commit"


def chunk_commits(commits: list[Commit], strategy: str,
                  cfg: Config, coupling: Optional[dict] = None,
                  communities: Optional[dict] = None) -> list[list[Commit]]:
    """Group commits WITHIN boundaries. Cross-boundary merging happens in cluster()."""
    if strategy in ("squash-boundary", "merge-boundary", "mixed"):
        return [[c] for c in commits]
    return _affinity_groups(commits, cfg, coupling=coupling, communities=communities)


def build_coupling(repo: str, pathspec: Optional[str] = None) -> dict:
    """Return frozenset({fileA, fileB}) → Jaccard coupling score over full history.

    Only pairs with ≥COUPLING_MIN_SHARED co-appearances and a score
    ≥COUPLING_MIN_SCORE are kept — Tornhill's empirical minimums.
    Always mines the full history regardless of the analysis rev_range, since
    coupling is a global prior, not scoped to the current window.
    """
    commits = parse_log(repo, rev_range=None, pathspec=pathspec, no_merges=True)
    file_revs: dict[str, int] = {}
    pair_revs: dict = {}
    for c in commits:
        paths = list({f.path for f in c.files})
        for p in paths:
            file_revs[p] = file_revs.get(p, 0) + 1
        for i in range(len(paths)):
            for j in range(i + 1, len(paths)):
                key = frozenset({paths[i], paths[j]})
                pair_revs[key] = pair_revs.get(key, 0) + 1

    coupling: dict = {}
    for pair, shared in pair_revs.items():
        if shared < COUPLING_MIN_SHARED:
            continue
        a, b = tuple(pair)
        denom = file_revs[a] + file_revs[b] - shared
        score = shared / denom if denom else 0.0
        if score >= COUPLING_MIN_SCORE:
            coupling[pair] = score
    return coupling


def _coupled(a_paths: set[str], b_paths: set[str], coupling: dict) -> bool:
    """True if any cross-pair (fileA ∈ A, fileB ∈ B) meets the coupling threshold."""
    for pa in a_paths:
        for pb in b_paths:
            if coupling.get(frozenset({pa, pb}), 0.0) >= COUPLING_MIN_SCORE:
                return True
    return False


def _shares(a_dirs: set[str], b_dirs: set[str], a_tok: set[str], b_tok: set[str],
            a_paths: set[str], b_paths: set[str], coupling: Optional[dict] = None,
            communities: Optional[dict] = None) -> bool:
    """Merge when ≥2 of 4 independent signals agree.

    1. file/dir overlap    — same files or deep-directory prefix
    2. topic-token overlap — distinctive words from commit messages / new-file names
    3. co-change coupling  — historical Jaccard coupling from full repo history
    4. Leiden community    — shared community in tree-sitter import graph (optional)

    Requiring two independent signals prevents the classic false-merge pairs:
    file-only ('everyone edits the same generic file') and topic-only ('two
    unrelated cache PRs'). Coupling raises recall for commits that share no files
    in this window but always co-change in history. The community signal further
    raises recall for structurally related modules that haven't co-changed yet.
    """
    file_overlap  = bool(a_paths & b_paths) or bool(a_dirs & b_dirs)
    topic_overlap = bool(a_tok & b_tok)
    coupled       = _coupled(a_paths, b_paths, coupling) if coupling else False
    # Community is only a meaningful second signal when there is no direct file
    # overlap — otherwise the same shared file would fire both file_overlap and
    # community_match, silently collapsing the two-signal guard into one.
    community_match = (
        not file_overlap
        and communities is not None
        and commits_share_community(a_paths, b_paths, communities)
    )
    return sum([file_overlap, topic_overlap, coupled, community_match]) >= 2


def _affinity_groups(commits: list[Commit], cfg: Config,
                     coupling: Optional[dict] = None,
                     communities: Optional[dict] = None) -> list[list[Commit]]:
    n = len(commits)
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        parent[find(i)] = find(j)

    for i in range(n):
        for j in range(i + 1, n):
            if _shares(
                commits[i].dirs_deep(cfg), commits[j].dirs_deep(cfg),
                commits[i].topic_tokens(cfg), commits[j].topic_tokens(cfg),
                commits[i].paths, commits[j].paths,
                coupling=coupling,
                communities=communities,
            ):
                union(i, j)

    groups: dict[int, list[Commit]] = {}
    for i, c in enumerate(commits):
        groups.setdefault(find(i), []).append(c)
    # stable order by earliest author date in group
    return sorted(groups.values(), key=lambda g: min(c.date for c in g))


def cluster(chunks: list[list[Commit]], cfg: Config,
            coupling: Optional[dict] = None,
            communities: Optional[dict] = None) -> list[list[Commit]]:
    """Step 2.5: merge related chunks ACROSS boundaries (may be non-adjacent).

    Merge when ≥2 of 4 signals agree: file overlap, topic-token overlap,
    co-change coupling, Leiden community match. Deterministic. Oversized
    clusters (> cfg.cluster_cap) are split when cfg.split_oversized.
    """
    m = len(chunks)
    parent = list(range(m))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        parent[find(i)] = find(j)

    def agg(chunk: list[Commit]):
        dirs, toks, paths = set(), set(), set()
        for c in chunk:
            dirs |= c.dirs_deep(cfg)
            toks |= c.topic_tokens(cfg)
            paths |= c.paths
        return dirs, toks, paths

    aggs = [agg(ch) for ch in chunks]
    for i in range(m):
        for j in range(i + 1, m):
            di, ti, pi = aggs[i]
            dj, tj, pj = aggs[j]
            if _shares(di, dj, ti, tj, pi, pj, coupling=coupling, communities=communities):
                union(i, j)

    groups: dict[int, list[Commit]] = {}
    for idx, chunk in enumerate(chunks):
        groups.setdefault(find(idx), []).extend(chunk)

    result: list[list[Commit]] = []
    for g in groups.values():
        g_sorted = sorted(g, key=lambda c: c.date)
        if cfg.split_oversized and len(g_sorted) > cfg.cluster_cap:
            for start in range(0, len(g_sorted), cfg.cluster_cap):
                result.append(g_sorted[start:start + cfg.cluster_cap])
        else:
            result.append(g_sorted)
    return sorted(result, key=lambda g: min(c.date for c in g))


def classify(commits: list[Commit], cfg: Config) -> tuple[int, str, list[str]]:
    """Diff-first scoring over the aggregated candidate. Returns (score, class, signals)."""
    files = [f for c in commits for f in c.files]
    paths = [f.path for f in files]
    subjects = " ".join(c.subject for c in commits)
    top_dirs = set()
    for c in commits:
        top_dirs |= {d for d in c.top_dirs if d}

    w = cfg.weights
    score = 0
    signals: list[str] = []

    if len(commits) > cfg.cluster_cap:
        signals.append(f"WARN: oversized cluster ({len(commits)} commits > "
                       f"cap {cfg.cluster_cap})")

    new_src = [f for f in files if f.status.startswith("A")
               and SRC_PATH_RE.search(f.path) and not TEST_RE.search(f.path)]
    if new_src:
        score += w["new_src"]
        signals.append("new source files added")

    dep_changed = [p for p in paths if p.split("/")[-1].lower() in DEP_FILES]
    if dep_changed:
        score += w["dep_file"]
        signals.append("dependency file changed")

    if any(SCHEMA_RE.search(p) for p in paths):
        score += w["schema"]
        signals.append("schema/migration/proto changed")

    if len(set(paths)) > 5 and len(top_dirs) >= 2:
        score += w["breadth"]
        signals.append(">5 files across >=2 dirs")

    if any(f.status.startswith("D") for f in files):
        score += w["deletion"]
        signals.append("file deleted (replacement pattern)")

    if REPLACE_WORDS.search(subjects):
        score += w["replace_verb"]
        signals.append("message: migrate/replace/adopt verb")

    # message demotions use the primary subject
    primary = commits[0].subject
    if DEMOTE_PREFIX.match(primary):
        score += w["demote_fix"]
        signals.append("message demote: fix/chore/style/bump")
    if EXCLUDE_PREFIX.match(primary):
        score += w["demote_test"]
        signals.append("message demote: test/docs/lint")

    # --- override rules ---
    non_empty = [p for p in paths if p]
    all_tests = bool(non_empty) and all(TEST_RE.search(p) for p in non_empty)
    all_docs = bool(non_empty) and all(DOCS_RE.search(p) for p in non_empty)
    only_dep_bump = (
        bool(dep_changed)
        and all(p.split("/")[-1].lower() in DEP_FILES for p in non_empty)
        and DEMOTE_PREFIX.match(primary) is not None
    )

    if any(SECURITY_RE.search(p) for p in paths):
        signals.append("FORCE_INCLUDE: security path")
        return max(score, cfg.score_arch_threshold), "architectural", signals
    if any(INFRA_RE.search(p) for p in paths):
        signals.append("FORCE_INCLUDE: ci/infra config")
        return max(score, cfg.score_arch_threshold), "architectural", signals
    if all_tests:
        signals.append("FORCE_EXCLUDE: tests only")
        return score, "skip", signals
    if all_docs:
        signals.append("FORCE_EXCLUDE: docs only")
        return score, "skip", signals
    if only_dep_bump:
        signals.append("FORCE_EXCLUDE: version bump only")
        return score, "skip", signals

    if score >= cfg.score_arch_threshold:
        cls = "architectural"
    elif score >= cfg.score_borderline_threshold:
        cls = "borderline"
    else:
        cls = "skip"
    return score, cls, signals


def output_dir_for(code_scope: str) -> str:
    if code_scope.startswith("module:"):
        path = code_scope.split(":", 1)[1].rstrip("/")
        return f"{path}/docs/decisions"
    return "docs/decisions"


def analyze(repo: str, history_scope: str = "full", code_scope: str = "repo",
            cfg: Optional[Config] = None) -> dict:
    cfg = cfg or Config()
    pathspec = None
    if code_scope.startswith("module:"):
        pathspec = code_scope.split(":", 1)[1]

    rev_range = None
    if history_scope.startswith("since:"):
        ref = history_scope.split(":", 1)[1]
        rev_range = f"{ref}..HEAD"

    out_dir = output_dir_for(code_scope)

    preflight = {
        "shallow": is_shallow(repo),
        "commit_count": commit_count(repo, rev_range),
        "next_adr": next_adr_number(repo, out_dir),
        "output_dir": out_dir,
        "leiden_available": _LEIDEN_AVAILABLE,
    }
    # hard stop on shallow — surfaced to skill, which halts
    if preflight["shallow"]:
        return {"preflight": preflight, "halt": "shallow_clone",
                "strategy": None, "candidates": []}

    strategy = detect_strategy(repo, pathspec)
    commits = parse_log(repo, rev_range, pathspec)
    coupling = build_coupling(repo, pathspec)
    communities = build_leiden_communities(repo)
    chunks = chunk_commits(commits, strategy, cfg, coupling=coupling, communities=communities)
    clusters = cluster(chunks, cfg, coupling=coupling, communities=communities)

    candidates: list[Candidate] = []
    for i, group in enumerate(clusters, start=1):
        score, cls, signals = classify(group, cfg)
        all_files = sorted({f.path for c in group for f in c.files})
        dirs = sorted({d for c in group for d in c.top_dirs if d})
        toks = sorted({t for c in group for t in c.topic_tokens(cfg)})
        candidates.append(Candidate(
            id=f"C{i}",
            commits=[c.sha for c in group],
            subjects=[c.subject for c in group],
            files=all_files,
            top_dirs=dirs,
            topic_tokens=toks,
            score=score,
            signals=signals,
            classification=cls,
            title_hint=group[0].subject,
            output_dir=out_dir,
        ))

    return {
        "repo": repo,
        "scope": {"history": history_scope, "code": code_scope},
        "preflight": preflight,
        "strategy": strategy,
        "candidates": [asdict(c) for c in candidates],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("repo")
    ap.add_argument("--history", default="full")
    ap.add_argument("--code", default="repo")
    ap.add_argument("--config", default=None,
                    help="path to a JSON config of pipeline knobs (see Config)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    try:
        cfg = Config.from_file(args.config)
    except (OSError, json.JSONDecodeError) as e:
        print(f"error: could not load config {args.config!r}: {e}", file=sys.stderr)
        return 2
    manifest = analyze(args.repo, args.history, args.code, cfg)
    if args.json:
        print(json.dumps(manifest, indent=2))
    else:
        pf = manifest["preflight"]
        print(f"strategy: {manifest['strategy']}  commits: {pf['commit_count']}  "
              f"next ADR: {pf['next_adr']:04d}  out: {pf['output_dir']}")
        if manifest.get("halt"):
            print(f"HALT: {manifest['halt']}")
        for c in manifest["candidates"]:
            print(f"  [{c['classification']:13}] score={c['score']:+d} "
                  f"{c['id']} ({len(c['commits'])} commit) :: {c['title_hint']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
