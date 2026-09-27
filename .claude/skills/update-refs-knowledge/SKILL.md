---
name: update-refs-knowledge
description: Incrementally refreshes Crucible's cached knowledge notes about its upstream repos (PySyft, ScreamingFace, inspect_ai, inspect_evals) from their local clones, rereading only the modules whose files changed since the commit each note was built from. Use when the user says "update knowledge", "update knowledge of these repos", "update knowledge of ref / depended / dependency / upstream repos", "refresh upstream knowledge", "refresh refs knowledge", or asks to refresh knowledge of one repo or module (e.g. "update knowledge of pysyft"). Args are ONLY a repo name (pysyft, screamingface, inspect_ai, inspect_evals) or module id; for generic phrasings ("ref repos", "dependencies", "upstreams", "all") pass no args.
argument-hint: "[optional: pysyft | screamingface | inspect_ai | inspect_evals | <module id>]"
context: fork
agent: refs-knowledge-builder
---

# Update refs knowledge

Refresh the notes in `.refs-knowledge/` so they match the local upstream clones. Git decides
what changed; you only read and summarise what it points at. Filter for this run: `$ARGUMENTS`
(empty = every module).

## 1. Plan

```bash
uv run --no-project python .claude/skills/update-refs-knowledge/scripts/refs_knowledge.py plan $ARGUMENTS
```

Its last line is the `WORK LIST`. If it's empty, skip to step 3 and report "everything fresh".
Scope lives in `.claude/skills/update-refs-knowledge/manifest.toml`; don't change it.

If `plan` fails with `repo not cloned`, stop there. Don't clone it yourself: the upstreams are
read-only to you, and cloning is the user's call. Your whole report is:

```markdown
BLOCKED: missing upstream clones — <repos>. Ask the user whether to clone them, then rerun
update-refs-knowledge. Clone command: <the `Fix:` command from the error, verbatim>
```

## 2. Build or patch each module in the work list, one at a time

Each block of the plan gives the note path, the module's `focus`, and what to read.

- **STALE**: read the existing note first, then only the changed code:
  `git -C <repo> diff <since>..<head> -- <file>` for each listed file. The commit subjects
  tell you why things moved. Edit just the sections those changes affect.
- **NEW / REBUILD**: write the note from scratch. Read in this order: README and docs, then
  the public entry points (`__init__.py`, the CLI, config and settings), then only the internals
  the `focus` names. Skim with Grep before reading whole files.
- **Outside the list**: a targeted Grep or partial read outside the listed files is fine when you
  need it to answer a wiring question, like which entry point loads the module. Don't read
  whole trees.
- **Uncommitted files** listed in the plan: read them with `git -C <repo> show HEAD:<path>`.
  The note has to match the commit being stamped, not the working copy.
- **Pin vs HEAD**: each plan block has a `pin vs HEAD` line comparing this module's code at HEAD
  with the pinned wheel Crucible installs. If files differ, write or refresh `## Pinned vs local`
  from that list and open the pinned copies of files your claims rest on. A module that's STALE
  only because "pinned-vs-HEAD comparison changed" (a pin bump) needs that section refreshed,
  plus the [HEAD only] tags; leave the rest alone. If the line now says `identical`, delete the
  section and every **[HEAD only]** / **[pin: …]** tag (and any pin-vs-HEAD wording in the
  diagram): those claims now describe code Crucible runs. Stamp refuses a note that keeps them.

Write the note in the shape of [NOTE_TEMPLATE.md](NOTE_TEMPLATE.md). Before stamping, **check
your own risky claims**. A fact-check of the first full build found about 25 wrong or overstated
claims, nearly all about security. For every red diagram node and every claim about egress,
auth, encryption, sandboxing, a listening port or a default, reopen the cited line and run the
counterexample grep the template's security rule describes. Then stamp it right away:

```bash
uv run --no-project python .claude/skills/update-refs-knowledge/scripts/refs_knowledge.py stamp <module-id>
```

Stamping records the planned commit, fills in the note's `source:` line and regenerates
`INDEX.md`. If a module fails, report the failure and don't stamp it, so the next run retries it.

## 3. Report back (this is all the main session sees)

```markdown
## refs-knowledge update — <date>
| Module | Status | What changed that matters to Crucible |
|---|---|---|
| pysyft/syft-enclave | STALE → updated | approval state now persisted (#9530); digest check default unchanged |

### ⚠️ Crucible impact
- <security defaults flipped, seam-breaking API changes, a local tree now ahead of Crucible's pin, a Crucible upstream ask that landed>

### Skipped
- <N fresh modules; uncommitted files left out; failures>
```

Leave out ⚠️ bullets that are only hypothetical. If nothing affects Crucible, say so in one line.
