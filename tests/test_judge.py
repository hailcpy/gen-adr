"""
Tests for scripts/judge.py — the output-safety layer.

The LLM call is injected, so every test here is deterministic and offline:
  - check_tag_syntax runs real (fast) language parsers on temp files
  - the options judge is driven by stub runners with canned responses
No `claude -p` is ever spawned in the suite.
"""
import json
import shutil

import pytest

import analyze
import judge
from conftest import repo
from judge import (
    JudgeVerdict,
    build_evidence,
    build_options_judge_prompt,
    check_tag_syntax,
    evidence_for_candidate,
    extract_options,
    judge_options,
    parse_verdict,
)


# --- syntax-safety check -----------------------------------------------------

def test_valid_python_passes(tmp_path):
    f = tmp_path / "mod.py"
    f.write_text("# @ADR-0001-foo: see docs\ndef ok():\n    return 1\n")
    r = check_tag_syntax(str(tmp_path), "mod.py")
    assert r.status == "ok"


def test_broken_python_fails(tmp_path):
    # a tag accidentally placed mid-expression breaks the parse
    f = tmp_path / "bad.py"
    f.write_text("def oops(:\n    return 1\n")
    r = check_tag_syntax(str(tmp_path), "bad.py")
    assert r.status == "fail"
    assert r.detail


def test_unknown_extension_skipped(tmp_path):
    f = tmp_path / "data.json"
    f.write_text('{"a": 1}')
    r = check_tag_syntax(str(tmp_path), "data.json")
    assert r.status == "skipped"


@pytest.mark.skipif(not shutil.which("bash"), reason="bash not available")
def test_broken_shell_fails(tmp_path):
    f = tmp_path / "s.sh"
    f.write_text("if true; then\n  echo hi\n")  # missing fi
    r = check_tag_syntax(str(tmp_path), "s.sh")
    assert r.status == "fail"


def test_check_tags_aggregate(tmp_path):
    (tmp_path / "good.py").write_text("x = 1\n")
    (tmp_path / "bad.py").write_text("x = (\n")
    report = judge.check_tags(str(tmp_path), ["good.py", "bad.py", "x.json"])
    assert report["ok"] is False
    assert report["failed"] == 1
    assert report["checked"] == 3


# --- options extraction ------------------------------------------------------

ADR_WITH_OPTIONS = """\
# Adopt Kafka

## Considered Options

- Kafka
- RabbitMQ
- in-process queue

## Decision Outcome

Chosen: Kafka.
"""

ADR_NO_ALTERNATIVES = """\
# Add thing

## Considered Options

No alternatives recorded in commit history.

## Decision Outcome

Chosen: the thing.
"""


def test_extract_options_reads_bullets():
    assert extract_options(ADR_WITH_OPTIONS) == ["Kafka", "RabbitMQ", "in-process queue"]


def test_extract_options_empty_when_no_alternatives():
    assert extract_options(ADR_NO_ALTERNATIVES) == []


def test_extract_options_missing_section():
    assert extract_options("# Title\n\n## Status\nAccepted\n") == []


# --- prompt construction -----------------------------------------------------

def test_prompt_contains_options_and_evidence():
    p = build_options_judge_prompt(["Kafka", "RabbitMQ"], "commit: migrate to kafka")
    assert "Kafka" in p and "RabbitMQ" in p
    assert "migrate to kafka" in p
    assert "JSON" in p  # judge is instructed to emit JSON


# --- verdict parsing ---------------------------------------------------------

def test_parse_clean_json():
    raw = json.dumps({
        "options": [{"option": "Kafka", "verdict": "evidenced", "evidence": "msg"}],
        "overall": "pass",
    })
    v = parse_verdict(raw)
    assert v.overall == "pass"
    assert v.options[0].verdict == "evidenced"


def test_parse_tolerates_code_fence_and_prose():
    raw = "Sure!\n```json\n" + json.dumps({
        "options": [{"option": "X", "verdict": "unevidenced", "evidence": ""}],
        "overall": "fail",
    }) + "\n```\nhope that helps"
    v = parse_verdict(raw)
    assert v is not None
    assert v.overall == "fail"


def test_parse_garbage_returns_none():
    assert parse_verdict("no json here at all") is None


def test_parse_infers_overall_when_missing():
    raw = json.dumps({"options": [{"option": "X", "verdict": "unevidenced"}]})
    v = parse_verdict(raw)
    assert v.overall == "fail"  # inferred from the unevidenced option


# --- evidence builder (against real fixture repos) ---------------------------

def test_build_evidence_has_subject_and_status():
    """Evidence for a real candidate carries the subject line and a name-status."""
    r = repo("repo_squash")
    cands = analyze.analyze(r)["candidates"]
    cand = next(c for c in cands if c["commits"])
    ev = evidence_for_candidate(r, cand)
    assert ev.startswith("commit ")
    # the candidate's first subject must appear in the evidence text
    assert cand["subjects"][0] in ev
    # at least one name-status row (A/M/D/R + tab + path)
    assert any(line.strip()[:1] in "AMDR" and "\t" in line
               for line in ev.splitlines())


def test_build_evidence_empty_shas():
    assert build_evidence(repo("repo_squash"), []) == ""


def test_evidence_composes_with_judge(monkeypatch):
    """End-to-end wiring: candidate -> evidence -> prompt, no model call."""
    r = repo("repo_squash")
    cand = next(c for c in analyze.analyze(r)["candidates"] if c["commits"])
    ev = evidence_for_candidate(r, cand)
    captured = {}

    def fake_runner(prompt):
        captured["prompt"] = prompt
        return '{"options": [{"option": "X", "verdict": "evidenced"}], "overall": "pass"}'

    judge_options(["X"], ev, runner=fake_runner, runs=1)
    # the built evidence actually reached the judge prompt
    assert cand["subjects"][0] in captured["prompt"]


# --- N-run aggregation -------------------------------------------------------

def _runner_returning(*responses):
    """Stub runner that yields the given responses in order, then repeats the last."""
    seq = list(responses)
    def run(_prompt):
        return seq.pop(0) if len(seq) > 1 else seq[0]
    return run


def test_empty_options_pass_without_running():
    called = {"n": 0}
    def boom(_):
        called["n"] += 1
        return ""
    v = judge_options([], "evidence", runner=boom, runs=3)
    assert v.overall == "pass"
    assert called["n"] == 0  # judge never invoked when there's nothing to audit


def test_majority_vote_and_agreement():
    pass_r = json.dumps({"options": [{"option": "X", "verdict": "evidenced"}],
                         "overall": "pass"})
    fail_r = json.dumps({"options": [{"option": "X", "verdict": "unevidenced"}],
                         "overall": "fail"})
    v = judge_options(["X"], "ev",
                      runner=_runner_returning(pass_r, pass_r, fail_r), runs=3)
    assert v.overall == "pass"
    assert v.agreement == pytest.approx(2 / 3)
    assert v.runs == 3


def test_all_runs_garbled_fails_closed():
    v = judge_options(["X"], "ev", runner=_runner_returning("garbage"), runs=2)
    assert v.overall == "fail"
    assert v.agreement == 0.0


def test_runner_is_called_per_run():
    calls = {"n": 0}
    ok = json.dumps({"options": [{"option": "X", "verdict": "evidenced"}],
                     "overall": "pass"})
    def counting(_):
        calls["n"] += 1
        return ok
    judge_options(["X"], "ev", runner=counting, runs=4)
    assert calls["n"] == 4
