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


# --- citation verification (deterministic, against real fixtures) ------------

def _first_commit_with_added_file(repo_name):
    """Return (repo, sha, added_path, subject) for a fixture commit that adds a file."""
    r = repo(repo_name)
    for cand in analyze.analyze(r)["candidates"]:
        for sha in cand["commits"]:
            for status, paths in judge._name_status(r, sha):
                if status == "A" and paths:
                    return r, sha, paths[0], cand["subjects"][0]
    raise AssertionError(f"no added-file commit in {repo_name}")


def _adr_with_option(option_text, comment):
    return (f"# Decision\n\n## Considered Options\n\n"
            f"- {option_text} {comment}\n\n## Decision Outcome\n\nChosen.\n")


def test_extract_cited_option_parses_citation():
    adr = _adr_with_option("Redis", "<!-- evidence: 3f4a2bc deleted:src/x.py -->")
    cited = judge.extract_cited_options(adr)
    assert len(cited) == 1
    assert cited[0].option == "Redis"
    assert cited[0].citation.sha == "3f4a2bc"
    assert cited[0].citation.etype == "deleted"
    assert cited[0].citation.detail == "src/x.py"


def test_extract_cited_option_no_citation():
    cited = judge.extract_cited_options(_adr_with_option("Redis", ""))
    assert cited[0].citation is None


def test_verify_added_file_citation_passes():
    r, sha, path, _ = _first_commit_with_added_file("repo_squash")
    adr = _adr_with_option("opt", f"<!-- evidence: {sha} added:{path} -->")
    report = judge.verify_options(r, adr)
    assert report["overall"] == "pass"
    assert report["verdicts"][0]["status"] == "verified"


def test_verify_wrong_path_fails():
    r, sha, _, _ = _first_commit_with_added_file("repo_squash")
    adr = _adr_with_option("opt", f"<!-- evidence: {sha} added:does/not/exist.py -->")
    report = judge.verify_options(r, adr)
    assert report["overall"] == "fail"
    assert report["verdicts"][0]["status"] == "failed"


def test_verify_fabricated_sha_fails():
    r = repo("repo_squash")
    adr = _adr_with_option("opt", "<!-- evidence: 0000000 added:foo.py -->")
    report = judge.verify_options(r, adr)
    assert report["verdicts"][0]["status"] == "failed"
    assert "not found" in report["verdicts"][0]["reason"]


def test_verify_message_phrase():
    r, sha, _, subject = _first_commit_with_added_file("repo_squash")
    word = next(w for w in subject.split() if len(w) > 4)
    adr = _adr_with_option("opt", f"<!-- evidence: {sha} message:{word} -->")
    report = judge.verify_options(r, adr)
    assert report["verdicts"][0]["status"] == "verified"


def test_verify_uncited_option_fails():
    report = judge.verify_options(repo("repo_squash"), _adr_with_option("Redis", ""))
    assert report["overall"] == "fail"
    assert report["verdicts"][0]["status"] == "uncited"


def test_verify_unknown_type_is_uncheckable():
    r, sha, _, _ = _first_commit_with_added_file("repo_squash")
    adr = _adr_with_option("opt", f"<!-- evidence: {sha} vibes:src/x.py -->")
    report = judge.verify_options(r, adr)
    assert report["overall"] == "fail"
    assert report["verdicts"][0]["status"] == "uncheckable"


def _commit_with_removed_line(repo_name):
    """(repo, sha, token) for a fixture commit that deletes a line, or None."""
    r = repo(repo_name)
    for cand in analyze.analyze(r)["candidates"]:
        for sha in cand["commits"]:
            diff = analyze.git(r, "show", "--format=", "--unified=0", sha).splitlines()
            for line in diff:
                if line.startswith("-") and not line.startswith("---"):
                    token = next((w for w in line[1:].split() if len(w) > 3), None)
                    if token:
                        return r, sha, token
    return None


def test_verify_removed_line_is_deterministic():
    found = _commit_with_removed_line("repo_squash")
    if found is None:
        import pytest
        pytest.skip("no removed-line commit in fixture")
    r, sha, token = found
    ok = _adr_with_option("old approach", f"<!-- evidence: {sha} removed:{token} -->")
    assert judge.verify_options(r, ok)["verdicts"][0]["status"] == "verified"
    bad = _adr_with_option("x", f"<!-- evidence: {sha} removed:zzz_not_present_zzz -->")
    assert judge.verify_options(r, bad)["verdicts"][0]["status"] == "failed"


def test_verify_no_options_passes():
    adr = ("# D\n\n## Considered Options\n\n"
           "No alternatives recorded in commit history.\n\n## Outcome\n\nx\n")
    assert judge.verify_options(repo("repo_squash"), adr)["overall"] == "pass"


# --- combined pipeline (deterministic) + provenance render -------------------

def test_verify_adr_keeps_verified_drops_rest():
    r, sha, path, _ = _first_commit_with_added_file("repo_squash")
    adr = (
        "# D\n\n## Considered Options\n\n"
        f"- good <!-- evidence: {sha} added:{path} -->\n"
        f"- bad <!-- evidence: {sha} added:nope.py -->\n"
        f"- vibes <!-- evidence: {sha} replaced:{path} -->\n"
        "- naked\n\n"
        "## Decision Outcome\n\nx\n"
    )
    result = judge.verify_adr(r, adr)
    kept = {o["option"] for o in result["kept"]}
    dropped = {o["option"]: o["status"] for o in result["dropped"]}
    assert kept == {"good"}
    assert dropped == {"bad": "failed", "vibes": "uncheckable", "naked": "uncited"}
    assert result["overall"] == "rewritten"


def test_verify_adr_is_reproducible():
    r, sha, path, _ = _first_commit_with_added_file("repo_squash")
    adr = _adr_with_option("good", f"<!-- evidence: {sha} added:{path} -->")
    assert judge.verify_adr(r, adr) == judge.verify_adr(r, adr)


def test_render_stamps_frontmatter_and_drops_options():
    r, sha, path, _ = _first_commit_with_added_file("repo_squash")
    adr = (
        "# D\n\n## Considered Options\n\n"
        f"- good <!-- evidence: {sha} added:{path} -->\n"
        f"- bad <!-- evidence: {sha} added:nope.py -->\n\n"
        "## Decision Outcome\n\nx\n"
    )
    result = judge.verify_adr(r, adr)
    out = judge.render_verified_adr(adr, result, date="2026-06-06")
    assert out.startswith("---\n")
    assert "evidence-verified: true" in out
    assert "verification: citation-structural" in out
    assert "options-kept: 1" in out and "options-dropped: 1" in out
    # verified option re-emitted verbatim (citation comment preserved)
    assert f"added:{path}" in out
    # dropped option gone from the rendered Considered Options
    assert "- bad <!-- evidence" not in out


def test_render_all_dropped_becomes_no_alternatives():
    r, sha, _, _ = _first_commit_with_added_file("repo_squash")
    adr = (
        "# D\n\n## Considered Options\n\n"
        f"- bad <!-- evidence: {sha} added:nope.py -->\n\n"
        "## Decision Outcome\n\nx\n"
    )
    result = judge.verify_adr(r, adr)
    out = judge.render_verified_adr(adr, result)
    assert judge.NO_ALT_LINE in out


def test_render_upserts_into_existing_frontmatter():
    r, sha, path, _ = _first_commit_with_added_file("repo_squash")
    adr = (
        "---\ntitle: D\nstatus: accepted\n---\n\n"
        "## Considered Options\n\n"
        f"- good <!-- evidence: {sha} added:{path} -->\n\n"
        "## Decision Outcome\n\nx\n"
    )
    result = judge.verify_adr(r, adr)
    out = judge.render_verified_adr(adr, result)
    assert out.count("---") == 2          # still a single frontmatter block
    assert "status: accepted" in out      # existing keys preserved
    assert "evidence-verified: true" in out


def test_no_options_adr_stamped_with_generation():
    """No-options ADRs now get stamped with the generation block (Fix 4)."""
    adr = ("# D\n\n## Considered Options\n\n" + judge.NO_ALT_LINE
           + "\n\n## Decision Outcome\n\nx\n")
    result = judge.verify_adr(repo("repo_squash"), adr)
    assert result["overall"] == "pass"
    rendered = judge.render_verified_adr(adr, result, date="2026-06-06")
    # Now should have frontmatter with generation block
    assert "---\n" in rendered
    assert "generation:" in rendered
    assert "evidence-verified: true" in rendered
    # Body should be unchanged
    assert "## Decision Outcome" in rendered


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


# --- Fix 3: Idempotent frontmatter ---

def test_upsert_frontmatter_idempotent():
    """Running verify_adr twice on the same ADR should produce identical output."""
    r, sha, path, _ = _first_commit_with_added_file("repo_squash")
    adr = (
        "# D\n\n## Considered Options\n\n"
        f"- good <!-- evidence: {sha} added:{path} -->\n\n"
        "## Decision Outcome\n\nx\n"
    )
    result1 = judge.verify_adr(r, adr)
    rendered1 = judge.render_verified_adr(adr, result1, date="2026-06-06")

    # Run verify_adr + render again on the first output
    result2 = judge.verify_adr(r, rendered1)
    rendered2 = judge.render_verified_adr(rendered1, result2, date="2026-06-06")

    assert rendered2 == rendered1
    # generation: block should appear exactly once
    assert rendered1.count("generation:") == 1


# --- Fix 4: Stamp no-options ADRs ---

def test_render_stamps_no_options_adr():
    """ADRs with no Considered Options section should get stamped with evidence-verified."""
    adr = "# D\n\nNo content.\n"
    result = judge.verify_adr(repo("repo_squash"), adr)
    assert result["had_options"] is False
    out = judge.render_verified_adr(adr, result, date="2026-06-06")
    assert "evidence-verified: true" in out
    assert "verification: citation-structural" in out
    assert "options-kept: 0" in out
    assert "options-dropped: 0" in out


def test_render_stamps_no_alternatives_recorded_adr():
    """ADRs with 'No alternatives recorded' should get stamped."""
    adr = (
        "# D\n\n## Considered Options\n\n" + judge.NO_ALT_LINE
        + "\n\n## Decision Outcome\n\nx\n"
    )
    result = judge.verify_adr(repo("repo_squash"), adr)
    assert result["had_options"] is False
    out = judge.render_verified_adr(adr, result, date="2026-06-06")
    assert "evidence-verified: true" in out
    assert "options-kept: 0" in out


def test_no_options_stamp_idempotent():
    """No-options ADRs should be stamped idempotently."""
    adr = "# D\n\nNo content.\n"
    r = repo("repo_squash")
    result1 = judge.verify_adr(r, adr)
    rendered1 = judge.render_verified_adr(adr, result1, date="2026-06-06")

    result2 = judge.verify_adr(r, rendered1)
    rendered2 = judge.render_verified_adr(rendered1, result2, date="2026-06-06")

    assert rendered2 == rendered1
