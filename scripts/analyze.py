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

# Lockfiles, generated code, and vendored trees co-change with everything and
# contribute no real coupling signal — they just blow up the O(k^2) pair count
# for giant refactor commits. Drop them before pair enumeration.
_COUPLING_DENY_RE = re.compile(
    r"\.(lock|min\.js|min\.css|map|generated\..*|pb\.go|pb\.py)$"
    r"|(^|/)(node_modules|vendor|dist|build|\.next|\.nuxt|target|coverage)/"
    r"|(^|/)(pnpm-lock\.yaml|package-lock\.json|yarn\.lock|poetry\.lock|cargo\.lock|gemfile\.lock|composer\.lock)$",
    re.I,
)
# A commit touching more files than this after filtering is a large refactor
# or merge dump — its co-change signal is unreliable, so skip pair enumeration
# for it entirely rather than pay O(k^2).
_MAX_COUPLING_FILES_PER_COMMIT = 200

# auto-tuned edge specificity (see Config.auto_specificity)
AUTO_SPEC_MIN_COMMITS = 50   # below this, hotness isn't meaningful → no-op
AUTO_SPEC_DIVISOR     = 50   # cap = max(3, commit_count // divisor)

DEP_FILES = {
    "package.json", "requirements.txt", "go.mod", "cargo.toml",
    "pom.xml", "build.gradle", "pipfile", "pyproject.toml", "gemfile",
}
SCHEMA_RE = re.compile(r"schema|migration|interface|contract|proto|\.sql$", re.I)
SECURITY_RE = re.compile(r"(^|/)(auth|certs|secrets)/", re.I)
INFRA_RE = re.compile(r"(^|/)(Dockerfile|k8s/|\.github/workflows/)", re.I)
TEST_RE = re.compile(r"(^|/)(tests?|__tests__)/|(_test\.|\.test\.|\.spec\.|_spec\.)", re.I)
DOCS_RE = re.compile(r"\.(md|rst|txt|adoc)$|(^|/)docs?/", re.I)
PR_REF_RE = re.compile(r"\(#\d+\)")

# legacy hard-coded source roots — always included in detect_source_roots union
_LEGACY_SOURCE_ROOTS = frozenset({"src", "lib", "core", "app", "services", "internal", "pkg"})

_SOURCE_EXTS = frozenset({
    ".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs",
    ".go", ".rs", ".java", ".rb", ".kt", ".swift",
    ".cpp", ".c", ".h", ".hpp", ".cs", ".scala", ".lua", ".sh", ".php",
})

_SOURCE_ROOT_DENY = frozenset({
    ".git", ".github", "node_modules", "dist", "build", "target", "vendor",
    "__pycache__", "docs", "doc", "tests", "test", "__tests__",
    ".venv", "venv", "coverage", ".next", ".nuxt",
})

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

# --- workaround detection ------------------------------------------------------
# Workarounds are evidenced by diff CONTENT (the marker comment, the pin, the
# shim), not by file topology — a separate signal class from the architectural
# score, not another threshold. A strong hit (marker in an added line, or a
# workaround-shaped file) flips the candidate's record_type to "workaround";
# message wording alone is recorded as a signal but never flips ("work around"
# in prose can describe anything).
WORKAROUND_STRONG_RE = re.compile(
    r"\b(hack|work.?around|kludge|xxx)\b|monkey.?patch|\bpolyfill\b|\bshim\b", re.I)
WORKAROUND_COND_RE = re.compile(
    r"\b(fixme|todo)\b.*\b(until|upstream|remove (when|once|after)|temporar)", re.I)
WORKAROUND_FILE_RE = re.compile(
    r"(^|/)patches?(/|$)|\.(patch|diff)$|(adapter|proxy|shim|polyfill)[^/]*$", re.I)
WORKAROUND_MAX_COMMITS = 20   # commits scanned per candidate
WORKAROUND_MAX_LINES = 4000   # added diff lines scanned per candidate


# --- source root detection ---------------------------------------------------

def detect_source_roots(repo: str) -> list[str]:
    """Return a sorted list of top-level dirs in `repo` that contain source code.

    Walks each depth-1 directory (bounded to 200 files) looking for files with
    a code extension. The result is unioned with the legacy hard-coded set so
    that existing fixtures keep passing regardless of what is found on disk.
    Dirs in the deny set (node_modules, dist, .git, …) are skipped entirely.
    """
    import os
    roots: set[str] = set()
    try:
        entries = os.listdir(repo)
    except OSError:
        return sorted(_LEGACY_SOURCE_ROOTS)
    for entry in entries:
        if entry in _SOURCE_ROOT_DENY or entry.startswith("."):
            continue
        full = os.path.join(repo, entry)
        if not os.path.isdir(full):
            continue
        # bounded walk: stop after 200 files to avoid scanning huge trees
        count = 0
        found = False
        for dirpath, dirnames, filenames in os.walk(full):
            # prune deny dirs in-place so os.walk skips them
            dirnames[:] = [d for d in dirnames if d not in _SOURCE_ROOT_DENY
                           and not d.startswith(".")]
            for fname in filenames:
                ext = os.path.splitext(fname)[1].lower()
                if ext in _SOURCE_EXTS:
                    found = True
                    break
                count += 1
                if count >= 200:
                    break
            if found or count >= 200:
                break
        if found:
            roots.add(entry)
    return sorted(roots | _LEGACY_SOURCE_ROOTS)


def _in_source_root(path: str, roots: list[str]) -> bool:
    """Return True iff `path` lives directly under any of the given `roots`."""
    for root in roots:
        if path == root or path.startswith(root + "/"):
            return True
    return False


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
      source_roots / extra_source_roots,
      weights (partial dict, merged over defaults).
    The `extra_*` lists union with the defaults; the bare keys replace them.
    `source_roots` defaults to None, which triggers auto-detection at analyze()
    time via detect_source_roots().
    """
    # clustering
    min_dir_depth: int = 2          # dir prefixes shorter than this don't bind
    cluster_cap: int = 8            # commits per cluster before it's "oversized"
    split_oversized: bool = False   # if True, split oversized clusters by date
    module_prefix: str = ""         # set at runtime under module: scope; makes
                                    # dir-overlap depth relative to the module
                                    # root so the module path itself never binds
    # edge specificity (IDF): a shared file/token only counts as affinity if it
    # appears in <= N commits across the window. A file edited in a quarter of
    # all commits ('hot file') carries no signal that two commits are one
    # decision; without this, presence-only overlap + union-find transitive
    # closure collapses small, hot-file repos into one mega-cluster. None
    # disables filtering, so Config() reproduces the original eval baseline.
    file_df_max: Optional[int] = None
    token_df_max: Optional[int] = None
    dir_df_max: Optional[int] = None
    # When no cap is set explicitly, derive all three from commit count for
    # repos large enough that 'hot' files are meaningful (a deterministic
    # function of history size, so output stays reproducible). This gives a
    # sane out-of-box result instead of one mega-cluster. Set any cap, or
    # auto_specificity=False, to opt out. Below AUTO_SPEC_MIN_COMMITS it is a
    # no-op, so the small-fixture eval baseline is unchanged.
    auto_specificity: bool = True
    # classification
    score_arch_threshold: int = 3       # >= this -> architectural
    score_borderline_threshold: int = 1  # >= this (but < arch) -> borderline
    # workaround detection (record_type routing; see scan_workarounds)
    detect_workarounds: bool = True
    extra_workaround_markers: list = field(default_factory=list)  # regex strings
    # source root detection
    source_roots: Optional[list] = None  # None = auto-detect at analyze() time
    extra_source_roots: list = field(default_factory=list)  # always unioned in
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
                    "file_df_max", "token_df_max", "dir_df_max",
                    "auto_specificity", "detect_workarounds",
                    "score_arch_threshold", "score_borderline_threshold"):
            if key in raw:
                setattr(cfg, key, raw[key])
        if "extra_workaround_markers" in raw:
            cfg.extra_workaround_markers = list(raw["extra_workaround_markers"])
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
        if "source_roots" in raw:
            cfg.source_roots = sorted(raw["source_roots"])
        if "extra_source_roots" in raw:
            cfg.extra_source_roots = list(raw["extra_source_roots"])
        if "weights" in raw:
            cfg.weights = {**cfg.weights, **raw["weights"]}
        return cfg


# --- data models -------------------------------------------------------------

@dataclass
class FileChange:
    status: str  # A, M, D, R...
    path: str
    old_path: Optional[str] = None


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
        """Directory prefixes deep enough to imply two commits share a decision.

        Depth-1 buckets (`src`, `lib`, `services`) are too coarse to bind, so
        binding starts one level below the coarse bucket: `lib/cache` counts;
        `lib` alone does not (with the default `min_dir_depth=2`).

        Under `module:` scope the module path *is* the coarse bucket. Measured
        from the repo root every file would share that prefix at depth 2, so
        file-overlap fires for every pair and the whole module collapses into
        one cluster. To avoid that, the depth window is shifted by the module's
        own depth: binding starts at `module_depth + 1`, i.e. the first subdir
        *inside* the module — exactly mirroring the repo-root case where the
        depth-1 bucket is excluded and bucket+1 binds.
        """
        prefix_parts = [p for p in cfg.module_prefix.split("/") if p]
        base_depth = len(prefix_parts) if prefix_parts else 1
        start = base_depth + (cfg.min_dir_depth - 1)
        out: set[str] = set()
        for f in self.files:
            parts = f.path.split("/")[:-1]  # drop filename
            for i in range(start, len(parts) + 1):
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
    diff_summary: dict = field(default_factory=dict)
    range: Optional[tuple] = None
    merge_reasons: list = field(default_factory=list)
    record_type: str = "decision"  # decision | workaround
    workaround_signals: list = field(default_factory=list)


# --- git helpers -------------------------------------------------------------

def git(repo: str, *args: str) -> str:
    res = subprocess.run(
        ["git", "-C", repo, *args],
        capture_output=True, text=True,
    )
    if res.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {res.stderr.strip()}")
    return res.stdout


def verify_ref(repo: str, ref: str) -> bool:
    """True iff `git rev-parse --verify <ref>` succeeds."""
    res = subprocess.run(
        ["git", "-C", repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
        capture_output=True, text=True,
    )
    return res.returncode == 0


def is_ancestor(repo: str, ref: str, head: str = "HEAD") -> bool:
    """True iff `ref` is an ancestor of `head` in the current history."""
    res = subprocess.run(
        ["git", "-C", repo, "merge-base", "--is-ancestor", ref, head],
        capture_output=True, text=True,
    )
    return res.returncode == 0


def read_diff(repo: str, base: str, head: str, path: str) -> str:
    """git diff <base> <head> -- <path>. Returns raw unified diff text, or '' on error."""
    res = subprocess.run(
        ["git", "-C", repo, "diff", base, head, "--", path],
        capture_output=True, text=True,
    )
    if res.returncode != 0:
        return ""
    return res.stdout


_VERSION_LINE_PATTERNS = [
    re.compile(r'^[+-]\s*"[^"]+":\s*"[~^]?\d+(\.\d+){0,2}(-[\w.]+)?"\s*,?\s*$'),
    re.compile(r'^[+-]\s*[\w_-]+\s*=\s*"[~^>=<]?\s*\d+(\.\d+){0,2}(-[\w.]+)?"\s*$'),
    re.compile(r'^[+-]\s*[\w_-]+\s*[=<>!~]+\s*\d+(\.\d+){0,2}\s*$'),
    re.compile(r'^[+-]\s*[\w./_-]+\s+v\d+(\.\d+){0,2}\s*$'),
]
# captures the package name from a version line (group 1), one per manifest shape
_VERSION_KEY_PATTERNS = [
    re.compile(r'^[+-]\s*"([^"]+)":\s*"[~^>=<]?[\d]'),         # package.json
    re.compile(r'^[+-]\s*([A-Za-z0-9_.-]+)\s*=\s*"[~^>=<]?\d'),  # pyproject/cargo TOML
    re.compile(r'^[+-]\s*([A-Za-z0-9_.-]+)\s*[=<>!~]+\s*\d'),    # requirements.txt
    re.compile(r'^[+-]\s*([\w./_-]+)\s+v\d'),                    # go.mod
]


def is_pure_version_bump(repo: str, base: str, head: str, manifest_paths: list) -> bool:
    """True iff every +/- line in the given manifest files is a version-literal change.

    Also verifies that no package name is added or removed — only version values change.
    """
    saw_any = False
    for path in manifest_paths:
        diff_text = read_diff(repo, base, head, path)
        if not diff_text:
            return False
        removed_keys: set = set()
        added_keys: set = set()
        for line in diff_text.splitlines():
            if line.startswith(("diff --git", "index ", "+++", "---", "@@")):
                continue
            if not line or line[0] not in ("+", "-"):
                continue
            saw_any = True
            if not any(pat.match(line) for pat in _VERSION_LINE_PATTERNS):
                return False
            for kp in _VERSION_KEY_PATTERNS:
                m = kp.match(line)
                if m:
                    if line[0] == "-":
                        removed_keys.add(m.group(1))
                    else:
                        added_keys.add(m.group(1))
                    break
        # a dep was renamed/replaced (key set changed) → not a pure bump
        if removed_keys != added_keys:
            return False
    return saw_any


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
        args += ["--full-diff", "--", pathspec]
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
            if status[0] in ("R", "C") and len(parts) >= 3:
                old_path = parts[1]
                path = parts[2]
                current.files.append(FileChange(status=status, path=path, old_path=old_path))
            else:
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


# git empty-tree hash — safe base for initial commits with no parent
_EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


def enumerate_merge_chunks(
    repo: str, pathspec: Optional[str]
) -> list[tuple[str, str, list[Commit]]]:
    """For each merge commit on the current branch return (merge_sha, base_sha, commits_in_pr).

    For a merge commit M with parents (p1, p2):
      base = git merge-base p1 p2
      commits_in_pr = parse_log over base..M with no_merges=True

    Returns chunks in chronological order (earliest merge first).
    """
    fmt_args = ["log", "--merges", "--pretty=format:%H|%P|%cd", "--date=short"]
    if pathspec:
        fmt_args += ["--", pathspec]
    raw = git(repo, *fmt_args)

    rows = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        sha, parents_str, date = line.split("|", 2)
        parents = parents_str.split()
        rows.append((date, sha, parents))

    # sort chronologically, tie-break on sha for determinism
    rows.sort(key=lambda r: (r[0], r[1]))

    result = []
    for date, merge_sha, parents in rows:
        if len(parents) < 2:
            continue
        p1, p2 = parents[0], parents[1]
        base_sha = git(repo, "merge-base", p1, p2).strip()
        pr_commits = parse_log(
            repo,
            rev_range=f"{base_sha}..{merge_sha}",
            pathspec=pathspec,
            no_merges=True,
        )
        result.append((merge_sha, base_sha, pr_commits))

    return result


def chunk_commits(
    commits: list[Commit],
    strategy: str,
    cfg: Config,
    repo: str = "",
    pathspec: Optional[str] = None,
    coupling: Optional[dict] = None,
    communities: Optional[dict] = None,
    spec: Optional["Specificity"] = None,
) -> tuple[list[list[Commit]], dict[int, Optional[tuple[str, str]]]]:
    """Group commits WITHIN boundaries. Cross-boundary merging happens in cluster().

    Returns (chunks, chunk_ranges) where chunk_ranges maps chunk index to an
    optional (base_sha, head_sha) diff range for that chunk.
    """
    if strategy == "squash-boundary":
        chunks = [[c] for c in commits]
        ranges: dict[int, Optional[tuple[str, str]]] = {}
        for i, c in enumerate(commits):
            if c.parents:
                ranges[i] = (f"{c.sha}^", c.sha)
            else:
                ranges[i] = (_EMPTY_TREE, c.sha)
        return chunks, ranges

    if strategy == "merge-boundary":
        merge_chunks = enumerate_merge_chunks(repo, pathspec)
        chunks = []
        ranges = {}
        seen: set[str] = set()
        for merge_sha, base_sha, pr_commits in merge_chunks:
            chunk = []
            for c in pr_commits:
                if c.sha not in seen:
                    seen.add(c.sha)
                    chunk.append(c)
            if chunk:
                idx = len(chunks)
                chunks.append(chunk)
                ranges[idx] = (base_sha, merge_sha)
        return chunks, ranges

    if strategy == "mixed":
        merge_chunks = enumerate_merge_chunks(repo, pathspec)
        chunks = []
        ranges = {}
        seen: set[str] = set()
        for merge_sha, base_sha, pr_commits in merge_chunks:
            chunk = []
            for c in pr_commits:
                if c.sha not in seen:
                    seen.add(c.sha)
                    chunk.append(c)
            if chunk:
                idx = len(chunks)
                chunks.append(chunk)
                ranges[idx] = (base_sha, merge_sha)
        # squash-style commits not covered by any merge range
        for c in commits:
            if c.sha not in seen:
                seen.add(c.sha)
                idx = len(chunks)
                chunks.append([c])
                if c.parents:
                    ranges[idx] = (f"{c.sha}^", c.sha)
                else:
                    ranges[idx] = (_EMPTY_TREE, c.sha)
        return chunks, ranges

    # direct-commit: affinity groups — no clean diff range per group
    aff_chunks = _affinity_groups(
        commits, cfg, coupling=coupling, communities=communities, spec=spec
    )
    aff_ranges: dict[int, Optional[tuple[str, str]]] = {
        i: None for i in range(len(aff_chunks))
    }
    return aff_chunks, aff_ranges


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
        paths = [p for p in paths if not _COUPLING_DENY_RE.search(p)]
        if len(paths) > _MAX_COUPLING_FILES_PER_COMMIT:
            continue
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


@dataclass
class Specificity:
    """Per-window document frequencies + caps for IDF-style edge filtering.

    `fdf[path]` / `tdf[token]` = number of commits in the window touching that
    file / carrying that token. A shared file or token only counts toward
    file/topic overlap when its frequency is <= the corresponding cap, so that
    'hot' files and ubiquitous words stop binding unrelated commits.
    """
    fdf: dict
    tdf: dict
    ddf: dict
    file_df_max: Optional[int]
    token_df_max: Optional[int]
    dir_df_max: Optional[int]

    def specific_files(self, shared: set[str]) -> set[str]:
        if self.file_df_max is None:
            return shared
        return {p for p in shared if self.fdf.get(p, 0) <= self.file_df_max}

    def specific_tokens(self, shared: set[str]) -> set[str]:
        if self.token_df_max is None:
            return shared
        return {t for t in shared if self.tdf.get(t, 0) <= self.token_df_max}

    def specific_dirs(self, shared: set[str]) -> set[str]:
        if self.dir_df_max is None:
            return shared
        return {d for d in shared if self.ddf.get(d, 0) <= self.dir_df_max}


def resolve_specificity_caps(cfg: Config, commit_count: int) -> tuple:
    """Effective (file, token, dir) df caps after auto-tuning.

    Explicit caps always win. Otherwise, when auto_specificity is on and the
    repo is large enough, derive a single deterministic cap from commit count.
    Returns the caps unchanged (None unless set) in every opt-out case, so the
    small-fixture eval baseline is untouched.
    """
    explicit = (cfg.file_df_max, cfg.token_df_max, cfg.dir_df_max)
    if not cfg.auto_specificity or any(c is not None for c in explicit):
        return explicit
    if commit_count <= AUTO_SPEC_MIN_COMMITS:
        return explicit
    cap = max(3, commit_count // AUTO_SPEC_DIVISOR)
    return (cap, cap, cap)


def diff_summary(group: list[Commit]) -> dict:
    """File-level change summary for a candidate, so the skill can triage
    without running `git diff` per candidate. Deterministic, no extra git call
    (statuses already come from the name-status log)."""
    added: set[str] = set()
    deleted: set[str] = set()
    modified: set[str] = set()
    renamed: list[tuple[str, str]] = []
    for c in group:
        for f in c.files:
            if f.status.startswith(("A", "R", "C")):
                added.add(f.path)
                if f.status[0] in ("R", "C") and f.old_path:
                    renamed.append((f.old_path, f.path))
            elif f.status.startswith("D"):
                deleted.add(f.path)
            else:
                modified.add(f.path)
    modified -= added | deleted

    def cap(paths: set[str], n: int) -> list[str]:
        s = sorted(paths)
        return s if len(s) <= n else s[:n] + [f"... +{len(s) - n} more"]

    result: dict = {
        "summary": f"+{len(added)} added, -{len(deleted)} deleted, "
                   f"{len(modified)} modified",
        "added": cap(added, 8),
        "deleted": cap(deleted, 8),
        "modified_count": len(modified),
        "modified_top": cap(modified, 5),
    }
    if renamed:
        result["renamed"] = sorted(renamed)
    return result


def build_specificity(commits: list[Commit], cfg: Config) -> Optional[Specificity]:
    """Compute file/token/dir document frequencies over the window, or None when
    no cap is set (the cheap, baseline-identical path)."""
    if (cfg.file_df_max is None and cfg.token_df_max is None
            and cfg.dir_df_max is None):
        return None
    fdf: dict[str, int] = {}
    tdf: dict[str, int] = {}
    ddf: dict[str, int] = {}
    for c in commits:
        for p in c.paths:
            fdf[p] = fdf.get(p, 0) + 1
        for t in c.topic_tokens(cfg):
            tdf[t] = tdf.get(t, 0) + 1
        for d in c.dirs_deep(cfg):
            ddf[d] = ddf.get(d, 0) + 1
    return Specificity(fdf, tdf, ddf,
                       cfg.file_df_max, cfg.token_df_max, cfg.dir_df_max)


def _shares(a_dirs: set[str], b_dirs: set[str], a_tok: set[str], b_tok: set[str],
            a_paths: set[str], b_paths: set[str], coupling: Optional[dict] = None,
            communities: Optional[dict] = None,
            spec: Optional["Specificity"] = None) -> bool:
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

    When `spec` is given, the file and topic signals only count shared files /
    tokens that are *specific* (low document-frequency). This stops 'hot' files
    and ubiquitous words from binding unrelated commits — the failure mode that
    collapses small repos into one cluster once union-find takes the transitive
    closure of even sparse pairwise overlap. Coupling still uses the full paths.
    """
    shared_files = a_paths & b_paths
    shared_toks  = a_tok & b_tok
    shared_dirs  = a_dirs & b_dirs
    if spec is not None:
        shared_files = spec.specific_files(shared_files)
        shared_toks  = spec.specific_tokens(shared_toks)
        shared_dirs  = spec.specific_dirs(shared_dirs)
    file_overlap  = bool(shared_files) or bool(shared_dirs)
    topic_overlap = bool(shared_toks)
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


def _shares_with_reasons(a_dirs: set[str], b_dirs: set[str], a_tok: set[str],
                         b_tok: set[str], a_paths: set[str], b_paths: set[str],
                         coupling: Optional[dict] = None,
                         communities: Optional[dict] = None,
                         spec: Optional["Specificity"] = None,
                         ) -> tuple[bool, list[str]]:
    """Same merge decision as `_shares`, plus a short human-readable list of
    which sub-signals fired — surfaced on `Candidate.merge_reasons` so the
    skill can explain *why* commits were grouped together.

    Returns (matched, reasons). `reasons` has at most 4 entries, one per
    signal, in signal order: file, token, coupled, community.
    """
    shared_files = a_paths & b_paths
    shared_toks  = a_tok & b_tok
    shared_dirs  = a_dirs & b_dirs
    if spec is not None:
        shared_files = spec.specific_files(shared_files)
        shared_toks  = spec.specific_tokens(shared_toks)
        shared_dirs  = spec.specific_dirs(shared_dirs)
    file_overlap  = bool(shared_files) or bool(shared_dirs)
    topic_overlap = bool(shared_toks)
    coupled       = _coupled(a_paths, b_paths, coupling) if coupling else False
    community_match = (
        not file_overlap
        and communities is not None
        and commits_share_community(a_paths, b_paths, communities)
    )
    matched = sum([file_overlap, topic_overlap, coupled, community_match]) >= 2
    if not matched:
        return False, []

    reasons: list[str] = []
    if shared_files:
        reasons.append(f"file:{sorted(shared_files)[0]}")
    elif shared_dirs:
        reasons.append(f"dir:{sorted(shared_dirs)[0]}")
    if topic_overlap:
        reasons.append(f"token:{sorted(shared_toks)[0]}")
    if coupled:
        reasons.append("coupled")
    if community_match:
        reasons.append("community")
    return True, reasons


def _candidate_pairs(aggs: list[tuple[set[str], set[str], set[str]]],
                     cfg: Config,
                     spec: Optional["Specificity"] = None,
                     communities: Optional[dict] = None,
                     coupling: Optional[dict] = None,
                     ) -> set[tuple[int, int]]:
    """Build the set of index pairs worth running `_shares` on, via inverted
    indexes over the aggregated entities (chunks for `cluster`, commits for
    `_affinity_groups`).

    Each `aggs[i]` is `(dirs, tokens, paths)`. Replaces the exhaustive
    O(n^2) double loop: instead of scoring every pair, only pairs that share
    at least one file, token, dir, community, or known coupling edge are
    scored at all. DF-capped keys (per `spec`) are dropped *before* pair
    generation — that's what keeps hot files from generating quadratic
    candidates.

    A fifth index, built from `coupling` itself, recovers coupling-only edges
    (no shared file/token/dir/community) that the first four indexes would
    otherwise miss, preserving `_shares`'s recall.
    """
    file_to_idx: dict[str, list[int]] = {}
    token_to_idx: dict[str, list[int]] = {}
    dir_to_idx: dict[str, list[int]] = {}
    community_to_idx: dict[int, list[int]] = {}

    for idx, (dirs, toks, paths) in enumerate(aggs):
        for p in paths:
            file_to_idx.setdefault(p, []).append(idx)
        for t in toks:
            token_to_idx.setdefault(t, []).append(idx)
        for d in dirs:
            dir_to_idx.setdefault(d, []).append(idx)
        if communities is not None:
            for p in paths:
                cid = communities.get(p)
                if cid is not None:
                    community_to_idx.setdefault(cid, []).append(idx)

    if spec is not None:
        if spec.file_df_max is not None:
            file_to_idx = {k: v for k, v in file_to_idx.items()
                           if spec.fdf.get(k, 0) <= spec.file_df_max}
        if spec.token_df_max is not None:
            token_to_idx = {k: v for k, v in token_to_idx.items()
                            if spec.tdf.get(k, 0) <= spec.token_df_max}
        if spec.dir_df_max is not None:
            dir_to_idx = {k: v for k, v in dir_to_idx.items()
                          if spec.ddf.get(k, 0) <= spec.dir_df_max}

    pairs: set[tuple[int, int]] = set()

    def _emit(index: dict) -> None:
        for key in sorted(index.keys()):
            members = index[key]
            if len(members) < 2:
                continue
            for a in range(len(members)):
                for b in range(a + 1, len(members)):
                    i, j = members[a], members[b]
                    pairs.add((i, j) if i < j else (j, i))

    _emit(file_to_idx)
    _emit(token_to_idx)
    _emit(dir_to_idx)
    if communities is not None:
        _emit(community_to_idx)

    if coupling:
        for key in sorted(coupling.keys(), key=lambda fs: sorted(fs)):
            score = coupling[key]
            if score < COUPLING_MIN_SCORE:
                continue
            path_a, path_b = sorted(key)
            members_a = file_to_idx.get(path_a, [])
            members_b = file_to_idx.get(path_b, [])
            for i in members_a:
                for j in members_b:
                    if i == j:
                        continue
                    pairs.add((i, j) if i < j else (j, i))

    return pairs


def _affinity_groups(commits: list[Commit], cfg: Config,
                     coupling: Optional[dict] = None,
                     communities: Optional[dict] = None,
                     spec: Optional["Specificity"] = None) -> list[list[Commit]]:
    n = len(commits)
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        parent[find(i)] = find(j)

    aggs = [(c.dirs_deep(cfg), c.topic_tokens(cfg), c.paths) for c in commits]
    candidate_pairs = _candidate_pairs(aggs, cfg, spec=spec,
                                       communities=communities, coupling=coupling)
    for (i, j) in candidate_pairs:
        di, ti, pi = aggs[i]
        dj, tj, pj = aggs[j]
        if _shares(di, dj, ti, tj, pi, pj, coupling=coupling,
                   communities=communities, spec=spec):
            union(i, j)

    groups: dict[int, list[Commit]] = {}
    for i, c in enumerate(commits):
        groups.setdefault(find(i), []).append(c)
    # stable order by earliest author date in group
    return sorted(groups.values(), key=lambda g: min(c.date for c in g))


def _overlap_score(a: Commit, b: Commit, cfg: Config,
                   spec: Optional["Specificity"]) -> float:
    """Pairwise overlap density used by `_split_oversized` to peel cohesive
    sub-clusters out of an oversized cluster.

    overlap(a, b) = |specific shared files| + 0.5 * |specific shared topic tokens|

    With `spec` given, 'hot' files/tokens (high document-frequency) are filtered
    out exactly as `_shares` does, so ubiquitous paths don't drive the split.
    Without `spec`, falls back to plain set-intersection size.
    """
    shared_files = a.paths & b.paths
    shared_toks = a.topic_tokens(cfg) & b.topic_tokens(cfg)
    if spec is not None:
        shared_files = spec.specific_files(shared_files)
        shared_toks = spec.specific_tokens(shared_toks)
    return len(shared_files) + 0.5 * len(shared_toks)


def _split_oversized(commits: list[Commit], cfg: Config,
                     spec: Optional["Specificity"]) -> tuple[list[list[Commit]], list[str]]:
    """Greedy overlap-density peel: split an oversized cluster into sub-clusters
    that stay <= cfg.cluster_cap without cutting cohesive decisions in half.

    Unlike a naive date-window slice, this groups commits by *shared signal*
    (specific files / topic tokens), so a cohesive change that happens to span
    the cap isn't sliced apart just because of commit ordering.

    Algorithm (deterministic — every tie breaks on (date, sha)):
      1. Score every pair (i, j) by `_overlap_score`.
      2. Seed = commit with the highest total overlap against all others.
      3. Greedily add the remaining commit with the highest total overlap
         against the current group, until the group hits the cap or no
         remaining commit overlaps the group at all.
      4. Recurse on what's left. Commits with no overlap to anything become
         their own one-commit groups — singletons are real outliers, not a
         sign to keep merging.

    Returns (sub_clusters, reasons) — `reasons[i]` describes how sub_clusters[i]
    was produced, for surfacing as an INFO signal in `classify`.
    """
    SPLIT_REASON = "split from oversized cluster (overlap-density peel)"

    n = len(commits)
    pair_score: dict[tuple[int, int], float] = {}
    for i in range(n):
        for j in range(i + 1, n):
            s = _overlap_score(commits[i], commits[j], cfg, spec)
            if s > 0:
                pair_score[(i, j)] = s

    def score(i: int, j: int) -> float:
        if i == j:
            return 0.0
        key = (i, j) if i < j else (j, i)
        return pair_score.get(key, 0.0)

    def sort_key(idx: int):
        c = commits[idx]
        return (c.date, c.sha)

    remaining = list(range(n))
    groups: list[list[int]] = []

    while remaining:
        # seed: highest total overlap against all other remaining commits
        totals = {i: sum(score(i, j) for j in remaining if j != i) for i in remaining}
        max_total = max(totals.values())
        if max_total <= 0:
            # no overlap left among remaining commits — each becomes a singleton
            for i in sorted(remaining, key=sort_key):
                groups.append([i])
            remaining = []
            break

        seed = min((i for i in remaining if totals[i] == max_total), key=sort_key)
        group = [seed]
        pool = [i for i in remaining if i != seed]

        while pool and len(group) < cfg.cluster_cap:
            cand_totals = {i: sum(score(i, g) for g in group) for i in pool}
            best = max(cand_totals.values())
            if best <= 0:
                break
            pick = min((i for i in pool if cand_totals[i] == best), key=sort_key)
            group.append(pick)
            pool.remove(pick)

        groups.append(group)
        remaining = pool

    sub_clusters = [
        sorted((commits[i] for i in g), key=lambda c: (c.date, c.sha))
        for g in groups
    ]
    reasons = [SPLIT_REASON for _ in sub_clusters]
    return sub_clusters, reasons


def cluster(
    chunks: list[list[Commit]],
    cfg: Config,
    chunk_ranges: Optional[dict[int, Optional[tuple[str, str]]]] = None,
    coupling: Optional[dict] = None,
    communities: Optional[dict] = None,
    spec: Optional["Specificity"] = None,
) -> tuple[list[list[Commit]], dict[int, Optional[tuple[str, str]]],
           dict[int, Optional[str]], dict[int, list[str]]]:
    """Step 2.5: merge related chunks ACROSS boundaries (may be non-adjacent).

    Merge when ≥2 of 4 signals agree: file overlap, topic-token overlap,
    co-change coupling, Leiden community match. Deterministic. Oversized
    clusters (> cfg.cluster_cap) are split when cfg.split_oversized, via a
    greedy overlap-density peel (`_split_oversized`) rather than a naive
    date-window slice — so a cohesive decision isn't cut in half just because
    its commits share a date window.

    Candidate pairs worth scoring are drawn from inverted indexes
    (`_candidate_pairs`) rather than the full O(m^2) cross product — this is
    what keeps clustering tractable on large histories. Each merge records
    *why* it fired (`_shares_with_reasons`), surfaced as `cluster_merge_reasons`.

    Returns (clusters, cluster_ranges, cluster_split_reasons,
    cluster_merge_reasons). A cluster that came from exactly one input chunk
    inherits that chunk's range; multi-chunk clusters get None.
    `cluster_split_reasons[i]` is a human-readable reason string when cluster i
    was produced by the oversized-split, else None. `cluster_merge_reasons[i]`
    is a list of up to 8 short strings (e.g. `"file:lib/bus.ts"`, `"token:kafka"`,
    `"coupled"`, `"community"`) explaining which signals bound the cluster
    together; split sub-clusters inherit their parent's reasons.
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
    candidate_pairs = _candidate_pairs(aggs, cfg, spec=spec,
                                       communities=communities, coupling=coupling)
    merge_reasons: dict[int, list[str]] = {}
    for (i, j) in candidate_pairs:
        di, ti, pi = aggs[i]
        dj, tj, pj = aggs[j]
        matched, reasons = _shares_with_reasons(di, dj, ti, tj, pi, pj,
                                                 coupling=coupling,
                                                 communities=communities, spec=spec)
        if matched:
            union(i, j)
            root = find(i)
            bucket = merge_reasons.setdefault(root, [])
            for r in reasons:
                if r not in bucket and len(bucket) < 8:
                    bucket.append(r)

    cluster_reasons: dict[int, list[str]] = {}
    for chunk_idx in range(m):
        root = find(chunk_idx)
        cluster_reasons.setdefault(root, merge_reasons.get(root, []))

    groups: dict[int, list[Commit]] = {}
    group_chunk_indices: dict[int, list[int]] = {}
    for idx, chunk in enumerate(chunks):
        root = find(idx)
        groups.setdefault(root, []).extend(chunk)
        group_chunk_indices.setdefault(root, []).append(idx)

    result: list[list[Commit]] = []
    result_ranges: list[Optional[tuple[str, str]]] = []
    result_split_reasons: list[Optional[str]] = []
    result_merge_reasons: list[list[str]] = []
    for root, g in groups.items():
        g_sorted = sorted(g, key=lambda c: c.date)
        src_indices = group_chunk_indices[root]
        if len(src_indices) == 1 and chunk_ranges is not None:
            rng = chunk_ranges.get(src_indices[0])
        else:
            rng = None
        reasons_for_root = cluster_reasons.get(root, [])
        if cfg.split_oversized and len(g_sorted) > cfg.cluster_cap:
            sub_clusters, reasons = _split_oversized(g_sorted, cfg, spec)
            for sg, reason in zip(sub_clusters, reasons):
                result.append(sg)
                result_ranges.append(None)  # split sub-clusters lose their range
                result_split_reasons.append(reason)
                result_merge_reasons.append(reasons_for_root)
        else:
            result.append(g_sorted)
            result_ranges.append(rng)
            result_split_reasons.append(None)
            result_merge_reasons.append(reasons_for_root)

    order = sorted(
        range(len(result)),
        key=lambda k: min(c.date for c in result[k]),
    )
    ordered_result = [result[k] for k in order]
    ordered_ranges: dict[int, Optional[tuple[str, str]]] = {
        new_i: result_ranges[old_k]
        for new_i, old_k in enumerate(order)
    }
    ordered_split_reasons: dict[int, Optional[str]] = {
        new_i: result_split_reasons[old_k]
        for new_i, old_k in enumerate(order)
    }
    ordered_merge_reasons: dict[int, list[str]] = {
        new_i: result_merge_reasons[old_k]
        for new_i, old_k in enumerate(order)
    }
    return ordered_result, ordered_ranges, ordered_split_reasons, ordered_merge_reasons


def classify(
    commits: list[Commit],
    cfg: Config,
    repo: Optional[str] = None,
    chunk_range: Optional[tuple] = None,
    split_reason: Optional[str] = None,
) -> tuple[int, str, list[str]]:
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
    if split_reason:
        signals.append(f"INFO: {split_reason}")

    new_src = [f for f in files if f.status.startswith("A")
               and _in_source_root(f.path, cfg.source_roots or [])
               and not TEST_RE.search(f.path)]
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

    has_deletion = any(f.status.startswith("D") for f in files)
    has_addition = any(f.status[0] in ("A", "R", "C") for f in files)
    if has_deletion and has_addition:
        score += w["deletion"]
        signals.append("file deleted with correlated addition (replacement pattern)")
    elif has_deletion:
        signals.append("file deleted (no replacement — likely dead code)")

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
        if repo and chunk_range:
            base, head = chunk_range
            confirmed = is_pure_version_bump(repo, base, head, dep_changed)
        else:
            confirmed = True
        if confirmed:
            signals.append("FORCE_EXCLUDE: version bump only")
            return score, "skip", signals

    if score >= cfg.score_arch_threshold:
        cls = "architectural"
    elif score >= cfg.score_borderline_threshold:
        cls = "borderline"
    else:
        cls = "skip"
    return score, cls, signals


def scan_workarounds(repo: str, group: list[Commit],
                     cfg: Config) -> tuple[str, list[str]]:
    """Deterministic workaround detection over one candidate's commits.

    Returns (record_type, signals). record_type flips to "workaround" only on
    a STRONG signal — a marker pattern in an ADDED diff line, or a
    workaround-shaped file (shim/adapter/polyfill/patches/) being added.
    Message wording is recorded as a supporting signal but never flips alone.
    Bounded: at most WORKAROUND_MAX_COMMITS commits / WORKAROUND_MAX_LINES
    added lines are scanned per candidate, so large clusters stay cheap.
    """
    if not cfg.detect_workarounds or not repo:
        return "decision", []
    extra = []
    for pat in cfg.extra_workaround_markers:
        try:
            extra.append(re.compile(pat, re.I))
        except re.error:
            pass

    def hit(text: str) -> bool:
        return bool(WORKAROUND_STRONG_RE.search(text)
                    or WORKAROUND_COND_RE.search(text)
                    or any(p.search(text) for p in extra))

    signals: list[str] = []
    strong = False

    for c in group:
        if hit(c.subject):
            signals.append(f"workaround wording in message ({c.sha[:7]})")
            break

    for c in group:
        shaped = next((f for f in c.files if f.status[0] in ("A", "R", "C")
                       and WORKAROUND_FILE_RE.search(f.path)), None)
        if shaped:
            signals.append(f"workaround-shaped file added: {shaped.path}")
            strong = True
            break

    scanned = 0
    for c in group[:WORKAROUND_MAX_COMMITS]:
        if scanned >= WORKAROUND_MAX_LINES:
            break
        try:
            diff = git(repo, "show", "--format=", "--unified=0", c.sha)
        except RuntimeError:
            continue
        for line in diff.splitlines():
            if scanned >= WORKAROUND_MAX_LINES:
                break
            if not line.startswith("+") or line.startswith("+++"):
                continue
            scanned += 1
            content = line[1:]
            if hit(content):
                signals.append(
                    f"workaround marker added ({c.sha[:7]}): "
                    f"{content.strip()[:80]!r}")
                strong = True
                break  # one sample per commit is enough

    if not strong:
        return "decision", signals
    return "workaround", signals


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
        # depth-overlap is measured relative to the module root, otherwise every
        # file shares the module prefix and the whole module collapses into one
        # cluster (see Commit.dirs_deep).
        cfg.module_prefix = pathspec.rstrip("/")

    rev_range = None
    if history_scope.startswith("since:"):
        ref = history_scope.split(":", 1)[1]
        if not verify_ref(repo, ref):
            return {
                "preflight": {
                    "shallow": is_shallow(repo),
                    "output_dir": output_dir_for(code_scope),
                    "leiden_available": _LEIDEN_AVAILABLE,
                },
                "halt": "bad_ref",
                "halt_detail": f"reference '{ref}' is not a valid commit",
                "strategy": None,
                "scope": {"history": history_scope, "code": code_scope},
                "candidates": [],
            }
        if not is_ancestor(repo, ref):
            return {
                "preflight": {
                    "shallow": is_shallow(repo),
                    "output_dir": output_dir_for(code_scope),
                    "leiden_available": _LEIDEN_AVAILABLE,
                },
                "halt": "ref_not_ancestor",
                "halt_detail": f"reference '{ref}' is not an ancestor of HEAD",
                "strategy": None,
                "scope": {"history": history_scope, "code": code_scope},
                "candidates": [],
            }
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

    if cfg.source_roots is None:
        cfg.source_roots = detect_source_roots(repo)
    if cfg.extra_source_roots:
        cfg.source_roots = sorted(set(cfg.source_roots) | set(cfg.extra_source_roots))
    preflight["source_roots"] = cfg.source_roots

    strategy = detect_strategy(repo, pathspec)
    commits = parse_log(repo, rev_range, pathspec)
    coupling = build_coupling(repo, pathspec)
    communities = build_leiden_communities(repo)
    # auto-tune edge specificity from history size unless overridden
    cfg.file_df_max, cfg.token_df_max, cfg.dir_df_max = \
        resolve_specificity_caps(cfg, len(commits))
    preflight["specificity_cap"] = cfg.file_df_max
    spec = build_specificity(commits, cfg)
    chunks, chunk_ranges = chunk_commits(
        commits, strategy, cfg,
        repo=repo, pathspec=pathspec,
        coupling=coupling, communities=communities, spec=spec,
    )
    clusters, cluster_ranges, cluster_split_reasons, cluster_merge_reasons = cluster(
        chunks, cfg,
        chunk_ranges=chunk_ranges,
        coupling=coupling, communities=communities, spec=spec,
    )

    candidates: list[Candidate] = []
    for i, group in enumerate(clusters, start=1):
        score, cls, signals = classify(
            group, cfg, repo=repo, chunk_range=cluster_ranges.get(i - 1),
            split_reason=cluster_split_reasons.get(i - 1),
        )
        record_type, wa_signals = scan_workarounds(repo, group, cfg)
        if record_type == "workaround" and cls == "architectural":
            # the decision dominates; surface the markers for the ADR prose
            record_type = "decision"
            wa_signals = ["WORKAROUND: markers present — note them inside "
                          "the ADR"] + wa_signals
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
            diff_summary=diff_summary(group),
            range=cluster_ranges.get(i - 1),
            merge_reasons=cluster_merge_reasons.get(i - 1, []),
            record_type=record_type,
            workaround_signals=wa_signals,
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
        if pf.get("specificity_cap") is not None:
            print(f"specificity: df cap = {pf['specificity_cap']} (auto-tuned)")
        for c in manifest["candidates"]:
            wa = "  [WA]" if c["record_type"] == "workaround" else ""
            print(f"  [{c['classification']:13}] score={c['score']:+d} "
                  f"{c['id']} ({len(c['commits'])} commit){wa} :: {c['title_hint']}")
            print(f"      {c['diff_summary']['summary']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
