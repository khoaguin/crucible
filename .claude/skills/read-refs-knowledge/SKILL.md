---
name: read-refs-knowledge
description: Answers questions about Crucible's upstream dependencies (PySyft — syft-enclave, syft-job, syft, datasets/permissions; ScreamingFace — engine, benchmarks, url4, aigateway, screamingface client; inspect_ai; inspect_evals) from the cached notes in .refs-knowledge/, after checking each note is fresh and whether the local tree matches Crucible's pinned version. Use whenever a question, plan, or review in Crucible depends on how one of those upstreams works (attestation, approval, job execution, sandboxing, benchmark registration, url4 config, gateway routing/egress, solver/scorer shape), before opening the upstream repos.
---

# Read refs knowledge

Answer from the cached notes, but only after two checks: the note is current, and it describes
the version the question is about.

## Steps

1. **Find the module.** Read `.refs-knowledge/INDEX.md`. The map shows every module, and the
   table gives each one a summary and its local-vs-pin versions.
2. **Check freshness.** This is read-only, takes about a second and costs no model tokens:
   ```bash
   uv run --no-project python .claude/skills/update-refs-knowledge/scripts/refs_knowledge.py check <module-id>
   ```
   - FRESH: trust the note.
   - STALE: the output lists the changed files and commits. Read the diffs that matter to the
     question (`git -C <repo> diff <since>..<head> -- <file>`) and tell the user the note is behind.
     Offer "update knowledge of <module-id>"; don't run it unasked.
   - NEW: there's no note yet. Read the upstream repo directly.
   - `repo not cloned`: this machine has no clone to check against. Ask the user whether to
     clone it into `.refs-repos/<repo>`, or to point
     `.claude/skills/update-refs-knowledge/repos.local.toml` at a clone they already have. On
     yes, run the `clone` command the error prints, then rerun `check`. On no, answer from the
     note and say its freshness is unchecked.
3. **Read only what the question needs.** Every note uses the same headings: `## Mental model`
   (with its diagram), `## Surface Crucible touches`, `## Defaults & invariants that matter` and
   `## Watch list`. Grep for the heading and read that section.
4. **Match the version to the question.** Notes describe the **local tree**, and INDEX's
   "Local vs pin" column shows when it's ahead of Crucible's pin. A question about what Crucible
   *runs* is about the pinned release. Read the note's `## Pinned vs local` section first; any
   claim tagged **[HEAD only]** doesn't hold for what Crucible runs. The `check` output's
   `pin vs HEAD` line lists exactly which files differ. The pinned source is installed in
   `.venv/lib/python3.12/site-packages/<import>/`, and the import names differ from the dist
   names: `syft_enclaves`, `syft_job`, `syft_datasets`, `syft_perms`, `syft_permissions`, `syft`,
   `screamingface`, `url4`, `aigateway`. When the two versions differ, grep there before stating
   pinned behaviour.
5. **Verify before acting.** Before a claim goes into code, docs, or a ticket, open the
   `path:line` it cites. Line numbers are true at the commit in the note's `source:` line.
6. **Fall back to the repo** (this machine's clone: the path in
   `.claude/skills/update-refs-knowledge/repos.local.toml`, else `.refs-repos/<repo>`)
   only for what the note doesn't cover, and say it came from the tree, not the cache.

## When answering

Cite the note (`.refs-knowledge/pysyft/syft-job.md`) plus the upstream `path:line`, and say which
version the answer holds for, e.g. "local dev @1e14d632a9; the pinned 0.1.40 matches". If the
note contradicts `docs/architecture.md`, report the conflict. Don't edit the doc unless the user
asks.
