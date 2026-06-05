# Eval harness — adr-generator

Splits the skill into two evaluable halves:

1. **Analytical pipeline** (`scripts/analyze.py`) — deterministic: preflight →
   detect → chunk → cluster → classify → JSON candidate manifest. Tested here
   with plain pytest, no Claude, no network.
2. **Prose + tagging** (the agent) — irreducibly judgmental. Evaluated by
   LLM-judge against the same fixtures. **Not yet built** — see "Next" below.

## Run

```bash
python3 -m pytest          # builds fixtures automatically, runs the suite
```

Fixtures are synthetic git repos with *planted* decisions, built by
`fixtures/make_fixtures.sh` (deterministic — fixed author dates). The answer key
is `fixtures/labels.json`.

Inspect the pipeline on a fixture directly:

```bash
python3 scripts/analyze.py tests/fixtures/synthetic/repo_squash
python3 scripts/analyze.py tests/fixtures/synthetic/repo_modules --code module:src/payments --json
```

## What is covered (deterministic)

| Eval | Test | Gate |
|------|------|------|
| Strategy detection | `test_strategy_detection` | exact match |
| **Cross-PR clustering** | `test_cross_pr_clustering` | 3 non-adjacent PRs → 1 candidate |
| Affinity clustering (linear) | `test_linear_affinity_clustering` | shared dir+token → 1 candidate |
| No over-clustering | `test_no_overclustering` | noise stays out |
| Classification P/R | `test_classification_precision_recall` | **precision == 1.0**, recall ≥ 0.8 |
| Noise excluded | `test_noise_is_skipped` | typo/bump/test → skip |
| Evidenced swap flagged | `test_evidenced_swap_is_architectural` | moment→date-fns architectural |
| Shallow halt | `test_shallow_clone_halts` | halt = shallow_clone |
| Scope-aware paths | `test_module_scope_output_paths` | module → `<path>/docs/decisions` |
| Module isolation | `test_module_scope_isolates_commits` | other module excluded |

**Precision is the gate, not recall** — a fabricated ADR is worse than a missed
one, so the suite requires zero false positives and tolerates some misses.

## Documented limitation (xfail)

`test_crossmodule_clustering_is_a_known_gap` is a **strict xfail**: a decision
spread across modules that share no files (only a concept, e.g. "grpc") cannot
be merged by deterministic clustering. This is the v2-embeddings case. When a
semantic clusterer is added, this test flips to pass — and the strict marker
will then fail loudly, forcing us to promote it to a real assertion.

## Next (LLM-judge pass — not yet built)

Two checks that protect credibility, run via headless `claude -p` against the
same fixtures:

1. **Syntax-safety after tagging** — repo must still parse/compile and tests
   pass after `@ADR` tags are placed. Fully automatable, must be 100%.
2. **Anti-hallucination on "Considered Options"** — judge model verifies every
   listed alternative is evidenced in the diff/commit. Adversarial fixtures with
   empty/garbage commit messages must yield "No alternatives recorded", not a
   fabrication.

These need N-run variance reporting (LLM output is non-deterministic), unlike
the deterministic suite above which is single-run.
