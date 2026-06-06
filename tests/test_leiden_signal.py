"""
Unit tests for scripts/leiden_signal.py.

All tests are self-contained (no real repo, no optional deps required).
The Leiden community-detection path is tested via dependency injection:
we pass pre-built node/edge data directly to the private helpers.
"""
import os
import sys
import textwrap
import tempfile

import pytest

# Make scripts/ importable regardless of how pytest is invoked
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import leiden_signal as ls


# ---------------------------------------------------------------------------
# Import extraction (regex path — no tree-sitter needed)
# ---------------------------------------------------------------------------

def test_py_import_extraction():
    src = textwrap.dedent("""\
        import os
        from pathlib import Path
        from mypackage.utils import foo
    """)
    out = ls._extract_imports_regex("foo.py", src)
    assert "os" in out
    assert "pathlib" in out
    assert "mypackage.utils" in out


def test_js_import_extraction():
    src = textwrap.dedent("""\
        import React from 'react';
        import { foo } from './utils';
        const bar = require('./bar');
    """)
    out = ls._extract_imports_regex("foo.js", src)
    assert "react" in out
    assert "./utils" in out
    assert "./bar" in out


def test_unknown_ext_returns_empty():
    assert ls._extract_imports_regex("file.rb", "require 'something'") == []


# ---------------------------------------------------------------------------
# Import resolution
# ---------------------------------------------------------------------------

def test_resolve_py_stdlib_not_in_file_set():
    file_set = {"mypackage/utils.py"}
    assert ls._resolve_import("os", "mypackage/main.py", file_set) is None


def test_resolve_py_local_module():
    file_set = {"mypackage/utils.py", "mypackage/main.py"}
    result = ls._resolve_import("mypackage.utils", "mypackage/main.py", file_set)
    assert result == "mypackage/utils.py"


def test_resolve_js_relative():
    file_set = {"src/utils.ts", "src/app.ts"}
    result = ls._resolve_import("./utils", "src/app.ts", file_set)
    assert result == "src/utils.ts"


def test_resolve_js_third_party_ignored():
    file_set = {"src/utils.ts"}
    assert ls._resolve_import("react", "src/app.ts", file_set) is None


def test_resolve_js_index():
    file_set = {"src/components/index.ts", "src/app.ts"}
    result = ls._resolve_import("./components", "src/app.ts", file_set)
    assert result == "src/components/index.ts"


# ---------------------------------------------------------------------------
# build_import_graph — uses a real temp directory
# ---------------------------------------------------------------------------

def _write_files(base: str, files: dict[str, str]) -> None:
    for rel, content in files.items():
        full = os.path.join(base, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as fh:
            fh.write(content)


def test_build_import_graph_py():
    with tempfile.TemporaryDirectory() as tmp:
        _write_files(tmp, {
            "pkg/a.py": "from pkg.b import something\n",
            "pkg/b.py": "x = 1\n",
        })
        result = ls.build_import_graph(tmp)
        assert result is not None
        nodes, edges = result
        assert "pkg/a.py" in nodes
        assert "pkg/b.py" in nodes
        a_idx = nodes.index("pkg/a.py")
        b_idx = nodes.index("pkg/b.py")
        assert (a_idx, b_idx) in edges or (b_idx, a_idx) in edges


def test_build_import_graph_no_sources():
    with tempfile.TemporaryDirectory() as tmp:
        _write_files(tmp, {"README.md": "# hi\n"})
        assert ls.build_import_graph(tmp) is None


def test_build_import_graph_skips_node_modules():
    with tempfile.TemporaryDirectory() as tmp:
        _write_files(tmp, {
            "src/app.ts": "import { foo } from './utils';\n",
            "src/utils.ts": "export const foo = 1;\n",
            "node_modules/react/index.js": "module.exports = {};\n",
        })
        result = ls.build_import_graph(tmp)
        assert result is not None
        nodes, _ = result
        assert not any("node_modules" in n for n in nodes)


# ---------------------------------------------------------------------------
# commits_share_community
# ---------------------------------------------------------------------------

def test_share_community_true():
    comms = {"a.py": 0, "b.py": 0, "c.py": 1}
    assert ls.commits_share_community({"a.py"}, {"b.py"}, comms)


def test_share_community_false():
    comms = {"a.py": 0, "b.py": 1, "c.py": 1}
    assert not ls.commits_share_community({"a.py"}, {"b.py", "c.py"}, comms)


def test_share_community_unknown_files():
    comms = {"a.py": 0}
    # b.py not in communities → treated as no signal
    assert not ls.commits_share_community({"a.py"}, {"unknown.py"}, comms)


# ---------------------------------------------------------------------------
# Integration: analyze._shares picks up community signal
# ---------------------------------------------------------------------------

def test_analyze_shares_community_is_suppressed_on_file_overlap():
    """Community must not count as a second signal when file_overlap already fired.

    Both commits touch src/a.py (file_overlap=True). Without topic/coupling,
    sum must be 1 — community is suppressed to prevent the same shared file
    from satisfying the two-signal threshold twice.
    """
    import analyze

    comms = {"src/a.py": 0, "src/b.py": 0}
    assert not analyze._shares(
        set(), set(),
        set(), set(),
        {"src/a.py"}, {"src/a.py", "src/b.py"},
        coupling=None,
        communities=comms,
    )


def test_analyze_shares_community_alone_not_enough():
    """Community match alone (1 of 4) should not trigger merge."""
    import analyze

    comms = {"auth/login.py": 0, "auth/register.py": 0}
    assert not analyze._shares(
        set(), set(),
        set(), set(),
        {"auth/login.py"}, {"auth/register.py"},
        coupling=None,
        communities=comms,
    )


def test_analyze_shares_same_file_community_not_double_counted():
    """Two commits sharing a source file must not merge on file+community alone.

    Regression for: commits_share_community returns True for any shared file
    because it maps to one community, which previously made file_overlap and
    community_match fire together and satisfy the >=2 threshold with only one
    real signal.
    """
    import analyze

    # Both commits touch auth/login.py → same file, same community.
    # No topic overlap, no coupling → must NOT merge.
    comms = {"auth/login.py": 0, "auth/register.py": 0}
    assert not analyze._shares(
        set(), set(),
        set(), set(),
        {"auth/login.py"}, {"auth/login.py"},
        coupling=None,
        communities=comms,
    )


def test_analyze_shares_community_plus_topic_merges():
    """Community in same module + topic overlap should trigger merge (no shared files)."""
    import analyze

    comms = {"auth/login.py": 0, "auth/register.py": 0, "auth/utils.py": 0}
    assert analyze._shares(
        set(), set(),
        {"oauth"}, {"oauth"},          # topic overlap
        {"auth/login.py"}, {"auth/register.py"},   # different files
        coupling=None,
        communities=comms,
    )
