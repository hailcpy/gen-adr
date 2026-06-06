#!/usr/bin/env python3
"""
leiden_signal.py — optional tree-sitter + Leiden community signal for analyze.py.

When the optional deps are present the module builds a file-import graph for
the repo and returns a Leiden community ID per source file. analyze.py folds
this in as a fourth clustering signal (on top of: file overlap, topic tokens,
co-change coupling). Two of the four signals must agree before two commits are
merged, so this raises recall without lowering precision.

Install (pick one):
    pip install tree-sitter leidenalg python-igraph
    pip install tree-sitter cdlib networkx

When none of the graph-algorithm deps are present, build_leiden_communities()
returns None silently and the rest of the pipeline is unaffected.
"""
from __future__ import annotations

import os
import re
from typing import Optional

# ---------------------------------------------------------------------------
# Import extraction
# ---------------------------------------------------------------------------

_PY_IMPORT_RE = re.compile(
    r"^(?:from\s+([\w.]+)\s+import|import\s+([\w.]+))", re.MULTILINE
)
_JS_IMPORT_RE = re.compile(
    r"""(?:import\s+[^'"]*?\s+from\s+['"]([^'"]+)['"]"""
    r"""|require\s*\(\s*['"]([^'"]+)['"]\s*\))""",
    re.MULTILINE,
)
_GO_IMPORT_RE = re.compile(r'"([\w./\-]+)"')


def _extract_imports_regex(rel_path: str, content: str) -> list[str]:
    ext = os.path.splitext(rel_path)[1].lower()
    if ext == ".py":
        return [m.group(1) or m.group(2) for m in _PY_IMPORT_RE.finditer(content)]
    if ext in (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"):
        return [m.group(1) or m.group(2) for m in _JS_IMPORT_RE.finditer(content)
                if (m.group(1) or m.group(2))]
    if ext == ".go":
        return [m.group(1) for m in _GO_IMPORT_RE.finditer(content)]
    return []


def _ts_lang_for_ext(ext: str) -> Optional[str]:
    return {
        ".py": "python",
        ".js": "javascript", ".jsx": "javascript",
        ".mjs": "javascript", ".cjs": "javascript",
        ".ts": "typescript", ".tsx": "tsx",
        ".go": "go",
    }.get(ext)


def _extract_imports_ts(rel_path: str, content: str) -> list[str]:
    """Extract import strings with tree-sitter; fall back to regex on any failure."""
    ext = os.path.splitext(rel_path)[1].lower()
    lang_name = _ts_lang_for_ext(ext)
    if lang_name is None:
        return []

    try:
        from tree_sitter import Language, Parser
    except ImportError:
        return _extract_imports_regex(rel_path, content)

    lang = _load_ts_language(lang_name)
    if lang is None:
        return _extract_imports_regex(rel_path, content)

    try:
        parser = Parser(lang)
        tree = parser.parse(content.encode())
    except Exception:
        return _extract_imports_regex(rel_path, content)

    imports: list[str] = []
    src = content.encode()
    _walk_ts_imports(tree.root_node, src, imports, lang_name)
    return imports or _extract_imports_regex(rel_path, content)


def _load_ts_language(lang_name: str):
    """Try to load a tree-sitter Language object; return None if unavailable."""
    try:
        from tree_sitter import Language
    except ImportError:
        return None

    # Preferred: tree-sitter-languages bundle
    try:
        import tree_sitter_languages  # type: ignore
        return tree_sitter_languages.get_language(lang_name)
    except (ImportError, Exception):
        pass

    # Individual grammar packages: tree_sitter_python, tree_sitter_javascript, …
    pkg = f"tree_sitter_{lang_name.replace('-', '_')}"
    try:
        import importlib
        mod = importlib.import_module(pkg)
        return Language(mod.language())
    except Exception:
        return None


def _walk_ts_imports(node, src: bytes, out: list[str], lang: str) -> None:
    """Iterative tree-sitter AST walk collecting import target strings."""
    stack = [node]
    while stack:
        n = stack.pop()

        if lang == "python" and n.type in ("import_statement", "import_from_statement"):
            text = src[n.start_byte:n.end_byte].decode(errors="replace")
            m = re.search(r"from\s+([\w.]+)\s+import|^\s*import\s+([\w.]+)", text)
            if m:
                out.append(m.group(1) or m.group(2))

        elif lang in ("javascript", "typescript", "tsx"):
            if n.type == "import_statement":
                text = src[n.start_byte:n.end_byte].decode(errors="replace")
                m = re.search(r"""from\s+['"]([^'"]+)['"]""", text)
                if m:
                    out.append(m.group(1))
            elif n.type == "call_expression":
                text = src[n.start_byte:n.end_byte].decode(errors="replace")
                m = re.search(r"""require\s*\(\s*['"]([^'"]+)['"]\s*\)""", text)
                if m:
                    out.append(m.group(1))

        elif lang == "go" and n.type in ("import_spec", "interpreted_string_literal"):
            text = src[n.start_byte:n.end_byte].decode(errors="replace").strip('"')
            if "/" in text or text.replace("_", "").isalnum():
                out.append(text)

        stack.extend(reversed(n.children))


# ---------------------------------------------------------------------------
# Import → repo-file resolution
# ---------------------------------------------------------------------------

def _resolve_import(raw: str, importer: str, file_set: set[str]) -> Optional[str]:
    """Best-effort mapping of a raw import string to a repo-relative file path."""
    ext = os.path.splitext(importer)[1].lower()

    if ext == ".py":
        candidate = raw.lstrip(".").replace(".", "/")
        for suffix in (".py", "/__init__.py"):
            if (p := candidate + suffix) in file_set:
                return p
        base = os.path.dirname(importer)
        for suffix in (".py", "/__init__.py"):
            p = os.path.normpath(os.path.join(base, candidate + suffix))
            if p in file_set:
                return p
        return None

    if ext in (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"):
        if not raw.startswith("."):
            return None  # third-party package
        base = os.path.dirname(importer)
        base_path = os.path.normpath(os.path.join(base, raw))
        for suffix in ("", ".ts", ".tsx", ".js", ".jsx",
                       "/index.ts", "/index.tsx", "/index.js"):
            if (p := base_path + suffix) in file_set:
                return p
        return None

    if ext == ".go":
        tail = raw.split("/")[-1]
        for f in file_set:
            if f.endswith(f"/{tail}.go") or f == f"{tail}.go":
                return f
        return None

    return None


# ---------------------------------------------------------------------------
# Graph construction
# ---------------------------------------------------------------------------

_SOURCE_EXTS = frozenset({
    ".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".go",
})
_SKIP_DIRS = frozenset({
    ".git", "node_modules", "__pycache__", ".venv", "venv", "vendor",
    "dist", "build", ".tox", ".mypy_cache", ".pytest_cache",
})


def build_import_graph(repo: str) -> Optional[tuple[list[str], list[tuple[int, int]]]]:
    """
    Walk repo source files, extract imports, resolve to repo-relative paths.
    Returns (node_list, edge_list) with node_list[i] being a repo-relative
    path and edge_list containing (i, j) undirected index pairs.
    Returns None if no source files are found.
    """
    all_files: list[str] = []
    for dirpath, dirnames, filenames in os.walk(repo):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for fn in filenames:
            if os.path.splitext(fn)[1].lower() in _SOURCE_EXTS:
                rel = os.path.relpath(os.path.join(dirpath, fn), repo)
                all_files.append(rel)

    if not all_files:
        return None

    file_set = set(all_files)
    file_idx = {f: i for i, f in enumerate(all_files)}
    edge_set: set[frozenset] = set()
    edges: list[tuple[int, int]] = []

    for src_file in all_files:
        full_path = os.path.join(repo, src_file)
        try:
            with open(full_path, encoding="utf-8", errors="replace") as fh:
                content = fh.read()
        except OSError:
            continue

        for raw in _extract_imports_ts(src_file, content):
            target = _resolve_import(raw, src_file, file_set)
            if target and target != src_file:
                key: frozenset = frozenset({src_file, target})
                if key not in edge_set:
                    edge_set.add(key)
                    edges.append((file_idx[src_file], file_idx[target]))

    return all_files, edges


# ---------------------------------------------------------------------------
# Leiden community detection
# ---------------------------------------------------------------------------

def _leiden_leidenalg(n: int, edges: list[tuple[int, int]]) -> Optional[list[int]]:
    try:
        import igraph as ig  # type: ignore
        import leidenalg  # type: ignore
    except ImportError:
        return None
    g = ig.Graph(n=n, edges=edges, directed=False)
    partition = leidenalg.find_partition(g, leidenalg.ModularityVertexPartition,
                                         seed=42)
    membership = [0] * n
    for cid, members in enumerate(partition):
        for node in members:
            membership[node] = cid
    return membership


def _leiden_cdlib(n: int, edges: list[tuple[int, int]]) -> Optional[list[int]]:
    try:
        import networkx as nx  # type: ignore
        from cdlib import algorithms  # type: ignore
    except ImportError:
        return None
    g = nx.Graph()
    g.add_nodes_from(range(n))
    g.add_edges_from(edges)
    if g.number_of_edges() == 0:
        return list(range(n))
    try:
        result = algorithms.leiden(g, seed=42)
        membership = [0] * n
        for cid, members in enumerate(result.communities):
            for node in members:
                membership[node] = cid
        return membership
    except Exception:
        return None


def build_leiden_communities(repo: str) -> Optional[dict[str, int]]:
    """
    Return {repo_relative_path: community_id} for all source files, or None
    if the required deps (leidenalg/cdlib) are not installed.

    The graph is built from import/require edges extracted via tree-sitter
    (with a regex fallback). Community detection uses leidenalg if available,
    cdlib otherwise.
    """
    graph_data = build_import_graph(repo)
    if graph_data is None:
        return None

    nodes, edges = graph_data
    n = len(nodes)

    membership = _leiden_leidenalg(n, edges) or _leiden_cdlib(n, edges)
    if membership is None:
        return None

    return {node: membership[i] for i, node in enumerate(nodes)}


# ---------------------------------------------------------------------------
# Signal helper (used by analyze._shares)
# ---------------------------------------------------------------------------

def commits_share_community(a_paths: set[str], b_paths: set[str],
                             communities: dict[str, int]) -> bool:
    """True if any file in A and any file in B share a Leiden community ID."""
    a_comms = {communities[p] for p in a_paths if p in communities}
    b_comms = {communities[p] for p in b_paths if p in communities}
    return bool(a_comms & b_comms)
