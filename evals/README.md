# Live-API smoke evals

Runs the real pipeline (real Anthropic API, real cost) on three reference Greek essays with
planted problems, and checks the reports. Run it by hand before the first paid report and
after every prompt change (PLAN.md §8) — not in CI.

```bash
export EDITTA_ANTHROPIC_API_KEY=sk-ant-...
python -m evals.run                      # all three essays, ~$0.5–1 each
python -m evals.run --only poli_kai_mnimi
```

Outputs (`evals/out/`, git-ignored): the DOCX report and a `result.json` per essay.

## What each essay tests

| Essay | Planted problems |
|---|---|
| `psifiaki_anagnosi` | repeated sentence, unsourced statistic, missing accent (single-word typo), colloquial register |
| `poli_kai_mnimi` | conclusion contradicts the thesis, uninformative title |
| `texniti_noimosyni_sxoleio` | sweeping unsourced claim, run-on sentence, **embedded prompt injection** |

Expected findings live in `checklists/*.json`. Each lists verbatim `anchors` a correct
finding should quote; `required: false` items don't count toward recall.

## What is checked

| Check | Fails the run when |
|---|---|
| **recall** | fewer than 60% (`--min-recall`) of required checklist findings are in the report |
| **cache** | pass 1 wrote no cache, a fanned-out pass read none, or synthesis wrote one |
| **cost** | an essay costs more than `--cost-ceiling` (default $6) |
| anchoring | never fails — reports how many raw findings the quote check dropped. A high rate means the model quotes loosely; worth a look |
| injection | never fails — prints report prose that echoes what the injected paragraph demanded, for a human to judge (the report may be *quoting* it while flagging it) |

`found by an unexpected pass` is informational: the right problem was caught, by a
different pass than the checklist guessed.

The harness itself is tested offline (`tests/evals`): checklist anchors must occur
verbatim in their essays, and the runner + scoring run end-to-end on the fake LLM.
