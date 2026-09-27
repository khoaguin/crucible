"""Deterministic half of the update-refs-knowledge skill: git decides what changed, not the model.

Think of it as a librarian's ledger. Every module in manifest.toml has one knowledge note
in .refs-knowledge/, and the ledger (state.json) remembers which commit that note was
written from. Three commands:

1. `plan [filters...]` — for each module, compare the ledger commit to the repo's HEAD and
   print a work list: NEW (no note yet), REBUILD (scope changed or history rewritten),
   STALE (files moved; lists exactly which, plus the commits that moved them), FRESH (skip).
   It also snapshots each HEAD into .plan.json, so a commit landing mid-build can't be
   stamped as "already read".
2. `stamp <id>...` — after the agent writes a note, record it against the HEAD that `plan`
   snapshotted, write the `source:` line into the note, and regenerate INDEX.md.
3. `clone <repo>...` — fetch a missing upstream from its manifest `url`, so a fresh checkout
   of Crucible can build the ledger without anyone's home-directory paths.

The manifest says WHICH repos; this machine's repos.local.toml (git-ignored) says WHERE each
clone sits, and a repo it doesn't name defaults to .refs-repos/<name>.

Worked example: syft-enclave's note was stamped at `9f3a2b1`; HEAD is now `1e14d63`, and
between them 12 commits touched the repo but only 2 touched `packages/syft-enclave` (tests
excluded) -> STALE with those 2 files and 2 commits. The agent reads two diffs instead of
60 files; unchanged modules cost one `git diff` each and zero model tokens.

Read-only against the upstream repos: only rev-parse / diff / log / ls-files / status.
The one exception is `clone`, which only ever creates a clone that isn't there yet.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import re
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

SKILL_DIR: Path = Path(__file__).resolve().parent.parent
MANIFEST_PATH: Path = SKILL_DIR / "manifest.toml"
LOCAL_PATHS_PATH: Path = SKILL_DIR / "repos.local.toml"  # git-ignored: repo name -> this machine's clone
CRUCIBLE_ROOT: Path = SKILL_DIR.parents[2]  # .claude/skills/<skill> -> repo root
DEFAULT_CLONE_DIR: Path = CRUCIBLE_ROOT / ".refs-repos"  # git-ignored
KNOWLEDGE_DIR: Path = CRUCIBLE_ROOT / ".refs-knowledge"
STATE_PATH: Path = KNOWLEDGE_DIR / "state.json"
PLAN_PATH: Path = KNOWLEDGE_DIR / ".plan.json"
INDEX_PATH: Path = KNOWLEDGE_DIR / "INDEX.md"
UV_LOCK_PATH: Path = CRUCIBLE_ROOT / "uv.lock"
CLI: str = "uv run --no-project python .claude/skills/update-refs-knowledge/scripts/refs_knowledge.py"

MAX_LISTED_FILES: int = 150  # a huge diff must not flood the agent's context
MAX_LISTED_COMMITS: int = 40
SHORT_SHA_LEN: int = 10
MANIFEST_HASH_LEN: int = 12
VERSION_RE: re.Pattern[str] = re.compile(r"""["'](\d+\.\d+[^"']*)["']""")
FRONTMATTER_RE: re.Pattern[str] = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)
SUMMARY_RE: re.Pattern[str] = re.compile(r"^summary:\s*(.+)$", re.MULTILINE)
SOURCE_RE: re.Pattern[str] = re.compile(r"^source:.*$", re.MULTILINE)
COMMIT_MARK: str = "\x00"
MERMAID_FENCE: str = "```mermaid"
PINNED_SECTION: str = "## Pinned vs local"  # required in a note whenever the pinned wheel differs from HEAD
HEAD_ONLY_TAG: str = "[HEAD only]"  # inline tag for claims that don't hold at the pin; must vanish when pin == HEAD
SITE_PACKAGES_GLOB: str = ".venv/lib/python*/site-packages"  # where Crucible's pinned wheels are installed
MAX_LISTED_PIN_FILES: int = 40
MAP_SHA_LEN: int = 7  # the INDEX map's node labels stay short; the table carries the longer SHA
MERMAID_CLASSDEFS: list[str] = [  # contrast lives inside the node, so light/dark themes can't break it
    "  classDef good  fill:#14532d,stroke:#30a46c,color:#dcfce7",
    "  classDef stage fill:#1e3a8a,stroke:#4a7fd4,color:#dbeafe",
    "  classDef plain fill:#374151,stroke:#9ca3af,color:#f3f4f6",
]
REPO_BOX_STYLE: str = "fill:#1f2937,stroke:#9ca3af,color:#f3f4f6"  # mermaid's default subgraph fill is pale yellow


class KnowledgeError(Exception):
    """A precondition the ledger can't work around (bad manifest, missing repo, git failure)."""


def git(repo: Path, *args: str) -> str:
    """Run one read-only git command in `repo` and return stdout, raising KnowledgeError on failure."""
    try:
        proc: subprocess.CompletedProcess[str] = subprocess.run(
            ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
        )
    except subprocess.CalledProcessError as exc:
        raise KnowledgeError(f"git {' '.join(args)} failed in {repo}: {exc.stderr.strip()}") from exc
    return proc.stdout


def load_manifest() -> dict[str, Any]:
    """Read manifest.toml and check every module names a known repo."""
    manifest: dict[str, Any] = tomllib.loads(MANIFEST_PATH.read_text())
    repos: dict[str, Any] = manifest.get("repos", {})
    for module in manifest.get("modules", []):
        if module["repo"] not in repos:
            raise KnowledgeError(f"module {module['id']} names unknown repo {module['repo']}")
    return manifest


def repo_path(name: str) -> Path:
    """Where repo `name` is cloned on this machine: repos.local.toml if it names it, else .refs-repos/<name>.

    A relative path in repos.local.toml resolves from the Crucible root, and `~` expands, so
    `pysyft = "../PySyft"` and `pysyft = "~/code/PySyft"` both work.
    """
    local_paths: dict[str, Any] = tomllib.loads(LOCAL_PATHS_PATH.read_text()) if LOCAL_PATHS_PATH.exists() else {}
    local: Any = local_paths.get(name)
    if local is None:
        return DEFAULT_CLONE_DIR / name
    if not isinstance(local, str):
        raise KnowledgeError(f"{LOCAL_PATHS_PATH}: `{name}` must be a path string, got {local!r}")
    path: Path = Path(local).expanduser()
    return path if path.is_absolute() else CRUCIBLE_ROOT / path


def require_repos(names: list[str]) -> None:
    """Fail once, naming every missing clone and the command that fixes it, so the caller asks the user one question."""
    missing: list[str] = [name for name in names if not (repo_path(name) / ".git").exists()]
    if missing:
        where: str = "; ".join(f"{name} (expected at {repo_path(name)})" for name in missing)
        raise KnowledgeError(
            f"repo not cloned: {where}. Fix: `{CLI} clone {' '.join(missing)}`, "
            f"or point {LOCAL_PATHS_PATH} at an existing clone."
        )


def load_json(path: Path) -> dict[str, Any]:
    """Read a JSON ledger file, treating a missing file as an empty ledger."""
    return json.loads(path.read_text()) if path.exists() else {}


def write_json(path: Path, data: dict[str, Any]) -> None:
    """Write a JSON ledger file, creating the knowledge dir on first use."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def note_path(module_id: str) -> Path:
    """Where a module's knowledge note lives (`pysyft/syft-enclave` -> .refs-knowledge/pysyft/syft-enclave.md)."""
    return KNOWLEDGE_DIR / f"{module_id}.md"


def module_hash(module: dict[str, Any], default_exclude: list[str]) -> str:
    """Fingerprint the module's scope, so editing its paths or focus forces a REBUILD."""
    scope: dict[str, Any] = {k: module.get(k) for k in ("paths", "exclude", "focus")}
    scope["default_exclude"] = default_exclude
    return hashlib.sha256(json.dumps(scope, sort_keys=True).encode()).hexdigest()[:MANIFEST_HASH_LEN]


def is_kept(path: str, excludes: list[str]) -> bool:
    """True when a repo-relative path survives the exclude patterns (fnmatch; `*` crosses `/`)."""
    return not any(fnmatch.fnmatch(path, pattern) for pattern in excludes)


def excludes_for(module: dict[str, Any], default_exclude: list[str]) -> list[str]:
    """The module's own excludes stacked on the manifest-wide defaults."""
    return [*default_exclude, *module.get("exclude", [])]


def local_version(repo: Path, module: dict[str, Any]) -> str | None:
    """The version the local tree declares: [project].version from a pyproject, else the first version string in the file."""
    version_file: Path = repo / module.get("version_file", "")
    if not version_file.is_file():
        return None
    text: str = version_file.read_text()
    if version_file.suffix == ".toml":
        version: Any = tomllib.loads(text).get("project", {}).get("version")
        return version if isinstance(version, str) else None
    match: re.Match[str] | None = VERSION_RE.search(text)
    return match.group(1) if match else None


def pinned_versions() -> dict[str, str]:
    """Every package version Crucible's uv.lock resolves to, keyed by distribution name."""
    if not UV_LOCK_PATH.exists():
        return {}
    lock: dict[str, Any] = tomllib.loads(UV_LOCK_PATH.read_text())
    return {pkg["name"]: pkg.get("version", "?") for pkg in lock.get("package", [])}


def drift_text(repo: Path, module: dict[str, Any], pins: dict[str, str]) -> str:
    """One-line 'local X · pin Y' so the reader knows the note describes the local tree, not the pinned release."""
    local: str | None = local_version(repo, module)
    pinned: list[str] = [f"{d}=={pins.get(d, 'unpinned')}" for d in module.get("dists", [])]
    parts: list[str] = [f"local {local or '?'}"]
    if pinned:
        parts.append("pin " + ", ".join(pinned))
    return " · ".join(parts)


def changed_files(repo: Path, since: str, head: str, paths: list[str], excludes: list[str]) -> list[str]:
    """`git diff --name-status` lines between two commits, restricted to the module and filtered by excludes."""
    lines: list[str] = git(repo, "diff", "--name-status", "-M", since, head, "--", *paths).splitlines()
    return [line for line in lines if any(is_kept(p, excludes) for p in line.split("\t")[1:])]


def touching_commits(repo: Path, since: str, head: str, paths: list[str], excludes: list[str]) -> list[str]:
    """Commits in since..head that touched at least one kept file of the module, newest first."""
    # %x00 makes git emit COMMIT_MARK itself; a literal NUL can't ride in an argv.
    raw: str = git(repo, "log", "--format=%x00%h %s", "--name-only", f"{since}..{head}", "--", *paths)
    commits: list[str] = []
    for block in raw.split(COMMIT_MARK)[1:]:
        subject, *files = [line for line in block.splitlines() if line]
        if any(is_kept(f, excludes) for f in files):
            commits.append(subject)
    return commits


@dataclass(frozen=True)
class PinDrift:
    """How a module's code at HEAD differs from the pinned wheel Crucible actually installs.

    The notes describe HEAD, but Crucible runs the pin. The first full build stated HEAD-only
    behaviour (e.g. syft's key pinning, absent at the pinned 0.10.0) as plain fact. This is the
    deterministic answer to "which of these claims hold for what Crucible runs?".
    """

    site_packages: Path
    compared: int  # module .py files present in both the pin and HEAD
    changed: list[str]  # repo paths whose bytes differ from the pinned copy
    head_only: list[str]  # repo paths the pinned wheel doesn't ship
    pin_only: list[str]  # wheel paths the pin ships that HEAD has dropped
    fingerprint: str  # changes when either side moves, so a pin bump re-stales the note

    @property
    def differs(self) -> bool:
        """True when the note must carry a `## Pinned vs local` section."""
        return bool(self.changed or self.head_only or self.pin_only)


def site_packages() -> Path | None:
    """Crucible's installed site-packages (where the pinned wheels live), or None without a venv."""
    matches: list[Path] = sorted(CRUCIBLE_ROOT.glob(SITE_PACKAGES_GLOB))
    return matches[0] if matches else None


def wheel_path(repo_rel: str) -> str:
    """Where a repo file lands inside a wheel: after the last `src/` dir, else unchanged (`syft/x.py`)."""
    parts: list[str] = repo_rel.split("/")
    if "src" in parts[:-1]:
        last_src: int = len(parts) - 1 - parts[::-1].index("src")
        return "/".join(parts[last_src + 1 :])
    return repo_rel


def hash_files(repo: Path, files: list[Path]) -> list[str]:
    """Git blob SHAs for files on disk, in order: one `git hash-object` call, comparable to `ls-tree` SHAs."""
    if not files:
        return []
    try:
        proc: subprocess.CompletedProcess[str] = subprocess.run(
            ["git", "-C", str(repo), "hash-object", "--stdin-paths"],
            input="\n".join(str(f) for f in files) + "\n", capture_output=True, text=True, check=True,
        )
    except subprocess.CalledProcessError as exc:
        raise KnowledgeError(f"git hash-object failed in {repo}: {exc.stderr.strip()}") from exc
    return proc.stdout.split()


def pin_drift(repo: Path, head: str, module: dict[str, Any], excludes: list[str]) -> PinDrift | None:
    """Compare the module's Python files at HEAD with the pinned wheel in .venv, blob by blob.

    Stage 1 — HEAD side: `git ls-tree` gives each kept .py file's blob SHA; map each to its path
              inside a wheel (`apps/aigateway/src/aigateway/x.py` -> `aigateway/x.py`), keeping only
              files whose top-level package is installed at all.
    Stage 2 — pin side: hash the installed copies with the same algorithm, so equal bytes give
              equal SHAs. Missing copy -> HEAD-only.
    Stage 3 — pinned files in the same package dirs that HEAD no longer has -> pin-only.

    Returns None when the module isn't installed (the inspect references), so nothing is required.
    """
    site: Path | None = site_packages()
    if site is None:
        return None
    # Stage 1 — HEAD blobs, keyed by wheel path.
    head_blobs: dict[str, tuple[str, str]] = {}  # wheel path -> (repo path, blob sha)
    for line in git(repo, "ls-tree", "-r", head, "--", *module["paths"]).splitlines():
        meta, path = line.split("\t", 1)
        rel: str = wheel_path(path)
        if path.endswith(".py") and is_kept(path, excludes) and (site / rel.split("/")[0]).is_dir():
            head_blobs[rel] = (path, meta.split()[2])
    if not head_blobs:
        return None
    # Stage 2 — hash the installed copies; absent ones are HEAD-only.
    installed: list[str] = sorted(rel for rel in head_blobs if (site / rel).is_file())
    pin_shas: dict[str, str] = dict(zip(installed, hash_files(repo, [site / rel for rel in installed])))
    changed: list[str] = sorted(head_blobs[rel][0] for rel in installed if pin_shas[rel] != head_blobs[rel][1])
    head_only: list[str] = sorted(head_blobs[rel][0] for rel in head_blobs if rel not in pin_shas)
    # Stage 3 — pinned files HEAD dropped, looked for only in dirs this module covers.
    dirs: set[str] = {rel.rsplit("/", 1)[0] for rel in head_blobs if "/" in rel}
    pin_only: list[str] = sorted(
        p.relative_to(site).as_posix()
        for d in dirs for p in (site / d).glob("*.py")
        if p.relative_to(site).as_posix() not in head_blobs
    )
    fingerprint: str = hashlib.sha256(
        json.dumps([sorted((r, head_blobs[r][1], pin_shas.get(r)) for r in head_blobs), pin_only]).encode()
    ).hexdigest()[:MANIFEST_HASH_LEN]
    return PinDrift(site, len(installed), changed, head_only, pin_only, fingerprint)


def pin_drift_lines(drift: PinDrift | None) -> list[str]:
    """The plan's 'pin vs HEAD' block: what differs, so the note can mark HEAD-only claims."""
    if drift is None:
        return ["pin vs HEAD: not installed in .venv (reference only; no pinned section needed)"]
    if not drift.differs:
        return [f"pin vs HEAD: identical ({drift.compared} files) — every claim holds for the pin"]
    lines: list[str] = [
        f"pin vs HEAD: {len(drift.changed)} changed, {len(drift.head_only)} HEAD-only, {len(drift.pin_only)} pin-only "
        f"(pinned source: {drift.site_packages}/<path after src/>) — note MUST carry `{PINNED_SECTION}`:"
    ]
    lines += [f"  changed    {p}" for p in drift.changed[:MAX_LISTED_PIN_FILES]]
    lines += [f"  HEAD-only  {p}" for p in drift.head_only[:MAX_LISTED_PIN_FILES]]
    lines += [f"  pin-only   {p}" for p in drift.pin_only[:MAX_LISTED_PIN_FILES]]
    return lines


def module_status(
    repo: Path, module: dict[str, Any], entry: dict[str, Any] | None, head: str, mhash: str, excludes: list[str],
    pin_fp: str | None = None,
) -> tuple[str, str]:
    """Classify one module as NEW / REBUILD / STALE / FRESH, with the reason in plain words."""
    if entry is None or not note_path(module["id"]).exists():
        return "NEW", "no note yet"
    if entry.get("manifest_hash") != mhash:
        return "REBUILD", "manifest scope/focus changed since last build"
    since: str = entry["sha"]
    reachable: bool = subprocess.run(
        ["git", "-C", str(repo), "merge-base", "--is-ancestor", since, head], capture_output=True
    ).returncode == 0
    if not reachable:
        return "REBUILD", f"built from {since[:SHORT_SHA_LEN]}, which is not an ancestor of HEAD (branch switch or rewrite)"
    if since != head and changed_files(repo, since, head, module["paths"], excludes):
        return "STALE", f"module files changed since {since[:SHORT_SHA_LEN]}"
    if entry.get("pin_fingerprint") != pin_fp:
        return "STALE", "pinned-vs-HEAD comparison changed (pin bump, or not recorded yet) — update `## Pinned vs local`"
    return "FRESH", f"no module changes since {since[:SHORT_SHA_LEN]}"


def capped(items: list[str], limit: int) -> list[str]:
    """Indent a list for the plan printout, cutting it at `limit` with a visible '… N more' line."""
    lines: list[str] = [f"  {item}" for item in items[:limit]]
    if len(items) > limit:
        lines.append(f"  … {len(items) - limit} more (use git to list them)")
    return lines


def matches_filter(module_id: str, filters: list[str]) -> bool:
    """True when no filters are given, or a filter matches the id or its repo (`pysyft` == `pysyft/*`)."""
    return not filters or any(fnmatch.fnmatch(module_id, f) or fnmatch.fnmatch(module_id, f + "/*") for f in filters)


def cmd_plan(filters: list[str], snapshot_heads: bool = True) -> None:
    """Print the work list for the agent and snapshot each repo's HEAD for `stamp`.

    `snapshot_heads=False` is the reader's freshness `check`: same printout, but .plan.json is
    left alone, so a reader can't clobber the snapshot a background build will stamp against.

    Stage 1 — select the modules the filters match; only their repos must be cloned, and one
              error names every missing clone. Resolve each repo's HEAD once (its modules share it).
    Stage 2 — per module: classify against the ledger; for STALE list the changed files and
              the commits that changed them; for NEW/REBUILD list the files to read.
    Stage 3 — snapshot HEADs into .plan.json and print the summary + work list.
    """
    manifest: dict[str, Any] = load_manifest()
    default_exclude: list[str] = manifest.get("default_exclude", [])
    state: dict[str, Any] = load_json(STATE_PATH)
    pins: dict[str, str] = pinned_versions()
    snapshot: dict[str, Any] = {}
    work: list[str] = []
    counts: dict[str, int] = {"NEW": 0, "REBUILD": 0, "STALE": 0, "FRESH": 0}
    out: list[str] = [f"# refs-knowledge plan — {date.today().isoformat()}", ""]

    # Stage 1 — select modules, require only their repos, one HEAD per repo. Both failures land
    # before the snapshot, so a mis-parsed filter ("ref repos") or a missing clone can't clobber
    # the last good plan mid-build.
    selected: list[dict[str, Any]] = [m for m in manifest["modules"] if matches_filter(m["id"], filters)]
    if not selected:
        valid: list[str] = [*manifest["repos"], *(m["id"] for m in manifest["modules"])]
        raise KnowledgeError(f"filter {filters} matched no module. Valid filters: {', '.join(valid)} (or none = all)")
    needed: list[str] = list(dict.fromkeys(m["repo"] for m in selected))  # dedupe, keep manifest order
    require_repos(needed)
    heads: dict[str, tuple[str, str, str]] = {}
    for name in needed:
        repo: Path = repo_path(name)
        head: str = git(repo, "rev-parse", "HEAD").strip()
        branch: str = git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip()
        head_date: str = git(repo, "log", "-1", "--format=%cs", head).strip()
        heads[name] = (head, branch, head_date)

    # Stage 2 — per module: classify, then list exactly what the agent must read.
    for module in selected:
        repo = repo_path(module["repo"])
        head, branch, head_date = heads[module["repo"]]
        excludes: list[str] = excludes_for(module, default_exclude)
        mhash: str = module_hash(module, default_exclude)
        entry: dict[str, Any] | None = state.get(module["id"])
        drift: PinDrift | None = pin_drift(repo, head, module, excludes)
        pin_fp: str | None = drift.fingerprint if drift else None
        status, reason = module_status(repo, module, entry, head, mhash, excludes, pin_fp)
        counts[status] += 1
        snapshot[module["id"]] = {
            "sha": head, "branch": branch, "date": head_date, "manifest_hash": mhash,
            "pin_fingerprint": pin_fp, "pin_differs": bool(drift and drift.differs),
        }

        out.append(f"## {module['id']} — {status}")
        out.append(f"repo {repo} · {branch} @ {head[:SHORT_SHA_LEN]} ({head_date}) · {drift_text(repo, module, pins)}")
        out.append(f"reason: {reason}")
        out += pin_drift_lines(drift)
        if status == "FRESH":
            out.append("")
            continue
        work.append(module["id"])
        out.append(f"note: {note_path(module['id']).relative_to(CRUCIBLE_ROOT)}")
        out.append("focus: " + " ".join(module["focus"].split()))

        if status == "STALE":
            since: str = entry["sha"]  # type: ignore[index]  # STALE implies an entry
            files: list[str] = changed_files(repo, since, head, module["paths"], excludes)
            commits: list[str] = touching_commits(repo, since, head, module["paths"], excludes)
            out.append(f"diff range: {since[:SHORT_SHA_LEN]}..{head[:SHORT_SHA_LEN]}")
            out.append(f"commits ({len(commits)}):")
            out += capped(commits, MAX_LISTED_COMMITS)
            out.append(f"changed files ({len(files)}):")
            out += capped(files, MAX_LISTED_FILES)
        else:
            tracked: list[str] = [
                f for f in git(repo, "ls-files", "--", *module["paths"]).splitlines() if is_kept(f, excludes)
            ]
            out.append(f"files to read ({len(tracked)}):")
            out += capped(tracked, MAX_LISTED_FILES)
        dirty: list[str] = [
            line for line in git(repo, "status", "--porcelain", "--", *module["paths"]).splitlines()
            if is_kept(line[3:], excludes)
        ]
        if dirty:
            out.append(f"uncommitted in local tree ({len(dirty)}) — read these via `git show HEAD:<path>`, not the working copy:")
            out += capped(dirty, MAX_LISTED_FILES)
        out.append("")

    # Stage 3 — snapshot HEADs for stamp, then print.
    if snapshot_heads:
        write_json(PLAN_PATH, {"planned_at": datetime.now().isoformat(timespec="seconds"), "modules": snapshot})
    summary: str = ", ".join(f"{n} {s.lower()}" for s, n in counts.items() if n)
    print("\n".join(out))
    print(f"SUMMARY: {summary}")
    print(f"WORK LIST: {', '.join(work) if work else '(empty — everything is fresh)'}")


def set_source_line(note: Path, source: str) -> None:
    """Write the `source:` provenance line into the note's frontmatter (the agent never types SHAs)."""
    text: str = note.read_text()
    frontmatter: re.Match[str] | None = FRONTMATTER_RE.match(text)
    if frontmatter is None or not SUMMARY_RE.search(frontmatter.group(1)):
        raise KnowledgeError(f"{note} needs a --- frontmatter block with a `summary:` line")
    block: str = frontmatter.group(1)
    new_block: str = SOURCE_RE.sub(f"source: {source}", block) if SOURCE_RE.search(block) else f"{block}\nsource: {source}"
    note.write_text(text.replace(block, new_block, 1))


def cmd_stamp(module_ids: list[str]) -> None:
    """Record each freshly written note against the HEAD `plan` snapshotted, then rebuild INDEX.md."""
    planned: dict[str, Any] = load_json(PLAN_PATH).get("modules", {})
    state: dict[str, Any] = load_json(STATE_PATH)
    manifest: dict[str, Any] = load_manifest()
    # The manifest name, not the clone's folder name: the latter differs per machine.
    repo_names: dict[str, str] = {m["id"]: m["repo"] for m in manifest["modules"]}
    wants_diagram: dict[str, bool] = {m["id"]: m.get("diagram", True) for m in manifest["modules"]}
    for module_id in module_ids:
        if module_id not in planned:
            raise KnowledgeError(f"{module_id} is not in the last plan — run `plan` first")
        note: Path = note_path(module_id)
        if not note.exists():
            raise KnowledgeError(f"no note at {note} — write it before stamping")
        # The first full build skipped the diagram on 6 of 11 flow modules when it was only a
        # template comment; a gate the agent must pass can't be skimmed.
        if wants_diagram[module_id] and MERMAID_FENCE not in note.read_text():
            raise KnowledgeError(f"{note} has no ```mermaid block — add the flow diagram (see NOTE_TEMPLATE.md)")
        snap: dict[str, Any] = planned[module_id]
        # The first build stated HEAD-only behaviour as fact for a pin that lacks it; when the
        # pinned wheel differs, the note must say how, or readers can't tell what Crucible runs.
        if snap.get("pin_differs") and PINNED_SECTION not in note.read_text():
            raise KnowledgeError(
                f"{note} lacks `{PINNED_SECTION}` — the pinned wheel differs from HEAD (see the plan's "
                "'pin vs HEAD' list); say which claims are HEAD-only (see NOTE_TEMPLATE.md)"
            )
        # The reverse matters too: once the pin catches up with HEAD, a leftover section or tag
        # tells readers Crucible lacks code it now runs.
        stale_markers: list[str] = [m for m in (PINNED_SECTION, HEAD_ONLY_TAG) if m in note.read_text()]
        if not snap.get("pin_differs") and stale_markers:
            raise KnowledgeError(
                f"{note} still carries {', '.join(stale_markers)} but the pin now matches HEAD — "
                "delete the section and the [HEAD only] / [pin: …] tags"
            )
        set_source_line(note, f"{repo_names[module_id]}@{snap['sha'][:SHORT_SHA_LEN]} ({snap['branch']}, {snap['date']})")
        state[module_id] = {**snap, "built": date.today().isoformat()}
        print(f"stamped {module_id} @ {snap['sha'][:SHORT_SHA_LEN]}")
    write_json(STATE_PATH, state)
    write_index(manifest, state)


def cmd_clone(names: list[str]) -> None:
    """Fetch each named upstream from its manifest `url` to where repo_path says this machine keeps it; existing clones are untouched."""
    repos: dict[str, Any] = load_manifest()["repos"]
    unknown: list[str] = [name for name in names if name not in repos]
    if unknown:
        raise KnowledgeError(f"unknown repo {', '.join(unknown)}. Valid: {', '.join(repos)}")
    for name in names:
        target: Path = repo_path(name)
        if (target / ".git").exists():
            print(f"{name}: already cloned at {target}")
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        try:  # git's own progress/errors stream straight to the terminal
            subprocess.run(["git", "clone", repos[name]["url"], str(target)], check=True)
        except subprocess.CalledProcessError as exc:
            raise KnowledgeError(f"git clone of {name} from {repos[name]['url']} failed (exit {exc.returncode})") from exc
        print(f"{name}: cloned into {target}")


def write_index(manifest: dict[str, Any], state: dict[str, Any]) -> None:
    """Regenerate INDEX.md: one row per module with its summary, provenance, and local-vs-pinned drift."""
    pins: dict[str, str] = pinned_versions()
    rows: list[str] = []
    for module in manifest["modules"]:
        note: Path = note_path(module["id"])
        entry: dict[str, Any] | None = state.get(module["id"])
        if entry is None or not note.exists():
            rows.append(f"| `{module['id']}` | _not built yet_ | — | — |")
            continue
        summary_match: re.Match[str] | None = SUMMARY_RE.search(note.read_text())
        summary: str = summary_match.group(1).strip() if summary_match else "?"
        repo: Path = repo_path(module["repo"])
        link: str = note.relative_to(KNOWLEDGE_DIR).as_posix()
        rows.append(
            f"| [`{module['id']}`]({link}) | {summary} | `{entry['sha'][:SHORT_SHA_LEN]}` {entry['date']} "
            f"| {drift_text(repo, module, pins)} |"
        )
    INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
    INDEX_PATH.write_text(
        "# Upstream knowledge index\n\n"
        "Generated by `.claude/skills/update-refs-knowledge` — do not edit by hand. "
        "Notes describe the **local tree at the stamped commit**, not the version Crucible pins: "
        "check the drift column before trusting a note about pinned behaviour.\n\n"
        + index_map(manifest, state)
        + "\n| Module | Summary | Built from | Local vs pin |\n|---|---|---|---|\n" + "\n".join(rows) + "\n"
    )


def mermaid_id(text: str) -> str:
    """Make a mermaid-safe node id that still reads as its name in raw source (`pysyft/syft-job` -> `pysyft_syft_job`)."""
    return re.sub(r"\W", "_", text)


def index_map(manifest: dict[str, Any], state: dict[str, Any]) -> str:
    """Draw the dependency map: Crucible -> each repo (edge = its role) -> its modules, green once a note exists.

    Deterministic on purpose: built from the manifest and ledger alone, so it never drifts from
    the table below it and costs no model tokens.
    """
    lines: list[str] = ["```mermaid", "flowchart LR", '  crucible["🤖 Crucible"]:::stage']
    for name, repo_cfg in manifest["repos"].items():
        repo_node: str = f"{mermaid_id(name)}_repo"
        lines.append(f'  subgraph {repo_node}["{name}"]')
        for module in (m for m in manifest["modules"] if m["repo"] == name):
            entry: dict[str, Any] | None = state.get(module["id"])
            short: str = module["id"].split("/", 1)[-1]
            if entry is not None and note_path(module["id"]).exists():
                lines.append(f'    {mermaid_id(module["id"])}["✅ {short}<br/>@{entry["sha"][:MAP_SHA_LEN]}"]:::good')
            else:
                lines.append(f'    {mermaid_id(module["id"])}["{short}<br/>not built"]:::plain')
        lines.append("  end")
        lines.append(f"  style {repo_node} {REPO_BOX_STYLE}")
        lines.append(f'  crucible -- "{repo_cfg.get("role", "depends on")}" --> {repo_node}')
    lines += [*MERMAID_CLASSDEFS, "```", "", "Green ✅ = note built (from the commit shown); grey = not built yet.", ""]
    return "\n".join(lines)


def main() -> int:
    """CLI entry: `plan [filters...]`, `check [filters...]` (read-only plan), `stamp <module-id>...` or `clone <repo>...`."""
    parser: argparse.ArgumentParser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    plan_p = sub.add_parser("plan", help="print the work list")
    plan_p.add_argument("filters", nargs="*", help="module ids or repo names, fnmatch globs allowed")
    check_p = sub.add_parser("check", help="same as plan, but read-only: never touches .plan.json")
    check_p.add_argument("filters", nargs="*", help="module ids or repo names, fnmatch globs allowed")
    stamp_p = sub.add_parser("stamp", help="record written notes against the planned HEAD")
    stamp_p.add_argument("module_ids", nargs="+")
    clone_p = sub.add_parser("clone", help="clone missing upstreams from their manifest url")
    clone_p.add_argument("repos", nargs="+", help="repo names from manifest.toml")
    args: argparse.Namespace = parser.parse_args()
    try:
        if args.command in ("plan", "check"):
            cmd_plan(args.filters, snapshot_heads=args.command == "plan")
        elif args.command == "clone":
            cmd_clone(args.repos)
        else:
            cmd_stamp(args.module_ids)
    except KnowledgeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
