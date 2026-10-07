# Eval candidates

Files here are written automatically (as PRs labelled `eval-candidate`) when a
reviewer **edits** an agent-generated dashboard PR before merging, or **closes** one
without merging. Each file holds:

- `request` — the original plain-English request
- `extracted` — what the LLM extracted
- `corrected` — the reviewer's version (from an edited `agent-spec` block in the PR
  description, or inferred from the merged dashboard JSON); `null` if the PR was
  closed without a fix
- `diff`, `notes`, `source` (PR link), `lineage` (prompt, model, Langfuse trace)

## Promoting a candidate

1. Check out the candidate PR's branch and confirm (or fill in) `corrected`.
2. `python -m evals.promote_candidate evals/candidates/<file>.json`
   — validates the spec, appends it to `evals/testset.jsonl`, deletes the candidate.
3. Commit + push. The eval gate runs on the PR, now including the new case.

If the candidate isn't a real extraction mistake (e.g. the reviewer just changed
their mind), close the PR instead.

`python -m evals.promote_candidate --list` shows everything pending.
