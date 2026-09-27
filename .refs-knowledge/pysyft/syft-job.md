---
module: pysyft/syft-job
summary: File-drop job submission for SyftBox — a data scientist's code lands in a data owner's inbox as plain files, gets approved, then runs as an unconfined `bash run.sh` subprocess; the kernel sandbox was built only on a side branch that `dev` never merged.
source: pysyft@1e14d632a9 (dev, 2026-09-25)
---
# pysyft/syft-job

## Mental model
A job is a folder, not a message. The data scientist's `submit_python_job`/`submit_bash_job`
writes `code/`, `run.sh` and `config.yaml` into the data owner's `inbox/`, and `syft`'s file
transport syncs it there. The runner's `scan_inbox` checks that folder's shape and writes a
`state.yaml` into `review/` (PENDING, or REJECTED if the layout is wrong). The code stays in
`inbox/`. Once the owner approves, `SyftJobRunner` runs `bash run.sh` from the inbox folder as a
plain `subprocess.Popen`, then copies `code/outputs` into `review/outputs` and deletes the inbox
copy. The whole lifecycle is files on disk (`JobState`, `JobStatus`) that a poller watches, with no
server or queue. Think of it as a mail slot and an in-tray, with a clerk who runs whatever the
approved envelope says, at the clerk's own desk and with the clerk's own keys. For Crucible this is
the layer under `syft-enclave`'s approval gate where code crosses the trust boundary and runs.

```mermaid
flowchart LR
  ds["👤 data scientist<br/>submit_python_job"]:::warn -- "code/, run.sh, config.yaml" --> inbox["🗄️ inbox/<br/>synced job folder"]:::data
  inbox -- "scan_inbox: state PENDING" --> owner["👤 data owner<br/>JobInfo.approve"]:::warn
  owner -- "state.yaml: APPROVED" --> runner["🤖 SyftJobRunner<br/>⚠️ bash run.sh: same uid,<br/>full env, network"]:::bad
  runner -- "stdout/stderr, outputs/" --> review["🗄️ review/<br/>read granted to owner"]:::data
  review -- "share_outputs (opt-in)" --> crucible["Crucible reads results"]:::good
  classDef bad   fill:#7f1d2b,stroke:#e5484d,color:#ffe8ea
  classDef good  fill:#14532d,stroke:#30a46c,color:#dcfce7
  classDef warn  fill:#78350f,stroke:#f5a524,color:#fef3c7
  classDef data  fill:#4c1d95,stroke:#a06ed4,color:#ede9fe
  classDef stage fill:#1e3a8a,stroke:#4a7fd4,color:#dbeafe
  classDef plain fill:#374151,stroke:#9ca3af,color:#f3f4f6
```

## Surface Crucible touches
- `JobClient.submit_python_job`/`submit_bash_job` (`client.py:376`, `:146`) are how a data
  scientist hands a job to a data owner. `EnclaveJobClient` wraps both
  (`packages/syft-enclave/src/syft_enclaves/enclave_job_client.py:35,48`).
- `JobClient.scan_inbox` → `receive_job` → `validate_submission` (`client.py:548-558`,
  `:509-546`, `:490-507`) accepts only `code/` + `run.sh` + `config.yaml` and writes PENDING or
  REJECTED.
- `SyftJobRunner._execute_job` (`job_runner.py:361-443`) dispatches to `_execute_job_streaming`
  (`:221-309`) or `_execute_job_captured` (`:311-359`). Both call `subprocess.Popen(["bash",
  str(run_script)], cwd=submission_dir, env=env, ...)` (`:254-261`, `:331-338`). `env` is
  `os.environ.copy()` plus four `SYFT*`/`PYTHONUNBUFFERED` keys (`:235-240`, `:324-329`).
- `JobStatus` (`models/job_state/v1.py:14-23`) has the values RECEIVED, PENDING, APPROVED,
  REJECTED, RUNNING, DONE and FAILED. RECEIVED is the status a job reports while no `state.yaml`
  exists (`job_runner.py:496-500`). `receive_job` writes PENDING or REJECTED straight away.
  `pysyft/syft-enclave`'s `EnclaveJobInfo` layers its multi-party approval over this enum.
- `JobInfo.approve`/`reject`/`accept_by_depositing_result`/`rerun`/`share_outputs`/`share_logs`
  (`job.py:194-384`) are the owner-side review API a Crucible reviewer UI would call.
- `get_job_timeout_seconds()` (`job_runner.py:22-29`) defaults to **600 s**
  (`DEFAULT_JOB_TIMEOUT_SECONDS`, `:19`) and can be overridden with
  `SYFT_DEFAULT_JOB_TIMEOUT_SECONDS`. On timeout, `_kill_process_tree` (`:35-48`) kills the process
  and every descendant through `psutil`. The docstrings on `_execute_job`/`process_approved_jobs`
  still say "300 (5 minutes)" (`:375`, `:530`). The code uses 600.

## Defaults & invariants that matter
- **No sandbox in `dev` HEAD.** Job code runs as a child of the runner, with the runner's uid,
  container, network namespace and full environment (`job_runner.py:235-261`). It can open sockets
  and read anything the runner can read, including bootstrap secrets in the environment.
  `git ls-files` at HEAD has no `sandbox` module. Crucible must not assume job code is
  network-isolated. The enclave runs jobs through this runner:
  `packages/syft-enclave/src/syft_enclaves/client.py:250-254` calls `syft_rds`'s
  `process_approved_jobs` with `force_execution=True` and both `share_*_with_submitter=True`, and
  `packages/syft-rds/src/syft_rds/client.py:128,469` builds the `SyftJobRunner` and delegates to it.
- **The sandbox exists only on `upstream/feat/enclave-job-sandbox`, which is not merged.** The
  branch has four feature commits (32a366f713..d38557d5ee, 2026-08-03, forked from `540e034cc7`)
  and one merge of `dev` into the branch (tip `a21437f98b`, 2026-08-26). Its tip is not an ancestor
  of `dev` HEAD (`git merge-base --is-ancestor` fails). The report that PR #9485 was closed without
  merging on 2026-09-08 can't be confirmed offline: git shows only that the branch never reached
  `dev`. What the branch contains (`git show a21437f98b:<path>`):
  - `sandbox.py`: `apply_lockdown(uid=65534, gid=65534, strict=True)` does `setgroups([])` →
    `setgid` → `setuid` → `PR_SET_NO_NEW_PRIVS` → a seccomp BPF filter denying `socket`,
    `socketpair`, `io_uring_setup`, `io_uring_enter`, `ptrace`, `process_vm_readv` and
    `process_vm_writev` (`sandbox.py:85-91`, `:192-258`). It fails closed, because any failure
    before the final `execvp` exits with `REFUSED_EXIT_CODE=93` and sentinel `SYFT_SANDBOX_REFUSED`
    (`:44-47`, `:260-304`). Only with `strict=False`, when the process isn't root, does it keep the
    caller's uid and still install the filter (`:228-238`). The filter blocks socket creation, not
    `connect`, and it is Linux/x86-64 only (`is_supported()`, `:176-184`).
  - `job_runner.py` on the branch does wire it in. `SYFT_JOB_SANDBOX` is `off` (the default),
    `on` (use the sandbox if supported, otherwise warn and run unsandboxed via `--best-effort`) or
    `require` (refuse if unsupported). The sandboxed job gets an allowlisted environment instead of
    the runner's, and Python jobs install dependencies in a separate unsandboxed phase
    (`job_runner.py:33-150` on the branch). The branch's enclave `Dockerfile` adds a `syftjob`
    user (uid 1500) and lets `SYFT_JOB_SANDBOX*` be overridden from `tee-env` metadata, but it
    doesn't turn the sandbox on by default.
- **Outputs are copied from `code/outputs/` into `review/outputs/`, then the inbox copy is
  deleted** (`_move_outputs_to_review`, `job_runner.py:452-466`). `_prepare_outputs_dir` grants
  read on `review/outputs/` to the data owner only (`:468-488`). The submitter gets the outputs
  only through `share_outputs`/`share_logs` (`job.py:369-384`), which `process_approved_jobs`
  calls only when `share_outputs_with_submitter`/`share_logs_with_submitter=True`
  (`job_runner.py:518-559`, `:573-582`). Both default to `False`.
- **Released job-object schemas are frozen by process.** `README.md:8-29` requires running
  `export_release_artifact.py` and `generate_release_fixture.py` on every release. The export
  script "refuses to run if the job protocol changed without bumping `JOB_PROTOCOL_VERSION`". The
  committed fixtures (`tests/migrations/p2p/fixtures/`) are replayed by
  `test_older_protocol_compatibility.py`.

## Watch list
- `upstream/feat/enclave-job-sandbox` is the only code that gives job execution a kernel sandbox,
  and it never reached `dev` (the reported PR closure is unconfirmed offline). Crucible's
  zero-egress claim for job execution has no upstream support today. Either Crucible ships its own
  confinement around `SyftJobRunner`, or a successor PR has to land. Re-check on every refresh.
- If a successor lands, confirm that the enclave sets `SYFT_JOB_SANDBOX=require` rather than
  relying on the `off` default, and that `on` mode's silent fallback to unsandboxed can't happen
  in production.
