#!/usr/bin/env python3
"""
judge.py — output-safety layer for the adr-generator skill.

The deterministic pipeline (analyze.py) decides *which* commits become ADR
candidates. This module guards the *output* the agent then produces, with two
independent checks the previous design called the "LLM-judge pass":

  1. check_tag_syntax  — DETERMINISTIC. After @ADR comment tags are placed in a
     source file, the file must still parse. No model call: we run the language's
     own compiler/parser. A misplaced tag that breaks the build is caught here.

  2. judge_options     — LLM. "Considered Options" is the MADR section most prone
     to hallucination (the model inventing plausible-but-unevidenced alternatives).
     We ask a judge model whether each listed option is actually evidenced by the
     diff / commit messages, run it N times, and take a majority vote. The judge
     can only ever DEMOTE an unevidenced option — it never invents — so like the
     clustering floor it cannot reduce correctness below the deterministic base.

Design mirrors analyze.py: the LLM call is injected (a `runner` callable), so the
prompt builder, verdict parser, and N-run aggregation are all unit-testable
without spawning Claude. Only `default_runner` actually shells out to `claude -p`.

Usage:
    python3 scripts/judge.py check-tags <repo> <file> [<file> ...]
    python3 scripts/judge.py judge-options <adr.md> --evidence <evidence.txt>
                                           [--runs N] [--json]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass, asdict, field
from typing import Callable, Optional

import analyze  # reuse git() + the parsed pipeline so the two scripts compose

# --- syntax-safety check (deterministic) -------------------------------------

# file extension -> argv template that returns non-zero on a parse error.
# {f} is substituted with the file path. Checks that are unavailable on the host
# degrade to SKIPPED rather than failing the gate.
SYNTAX_CHECKERS: dict[str, list[str]] = {
    ".py": ["python3", "-m", "py_compile", "{f}"],
    ".js": ["node", "--check", "{f}"],
    ".mjs": ["node", "--check", "{f}"],
    ".cjs": ["node", "--check", "{f}"],
    ".rb": ["ruby", "-c", "{f}"],
    ".sh": ["bash", "-n", "{f}"],
    ".bash": ["bash", "-n", "{f}"],
    ".go": ["gofmt", "-e", "{f}"],
}


@dataclass
class SyntaxResult:
    path: str
    status: str  # ok | fail | skipped
    detail: str = ""


def _have(cmd: str) -> bool:
    from shutil import which
    return which(cmd) is not None


def check_tag_syntax(repo: str, rel_path: str) -> SyntaxResult:
    """Parse-check a single (already tag-annotated) file. Pure over the filesystem."""
    ext = os.path.splitext(rel_path)[1].lower()
    checker = SYNTAX_CHECKERS.get(ext)
    abs = os.path.join(repo, rel_path)
    if checker is None:
        return SyntaxResult(rel_path, "skipped", f"no checker for {ext or 'no-ext'}")
    if not _have(checker[0]):
        return SyntaxResult(rel_path, "skipped", f"{checker[0]} not on PATH")
    argv = [a.replace("{f}", abs) for a in checker]
    res = subprocess.run(argv, capture_output=True, text=True)
    if res.returncode == 0:
        return SyntaxResult(rel_path, "ok")
    detail = (res.stderr or res.stdout).strip().splitlines()
    return SyntaxResult(rel_path, "fail", detail[-1] if detail else "parse error")


def check_tags(repo: str, paths: list[str]) -> dict:
    results = [check_tag_syntax(repo, p) for p in paths]
    failed = [r for r in results if r.status == "fail"]
    return {
        "ok": not failed,
        "checked": len(results),
        "failed": len(failed),
        "results": [asdict(r) for r in results],
    }


# --- evidence builder --------------------------------------------------------

def build_evidence(repo: str, shas: list[str], max_files: int = 40) -> str:
    """Render the commit evidence block the options judge reads, from SHAs.

    Per commit: the subject line (where "instead of X" / "migrate from Y" live)
    plus name-status (where deletions = the replaced old approach show up). This
    is exactly the (a)/(b) evidence the judge is told to look for; full hunks are
    intentionally omitted to keep the prompt small and the signal dense.
    """
    blocks: list[str] = []
    for sha in shas:
        subject = analyze.git(repo, "show", "--no-patch", "--format=%H %s",
                              sha).strip()
        name_status = analyze.git(repo, "show", "--name-status", "--format=",
                                  sha).strip().splitlines()
        lines = [f"commit {subject}"]
        for ns in name_status[:max_files]:
            if ns.strip():
                lines.append(f"  {ns}")
        if len(name_status) > max_files:
            lines.append(f"  ... ({len(name_status) - max_files} more files)")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks).strip()


def evidence_for_candidate(repo: str, candidate: dict) -> str:
    """Evidence for one analyze.py candidate dict (uses its `commits` SHAs)."""
    return build_evidence(repo, candidate.get("commits", []))


# --- citation verification (deterministic, tier 1) ---------------------------
#
# The strongest guard against fabricated "Considered Options" is to make the
# generator CITE each option inline, then check the citation against git. The
# git history is a closed corpus, so most checks are exact lookups, not model
# calls. A fabricated SHA fails `git cat-file`; a claimed deletion that didn't
# happen fails the name-status check. Only genuinely fuzzy citations (a rename
# that may or may not be an alternative, replaced code inside a modified file)
# fall through to the LLM residue judge (judge_options).
#
# Citation grammar, embedded as an HTML comment after the option (invisible when
# the markdown is rendered):
#   * **Redis** <!-- evidence: 3f4a2bc deleted:src/cache/redis_client.py -->
#   * RabbitMQ   <!-- evidence: a1b2c3d message:instead of rabbitmq -->

CITATION_RE = re.compile(
    r"<!--\s*evidence:\s*(?P<sha>[0-9a-fA-F]{7,40})\s+"
    r"(?P<etype>[a-z-]+):(?P<detail>.+?)\s*-->",
    re.I,
)
# evidence types verifiable by an exact git lookup; anything else is fuzzy
DETERMINISTIC_TYPES = {"deleted", "added", "renamed", "message"}


@dataclass
class Citation:
    sha: str
    etype: str
    detail: str


@dataclass
class CitedOption:
    option: str               # human-visible option text (comment stripped)
    citation: Optional[Citation]


@dataclass
class CitationVerdict:
    option: str
    status: str               # verified | failed | fuzzy | uncited
    reason: str = ""


def extract_cited_options(adr_text: str) -> list[CitedOption]:
    """Parse '## Considered Options' bullets into (visible text, citation?)."""
    m = OPTIONS_HEADER_RE.search(adr_text)
    if not m:
        return []
    rest = adr_text[m.end():]
    nxt = NEXT_HEADER_RE.search(rest)
    section = rest[: nxt.start()] if nxt else rest
    if NO_ALT_RE.search(section):
        return []
    out: list[CitedOption] = []
    for raw in LIST_ITEM_RE.findall(section):
        cite_m = CITATION_RE.search(raw)
        citation = None
        if cite_m:
            citation = Citation(cite_m.group("sha"),
                                cite_m.group("etype").lower(),
                                cite_m.group("detail").strip())
        # strip the comment and common markdown emphasis from the visible text
        text = CITATION_RE.sub("", raw).strip().strip("*_ ").strip()
        out.append(CitedOption(option=text, citation=citation))
    return out


def _name_status(repo: str, sha: str) -> list[tuple[str, list[str]]]:
    """[(status_letter, [paths])] for a commit, status normalized (R100 -> R)."""
    raw = analyze.git(repo, "show", "--name-status", "--format=", sha)
    rows: list[tuple[str, list[str]]] = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        rows.append((parts[0][0].upper(), [p for p in parts[1:] if p]))
    return rows


def verify_citation(repo: str, c: Citation) -> tuple[str, str]:
    """Deterministically check one citation. Returns (status, reason).

    status: verified | failed | fuzzy. Non-deterministic evidence types are
    reported as fuzzy for the LLM residue judge rather than guessed at here.
    """
    try:
        kind = analyze.git(repo, "cat-file", "-t", c.sha).strip()
    except RuntimeError:
        return "failed", f"commit {c.sha} not found"
    if kind != "commit":
        return "failed", f"{c.sha} is not a commit"

    if c.etype not in DETERMINISTIC_TYPES:
        return "fuzzy", f"'{c.etype}' needs semantic review"

    if c.etype == "message":
        body = analyze.git(repo, "show", "-s", "--format=%B", c.sha)
        if c.detail.lower() in body.lower():
            return "verified", "phrase present in commit message"
        return "failed", "phrase not in commit message"

    want = c.detail
    for status, paths in _name_status(repo, c.sha):
        if c.etype == "deleted" and status == "D" and want in paths:
            return "verified", f"{want} deleted in {c.sha}"
        if c.etype == "added" and status == "A" and want in paths:
            return "verified", f"{want} added in {c.sha}"
        if c.etype == "renamed" and status == "R" and want in paths:
            return "verified", f"{want} renamed in {c.sha}"
    return "failed", f"no {c.etype} of {want} in {c.sha}"


def verify_options(repo: str, adr_text: str) -> dict:
    """Tier-1 deterministic pass over an ADR's cited options.

    overall: pass (all verified / nothing to check), fail (any failed or
    uncited), or review (some fuzzy, none failed) -> hand fuzzy ones to the
    LLM residue judge.
    """
    cited = extract_cited_options(adr_text)
    verdicts: list[CitationVerdict] = []
    for co in cited:
        if co.citation is None:
            verdicts.append(CitationVerdict(co.option, "uncited",
                                            "no inline evidence citation"))
            continue
        status, reason = verify_citation(repo, co.citation)
        verdicts.append(CitationVerdict(co.option, status, reason))

    statuses = {v.status for v in verdicts}
    if "failed" in statuses or "uncited" in statuses:
        overall = "fail"
    elif "fuzzy" in statuses:
        overall = "review"
    else:
        overall = "pass"
    return {
        "overall": overall,
        "verdicts": [asdict(v) for v in verdicts],
        "fuzzy": [v.option for v in verdicts if v.status == "fuzzy"],
    }


# --- options judge (LLM, tier 2 residue) -------------------------------------

OPTIONS_HEADER_RE = re.compile(r"^##\s+Considered Options\s*$", re.I | re.M)
NEXT_HEADER_RE = re.compile(r"^##\s+", re.M)
NO_ALT_RE = re.compile(r"no alternatives recorded", re.I)
LIST_ITEM_RE = re.compile(r"^\s*[-*]\s+(.+?)\s*$", re.M)


def extract_options(adr_text: str) -> list[str]:
    """Pull the bullet items from the '## Considered Options' section of a MADR."""
    m = OPTIONS_HEADER_RE.search(adr_text)
    if not m:
        return []
    rest = adr_text[m.end():]
    nxt = NEXT_HEADER_RE.search(rest)
    section = rest[: nxt.start()] if nxt else rest
    if NO_ALT_RE.search(section):
        return []
    return [item.strip() for item in LIST_ITEM_RE.findall(section)]


JUDGE_INSTRUCTIONS = """\
You are auditing one section of a retroactively-generated Architectural Decision \
Record for hallucination. An "option" in "Considered Options" is only legitimate \
if it is EVIDENCED by the supplied commit evidence, meaning at least one of:
  (a) code/config being replaced or deleted in the diff (the old approach),
  (b) an explicit "instead of X" / "replacing Y" / "migrate from Z" in a message,
  (c) a branch or PR name referencing an alternative approach.
General knowledge that an alternative "exists" is NOT evidence.

For each option, decide "evidenced" (point to the specific evidence) or \
"unevidenced". Respond with ONLY a JSON object, no prose:
{"options": [{"option": "<verbatim>", "verdict": "evidenced"|"unevidenced", \
"evidence": "<pointer or empty>"}], "overall": "pass"|"fail"}
"overall" is "fail" if ANY option is unevidenced.
"""


def build_options_judge_prompt(options: list[str], evidence: str) -> str:
    listed = "\n".join(f"{i+1}. {o}" for i, o in enumerate(options))
    return (
        f"{JUDGE_INSTRUCTIONS}\n"
        f"=== COMMIT EVIDENCE ===\n{evidence.strip()}\n\n"
        f"=== OPTIONS TO AUDIT ===\n{listed}\n"
    )


@dataclass
class OptionVerdict:
    option: str
    verdict: str  # evidenced | unevidenced
    evidence: str = ""


@dataclass
class JudgeVerdict:
    overall: str  # pass | fail
    options: list[OptionVerdict] = field(default_factory=list)
    runs: int = 1
    agreement: float = 1.0  # fraction of runs that agreed on the winning overall
    raw_runs: list[str] = field(default_factory=list)


def parse_verdict(raw: str) -> Optional[JudgeVerdict]:
    """Parse one judge response. Tolerant of code fences / surrounding prose.

    Returns None if no JSON object can be recovered (treated as an abstaining run).
    """
    text = raw.strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        obj = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    opts = [
        OptionVerdict(
            option=str(o.get("option", "")),
            verdict=("evidenced" if o.get("verdict") == "evidenced" else "unevidenced"),
            evidence=str(o.get("evidence", "")),
        )
        for o in obj.get("options", [])
        if isinstance(o, dict)
    ]
    overall = obj.get("overall")
    if overall not in ("pass", "fail"):
        overall = "fail" if any(o.verdict == "unevidenced" for o in opts) else "pass"
    return JudgeVerdict(overall=overall, options=opts)


# A verdict task does not need a frontier model; a small fast model keeps the
# N-run variance loop cheap. Overridable via the JUDGE_MODEL env var.
DEFAULT_JUDGE_MODEL = os.environ.get("JUDGE_MODEL", "claude-haiku-4-5-20251001")


def make_default_runner(model: str = DEFAULT_JUDGE_MODEL,
                        timeout: int = 180) -> Callable[[str], str]:
    """Build a runner that shells out to `claude -p`. Isolated so tests stub it."""
    def run(prompt: str) -> str:
        res = subprocess.run(
            ["claude", "-p", prompt, "--model", model],
            capture_output=True, text=True, timeout=timeout,
        )
        if res.returncode != 0:
            raise RuntimeError(f"claude -p failed: {res.stderr.strip()}")
        return res.stdout
    return run


default_runner = make_default_runner()


def judge_options(
    options: list[str],
    evidence: str,
    runner: Callable[[str], str] = default_runner,
    runs: int = 3,
) -> JudgeVerdict:
    """Run the options judge `runs` times and take the majority overall verdict.

    agreement = winning-vote fraction, the variance signal the previous design
    asked for. An empty options list passes trivially (the safe MADR default of
    'No alternatives recorded' needs no judging).
    """
    if not options:
        return JudgeVerdict(overall="pass", runs=0, agreement=1.0)

    prompt = build_options_judge_prompt(options, evidence)
    parsed: list[JudgeVerdict] = []
    raw_runs: list[str] = []
    for _ in range(max(1, runs)):
        raw = runner(prompt)
        raw_runs.append(raw)
        v = parse_verdict(raw)
        if v is not None:
            parsed.append(v)

    if not parsed:
        # every run abstained/garbled — fail closed (safer than passing blind)
        return JudgeVerdict(overall="fail", runs=runs, agreement=0.0,
                            raw_runs=raw_runs)

    tally = Counter(v.overall for v in parsed)
    overall, votes = tally.most_common(1)[0]
    winner = next(v for v in parsed if v.overall == overall)
    winner.runs = runs
    winner.agreement = votes / len(parsed)
    winner.raw_runs = raw_runs
    return winner


# --- CLI ---------------------------------------------------------------------

def _cmd_check_tags(args) -> int:
    report = check_tags(args.repo, args.files)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        for r in report["results"]:
            print(f"  [{r['status']:7}] {r['path']}"
                  + (f"  — {r['detail']}" if r["detail"] else ""))
        print(f"{'OK' if report['ok'] else 'FAIL'}: "
              f"{report['failed']}/{report['checked']} files broke parsing")
    return 0 if report["ok"] else 1


def _split_shas(spec: Optional[str]) -> list[str]:
    if not spec:
        return []
    return [s for s in re.split(r"[,\s]+", spec.strip()) if s]


def _cmd_evidence(args) -> int:
    print(build_evidence(args.repo, _split_shas(args.commits)))
    return 0


def _cmd_verify_options(args) -> int:
    adr_text = open(args.adr).read()
    report = verify_options(args.repo, adr_text)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(f"overall: {report['overall']}")
        for v in report["verdicts"]:
            print(f"  [{v['status']:8}] {v['option']}"
                  + (f"  — {v['reason']}" if v["reason"] else ""))
    return 0 if report["overall"] == "pass" else 1


def _cmd_judge_options(args) -> int:
    adr_text = open(args.adr).read()
    options = extract_options(adr_text)
    if args.commits:
        evidence = build_evidence(args.repo, _split_shas(args.commits))
    elif args.evidence:
        evidence = open(args.evidence).read()
    else:
        evidence = ""
    verdict = judge_options(options, evidence, runs=args.runs)
    out = asdict(verdict)
    out.pop("raw_runs", None)
    if args.json:
        print(json.dumps(out, indent=2))
    else:
        print(f"overall: {verdict.overall}  runs: {verdict.runs}  "
              f"agreement: {verdict.agreement:.2f}")
        for o in verdict.options:
            print(f"  [{o.verdict:11}] {o.option}"
                  + (f"  ⟵ {o.evidence}" if o.evidence else ""))
    return 0 if verdict.overall == "pass" else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    ct = sub.add_parser("check-tags", help="parse-check tag-annotated files")
    ct.add_argument("repo")
    ct.add_argument("files", nargs="+")
    ct.add_argument("--json", action="store_true")
    ct.set_defaults(func=_cmd_check_tags)

    ev = sub.add_parser("evidence", help="build a judge evidence block from commit SHAs")
    ev.add_argument("repo")
    ev.add_argument("--commits", required=True, help="comma/space-separated SHAs")
    ev.set_defaults(func=_cmd_evidence)

    vo = sub.add_parser("verify-options",
                        help="tier-1 deterministic check of inline option citations")
    vo.add_argument("adr")
    vo.add_argument("--repo", default=".")
    vo.add_argument("--json", action="store_true")
    vo.set_defaults(func=_cmd_verify_options)

    jo = sub.add_parser("judge-options", help="audit Considered Options for hallucination")
    jo.add_argument("adr")
    jo.add_argument("--repo", default=".", help="repo for --commits evidence")
    jo.add_argument("--commits", default=None,
                    help="comma/space-separated SHAs; builds evidence automatically")
    jo.add_argument("--evidence", default=None,
                    help="path to a pre-built evidence text file (ignored if --commits given)")
    jo.add_argument("--runs", type=int, default=3)
    jo.add_argument("--json", action="store_true")
    jo.set_defaults(func=_cmd_judge_options)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
