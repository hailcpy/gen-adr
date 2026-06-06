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
 
### Output Location (scope-aware)
 
Where ADRs are written depends on code scope. Do NOT dump module ADRs into the global pool — co-locate them with the module so they stay discoverable and numbering does not collide across modules.
 
| Code scope | Output directory | Numbering |
|------------|------------------|-----------|
| `repo` | `docs/decisions/` | continues from highest existing global ADR |
| `module:<path>` | `<path>/docs/decisions/` (co-located) | numbered independently per module, continues from highest existing ADR in that folder |
 
**Boundary-crossing decisions:** when a module-scoped decision also touches shared paths (`lib/`, `core/`, etc.), write the ADR in the module's folder, but place the `@ADR` tag in the shared file too — pointing back across the boundary to the module's decision file. This keeps the single source of truth in the module while making the decision discoverable from the shared code.
 
Throughout the rest of this skill, `docs/decisions/` refers to the **scope-resolved** output directory from this table, not necessarily the repo root.
 
---
 
## Core Loop
 
```
0. PREFLIGHT  → validate repo state before doing anything
1. DETECT     → chunking strategy (merge / squash / rebase / direct)
2. CHUNK      → group commits within boundaries into logical units
2.5 CLUSTER   → merge related chunks ACROSS boundaries into decision candidates
3. CLASSIFY   → filter architectural decisions from noise
4. GENERATE   → produce MADR per decision candidate (batch-aware)
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
 
## Step 2.5: Cluster Across Boundaries
 
**Do not assume one PR/merge = one decision.** A single architectural decision often lands across several PRs over weeks, interleaved with unrelated work (e.g. an event-driven migration spread across PR #210, #230, #255). One ADR per boundary shatters that into three weak, redundant records. Step 2 groups *within* a boundary; this step merges *across* boundaries into **decision candidates**.
 
This pass is **deterministic by design** — no embeddings, no temporal proximity. It uses signals already extracted in Step 2 so that re-runs are reproducible (which is what makes the skill eval-able and regression-testable).
 
**Merge two chunks (even non-adjacent ones) into one candidate when BOTH hold:**
1. **File overlap** — they touch an overlapping set of files or share ≥2 top-level directories, OR one chunk adds a file/symbol the other modifies.
2. **Topic affinity** — their commit/PR subjects share a distinctive token (a library name, subsystem, feature flag, or migration name like `kafka`, `grpc`, `hexagonal`) — not generic words like `fix`, `add`, `update`.
 
**Guards:**
- A merged candidate may span non-sequential PRs/commits — that is the point. Order its links by author date.
- Cap a candidate at ~8 source chunks; if larger, it is probably several decisions — keep the strongest file-overlap cluster and split the rest.
- Do NOT merge on topic affinity alone (two unrelated `cache` PRs) or file overlap alone (everyone edits `config.ts`). Require both.
- A chunk that clusters with nothing stays a singleton candidate — that is fine.
 
The output of this step is a list of **decision candidates**, each backed by one or more chunks. Steps 3–5 operate on candidates, not raw chunks.
 
**Mega-cluster guard (small / hot-file repos).** Clustering is connected-components over a pairwise-affinity graph, so even *sparse* overlap collapses into one giant cluster once a "hot" file or directory (touched by a large fraction of commits) or a recurring word binds otherwise-unrelated commits — relatedness is not transitive. Watch for it:
- A candidate carrying a `WARN: oversized cluster` signal, **or** one candidate that swallows more than ~25% of all commits → the affinity graph has collapsed.
- Fix deterministically by re-running `analyze.py --config knobs.json` with **edge specificity** caps so only *rare* shared files/tokens/dirs count as affinity (IDF intuition: a file edited in 25% of commits proves nothing about two commits being one decision):
  ```json
  { "file_df_max": 4, "token_df_max": 4, "dir_df_max": 4 }
  ```
  Start at ~`max(3, commits/40)` for each cap and tighten until the largest cluster is a believable single decision. These default to `null` (off) so the eval baseline is unchanged.
- Do NOT reach for embeddings/LLM here: that addresses the opposite problem (merging commits that share *no* files/words — see the v2 note), would cluster on the same low-signal commit messages, and discards the deterministic, regression-testable guarantee.
 
> **v2 note (not implemented here):** semantic clustering via embeddings + graph community detection would also catch conceptually-related PRs that share no files. It is deliberately out of scope: it is non-deterministic, needs a vector store, and would make output impossible to regression-test. Prototype it offline and prove it beats this deterministic pass before adopting it.
 
In **assisted mode**, show the proposed candidates (which chunks merged into each, and why) and let the user split/merge before classifying.
 
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
 
### Hard gate: diff before draft
 
**Before writing ANY MADR, you MUST have loaded and read the chunk's actual diff** (`git diff $BASE $COMMIT -- [module-path]`). The commit subject or PR/merge title is NOT sufficient — it may seed the title only. This is the most common failure mode: for merge/squash commits the subject line is right there ("Merge PR #214: add caching") and it is tempting to write the ADR straight from it. Do not. If you have not read the diff for this chunk, go back to Step 2 and load it before generating.
 
**Thin-message rule (mirror of the Considered-Options rule):** when the commit/PR message is uninformative (`wip`, `fix`, `Merge #214`, empty), the Context, Decision, and Outcome MUST be reconstructed from the **diff** — new files, deleted code, dependency/schema changes — not paraphrased from the subject line. If the diff is *also* uninformative, say so explicitly ("Implementation inferred from diff; no clear rationale recoverable") rather than restating the PR title as if it were a decision.
 
### Numbering and deduplication
 
```bash
# Get next available number
ls docs/decisions/ 2>/dev/null | grep -E "^[0-9]{4}-" | sort | tail -1
```
 
Number sequentially from the next available. Order by **author date** (`--format="%ad"`), not committer date (`%cd`), since rebase/cherry-pick rewrites committer timestamps while preserving original author dates.
 
### MADR Template
 
**Title rule:** the title states the *decision*, not the PR. `Merge #214` or `add caching` is a PR title; `Adopt Redis for session cache` is a decision title. Derive it from what the diff actually did, not from the commit subject.
 
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
 
<STRICT RULE: Only list options that are directly evidenced, and CITE each one
inline with a hidden HTML-comment so the verifier can check it against git:
 
  - `* <option> <!-- evidence: <sha> deleted:<path> -->`     (whole file for the old approach removed)
  - `* <option> <!-- evidence: <sha> added:<path> -->`       (new approach added)
  - `* <option> <!-- evidence: <sha> renamed:<path> -->`     (file moved/renamed)
  - `* <option> <!-- evidence: <sha> removed:"<line>" -->`   (a specific deleted line — for code replaced *inside* a modified file)
  - `* <option> <!-- evidence: <sha> message:<phrase> -->`   (phrase in commit msg, e.g. "instead of redis")
 
The `<sha>` MUST be one of this candidate's commits, and the cited change must be
real — the verifier checks each citation against git and DROPS any option whose
citation does not hold up. Prefer `removed:"<exact deleted line>"` over a vague
claim: if an alternative is only evidenced by code changed inside a file, cite the
actual line that was deleted. Every option needs a citation — an option you cannot
pin to a specific commit and change does not belong here. If none are evidenced,
write exactly "No alternatives recorded in commit history." and leave the list
empty. DO NOT populate this with plausible alternatives from general knowledge.>
 
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
 
### Verify Considered Options (deterministic citation check)
 
After writing an ADR, run the verifier. It checks every option's inline citation against git and rewrites the section to keep only what holds up. **No model is involved — same repo + text always yields the same result:**
 
```bash
python3 scripts/judge.py verify-adr docs/decisions/<NNNN-slug>.md \
    --repo . --write --json
```
 
For each option's citation it confirms the SHA exists, then confirms the claimed change is real: `deleted`/`added`/`renamed` against name-status, `removed:"line"` by grepping the commit's diff for that deleted line, `message:` by searching the commit message. An option is **dropped** if its citation fails, if its evidence type is not one of these (uncheckable), or if it has **no citation at all**. Because the git history is a closed corpus, every check is an exact lookup — a fabricated SHA fails `git cat-file` immediately.
 
With `--write`, the verifier rewrites the section (dropping unevidenced options, collapsing to "No alternatives recorded" if none survive) and stamps the frontmatter (see below). Run without `--write` to preview the kept/dropped table first; in **assisted mode**, preview, then apply.
 
> An option that can only be argued semantically — not pinned to a specific commit and change — is dropped, not adjudicated by an LLM. For a retroactive ADR that is the correct call: if you cannot point at the bytes, it should not be asserted as a considered option.
 
### Provenance and the downstream trust boundary
 
`verify-adr --write` stamps the ADR's YAML frontmatter:
 
```yaml
generation:
  method: gen-adr/v1
  evidence-verified: true
  verification: citation-structural
  options-kept: 2
  options-dropped: 1
```
 
The per-option `<!-- evidence: ... -->` comments are kept (invisible when rendered) as an audit trail. **This is a trust boundary, not an invitation to re-check.** A downstream coding agent reading an ADR with `evidence-verified: true` should **trust the Considered Options prose as already cross-checked against git, and must NOT re-investigate the cited commits.** The citations exist for a human auditor or a re-run of `verify-adr`, not for the consuming agent to chase. Treat the listed options as the verified historical decision space.
 
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
| JSON | **SKIP — no comment syntax** | |
| YAML/TOML | **SKIP — supports `#` but fragile; formatters/tools may strip inline comments. Tag the code that reads the config instead.** | |
 
In **assisted mode**, show proposed placements with file + line before writing:
```
ADR-0004 tag placements:
  src/events/processor.py:42  → process_events()
  src/events/queue.py:10      → class EventQueue
Confirm? [yes / adjust / skip tagging]
```
 
### Verify tag syntax (after placement)
 
A misplaced comment can break a file. After writing tags, parse-check every file you touched:
 
```bash
python3 scripts/judge.py check-tags . <file1> <file2> ... --json
```
 
- **`ok`** → the tag parsed cleanly, keep it.
- **`fail`** → the tag broke the file. Revert that one insertion (or move it to a safer line — e.g. above the `def`/`func` rather than inside a signature) and re-check.
- **`skipped`** → no syntax checker for that language, or the tool isn't installed. The tag is kept un-verified; note it in the run summary.
 
**Syntax-check coverage is a fixed set:** Python, JavaScript (`.js`/`.mjs`/`.cjs`), Ruby, Shell (`.sh`/`.bash`), and Go (needs `gofmt` on `PATH`). TypeScript, Java/Kotlin, and Rust get tags placed but are **not** parse-checked yet — they return `skipped`. For those, eyeball the insertion point.
 
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