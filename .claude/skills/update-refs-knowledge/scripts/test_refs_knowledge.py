"""Contract tests for the refs-knowledge ledger: each test names the incremental-build rule it defends.

Run: uv run --no-project --with pytest pytest .claude/skills/update-refs-knowledge/scripts -q
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

import refs_knowledge as rk

MODULE_ID: str = "demo/core"


def sh(repo: Path, *args: str) -> str:
    """Run git in the throwaway upstream repo."""
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout


def commit(repo: Path, rel: str, content: str, message: str) -> str:
    """Write one file in the upstream repo, commit it, and return the new HEAD."""
    path: Path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    sh(repo, "add", rel)
    sh(repo, "commit", "-q", "-m", message)
    return sh(repo, "rev-parse", "HEAD").strip()


def write_manifest(root: Path, repo: Path, focus: str = "the core loop") -> None:
    """Point the ledger at a one-module manifest whose repo `url` is the throwaway upstream."""
    (root / "manifest.toml").write_text(
        f'default_exclude = ["*/tests/*"]\n[repos.demo]\nurl = "{repo}"\n'
        f'[[modules]]\nid = "{MODULE_ID}"\nrepo = "demo"\npaths = ["pkg"]\ndists = []\n'
        f'version_file = "pkg/pyproject.toml"\nfocus = "{focus}"\n'
    )


def write_note(summary: bool = True, diagram: bool = True) -> None:
    """Stand in for the agent: write a note, optionally missing the summary line or the diagram."""
    note: Path = rk.note_path(MODULE_ID)
    note.parent.mkdir(parents=True, exist_ok=True)
    head: str = "summary: the core loop\n" if summary else ""
    flow: str = "```mermaid\nflowchart LR\n  a --> b\n```\n" if diagram else ""
    note.write_text(f"---\nmodule: {MODULE_ID}\n{head}source: (filled in by stamp)\n---\n# body\n{flow}")


def plan(capsys: pytest.CaptureFixture[str]) -> str:
    """Run `plan` and return its printout."""
    rk.cmd_plan([])
    return capsys.readouterr().out


@pytest.fixture
def upstream(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A throwaway upstream repo plus a throwaway knowledge dir wired into the module globals."""
    repo: Path = tmp_path / "upstream"
    repo.mkdir()
    sh(repo, "init", "-q")
    sh(repo, "config", "user.email", "t@t")
    sh(repo, "config", "user.name", "t")
    commit(repo, "pkg/core.py", "x = 1\n", "init core")
    knowledge: Path = tmp_path / "crucible" / ".refs-knowledge"
    monkeypatch.setattr(rk, "MANIFEST_PATH", tmp_path / "manifest.toml")
    monkeypatch.setattr(rk, "LOCAL_PATHS_PATH", tmp_path / "repos.local.toml")
    monkeypatch.setattr(rk, "DEFAULT_CLONE_DIR", tmp_path / "crucible" / ".refs-repos")
    monkeypatch.setattr(rk, "CRUCIBLE_ROOT", tmp_path / "crucible")
    monkeypatch.setattr(rk, "KNOWLEDGE_DIR", knowledge)
    monkeypatch.setattr(rk, "STATE_PATH", knowledge / "state.json")
    monkeypatch.setattr(rk, "PLAN_PATH", knowledge / ".plan.json")
    monkeypatch.setattr(rk, "INDEX_PATH", knowledge / "INDEX.md")
    monkeypatch.setattr(rk, "UV_LOCK_PATH", tmp_path / "uv.lock")
    write_manifest(tmp_path, repo)
    (tmp_path / "repos.local.toml").write_text(f'demo = "{repo}"\n')
    return repo


def build_once(capsys: pytest.CaptureFixture[str]) -> None:
    """Plan, write the note, stamp: the state after one successful build."""
    plan(capsys)
    write_note()
    rk.cmd_stamp([MODULE_ID])
    capsys.readouterr()


def test_first_run_lists_module_as_new_with_its_files(upstream: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A module with no note must be built in full, so the agent gets the full file list."""
    out: str = plan(capsys)
    assert f"## {MODULE_ID} — NEW" in out
    assert "pkg/core.py" in out
    assert f"WORK LIST: {MODULE_ID}" in out


def test_stamp_refuses_note_without_summary(upstream: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """INDEX.md is built from `summary:` lines, so a note without one must not enter the ledger."""
    plan(capsys)
    write_note(summary=False)
    with pytest.raises(rk.KnowledgeError, match="summary"):
        rk.cmd_stamp([MODULE_ID])
    assert not rk.STATE_PATH.exists()


def test_stamp_refuses_flow_note_without_diagram(upstream: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """The first real build skipped 6 of 11 diagrams when the rule was only prose; the gate must hold."""
    plan(capsys)
    write_note(diagram=False)
    with pytest.raises(rk.KnowledgeError, match="mermaid"):
        rk.cmd_stamp([MODULE_ID])


def test_catalog_module_may_skip_diagram(upstream: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """An inventory has no flow to draw, so `diagram = false` must let it through without one."""
    manifest: Path = tmp_path / "manifest.toml"
    manifest.write_text(manifest.read_text().replace('dists = []\n', 'dists = []\ndiagram = false\n'))
    plan(capsys)
    write_note(diagram=False)
    rk.cmd_stamp([MODULE_ID])
    assert MODULE_ID in json.loads(rk.STATE_PATH.read_text())


def test_stamp_writes_provenance_and_index(upstream: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """The note must carry the commit it describes, labelled by manifest repo name (not the clone's folder, which varies per machine)."""
    head: str = sh(upstream, "rev-parse", "HEAD").strip()
    build_once(capsys)
    assert f"source: demo@{head[:rk.SHORT_SHA_LEN]}" in rk.note_path(MODULE_ID).read_text()
    assert "the core loop" in rk.INDEX_PATH.read_text()
    assert json.loads(rk.STATE_PATH.read_text())[MODULE_ID]["sha"] == head


def test_index_map_marks_built_modules(upstream: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """The INDEX map must flip a module from grey to green once its note exists, or it lies about coverage."""
    rk.write_index(rk.load_manifest(), {})
    assert f'{rk.mermaid_id(MODULE_ID)}["core<br/>not built"]:::plain' in rk.INDEX_PATH.read_text()
    build_once(capsys)
    index: str = rk.INDEX_PATH.read_text()
    assert f'{rk.mermaid_id(MODULE_ID)}["✅ core<br/>@' in index and ":::good" in index
    assert index.count("```mermaid") == 1


def test_unchanged_module_is_fresh_and_costs_nothing(upstream: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """The whole point: a rerun with no upstream movement must hand the agent an empty work list."""
    build_once(capsys)
    assert "WORK LIST: (empty" in plan(capsys)


def test_test_only_commits_do_not_make_module_stale(upstream: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Test churn doesn't change what the module does, so it must not trigger a paid reread."""
    build_once(capsys)
    commit(upstream, "pkg/tests/test_core.py", "def test(): pass\n", "add tests")
    out: str = plan(capsys)
    assert f"## {MODULE_ID} — FRESH" in out


def test_code_change_is_stale_with_only_relevant_files_and_commits(
    upstream: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The agent should read exactly the moved files, and the commit subjects that say why."""
    build_once(capsys)
    commit(upstream, "pkg/tests/test_core.py", "def test(): pass\n", "tests only")
    commit(upstream, "pkg/core.py", "x = 2\n", "change core default")
    commit(upstream, "other/readme.md", "hi\n", "outside the module")
    out: str = plan(capsys)
    assert f"## {MODULE_ID} — STALE" in out
    assert "change core default" in out
    assert "tests only" not in out and "outside the module" not in out
    assert "M\tpkg/core.py" in out and "test_core.py" not in out


def test_commit_landing_mid_build_is_not_marked_read(upstream: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Stamp records the HEAD the agent was shown; a commit it never saw must show up next run."""
    plan(capsys)
    commit(upstream, "pkg/core.py", "x = 3\n", "landed during build")
    write_note()
    rk.cmd_stamp([MODULE_ID])
    capsys.readouterr()
    out: str = plan(capsys)
    assert f"## {MODULE_ID} — STALE" in out
    assert "landed during build" in out


def test_reader_check_never_clobbers_a_build_in_flight(upstream: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A reader asking 'is this note fresh?' mid-build must not replace the snapshot the build stamps against."""
    plan(capsys)
    planned: str = rk.PLAN_PATH.read_text()
    commit(upstream, "pkg/core.py", "x = 4\n", "moves HEAD mid-build")
    rk.cmd_plan([MODULE_ID], snapshot_heads=False)
    assert f"## {MODULE_ID} — NEW" in capsys.readouterr().out
    assert rk.PLAN_PATH.read_text() == planned


def install_pinned(root: Path, rel: str, content: str) -> None:
    """Stand in for `uv sync`: put a pinned wheel's file into the fake Crucible's site-packages."""
    path: Path = root / "crucible" / ".venv" / "lib" / "python3.12" / "site-packages" / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def test_pin_differing_from_head_requires_pinned_section(
    upstream: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Crucible runs the pin, not HEAD: when they differ, a note with no pinned section passes off HEAD as the pin."""
    install_pinned(tmp_path, "pkg/core.py", "x = 0  # older pinned release\n")
    commit(upstream, "pkg/keys.py", "PIN_KEYS = True\n", "HEAD-only feature")
    out: str = plan(capsys)
    assert "1 changed, 1 HEAD-only" in out and "changed    pkg/core.py" in out and "HEAD-only  pkg/keys.py" in out
    write_note()
    with pytest.raises(rk.KnowledgeError, match="Pinned vs local"):
        rk.cmd_stamp([MODULE_ID])
    rk.note_path(MODULE_ID).write_text(rk.note_path(MODULE_ID).read_text() + "\n## Pinned vs local\n- keys: HEAD only\n")
    rk.cmd_stamp([MODULE_ID])


def test_pin_catching_up_with_head_rejects_leftover_section(
    upstream: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Once the pin equals HEAD, a leftover 'HEAD only' claim tells readers Crucible lacks code it now runs."""
    install_pinned(tmp_path, "pkg/core.py", "x = 1\n")
    plan(capsys)
    rk.note_path(MODULE_ID).parent.mkdir(parents=True, exist_ok=True)
    write_note()
    rk.note_path(MODULE_ID).write_text(rk.note_path(MODULE_ID).read_text() + "\n- keys **[HEAD only]**\n")
    with pytest.raises(rk.KnowledgeError, match="pin now matches HEAD"):
        rk.cmd_stamp([MODULE_ID])


def test_identical_pin_needs_no_pinned_section(upstream: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """When the pinned wheel is byte-identical to HEAD, every claim holds for the pin, so no section is demanded."""
    install_pinned(tmp_path, "pkg/core.py", "x = 1\n")
    assert "identical (1 files)" in plan(capsys)
    write_note()
    rk.cmd_stamp([MODULE_ID])


def test_pin_bump_restales_a_note_with_no_code_change(
    upstream: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A pin bump changes which claims hold for what Crucible runs, even though HEAD didn't move."""
    install_pinned(tmp_path, "pkg/core.py", "x = 1\n")
    build_once(capsys)
    assert "WORK LIST: (empty" in plan(capsys)
    install_pinned(tmp_path, "pkg/core.py", "x = 0\n")
    out: str = plan(capsys)
    assert f"## {MODULE_ID} — STALE" in out and "pinned-vs-HEAD comparison changed" in out


def test_uninstalled_module_is_reference_only(upstream: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A reference repo (inspect) isn't installed, so there is no pin to compare and nothing to demand."""
    assert "not installed in .venv" in plan(capsys)


def test_wheel_path_strips_through_last_src_dir() -> None:
    """Repo layouts differ; the wheel path is what's under the last `src/`, or the path itself for root packages."""
    assert rk.wheel_path("apps/aigateway/src/aigateway/x.py") == "aigateway/x.py"
    assert rk.wheel_path("syft/core.py") == "syft/core.py"
    assert rk.wheel_path("packages/a/src/pkg/src/y.py") == "y.py"


def test_scope_change_forces_rebuild(upstream: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A note written for an old focus answers the wrong questions, so a focus edit must rebuild it."""
    build_once(capsys)
    write_manifest(tmp_path, upstream, focus="a different question")
    assert f"## {MODULE_ID} — REBUILD" in plan(capsys)


def test_rewritten_history_forces_rebuild(upstream: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """If the stamped commit isn't in HEAD's history, a diff from it would lie, so rebuild instead."""
    commit(upstream, "pkg/core.py", "x = 9\n", "soon to be dropped")
    build_once(capsys)
    sh(upstream, "reset", "-q", "--hard", "HEAD~1")
    commit(upstream, "pkg/core.py", "x = 10\n", "rewritten")
    out: str = plan(capsys)
    assert f"## {MODULE_ID} — REBUILD" in out and "not an ancestor" in out


def test_stamp_requires_a_plan(upstream: Path) -> None:
    """Without a planned HEAD there's no honest commit to record, so stamping must refuse."""
    write_note()
    with pytest.raises(rk.KnowledgeError, match="plan"):
        rk.cmd_stamp([MODULE_ID])


def test_filter_by_repo_name(upstream: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """`update knowledge of demo` should mean every module in the demo repo, and nothing else."""
    rk.cmd_plan(["demo"])
    assert MODULE_ID in capsys.readouterr().out
    capsys.readouterr()
    planned: str = rk.PLAN_PATH.read_text()
    with pytest.raises(rk.KnowledgeError, match="Valid filters: demo"):
        rk.cmd_plan(["ref repos"])  # a mis-parsed phrase must fail loudly, not "succeed" doing nothing
    assert rk.PLAN_PATH.read_text() == planned  # ...and must not clobber the plan a build may be mid-way through


def test_drift_shows_local_version_against_pin(upstream: Path, tmp_path: Path) -> None:
    """Notes describe the local tree; the drift line is how a reader knows it's ahead of Crucible's pin."""
    commit(upstream, "pkg/pyproject.toml", '[project]\nname = "demo"\nversion = "0.2.0"\n', "bump")
    (tmp_path / "uv.lock").write_text('[[package]]\nname = "demo"\nversion = "0.1.0"\n')
    module: dict[str, Any] = {"version_file": "pkg/pyproject.toml", "dists": ["demo"]}
    assert rk.drift_text(upstream, module, rk.pinned_versions()) == "local 0.2.0 · pin demo==0.1.0"


def test_missing_clone_names_the_fix_and_keeps_the_plan(
    upstream: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A fresh checkout has no clones; the error must hand the caller the exact clone command to offer the user."""
    plan(capsys)
    planned: str = rk.PLAN_PATH.read_text()
    (tmp_path / "repos.local.toml").unlink()  # demo now expected at the empty default dir
    with pytest.raises(rk.KnowledgeError, match=r"repo not cloned: demo .*refs_knowledge\.py clone demo"):
        rk.cmd_plan([])
    assert rk.PLAN_PATH.read_text() == planned  # a build mid-way must still stamp against its snapshot


def test_filter_requires_only_the_repos_it_touches(
    upstream: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`update knowledge of demo` must not demand a clone of every other upstream first."""
    manifest: Path = tmp_path / "manifest.toml"
    manifest.write_text(
        manifest.read_text()
        + '[repos.ghost]\nurl = "https://example.invalid/ghost"\n'
        + '[[modules]]\nid = "ghost/x"\nrepo = "ghost"\npaths = ["x"]\nfocus = "x"\n'
    )
    rk.cmd_plan(["demo"])
    assert f"WORK LIST: {MODULE_ID}" in capsys.readouterr().out
    with pytest.raises(rk.KnowledgeError, match="repo not cloned: ghost") as exc:
        rk.cmd_plan([])
    assert "demo" not in str(exc.value)  # only the clone that is actually missing is named


def test_relative_local_path_resolves_from_crucible_root(
    upstream: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A committed-looking relative path like `../PySyft` must mean the same thing whatever the cwd is."""
    (tmp_path / "crucible").mkdir()
    (tmp_path / "repos.local.toml").write_text('demo = "../upstream"\n')
    assert rk.repo_path("demo") == tmp_path / "crucible" / ".." / "upstream"
    assert f"## {MODULE_ID} — NEW" in plan(capsys)


def test_non_string_local_path_is_rejected(upstream: Path, tmp_path: Path) -> None:
    """A hand-edited file is a boundary: a table where a path belongs must fail loudly, not resolve to garbage."""
    (tmp_path / "repos.local.toml").write_text("[demo]\npath = 1\n")
    with pytest.raises(rk.KnowledgeError, match="must be a path string"):
        rk.repo_path("demo")


def test_clone_fetches_missing_repo_into_default_dir(
    upstream: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Saying yes to the clone prompt must leave a checkout that `plan` accepts, and a second clone must be a no-op."""
    (tmp_path / "repos.local.toml").unlink()
    rk.cmd_clone(["demo"])
    assert (rk.DEFAULT_CLONE_DIR / "demo" / ".git").exists()
    assert f"## {MODULE_ID} — NEW" in plan(capsys)
    rk.cmd_clone(["demo"])
    assert "already cloned" in capsys.readouterr().out


def test_clone_rejects_unknown_repo(upstream: Path) -> None:
    """Only manifest repos have a trusted url; an arbitrary name must not reach `git clone`."""
    with pytest.raises(rk.KnowledgeError, match="unknown repo nope. Valid: demo"):
        rk.cmd_clone(["nope"])
