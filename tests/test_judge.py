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


def test_upsert_frontmatter_replaces_generation_only_block():
    """Frontmatter holding ONLY a generation: block is replaced, not appended to."""
    text = "---\ngeneration:\n  method: old\n---\n\n# T\n"
    out = judge._upsert_frontmatter(text, ["generation:", "  method: new"])
    assert out.count("generation:") == 1
    assert "method: new" in out
    assert "method: old" not in out
    assert out.startswith("---\n")
    assert "# T" in out


def test_upsert_frontmatter_blank_line_inside_generation_block():
    """A blank line between generation children must not end the block early."""
    text = ("---\ntitle: x\ngeneration:\n  method: old\n\n  options-kept: 1\n---\n"
            "\n# T\n")
    out = judge._upsert_frontmatter(text, ["generation:", "  method: new"])
    assert out.count("generation:") == 1
    assert "options-kept: 1" not in out
    assert "title: x" in out


# --- Fix 4: Stamp no-options ADRs ---

def test_render_stamps_no_options_adr():
    """ADRs with no Considered Options section should get stamped with evidence-verified."""
    adr = "# D\n\nNo content.\n"
    result = judge.verify_adr(repo("repo_squash"), adr)
    assert result["had_options"] is False
    out = judge.render_verified_adr(adr, result, date="2026-06-06")
    assert "evidence-verified: true" in out
    assert "verification: citation-structural-unscoped" in out
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


# --- Fix 5: Enforce cited SHA & scoping ---

def test_extract_linked_shas_parses_commits_line():
    """extract_linked_shas should parse Links section with Commits: line."""
    adr = (
        "# D\n\n## Links\n\n"
        "- Commits: abc1234, def5678\n\n"
        "## Outcome\n\nx\n"
    )
    shas = judge.extract_linked_shas(adr)
    assert shas == ["abc1234", "def5678"]


def test_extract_linked_shas_missing_links_section():
    """extract_linked_shas returns [] when no Links section."""
    adr = "# D\n\n## Outcome\n\nx\n"
    shas = judge.extract_linked_shas(adr)
    assert shas == []


def test_extract_linked_shas_case_insensitive_commits():
    """extract_linked_shas is case-insensitive on 'Commits' keyword."""
    adr = (
        "# D\n\n## Links\n\n"
        "- commits: abc1234\n\n"
        "## Outcome\n\nx\n"
    )
    shas = judge.extract_linked_shas(adr)
    assert shas == ["abc1234"]


def test_extract_linked_shas_tolerates_whitespace():
    """extract_linked_shas tolerates varied whitespace."""
    adr = (
        "# D\n\n## Links\n\n"
        "- Commits:  abc1234  ,  def5678  \n\n"
        "## Outcome\n\nx\n"
    )
    shas = judge.extract_linked_shas(adr)
    assert set(shas) == {"abc1234", "def5678"}


def test_citation_sha_not_in_allowed_set_fails():
    """A citation citing a real commit OUTSIDE the allowed set is dropped."""
    r, sha_cited, path, _ = _first_commit_with_added_file("repo_squash")
    # any other commit in the repo serves as the (wrong) allowed set
    all_shas = analyze.git(r, "rev-list", "HEAD").split()
    sha_allowed = next(s for s in all_shas if s != sha_cited)

    # the cited change is REAL (added:path holds) — it must fail on scope alone
    adr = _adr_with_option("opt", f"<!-- evidence: {sha_cited} added:{path} -->")
    result = judge.verify_adr(r, adr, allowed_shas=[sha_allowed])
    dropped = {o["option"]: o["reason"] for o in result["dropped"]}
    assert "opt" in dropped
    assert "not part of this candidate" in dropped["opt"]


def test_short_allowed_sha_never_matches():
    """Sub-7-char prefixes must not match — a 1-char typo would otherwise
    admit ~1/16 of all commits into scope."""
    r, sha, path, _ = _first_commit_with_added_file("repo_squash")
    adr = _adr_with_option("opt", f"<!-- evidence: {sha} added:{path} -->")
    result = judge.verify_adr(r, adr, allowed_shas=[sha[0]])
    dropped = {o["option"]: o["reason"] for o in result["dropped"]}
    assert "not part of this candidate" in dropped["opt"]


def test_explicit_empty_allowed_set_rejects_all():
    """allowed_shas=[] means 'no commits in scope': scoped, and reject all."""
    r, sha, path, _ = _first_commit_with_added_file("repo_squash")
    adr = _adr_with_option("opt", f"<!-- evidence: {sha} added:{path} -->")
    result = judge.verify_adr(r, adr, allowed_shas=[])
    assert result["scoped"] is True
    dropped = {o["option"]: o["reason"] for o in result["dropped"]}
    assert "not part of this candidate" in dropped["opt"]


def test_citation_sha_in_allowed_set_checked():
    """Citation with SHA in the allowed set should be checked normally."""
    r, sha, path, _ = _first_commit_with_added_file("repo_squash")
    adr = _adr_with_option("good", f"<!-- evidence: {sha} added:{path} -->")
    result = judge.verify_adr(r, adr, allowed_shas=[sha])
    kept = {o["option"] for o in result["kept"]}
    assert "good" in kept


def test_sha_prefix_matching():
    """Short SHA in allowed set should match full SHA in citation and vice versa."""
    r, sha_full, path, _ = _first_commit_with_added_file("repo_squash")
    sha_short = sha_full[:7]
    # Test 1: short in allowed, full in citation
    adr1 = _adr_with_option("opt", f"<!-- evidence: {sha_full} added:{path} -->")
    result1 = judge.verify_adr(r, adr1, allowed_shas=[sha_short])
    assert result1["kept"], "full SHA citation should match short allowed SHA"

    # Test 2: full in allowed, short in citation
    adr2 = _adr_with_option("opt", f"<!-- evidence: {sha_short} added:{path} -->")
    result2 = judge.verify_adr(r, adr2, allowed_shas=[sha_full])
    assert result2["kept"], "short SHA citation should match full allowed SHA"


def test_verify_adr_scoped_true_with_allowed_shas():
    """verify_adr should set scoped=true when allowed_shas is non-empty."""
    r, sha, path, _ = _first_commit_with_added_file("repo_squash")
    adr = _adr_with_option("opt", f"<!-- evidence: {sha} added:{path} -->")
    result = judge.verify_adr(r, adr, allowed_shas=[sha])
    assert result["scoped"] is True


def test_verify_adr_scoped_true_with_links_section():
    """verify_adr should set scoped=true when Links/Commits present."""
    r, sha, path, _ = _first_commit_with_added_file("repo_squash")
    adr = (
        "# D\n\n## Considered Options\n\n"
        f"- opt <!-- evidence: {sha} added:{path} -->\n\n"
        "## Links\n\n"
        f"- Commits: {sha}\n\n"
        "## Outcome\n\nx\n"
    )
    result = judge.verify_adr(r, adr)
    assert result["scoped"] is True


def test_verify_adr_scoped_false_without_allowed_set():
    """verify_adr should set scoped=false when no Links/Commits and no --commits."""
    r, sha, path, _ = _first_commit_with_added_file("repo_squash")
    adr = _adr_with_option("opt", f"<!-- evidence: {sha} added:{path} -->")
    result = judge.verify_adr(r, adr)  # no allowed_shas parameter
    assert result["scoped"] is False


def test_render_uses_citation_structural_when_scoped():
    """render_verified_adr should use 'citation-structural' when scoped."""
    r, sha, path, _ = _first_commit_with_added_file("repo_squash")
    adr = _adr_with_option("opt", f"<!-- evidence: {sha} added:{path} -->")
    result = judge.verify_adr(r, adr, allowed_shas=[sha])
    out = judge.render_verified_adr(adr, result, date="2026-06-06")
    assert "verification: citation-structural\n" in out
    assert "citation-structural-unscoped" not in out


def test_render_uses_citation_structural_unscoped_when_not_scoped():
    """render_verified_adr should use 'citation-structural-unscoped' when not scoped."""
    r, sha, path, _ = _first_commit_with_added_file("repo_squash")
    adr = _adr_with_option("opt", f"<!-- evidence: {sha} added:{path} -->")
    result = judge.verify_adr(r, adr)  # no allowed_shas
    out = judge.render_verified_adr(adr, result, date="2026-06-06")
    assert "verification: citation-structural-unscoped\n" in out
    assert "verification: citation-structural\n" not in out


def test_render_no_options_uses_scoped_logic():
    """No-options ADR stamp should follow scoped/unscoped logic."""
    r = repo("repo_squash")
    # Get a real SHA for the Links section
    cands = analyze.analyze(r)["candidates"]
    if not cands or not cands[0]["commits"]:
        pytest.skip("no commits in fixture")
    sha = cands[0]["commits"][0]

    # With Links/Commits: scoped
    adr_with_links = (
        "# D\n\n## Links\n\n"
        f"- Commits: {sha}\n\n"
        "## Outcome\n\nx\n"
    )
    result_scoped = judge.verify_adr(r, adr_with_links)
    out_scoped = judge.render_verified_adr(adr_with_links, result_scoped, date="2026-06-06")
    assert "verification: citation-structural\n" in out_scoped

    # Without Links: unscoped
    adr_no_links = "# D\n\n## Outcome\n\nx\n"
    result_unscoped = judge.verify_adr(r, adr_no_links)
    out_unscoped = judge.render_verified_adr(adr_no_links, result_unscoped, date="2026-06-06")
    assert "verification: citation-structural-unscoped\n" in out_unscoped


# --- CLI: unscoped warning ---------------------------------------------------

def _run_cli(*argv):
    import os
    import subprocess
    import sys
    return subprocess.run([sys.executable, judge.__file__, *argv],
                          capture_output=True, text=True)


def test_cli_verify_adr_warns_when_unscoped(tmp_path):
    """No --commits and no Links/Commits line -> stderr warning."""
    r, sha, path, _ = _first_commit_with_added_file("repo_squash")
    adr = tmp_path / "adr.md"
    adr.write_text(_adr_with_option("opt", f"<!-- evidence: {sha} added:{path} -->"))
    res = _run_cli("verify-adr", str(adr), "--repo", r)
    assert res.returncode == 0
    assert "citations verified unscoped" in res.stderr


def test_cli_verify_adr_silent_when_commits_given(tmp_path):
    r, sha, path, _ = _first_commit_with_added_file("repo_squash")
    adr = tmp_path / "adr.md"
    adr.write_text(_adr_with_option("opt", f"<!-- evidence: {sha} added:{path} -->"))
    res = _run_cli("verify-adr", str(adr), "--repo", r, "--commits", sha)
    assert "unscoped" not in res.stderr


# --- marker citations + record types (workaround support) --------------------

def _first_added_line(r, sha):
    """First non-empty quote-free added line of a commit (marker test target)."""
    diff = analyze.git(r, "show", "--format=", "--unified=0", sha)
    return next(l[1:].strip() for l in diff.splitlines()
                if l.startswith("+") and not l.startswith("+++")
                and '"' not in l and l[1:].strip())


def test_marker_citation_verified():
    r, sha, _, _ = _first_commit_with_added_file("repo_squash")
    line = _first_added_line(r, sha)
    status, reason = judge.verify_citation(r, judge.Citation(sha, "marker", line))
    assert status == "verified"
    assert "added in" in reason


def test_marker_citation_fails_for_absent_line():
    r, sha, _, _ = _first_commit_with_added_file("repo_squash")
    status, _ = judge.verify_citation(
        r, judge.Citation(sha, "marker", "THIS LINE WAS NEVER ADDED"))
    assert status == "failed"


def test_record_type_of_frontmatter():
    assert judge.record_type_of(
        "---\ntype: workaround\nstatus: active\n---\n\n# T\n") == "workaround"
    assert judge.record_type_of("# T\n") == "madr"
    # unknown types fall back to madr rather than guessing a section
    assert judge.record_type_of("---\ntype: exotic\n---\n\n# T\n") == "madr"


def _workaround_record(sha, marker_line, extra_bullets=""):
    return (
        "---\ntype: workaround\nstatus: active\n---\n\n"
        "# Work around upstream bug\n\n"
        "## Trigger\n\nUpstream bug in dep.\n\n"
        "## Evidence\n\n"
        f'- marker comment <!-- evidence: {sha} marker:"{marker_line}" -->\n'
        f"{extra_bullets}"
        "\n## Removal Condition\n\nDelete when the fix ships.\n\n"
        f"## Links\n\n- Commits: {sha}\n"
    )


def test_verify_adr_workaround_checks_evidence_section():
    r, sha, _, _ = _first_commit_with_added_file("repo_squash")
    line = _first_added_line(r, sha)
    adr = _workaround_record(
        sha, line,
        extra_bullets=f'- bogus <!-- evidence: {sha} marker:"NEVER ADDED" -->\n')
    result = judge.verify_adr(r, adr)
    assert result["record_type"] == "workaround"
    kept = {o["option"] for o in result["kept"]}
    dropped = {o["option"] for o in result["dropped"]}
    assert "marker comment" in kept
    assert "bogus" in dropped

    rendered = judge.render_verified_adr(adr, result, date="2026-06-10")
    assert "NEVER ADDED" not in rendered
    assert "evidence-verified: true" in rendered
    assert "type: workaround" in rendered      # original frontmatter preserved
    assert "verification: citation-structural\n" in rendered  # Links -> scoped


def test_workaround_all_evidence_dropped_collapses():
    r, sha, _, _ = _first_commit_with_added_file("repo_squash")
    adr = (
        "---\ntype: workaround\n---\n\n# W\n\n## Evidence\n\n"
        f'- bogus <!-- evidence: {sha} marker:"NEVER ADDED" -->\n'
        f"\n## Links\n\n- Commits: {sha}\n"
    )
    result = judge.verify_adr(r, adr)
    rendered = judge.render_verified_adr(adr, result, date="2026-06-10")
    assert judge.NO_EVIDENCE_LINE in rendered
    assert "bogus" not in rendered


def test_madr_ignores_evidence_section():
    """A plain MADR with an Evidence section still verifies Considered Options."""
    r, sha, path, _ = _first_commit_with_added_file("repo_squash")
    adr = (
        "# D\n\n## Considered Options\n\n"
        f"- good <!-- evidence: {sha} added:{path} -->\n\n"
        "## Evidence\n\n- stray bullet with no citation\n\n"
        f"## Links\n\n- Commits: {sha}\n"
    )
    result = judge.verify_adr(r, adr)
    assert result["record_type"] == "madr"
    assert {o["option"] for o in result["kept"]} == {"good"}
    assert not result["dropped"]  # the Evidence bullet was not treated as an option
