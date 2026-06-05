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

# --- constants ---------------------------------------------------------------

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
STOPWORDS = {
    "fix", "add", "adds", "added", "update", "updates", "updated", "the", "to",
    "for", "a", "an", "in", "of", "and", "or", "remove", "removes", "removed",
    "merge", "pr", "wip", "chore", "bump", "use", "using", "with", "from",
    "into", "via", "new", "initial", "support", "refactor", "cleanup", "test",
    "tests", "docs", "doc", "lint", "format", "style", "minor", "misc",
    "everywhere", "correct", "comment", "scaffold", "project", "wire",
}
# generic file basenames that should NOT be treated as distinctive topic tokens
# (everyone edits index.ts / store.go / config.* — merging on these is noise)
GENERIC_BASENAMES = {
    "index", "main", "mod", "init", "utils", "util", "config", "common",
    "types", "constants", "helpers", "app", "store", "store_test", "lib",
    "setup", "base", "core", "model", "models", "service", "services",
}
WORD_RE = re.compile(r"[a-z][a-z0-9]{2,}")

REPLACE_WORDS = re.compile(r"\b(migrate|replace|adopt|switch|introduce|implement)\b", re.I)
DEMOTE_PREFIX = re.compile(r"^(fix|chore|style|bump)\b", re.I)
EXCLUDE_PREFIX = re.compile(r"^(test|docs|lint|format)\b", re.I)


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

    @property
    def dirs_deep(self) -> set[str]:
        """Directory prefixes of depth >= 2, for clustering overlap.

        Depth-1 buckets (`src`, `lib`, `services`) are too coarse to imply two
        commits belong to the same decision, so they are excluded. `lib/cache`
        counts; `lib` alone does not.
        """
        out: set[str] = set()
        for f in self.files:
            parts = f.path.split("/")[:-1]  # drop filename
            for i in range(2, len(parts) + 1):
                out.add("/".join(parts[:i]))
        return out

    @property
    def paths(self) -> set[str]:
        return {f.path for f in self.files}

    @property
    def topic_tokens(self) -> set[str]:
        toks = {w for w in WORD_RE.findall(self.subject.lower()) if w not in STOPWORDS}
        # add new-file basenames (without extension) as topic signal,
        # skipping generic names that would cause spurious merges
        for f in self.files:
            if f.status.startswith("A"):
                base = f.path.split("/")[-1].split(".")[0].lower()
                if len(base) >= 3 and base not in STOPWORDS and base not in GENERIC_BASENAMES:
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


def chunk_commits(commits: list[Commit], strategy: str) -> list[list[Commit]]:
    """Group commits WITHIN boundaries. Cross-boundary merging happens in cluster()."""
    if strategy in ("squash-boundary", "merge-boundary", "mixed"):
        # each commit is its own chunk; cluster() bundles related ones
        return [[c] for c in commits]
    # direct-commit: union-find by file-path affinity (no temporal grouping)
    return _affinity_groups(commits)


def _shares(a_dirs: set[str], b_dirs: set[str], a_tok: set[str], b_tok: set[str],
            a_paths: set[str], b_paths: set[str]) -> bool:
    """Merge rule: BOTH a file/dir overlap AND a distinctive topic-token overlap.

    Requiring both prevents the two classic false merges: 'everyone edits the
    same generic file' (file-only) and 'two unrelated cache PRs' (topic-only).
    """
    file_overlap = bool(a_paths & b_paths) or bool(a_dirs & b_dirs)
    topic_overlap = bool(a_tok & b_tok)
    return file_overlap and topic_overlap


def _affinity_groups(commits: list[Commit]) -> list[list[Commit]]:
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
                commits[i].dirs_deep, commits[j].dirs_deep,
                commits[i].topic_tokens, commits[j].topic_tokens,
                commits[i].paths, commits[j].paths,
            ):
                union(i, j)

    groups: dict[int, list[Commit]] = {}
    for i, c in enumerate(commits):
        groups.setdefault(find(i), []).append(c)
    # stable order by earliest author date in group
    return sorted(groups.values(), key=lambda g: min(c.date for c in g))


def cluster(chunks: list[list[Commit]], cap: int = 8) -> list[list[Commit]]:
    """Step 2.5: merge related chunks ACROSS boundaries (may be non-adjacent).

    Merge when BOTH hold: file overlap AND topic-token overlap. Deterministic.
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
            dirs |= c.dirs_deep
            toks |= c.topic_tokens
            paths |= c.paths
        return dirs, toks, paths

    aggs = [agg(ch) for ch in chunks]
    for i in range(m):
        for j in range(i + 1, m):
            di, ti, pi = aggs[i]
            dj, tj, pj = aggs[j]
            if _shares(di, dj, ti, tj, pi, pj):
                union(i, j)

    groups: dict[int, list[Commit]] = {}
    for idx, chunk in enumerate(chunks):
        groups.setdefault(find(idx), []).extend(chunk)

    # enforce cap: oversized clusters keep all (a warning would be surfaced by skill)
    result = []
    for g in groups.values():
        g_sorted = sorted(g, key=lambda c: c.date)
        result.append(g_sorted)
    return sorted(result, key=lambda g: min(c.date for c in g))


def classify(commits: list[Commit]) -> tuple[int, str, list[str]]:
    """Diff-first scoring over the aggregated candidate. Returns (score, class, signals)."""
    files = [f for c in commits for f in c.files]
    paths = [f.path for f in files]
    subjects = " ".join(c.subject for c in commits)
    top_dirs = set()
    for c in commits:
        top_dirs |= {d for d in c.top_dirs if d}

    score = 0
    signals: list[str] = []

    new_src = [f for f in files if f.status.startswith("A")
               and SRC_PATH_RE.search(f.path) and not TEST_RE.search(f.path)]
    if new_src:
        score += 3
        signals.append("new source files added")

    dep_changed = [p for p in paths if p.split("/")[-1].lower() in DEP_FILES]
    if dep_changed:
        score += 3
        signals.append("dependency file changed")

    if any(SCHEMA_RE.search(p) for p in paths):
        score += 3
        signals.append("schema/migration/proto changed")

    if len(set(paths)) > 5 and len(top_dirs) >= 2:
        score += 2
        signals.append(">5 files across >=2 dirs")

    if any(f.status.startswith("D") for f in files):
        score += 2
        signals.append("file deleted (replacement pattern)")

    if REPLACE_WORDS.search(subjects):
        score += 1
        signals.append("message: migrate/replace/adopt verb")

    # message demotions use the primary subject
    primary = commits[0].subject
    if DEMOTE_PREFIX.match(primary):
        score -= 1
        signals.append("message demote: fix/chore/style/bump")
    if EXCLUDE_PREFIX.match(primary):
        score -= 2
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
        return max(score, 3), "architectural", signals
    if any(INFRA_RE.search(p) for p in paths):
        signals.append("FORCE_INCLUDE: ci/infra config")
        return max(score, 3), "architectural", signals
    if all_tests:
        signals.append("FORCE_EXCLUDE: tests only")
        return score, "skip", signals
    if all_docs:
        signals.append("FORCE_EXCLUDE: docs only")
        return score, "skip", signals
    if only_dep_bump:
        signals.append("FORCE_EXCLUDE: version bump only")
        return score, "skip", signals

    if score >= 3:
        cls = "architectural"
    elif score >= 1:
        cls = "borderline"
    else:
        cls = "skip"
    return score, cls, signals


def output_dir_for(code_scope: str) -> str:
    if code_scope.startswith("module:"):
        path = code_scope.split(":", 1)[1].rstrip("/")
        return f"{path}/docs/decisions"
    return "docs/decisions"


def analyze(repo: str, history_scope: str = "full", code_scope: str = "repo") -> dict:
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
    }
    # hard stop on shallow — surfaced to skill, which halts
    if preflight["shallow"]:
        return {"preflight": preflight, "halt": "shallow_clone",
                "strategy": None, "candidates": []}

    strategy = detect_strategy(repo, pathspec)
    commits = parse_log(repo, rev_range, pathspec)
    chunks = chunk_commits(commits, strategy)
    clusters = cluster(chunks)

    candidates: list[Candidate] = []
    for i, group in enumerate(clusters, start=1):
        score, cls, signals = classify(group)
        all_files = sorted({f.path for c in group for f in c.files})
        dirs = sorted({d for c in group for d in c.top_dirs if d})
        toks = sorted({t for c in group for t in c.topic_tokens})
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
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    manifest = analyze(args.repo, args.history, args.code)
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
