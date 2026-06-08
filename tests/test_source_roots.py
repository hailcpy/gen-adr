"""
Tests for detect_source_roots, _in_source_root, and Config.from_file
source_roots / extra_source_roots keys.
"""
import json
import os

import pytest

import analyze
from conftest import repo

REPO_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))


# --- detect_source_roots -----------------------------------------------------

def test_detect_source_roots_finds_scripts_in_this_repo():
    """Running against the gen-adr repo itself must include 'scripts'."""
    roots = analyze.detect_source_roots(REPO_ROOT)
    assert "scripts" in roots, f"'scripts' missing from detected roots: {roots}"


def test_detect_source_roots_includes_legacy_set():
    """Even if a dir doesn't exist on disk, legacy roots are always present."""
    roots = analyze.detect_source_roots(REPO_ROOT)
    for legacy in ("src", "lib", "core", "app", "services", "internal", "pkg"):
        assert legacy in roots, f"legacy root '{legacy}' missing: {roots}"


def test_detect_source_roots_returns_sorted():
    roots = analyze.detect_source_roots(REPO_ROOT)
    assert roots == sorted(roots)


def test_detect_source_roots_excludes_deny_dirs(tmp_path):
    """Directories in the deny set must not appear even if they contain code."""
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "foo.js").write_text("module.exports = {};")
    (tmp_path / "dist").mkdir()
    (tmp_path / "dist" / "bundle.js").write_text("// compiled");
    # add a real source dir so the result is non-trivial
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("pass")
    roots = analyze.detect_source_roots(str(tmp_path))
    assert "node_modules" not in roots
    assert "dist" not in roots
    assert "src" in roots


# --- _in_source_root ---------------------------------------------------------

def test_in_source_root_exact_match():
    assert analyze._in_source_root("scripts", ["scripts", "src"])


def test_in_source_root_child_path():
    assert analyze._in_source_root("scripts/analyze.py", ["scripts"])


def test_in_source_root_nested():
    assert analyze._in_source_root("scripts/pipeline/transform.py", ["scripts"])


def test_in_source_root_no_match():
    assert not analyze._in_source_root("docs/readme.md", ["src", "lib"])


def test_in_source_root_prefix_not_partial():
    """'scriptsXY/foo.py' must NOT match root 'scripts'."""
    assert not analyze._in_source_root("scriptsXY/foo.py", ["scripts"])


# --- Config.from_file source_roots / extra_source_roots ---------------------

def test_config_from_file_source_roots_replace(tmp_path):
    cfg_file = tmp_path / "cfg.json"
    cfg_file.write_text(json.dumps({"source_roots": ["scripts", "bin"]}))
    cfg = analyze.Config.from_file(str(cfg_file))
    assert cfg.source_roots == ["bin", "scripts"]  # sorted


def test_config_from_file_extra_source_roots_preserves_auto_detect(tmp_path):
    """extra_source_roots alone leaves source_roots=None so analyze() still auto-detects."""
    cfg_file = tmp_path / "cfg.json"
    cfg_file.write_text(json.dumps({"extra_source_roots": ["tools", "cmd"]}))
    cfg = analyze.Config.from_file(str(cfg_file))
    assert cfg.source_roots is None
    assert set(cfg.extra_source_roots) == {"tools", "cmd"}


def test_config_from_file_source_roots_then_extra(tmp_path):
    """source_roots replaces; extra_source_roots stays separate to union at analyze() time."""
    cfg_file = tmp_path / "cfg.json"
    cfg_file.write_text(json.dumps({
        "source_roots": ["scripts"],
        "extra_source_roots": ["bin"],
    }))
    cfg = analyze.Config.from_file(str(cfg_file))
    assert cfg.source_roots == ["scripts"]
    assert cfg.extra_source_roots == ["bin"]


def test_analyze_unions_extra_source_roots_with_autodetect(tmp_path):
    """When source_roots=None and extra_source_roots is set, analyze() unions them with auto-detect."""
    import subprocess
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "t"], check=True)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("x = 1\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-q", "-m", "init"], check=True)
    cfg = analyze.Config()
    cfg.extra_source_roots = ["custom_dir"]
    m = analyze.analyze(str(tmp_path), cfg=cfg)
    roots = m["preflight"]["source_roots"]
    assert "custom_dir" in roots
    assert "src" in roots  # auto-detected still present


# --- end-to-end: new_src signal fires for scripts/ layout --------------------

def test_scripts_layout_new_src_signal(build_fixtures):
    """Files added under scripts/ must trigger the new_src signal in classify()."""
    m = analyze.analyze(repo("repo_scripts_layout"))
    cands = m["candidates"]
    # find the candidate for the transform pipeline commit (#2)
    target = None
    for c in cands:
        if any("#2" in s for s in c["subjects"]):
            target = c
            break
    assert target is not None, f"commit (#2) not found in candidates: {[c['subjects'] for c in cands]}"
    assert target["classification"] in ("architectural", "borderline"), (
        f"expected architectural/borderline for scripts/ new file, got "
        f"'{target['classification']}' signals={target['signals']}"
    )
    assert "new source files added" in target["signals"], (
        f"new_src signal missing: {target['signals']}"
    )


def test_preflight_contains_source_roots():
    """analyze() must populate preflight['source_roots']."""
    m = analyze.analyze(REPO_ROOT)
    assert "source_roots" in m["preflight"]
    roots = m["preflight"]["source_roots"]
    assert isinstance(roots, list)
    assert "scripts" in roots
