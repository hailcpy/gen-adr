<p align="center">
  <img src="assets/logo.png?v=2" alt="adr-generator" width="600"/>
</p>

![test](https://github.com/hailcpy/gen-adr/actions/workflows/test.yml/badge.svg)

# adr-generator

A skill for AI coding agents (Claude) that retroactively generates [MADR](https://adr.github.io/madr/)-format Architectural Decision Records from an existing codebase's git history, and annotates relevant code with inline `@ADR` reference tags.

---

## The Problem

Coding agents work great on greenfield projects — give them a spec and they build with context. But on existing codebases, they're flying blind. They see *what* the code does, but not *why* it looks the way it does. Why is Redis used here instead of Postgres? Why is this service async? Why did the team move away from REST?

That context lives in the heads of engineers who wrote the code, or buried in PRs nobody reads. New developers ramp up slowly. Agents make decisions that contradict ones already made.

ADRs solve this — but nobody writes them retroactively by hand for a three-year-old codebase.

This skill does it automatically.

---

## What It Does

1. **Reads your git history** — commit by commit, PR by PR
2. **Detects what kind of history you have** — merge commits, squash merges, rebase/linear, or mixed
3. **Groups commits into logical decision chunks** — not just by time, but by what subsystems they touched and what they changed — then clusters related chunks *across* PR boundaries, so one decision spread over several PRs becomes a single ADR instead of three redundant ones
4. **Classifies which chunks represent architectural decisions** — filtering out bug fixes, lint, version bumps, and test-only changes using diff signals, not just commit messages
5. **Generates a MADR file per decision** — with context, considered options (only what's evidenced in the diff), outcome, and links back to the original commits
6. **Places `@ADR` inline tags** on the relevant functions and classes — so a coding agent grepping the codebase finds the decision log right at the code it affected

---

## Output

### MADR files in `docs/decisions/`

```
docs/decisions/
├── 0001-redis-cache-strategy.md
├── 0002-async-event-queue.md
├── 0003-grpc-inter-service-transport.md
└── ...
```

Each file follows the [MADR template](https://adr.github.io/madr/) with a retroactive notice and links to original commits.

Pass `--json` to `analyze.py` to get machine-readable candidates — each entry includes `score`, `classification`, and a `diff_summary` (files added/deleted/modified) so the agent can rank and triage without running `git diff` manually. Edge specificity auto-tunes from commit count by default, so clustering is sane out-of-box with no extra config.

### Inline `@ADR` tags in code

```python
# @ADR-0002-async-event-queue: switched from sync to async event processing — see docs/decisions/0002-async-event-queue.md
async def process_events(self, batch: list[Event]) -> None:
```

```go
// @ADR-0003-grpc-transport: gRPC adopted over REST for inter-service comms — see docs/decisions/0003-grpc-inter-service-transport.md
func NewServiceClient(addr string) (*ServiceClient, error) {
```

A coding agent can now `grep -r "@ADR"` and surface every architectural decision site in the codebase.

### Workaround logs

Not every significant change is a decision — some are **workarounds**: monkey patches, shims, vendored patches, pins "until upstream fixes it". `analyze.py` detects these from diff content (HACK/WORKAROUND/FIXME-until markers, shim/adapter/polyfill files, `patches/`) and routes them to a different record type with the fields MADR lacks: **Trigger** (the upstream bug) and **Removal Condition** (when it can be deleted).

```
docs/decisions/
├── 0006-grpc-inter-service-transport.md      ← decision (MADR)
└── 0007-wa-aws-multipart-metadata.md         ← workaround log (type: workaround)
```

Workaround sites get a distinct tag prefix, so live workarounds are separately greppable:

```typescript
// @WA-0007-aws-multipart-metadata: manual metadata copy around aws-sdk bug — see docs/decisions/0007-wa-aws-multipart-metadata.md
```

The same citation verifier covers them (`marker:"<added line>"` evidence pinned to the introducing commit), and `judge.py check-workarounds` turns the log into a standing cleanup queue: it flags active records whose marker line has disappeared from the working tree ("possibly removed — update status") and, with `--check-upstream`, linked upstream issues that have since closed ("removal condition may be met").

---

## Modes

### Quick (default for repos under ~500 commits)

For small/medium repos the full multi-step loop is overkill. The agent runs `analyze.py` once as a scored classifier, reads its JSON output (each candidate carries `score`, `classification`, and a `diff_summary`), triages without calling `git diff` by hand, writes MADRs directly, then runs `judge.py` as the safety net.

```
"Run adr-generator on this repo"
```

### Autonomous
The agent detects everything and runs the full chunking → clustering → classification → generation → verification loop without interruption. Best for large repos (≈500+ commits) or histories with many interleaved decisions across PR boundaries.

```
"Run adr-generator on this repo in autonomous mode, scope to src/payments since v2.0.0"
```

### Assisted
The agent pauses at each major step — chunking, classification, draft review, tag placement — and waits for your confirmation before proceeding. Best when you care about quality over speed, or when commit history is messy.

```
"Run adr-generator in assisted mode on the full repo"
```

---

## Scope Options

**History scope**

| Option | What it does |
|--------|-------------|
| `full` | Entire git history |
| `since:<commit-or-tag>` | From a specific commit or tag onward (e.g. `since:v2.0.0`) |

**Code scope**

| Option | What it does |
|--------|-------------|
| `repo` | Entire repository |
| `module:<path>` | Specific folder only (e.g. `module:src/payments`) |

Any combination works. For large repos or monorepos, `module:` + `since:` is the recommended starting point.

---

## When to Use This

**Good fit:**
- Onboarding new engineers or agents to an existing codebase
- Codebase has meaningful git history with decent commit messages
- You want to run a coding agent on legacy code without it undoing past decisions
- Modules with clear boundaries (payments, auth, infra) that you want to document independently
- Repos using squash-merge or merge-commit workflows (clear PR boundaries to chunk from — though a single decision may still span several PRs, which the clustering pass bundles back together)

**Not a good fit:**
- Shallow clones — the skill will stop and tell you to unshallow first
- Repos where every commit is `"fix"` or `"update"` with no diff discipline — the output will be low quality
- Repos with >500 commits and no clear versioning — use `since:` to scope it down first
- Codebases where all the decision context lives in Confluence/Notion and never touched the commit history

---

## How It Handles Different Git Workflows

One of the trickier parts of retroactive ADR generation is that repos use git very differently. The skill detects your workflow automatically:

| Workflow | Detection | Chunking |
|----------|-----------|---------|
| Merge commits (classic PR flow) | `git log --merges` | Each merge commit = one chunk |
| Squash merges (GitHub default) | Commits with `(#NNN)` in subject | Each squash commit = one chunk |
| Rebase / linear history | No merges, no PR refs | Group by file-path affinity |
| Mixed | Both patterns present | Merge-boundary where possible, squash elsewhere |

Note: temporal proximity (grouping commits by time) is explicitly disabled for rebase workflows, since rebasing destroys original timestamps.

---

## Hallucination Guardrails

The biggest risk with retroactive documentation is the AI confidently making things up. The skill has explicit rules to prevent this:

- **Considered Options** only lists alternatives that are directly evidenced in the diff (code being replaced) or the commit message ("switched from X to Y"). If none are found, it writes `"No alternatives recorded in commit history."` — not a plausible guess.
- **Rationale** is only drawn from commit messages and diff context. If unclear, the MADR says so explicitly rather than fabricating a reason.
- Every generated MADR includes a header note: `"This ADR was retroactively generated from commit history. Rationale accuracy depends on commit message quality."`

### Output verification

Beyond the in-generation rules above, a verification pass (`scripts/judge.py`) re-checks the output instead of trusting the generator to police itself.

**Considered Options — citation-grounded and fully deterministic.** The generator must *cite* each option inline (a hidden `<!-- evidence: <sha> deleted:path -->` comment). Because the git history is a closed corpus, every check is an exact lookup, not a model call:

- Confirm the cited SHA exists, then confirm the claimed change is real: file `deleted`/`added`/`renamed` (name-status), `removed:"line"` (grep the commit's diff for that deleted line — this covers code replaced *inside* a modified file), or a `message:` phrase. A fabricated SHA or a change that didn't happen → the option is dropped. An uncited option, or one citing an evidence type that can't be checked → dropped.
- There is **no LLM on the verification path**. An option that can only be argued semantically is dropped, not adjudicated — for a retroactive ADR, if you can't point at the bytes, it shouldn't be asserted. (This replaced an earlier LLM-judge design: a model verdict over terse commit metadata measures sample *variance*, not the judge's *systematic* over-inference, so citation grounding is both stronger and reproducible.)

Surviving options are kept; the rest are dropped (collapsing to "No alternatives recorded" if none survive), and the ADR's frontmatter is stamped `evidence-verified: true`. A downstream agent reading that flag should **trust the prose and not re-investigate the cited commits** — the citations are an audit trail, not a re-check prompt.

**Tag syntax check (deterministic).** After `@ADR` tags are inserted, each touched file is parse-checked with the language's own compiler so a misplaced comment can't break the build. No model is involved.

### Tag validation coverage

The deterministic syntax check supports a **fixed set** of languages. Files outside this set still get tags placed, but the syntax check **skips** them (it never false-fails):

| Language | `@ADR` tags placed | Syntax-checked |
|----------|:---:|:---:|
| Python | ✅ | ✅ |
| JavaScript (`.js` / `.mjs` / `.cjs`) | ✅ | ✅ |
| Ruby | ✅ | ✅ |
| Shell (`.sh` / `.bash`) | ✅ | ✅ |
| Go | ✅ | ✅ *(requires `gofmt` on `PATH`)* |
| TypeScript (`.ts` / `.tsx`) | ✅ | ❌ skipped |
| Java / Kotlin | ✅ | ❌ skipped |
| Rust | ✅ | ❌ skipped |

If a checker's tool isn't installed on the host (e.g. `gofmt`, `node`, `ruby`), that file is also skipped rather than failed.

---

## Installation

The skill requires three components to be co-located: the skill definition (`SKILL.md`), the deterministic scripts (`scripts/`), and the reference files (`references/`). Clone the repo and symlink or copy the whole directory into your Claude skills folder:

```bash
git clone https://github.com/hailcpy/gen-adr.git
cp -r gen-adr ~/.claude/skills/adr-generator
```

The resulting layout should be:

```
~/.claude/skills/
└── adr-generator/
    ├── SKILL.md
    ├── scripts/
    │   ├── analyze.py   ← deterministic chunking/clustering/classification
    │   └── judge.py     ← citation verifier and tag syntax checker
    └── references/
        ├── madr-examples.md
        └── language-tag-patterns.md
```

**Requirements:** Python 3 (stdlib only — no `pip install` needed). For Go syntax checking, `gofmt` must be on `PATH`; all other checked languages use built-in tools (`node`, `ruby`, `bash -n`). If a checker tool is absent, that file is skipped rather than failed.

The agent will pick up the skill automatically when you ask it to generate ADRs or document architectural decisions.

---

## Example Prompt

```
Using the adr-generator skill, run in assisted mode on the module src/billing, 
since the tag v1.4.0. I want to review each classification before you generate anything.
```

---

## Troubleshooting: Clustering Mega-Collapse

Clustering is connected-components over a pairwise-affinity graph. A "hot" file or token touched by many commits can bridge otherwise-unrelated commits into one giant cluster.

**Signs something is wrong:**
- A candidate with `WARN: oversized cluster` in its output
- One candidate that absorbs >25% of all commits

**Fix:** re-run with a config file that caps edge specificity so only *rare* shared files/tokens/directories count as affinity:

```bash
python3 scripts/analyze.py <repo> --config knobs.json --json
```

```json
{ "file_df_max": 4, "token_df_max": 4, "dir_df_max": 4 }
```

Start at roughly `max(3, commits/40)` for each cap and tighten until the largest cluster represents a believable single decision. These default to `null` (off), preserving the baseline for repos that don't need it.

---

## Caveats

- **Not a substitute for writing ADRs as you go.** Retroactive ADRs are always an approximation. They're better than nothing, but they'll miss context that lived in Slack threads, meetings, or engineers' heads.
- **Quality scales with commit discipline.** Repos with descriptive commit messages and clean PR history produce much better ADRs than repos full of `"wip"` and `"fix stuff"` commits.
- **The `@ADR` tag format is an extension of MADR**, not part of the official spec. It's designed for grep-ability and agent discoverability, not for standard MADR tooling.

