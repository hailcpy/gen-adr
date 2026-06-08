"""
Tests for Gap A (rename old_path), Gap B (diff-based version bump), Gap C
(deletion-only vs. deletion+addition).
"""
import os
import subprocess
import tempfile

import analyze
from conftest import repo


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _sh(*args, cwd, env=None):
    env_full = {**os.environ, **(env or {})}
    subprocess.run(list(args), cwd=cwd, check=True, capture_output=True, env=env_full)


_DATE_ENV = {
    "GIT_AUTHOR_DATE": "2024-01-01T12:00:00",
    "GIT_COMMITTER_DATE": "2024-01-01T12:00:00",
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t",
}


def _date_env(d):
    return {
        **_DATE_ENV,
        "GIT_AUTHOR_DATE": f"{d}T12:00:00",
        "GIT_COMMITTER_DATE": f"{d}T12:00:00",
    }


def _init_repo(tmp):
    _sh("git", "init", "-q", "-b", "main", cwd=tmp)
    _sh("git", "config", "user.email", "t@t", cwd=tmp)
    _sh("git", "config", "user.name", "t", cwd=tmp)


def _commit(tmp, msg, date="2024-01-01"):
    env = _date_env(date)
    _sh("git", "add", "-A", cwd=tmp, env=env)
    _sh("git", "commit", "-qm", msg, cwd=tmp, env=env)


def _head(tmp):
    return subprocess.check_output(
        ["git", "-C", tmp, "rev-parse", "HEAD"], text=True
    ).strip()


# ---------------------------------------------------------------------------
# Gap A — rename old_path preserved
# ---------------------------------------------------------------------------

def test_parse_log_preserves_rename_old_path(build_fixtures):
    commits = analyze.parse_log(repo("repo_rename"), rev_range=None, pathspec=None)
    rename_commits = [
        c for c in commits
        if any(f.status.startswith("R") for f in c.files)
    ]
    assert rename_commits, "expected at least one rename commit in repo_rename"
    fc = next(f for c in rename_commits for f in c.files if f.status.startswith("R"))
    assert fc.old_path is not None, "old_path must be set for rename"
    assert fc.old_path == "src/old_name.ts"
    assert fc.path == "src/new_name.ts"


# ---------------------------------------------------------------------------
# Gap B — is_pure_version_bump
# ---------------------------------------------------------------------------

def test_is_pure_version_bump_true_for_package_json():
    with tempfile.TemporaryDirectory() as tmp:
        _init_repo(tmp)
        with open(os.path.join(tmp, "package.json"), "w") as f:
            f.write('{\n  "dependencies": {\n    "lodash": "^4.17.20"\n  }\n}\n')
        _commit(tmp, "init", "2024-01-01")
        base = _head(tmp)

        with open(os.path.join(tmp, "package.json"), "w") as f:
            f.write('{\n  "dependencies": {\n    "lodash": "^4.17.21"\n  }\n}\n')
        _commit(tmp, "chore: bump lodash", "2024-01-02")
        head = _head(tmp)

        assert analyze.is_pure_version_bump(tmp, base, head, ["package.json"])


def test_is_pure_version_bump_false_for_dep_swap():
    with tempfile.TemporaryDirectory() as tmp:
        _init_repo(tmp)
        with open(os.path.join(tmp, "package.json"), "w") as f:
            f.write('{\n  "dependencies": {\n    "moment": "^2.29.0"\n  }\n}\n')
        _commit(tmp, "init", "2024-01-01")
        base = _head(tmp)

        with open(os.path.join(tmp, "package.json"), "w") as f:
            f.write('{\n  "dependencies": {\n    "date-fns": "^2.30.0"\n  }\n}\n')
        _commit(tmp, "chore: swap moment for date-fns", "2024-01-02")
        head = _head(tmp)

        assert not analyze.is_pure_version_bump(tmp, base, head, ["package.json"])


def test_is_pure_version_bump_true_for_pyproject():
    with tempfile.TemporaryDirectory() as tmp:
        _init_repo(tmp)
        with open(os.path.join(tmp, "pyproject.toml"), "w") as f:
            f.write('[tool.poetry.dependencies]\nrequests = "^2.28.0"\n')
        _commit(tmp, "init", "2024-01-01")
        base = _head(tmp)

        with open(os.path.join(tmp, "pyproject.toml"), "w") as f:
            f.write('[tool.poetry.dependencies]\nrequests = "^2.29.0"\n')
        _commit(tmp, "chore: bump requests", "2024-01-02")
        head = _head(tmp)

        assert analyze.is_pure_version_bump(tmp, base, head, ["pyproject.toml"])


# ---------------------------------------------------------------------------
# Gap B — classify skips / keeps based on diff content
# ---------------------------------------------------------------------------

def test_classify_skips_real_version_bump():
    with tempfile.TemporaryDirectory() as tmp:
        _init_repo(tmp)
        with open(os.path.join(tmp, "package.json"), "w") as f:
            f.write('{\n  "dependencies": {\n    "lodash": "^4.17.20"\n  }\n}\n')
        _commit(tmp, "init", "2024-01-01")
        base = _head(tmp)

        with open(os.path.join(tmp, "package.json"), "w") as f:
            f.write('{\n  "dependencies": {\n    "lodash": "^4.17.21"\n  }\n}\n')
        _commit(tmp, "chore: bump lodash", "2024-01-02")
        head = _head(tmp)

        commits = analyze.parse_log(tmp, rev_range=f"{base}..{head}", pathspec=None)
        assert commits
        _, cls, signals = analyze.classify(
            commits, analyze.Config(),
            repo=tmp, chunk_range=(base, head),
        )
        assert cls == "skip"
        assert any("version bump" in s for s in signals)


def test_classify_keeps_real_dep_swap():
    with tempfile.TemporaryDirectory() as tmp:
        _init_repo(tmp)
        with open(os.path.join(tmp, "package.json"), "w") as f:
            f.write('{\n  "dependencies": {\n    "moment": "^2.29.0"\n  }\n}\n')
        _commit(tmp, "init", "2024-01-01")
        base = _head(tmp)

        with open(os.path.join(tmp, "package.json"), "w") as f:
            f.write('{\n  "dependencies": {\n    "date-fns": "^2.30.0"\n  }\n}\n')
        _commit(tmp, "chore: swap moment for date-fns", "2024-01-02")
        head = _head(tmp)

        commits = analyze.parse_log(tmp, rev_range=f"{base}..{head}", pathspec=None)
        assert commits
        _, cls, signals = analyze.classify(
            commits, analyze.Config(),
            repo=tmp, chunk_range=(base, head),
        )
        assert cls != "skip" or not any("version bump" in s for s in signals)


# ---------------------------------------------------------------------------
# Gap C — deletion signal
# ---------------------------------------------------------------------------

def test_deletion_without_addition_no_replacement_signal(build_fixtures):
    commits = analyze.parse_log(
        repo("repo_dead_code_delete"), rev_range=None, pathspec=None
    )
    delete_commits = [
        c for c in commits if any(f.status.startswith("D") for f in c.files)
    ]
    assert delete_commits, "expected a delete commit in repo_dead_code_delete"
    _, _, signals = analyze.classify(delete_commits, analyze.Config())
    signal_text = " ".join(signals)
    assert "replacement pattern" not in signal_text
    assert "likely dead code" in signal_text


def test_deletion_with_addition_keeps_replacement_signal(build_fixtures):
    commits = analyze.parse_log(
        repo("repo_replacement"), rev_range=None, pathspec=None
    )
    relevant = [
        c for c in commits
        if any(f.status.startswith("D") for f in c.files)
        and any(f.status.startswith("A") for f in c.files)
    ]
    assert relevant, "expected a delete+add commit in repo_replacement"
    _, _, signals = analyze.classify(relevant, analyze.Config())
    assert any("replacement pattern" in s for s in signals)
