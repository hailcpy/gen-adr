---
name: adr-generator
description: Retroactively generate MADR (Markdown Architectural Decision Records) from existing codebases by analyzing git commit history and diffs. Use this skill when the user wants to document architectural decisions for an existing codebase, generate ADRs from git history, add decision tags to code, annotate functions or classes with ADR references, reverse-engineer why a codebase looks the way it does, or onboard new developers by surfacing historical decisions. Trigger when the user mentions "ADR", "architectural decisions", "decision records", "document our codebase decisions", "why was this built this way", or wants to annotate existing code with decision context. Works on full repos or specific modules/folders.
---
 
# ADR Generator
 
Generates MADR-format Architectural Decision Records from an existing codebase's git history, and annotates relevant code with inline `@ADR` reference tags.
 
## Modes
 
### Interaction Mode
Always ask the user which mode they want upfront if not specified:
 
- **Autonomous** — detects everything automatically, generates ADRs and places tags end-to-end with no interruptions
- **Assisted** — pauses at each major step, shows findings, waits for user confirmation before proceeding
### Scope
 
Always clarify scope before starting (or auto-detect in autonomous mode):
 
**History scope:**
- `full` — entire git history
- `since:<commit-or-tag>` — from a specific commit/tag onward (e.g. `since:v2.0.0`)
**Code scope:**
- `repo` — entire repository
- `module:<path>` — specific folder/module only (e.g. `module:src/payments`)
**Note on module scope:** architectural decisions for a module are often partially implemented outside it (e.g. shared event bus, config changes). When scoping to a module path, also scan `-- <module-path>` diffs AND flag any commits that touch both the module path and shared paths (`lib/`, `core/`, `shared/`, `common/`) — include those as candidates even if they partially fall outside scope.
 
---
 
## Core Loop
 
```
0. PREFLIGHT  → validate repo state before doing anything
1. DETECT     → chunking strategy (merge / squash / rebase / direct)
2. CHUNK      → group commits into logical decision units
3. CLASSIFY   → filter architectural decisions from noise
4. GENERATE   → produce MADR per decision (batch-aware)
5. TAG        → place @ADR inline markers in code
```
 
---
 
## Step 0: Preflight Checks
 
Run these before anything else. Surface warnings to the user and halt if critical.
 
```bash
# Check clone depth
git rev-list HEAD --count 2>/dev/null
git rev-parse --is-shallow-repository
```
 
**If shallow clone detected (`--is-shallow-repository` returns `true`):**
- STOP. Tell the user: "This is a shallow clone. History is incomplete — ADRs generated would be misleading. Run `git fetch --unshallow` first, then re-run."
- Do not proceed in autonomous mode. In assisted mode, ask if they want to continue with the caveat that output will be incomplete.
```bash
# Check existing ADRs to avoid duplication
ls docs/decisions/ 2>/dev/null | sort | tail -5
```
 
- Note the highest existing ADR number — new ADRs continue from there
- Note existing ADR slugs — skip any chunk whose diff is already covered by an existing ADR (match by commit hash in the Links section)
```bash
# Estimate repo size
git log --oneline | wc -l
```
 
**If commit count > 500:** warn the user that full history analysis may be very slow and suggest using `since:<tag>` scope instead. In autonomous mode, default to analyzing the last 200 commits unless `full` was explicitly requested.
 
In **assisted mode**, show preflight summary before proceeding:
```
Preflight
─────────
Clone depth:    full (not shallow) ✓
Existing ADRs:  3 found (next: 0004)
Commits in scope: 142
Estimated chunks: ~15-25
Proceed?
```
 
---
 
## Step 1: Detect Chunking Strategy
 
Run all three checks — repos can mix strategies:
 
```bash
# Check for true merge commits
git log --oneline --merges -- [module-path] | wc -l
 
# Check for squash merges (GitHub squash-merge pattern: single commit per PR, often titled with PR number)
git log --oneline --no-merges --pretty=format:"%s" | grep -E "\(#[0-9]+\)" | head -10
 
# Check for linear/rebase history (no merges, no PR refs)
git log --oneline | head -20
```
 
**Strategy selection:**
 
| Condition | Mode |
|-----------|------|
| ≥3 merge commits | **merge-boundary** — use merge commits as chunk boundaries |
| Few/no merges but commits with `(#NNN)` pattern | **squash-boundary** — each squash commit is one chunk |
| Linear history, no PR refs | **direct-commit** — group by file-path affinity + commit topic |
| Mixed | use merge-boundary for the merge portion, squash-boundary for the rest |
 
**Note:** rebase workflows destroy original author timestamps. Do NOT use temporal proximity (48h window) for grouping in linear/rebase repos — it will be wrong. Use file-path affinity only.
 
In **assisted mode**, show detection result and let user override before proceeding.
 
---
 
## Step 2: Chunk Commits
 
### Merge-boundary mode
 
```bash
# List merge commits with their hashes
git log --merges --pretty=format:"%H %s" -- [module-path]
 
# For each merge commit, get the correct diff using merge-base
MERGE_COMMIT=<hash>
PARENT1=$(git log --pretty=format:"%P" -n 1 $MERGE_COMMIT | awk '{print $1}')
PARENT2=$(git log --pretty=format:"%P" -n 1 $MERGE_COMMIT | awk '{print $2}')
BASE=$(git merge-base $PARENT1 $PARENT2)
git diff $BASE $MERGE_COMMIT -- [module-path]
git log $BASE..$MERGE_COMMIT --oneline -- [module-path]
```
 
### Squash-boundary mode
 
```bash
# Each squash commit is one chunk
git log --no-merges --pretty=format:"%H %s" -- [module-path]
# Diff per commit
git diff <hash>^..<hash> -- [module-path]
```
 
### Direct-commit mode
 
```bash
git log --oneline --stat -- [module-path]
```
 
Group commits by **file-path affinity only** (no temporal grouping):
- Extract the top-level directories touched by each commit
- Commits sharing ≥2 top-level dirs AND similar message topics → one chunk
- Keep chunks to max ~10 commits; if a natural group is larger, split by sub-path
**Context window management:** do NOT load all diffs into context at once. Process chunks sequentially — load diff, generate MADR, write file, unload. For repos with many chunks (>20), process in batches of 5 and write to disk between batches.
 
In **assisted mode**, show proposed chunk list (commit count per chunk, not full diffs) and let user merge/split before generating.
 
---
 
## Step 3: Classify Chunks
 
For each chunk, classify using **diff-first** scoring — commit messages are a weak signal, diff content is the primary signal.
 
**Diff signals (check these first):**
 
```bash
# New files added (strong signal)
git diff $BASE $COMMIT --name-status | grep "^A"
 
# Dependency file changes
git diff $BASE $COMMIT -- package.json requirements.txt go.mod Cargo.toml pom.xml build.gradle
 
# Interface/schema changes (files with schema-like names or migrations)
git diff $BASE $COMMIT --name-only | grep -iE "schema|migration|interface|contract|proto|api"
```
 
**Scoring:**
 
```
score = 0
 
# Diff signals (primary)
+3 if new non-test files added in src/lib/core/app paths
+3 if dependency file changed (package.json, go.mod, etc.)
+3 if schema/migration/proto file changed
+2 if >5 files changed spanning ≥2 top-level directories
+2 if an existing file was deleted (replacement pattern)
 
# Message signals (secondary, can boost but not solely qualify)
+1 if message contains: migrate, replace, adopt, switch, introduce, implement
-1 if message starts with: fix, chore, style, bump
-2 if message starts with: test, docs, lint, format
 
# Override rules (applied after score)
FORCE_INCLUDE if: security-related path changed (auth/, certs/, secrets/)
FORCE_INCLUDE if: CI/CD or infra config changed (Dockerfile, k8s/, .github/workflows/)
FORCE_EXCLUDE if: diff is 100% test files only
FORCE_EXCLUDE if: diff is 100% docs/comments only
FORCE_EXCLUDE if: only version numbers changed in dependency files (bump with no structural change)
 
if score >= 3 → ARCHITECTURAL
if score 1-2 → BORDERLINE (show to user in assisted; skip in autonomous)
if score <= 0 → SKIP
```
 
In **assisted mode**, show the full classification table including BORDERLINEs with scores so the user can promote/demote before generating.
 
---
 
## Step 4: Generate MADR
 
Process one chunk at a time. Write each file to disk before moving to the next — do not batch in context.
 
### Numbering and deduplication
 
```bash
# Get next available number
ls docs/decisions/ 2>/dev/null | grep -E "^[0-9]{4}-" | sort | tail -1
```
 
Number sequentially from the next available. Order by commit date (author date, `--format="%ad"`) not commit date, since rebase/cherry-pick skews commit timestamps.
 
### MADR Template
 
```markdown
# <short decision title>
 
## Status
 
Accepted
 
> Note: This ADR was retroactively generated from commit history by adr-generator.
> Rationale accuracy depends on commit message quality. See linked commits to verify.
 
## Context and Problem Statement
 
<What problem or need forced this change? Infer from: what existed before (deleted code in diff),
what broke or was missing, what the surrounding code suggests. Be specific about what changed,
not just what was added.>
 
## Decision Drivers
 
<Only include drivers evidenced by the diff or commit message. If none are explicit, omit this section.>
 
## Considered Options
 
<STRICT RULE: Only list options that are directly evidenced:
  - Code being replaced/deleted in the diff (old approach)
  - Explicit "instead of X" / "replacing Y" in commit message
  - Branch name referencing an alternative approach
If none evidenced: write exactly "No alternatives recorded in commit history." and leave the list empty.
DO NOT populate this with plausible alternatives from general knowledge.>
 
## Decision Outcome
 
Chosen option: **<option>**, because <reason — only from diff/message evidence>.
 
If rationale is not captured: write "Rationale not explicitly recorded. Implementation inferred from diff."
 
### Positive Consequences
 
<Only from explicit signals — e.g. removed code suggests what was painful before, 
added tests suggest what was now guaranteed. Do not speculate.>
 
### Negative Consequences / Trade-offs
 
<Only if evidenced — e.g. increased file count, added complexity visible in diff. 
If none evident, write "No trade-offs recorded.">
 
## Links
 
- Commits: <sha1>, <sha2>, ...
- Merge/PR: <merge commit subject or squash commit title>
- Generated by: adr-generator [autonomous|assisted] on <date>
```
 
### MADR generation discipline
 
The most likely hallucination point is "Considered Options." Enforce this mentally before writing:
 
> "Can I point to a specific line in the diff or commit message that shows this alternative existed? If not, it goes in the empty list with the 'No alternatives recorded' note."
 
In **assisted mode**, show the draft and wait for `[write / edit / skip]` before touching disk.
 
---
 
## Step 5: Place Inline @ADR Tags
 
### Tag format
 
```
@ADR-<NNN>-<slug>: <one-line summary> — see docs/decisions/<NNN>-<slug>.md
```
 
### Placement strategy
 
For each ADR, find the primary entry point(s) by running:
 
```bash
git diff $BASE $COMMIT -- [module-path] | grep -E "^(\+){1}[^+].*(def |class |func |function |interface |type |struct )"
```
 
**Entry point selection rules:**
- Pick the function/class that is the **outermost public interface** of the decision — the thing callers use, not internal helpers
- If the decision is about a data store/infra component, tag the constructor or factory function
- If the decision is cross-cutting (e.g. "adopt hexagonal architecture"), tag the file-level header with a module-scope comment instead of a specific function
- Cap at **3 tags per ADR** (raised from 2 to handle cross-cutting decisions better)
- **Skip tagging** for: JSON/YAML/TOML/config files (no comment syntax), generated files, migration files (tag the migration runner instead), test files
**Language comment syntax:**
 
| Language | Function/method | Class/interface |
|----------|----------------|-----------------|
| Python | `# @ADR-...` | `# @ADR-...` |
| JS/TS | `// @ADR-...` | `/** @ADR-... */` |
| Go | `// @ADR-...` | `// @ADR-...` |
| Java/Kotlin | `// @ADR-...` | `/** @ADR-... */` |
| Rust | `// @ADR-...` | `// @ADR-...` |
| Ruby | `# @ADR-...` | `# @ADR-...` |
| Shell | `# @ADR-...` | n/a |
| YAML/JSON/TOML | **SKIP — no comment syntax** | |
 
In **assisted mode**, show proposed placements with file + line before writing:
```
ADR-0004 tag placements:
  src/events/processor.py:42  → process_events()
  src/events/queue.py:10      → class EventQueue
Confirm? [yes / adjust / skip tagging]
```
 
---
 
## Autonomous Mode Summary
 
Print at the end:
 
```
ADR Generation Complete
───────────────────────
Mode:           Autonomous / squash-boundary
Scope:          src/payments (since v2.1.0)
Clone depth:    full ✓
Commits scoped: 142
Chunks found:   23
  Architectural: 8
  Borderline:    4  (skipped — use assisted mode to review)
  Skipped:       11
ADRs written:   8  (docs/decisions/0004–0011)
Tags placed:    19 markers across 12 files
Skipped tagging: 2 files (JSON — no comment syntax)
```
 
---
 
## Failure Modes to Handle Explicitly
 
| Situation | Action |
|-----------|--------|
| Shallow clone | STOP, tell user to unshallow |
| >500 commits, no scope given | Warn, default to last 200, suggest `since:` |
| Chunk diff > ~50KB | Summarize file list only, note "diff too large to fully analyze" in MADR |
| Commit message is empty or just "." | Rely entirely on diff signals; note in MADR |
| Binary files in diff | Skip binary files, note them in MADR links section |
| docs/decisions/ doesn't exist | Create it silently |
| ADR already exists for these commits | Skip, log "already covered by ADR-NNNN" |
 
---
 
## Reference Files
 
- `references/madr-examples.md` — 2 full worked examples: one with evidenced alternatives, one with none
- `references/language-tag-patterns.md` — comment syntax edge cases (decorators, JSDoc, annotation processors)