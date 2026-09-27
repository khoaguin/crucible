---
module: inspect_evals/catalog
summary: UKGovernmentBEIS's catalog of 129 published Inspect eval packages (246 tasks), self-registered into Inspect via a plugin entry point; the evidence base for Crucible's seam-shape claim.
source: inspect_evals@356c13d21f (main, 2026-07-17)
---
# inspect_evals/catalog

## Mental model
`inspect_evals` doesn't run evals itself — it's a card catalog. Each eval subfolder under
`src/inspect_evals/` is one published benchmark package; a single file (`_registry.py`) imports
every one of them, and each import's side effect is an Inspect `@task` decorator registering that
eval into Inspect's global task registry. `inspect eval inspect_evals/gpqa` works because
installing the `inspect_evals` package advertises `_registry` as an `inspect_ai` plugin, and Inspect
imports it the first time it can't find an `inspect_evals/...` name. For Crucible, this catalog is
the evidence for "do real evals actually decompose into the dataset+solver+scorer shape Crucible's
BenchmarkSpec assumes?" — browsing it, not any one eval's internals, is the point.

## Surface Crucible touches
- `_registry.py` (`src/inspect_evals/_registry.py:1-271`) — one `# ruff: noqa: F401` file of flat
  `from inspect_evals.<pkg> import <name>[, ...]` statements. 129 packages imported, 247 names
  total: 246 `@task` functions plus 1 `@scorer`, `persistbench_judge` (`_registry.py:210`, defined
  `persistbench/scorers.py:125-126`). 36 packages export more than one name (e.g. `agieval`
  exports 9 sub-tasks at `_registry.py:13-23`; `gdm_self_proliferation` exports 20
  milestone/e2e variants at `_registry.py:100-121`). Counted by parsing `_registry.py` with `ast`
  and matching each name to its `def` and decorator.
- Plugin wiring: `[project.entry-points.inspect_ai]` → `inspect_evals = "inspect_evals._registry"`
  (`pyproject.toml:306-307`). This is the "runtime plugin socket" pattern the
  `screamingface/engine-benchmarks` note compares against `BenchmarkRegistry` — here the socket is
  Python's standard entry-points mechanism. Inspect loads it lazily: a registry lookup miss for
  `inspect_evals/<name>` calls `ensure_entry_points("inspect_evals")`
  (`inspect_ai/_util/registry.py:289-293`), which runs `ep.load()` once per package
  (`inspect_ai/_util/entrypoints.py:19-26`). One import pulls in the whole catalog; there is no
  per-eval registration call.
- A failed plugin import is logged, not raised (`inspect_ai/_util/entrypoints.py:34-37`): one
  broken eval import in `_registry.py` leaves every `inspect_evals/...` name unresolvable behind a
  single warning.
- 131 top-level directories exist under `src/inspect_evals/` (counted as distinct first path
  segments of `git ls-files src/inspect_evals`; `find -maxdepth 1 -type d` agrees, no
  `__pycache__`). 129 are imported by `_registry.py`; the two others are not evals. `utils/` holds
  shared helpers, e.g. the HF-download wrappers that `pyproject.toml:110-119` bans calling around.
  `gdm_capabilities/` holds only a `README.md`: a leftover marker saying its 5 GDM evals
  (`gdm_in_house_ctf`, `gdm_intercode_ctf`, `gdm_self_proliferation`, `gdm_self_reasoning`,
  `gdm_stealth`) "were moved one level up" (`gdm_capabilities/README.md:1-3`).

## Defaults & invariants that matter
- **New eval code is not accepted as a direct PR to `src/`** (`CONTRIBUTING.md:10`, since
  2026-05-05). Contributors go through "the Inspect Evals Register": a register entry points at a
  pinned commit of the author's own repo, and users clone that commit and run `inspect eval` on its
  task file (`register/README.md:3,20`). This decouples *publishing* an eval from a PR into
  `_registry.py` — the closest upstream precedent to the "runtime or PRIVATE-content registration
  path" the `screamingface/engine-benchmarks` focus asks about. It is still public and reviewed,
  not private, and a registered eval is not in `_registry.py` (all 129 of its imports resolve to
  local `src/inspect_evals/<pkg>` directories).
- **Task versioning is result-triggered, not code-triggered**: "bump the task version if your
  change could affect eval results or the task interface" (`CONTRIBUTING.md:71`, full rule in
  `TASK_VERSIONING.md` — not read). Same invariant Crucible needs for any versioned
  `BenchmarkSpec`: version bumps track score-affecting changes, not diffs.
- **Deterministic, mocked tests are the stated testing guidance**: use `mockllm/model` for model
  output, `unittest.mock` for external APIs, and mock sandbox outputs and exit codes rather than
  starting real containers in unit tests (`CONTRIBUTING.md:198-201`). Container-backed tests exist
  but carry `@pytest.mark.docker` (`:201`). It is the same "no live external calls in unit tests"
  stance Crucible takes for zero-egress.
- **Selection criteria shape what's in the catalog**: CONTRIBUTING lists what maintainers
  *prioritize* — established in research, verifiable, baseline results for at least one frontier
  model ("may not be accepted unless it meets a strong strategic need" without them), credible
  source (`CONTRIBUTING.md:131-143`); `docs/methodology.md:32-38` adds a published paper or widely
  used dataset and at least one set of reference results. These are priorities, not hard gates,
  but they make the catalog a filtered sample of "what a real eval looks like", not a random one.

## Watch list
- `EVAL_REGISTER.md` (repo root; linked from `README.md:12` and `register/README.md:5`) — the
  rationale for the register model, not read. Read it if Crucible starts caring about how new evals
  get published; the register is the closest upstream analogue to a private registration seam.
- Whether `gdm_capabilities/` gets deleted or `utils/` gains task exports — either changes the
  131-directories / 129-imported gap above.
