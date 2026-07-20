# Crucible — architecture & design

**A trust-minimized audit framework for language models: model weights, a private benchmark, and auditor eval code meet only inside an attested hardware enclave — and the only thing that ever leaves is a signed scorecard.**

---

## 1. The problem — three parties, three secrets

Three people walk into a negotiation, and each one is hiding something.

- An **AI lab** has a model it does not need to open-source
- A **(Vietnamese) safety org** has a benchmark of local harms — real scam scripts, real medical misinformation — that it can't publish (publishing it hands labs the answer key, and some of it is dangerous to ship)
- An **auditor** — think regulator, or a central bank buying an AI system — just wants one number: *is this model safe in Vietnamese?*

Nobody will hand their secret to anybody else. The lab won't ship weights. The org won't reveal prompts / expected answers. Furthermore, if the lab *did* see the prompts, it could train on them and ace the test — so even a well-meaning handover ruins the benchmark forever.

![The three-party standoff](diagrams/01-three-party-standoff.drawio.png)

Crucible answers the question in the diamond: the three secrets meet **inside a sealed computer** — a hardware enclave nobody can log into — which runs the eval and emits exactly one thing: a signed scorecard.

Why a system and not just a study: the mechanism is what makes the benchmark *runnable at all*. A hazardous local-harms set is precisely the benchmark you can't hand to the lab — remove the enclave and the eval collapses into either "trust me" or "leak the test."

---

## 2. Where Crucible comes from — Blindfold

**Crucible** is the successor to [**Blindfold**](https://github.com/khoaguin/blindfold), built for the **Global South AI Safety Hackathon 2026** (Apart Research × AnToàn.AI, Ho Chi Minh City), where it placed in the **top 10% of 217 submissions**. Blindfold shipped:

- A three-party blind-audit harness on OpenMined's [syft-client](https://github.com/OpenMined/syft-client) (now in [PySyft](https://github.com/OpenMined/PySyft)) compute-to-data flow, in two byte-identical configs: an in-memory notebook demo and a real **GCP Confidential Space** deployment (AMD SEV, no operator login) with hardware remote attestation and Google Drive as the untrusted, E2E-encrypted transport.
- A **47-prompt bilingual EN↔VN benchmark** — scam (8) · medical (8) · jailbreak (26, MultiJail) · benign over-refusal controls (5) — every harmful prompt citing a real Vietnamese source (the AIS catalogue of online fraud forms, Ministry of Health misinformation warnings).
- **The finding**, across 4 Vietnamese-capable models (`seallm-v3-7b`, `qwen2.5-3b`, `qwen2.5-0.5b`, `phogpt-4b`): `phogpt-4b`, the Vietnamese-*specialized* model, is the *least safe in Vietnamese* — refusing **14% of harmful VN prompts vs 38% in EN**, the worst gap of the four. An English-only audit misses it entirely.

Hackathon reviewers converged on one direction: **the architecture is the primary contribution; the measurement is the proof-of-concept**. They asked for a scaled benchmark with seeds and variance, a validated (not heuristic) refusal judge, an explicit attestation claim set with failure modes, and a threat model for malicious eval code. Crucible is the answer to that list.

---

## 3. What Crucible claims

**Primary claim (systems):** Crucible is a practical N-party trust-minimized audit framework for language models: model weights, a private benchmark, and auditor eval code meet only inside an attested hardware enclave; every asset owner must approve the eval code before it runs; the only thing that ever leaves is a fixed-schema, signed scorecard.

**Case study (measurement):** a scaled, statistically defensible version of the Blindfold finding — *language-specialized models can be least safe in their own language* — plus showcase audits of 2–3 **multi-model ensembles** whose member models belong to different owners.

| Decision | Choice |
|---|---|
| Framing | Systems/architecture first; measurement is the evaluation case study |
| Enclave infra | PySyft `syft-enclave` (GCP Confidential Space, EAT-JWT attestation) |
| Eval decomposition | Four plugin seams (`load → run → grade → aggregate`), patterned on `inspect_ai`'s proven solver/scorer decomposition |
| Audit targets | 6–10 single models + 2–3 ensemble showcases (each 2–3 private models, distinct owners) |
| Deployment | CPU CVM for the full suite; one confidential-GPU (H100 CC) demo run; code autodetects CPU / CUDA / Apple Metal |
| Security depth | Full threat model; cheap channels mitigated; timing side-channels analyzed and declared as residual risk |

---

## 4. The ground we stand on

Crucible is deliberately a *consumer*, not a platform. What it builds on:

| Layer | Component | Visibility | What it provides |
|---|---|---|---|
| **Trust** | PySyft [`packages/syft-enclave`](https://github.com/OpenMined/PySyft) | open source | Confidential Space terraform, EAT-JWT attestation, N-party unanimous approval, sandboxed job runner. Working alpha with an end-to-end demo. One gap found and fixed upstream: the container image digest was not pinned in attestation verification — [PySyft #9454](https://github.com/OpenMined/PySyft/pull/9454) (fail-closed digest pinning). |
| **Execution** | OpenMined model-execution SDK | pre-release, closed | A gateway that routes model calls; Crucible deploys it entirely *inside* the enclave, routed only to local model servers — zero external egress, so prompts can never leak to a provider API. |
| **Benchmarking** | OpenMined benchmarking platform | pre-release, closed | The four-seam eval API Crucible plugs into. The seam taxonomy follows inspect_ai — whose 137-eval [inspect_evals](https://github.com/UKGovernmentBEIS/inspect_evals) suite is our public reference that the decomposition holds. |
| **Transport** | PySyft `syft-client` (the standalone [syft-client](https://github.com/OpenMined/syft-client) repo is archived — code and history merged into [PySyft](https://github.com/OpenMined/PySyft)) | open source | Compute-to-data file sync; transport (e.g. Google Drive) is untrusted by design — everything E2E-encrypted and signed. |
| **Inference** | llama.cpp / vLLM / MLX | open source | One model-server interface, backend autodetected (CPU / CUDA / Apple Metal). |

A thin compatibility shim pins the benchmarking platform's API contract, so Crucible's plugins survive pre-release API drift.

---

## 5. The architecture

One picture. Everything above the green line is the untrusted world; everything below it runs inside a hardware-attested Confidential Space VM.

![Architecture layers](diagrams/02-architecture-layers.drawio.png)

Read the enclave stack top-down, like tracing a call:

1. **crucible · audit layer** — the only genuinely new abstractions, and deliberately few:
   - `AuditJob` — who the parties are, which assets they stake, which eval runs, what the consent policy is.
   - `Scorecard` — a **fixed schema** (per-category refusal rates, EN↔VN gap, over-refusal rate, CIs, run metadata). This is a security control, not a convenience: it is the *only* channel out of the enclave, so a fixed, numeric, length-bounded schema is the cheap answer to "malicious eval code exfiltrates the weights through the output."
   - `crucible verify` — an offline CLI any party runs against the scorecard + cert: checks Google's signature on the EAT JWT, secure-boot and debug-disabled claims, the **pinned image digest**, the eval-code hash both owners approved, and the scorecard hash. The attestation claim set, as executable code.
2. **eval-harness seams** — `load → run → grade → aggregate`, inspect_ai-style. Crucible registers plugins here (§6) and writes *no* orchestration of its own.
3. **execution layer** — the auditor's eval code doesn't call models directly; a routing gateway dispatches every call to in-enclave model servers only. Auditing one model and auditing an ensemble are the same code path.
4. **model server** — llama.cpp / vLLM / MLX behind one interface, autodetected: CPU (CVM main runs), CUDA (H100 CC demo), Apple Metal (every developer's laptop). Same audit code on all three; only wall-clock changes.
5. **syft-enclave runtime** — the trust machinery, consumed as-is: per-owner approval files (unanimous or no run), attestation publishing, sandboxed execution.

**The deletion test** (why the audit layer deserves to exist): delete it, and every future consumer of this stack re-invents party policy, output sealing, and claim verification — scattered and probably wrong. Keep it, and the layers below stay clean of audit concerns.

---

## 6. One audit, step by step

![One audit, in sequence](diagrams/03-one-audit-sequence.drawio.png)

The two moments that carry all the trust, in plain words:

- **Before anything is uploaded** (steps 1–2), each data owner verifies the enclave's attestation: *"Google's hardware signs that this exact container image, with debugging disabled, generated these encryption keys."* Only then do secrets flow — encrypted to keys that provably live inside the sealed VM.
- **Before anything runs** (steps 4–5), both owners read the auditor's eval code (they see the *code* and *mock* data samples — never each other's private assets). Either can veto. The lab therefore never sees the prompts it's being tested on, and the org never sees the weights: **non-gameable by construction, not by promise.**

Then the run happens where nobody can watch (step 6), and one signed artifact leaves (step 7), which anyone can verify forever (step 8).

### The four seams, concretely

| Seam | Crucible's implementation |
|---|---|
| `loader` | VN harms benchmark with a **mock/private split** (reviewers see 5 mock rows; the enclave sees all ~150) |
| `runner` | In-enclave runner: seeds, temperature, N samples per prompt |
| `grader` | Refusal grader: LLM judge (runs off the private boundary — no API key ever enters the enclave) + keyword fallback, **validated against human labels on a subset, reporting Cohen's κ** |
| `aggregator` | Per-category refusal rates, EN↔VN gap with bootstrap CIs, over-refusal rate |

---

## 7. Threat model — who can cheat, and what stops them

Scope: **analyze everything, mitigate the cheap channels, declare the rest honestly.**

| Adversary | Attack | Defense | Status |
|---|---|---|---|
| Lab | Sees benchmark → trains on it / special-cases answers | Prompts only enter sealed memory; lab reviews eval *code* + mock rows only | Mitigated (core design) |
| Lab | Submits a different model than audited | Weights hashed in scorecard metadata; attestation binds run to inputs | Mitigated |
| Benchmark org | Learns weights | Same sealing, symmetric | Mitigated (core design) |
| Auditor (malicious eval code) | Exfiltrates weights/prompts via output | **Fixed-schema numeric Scorecard, length-bounded — the only exit**; both owners review the code; sandbox blocks network egress | Mitigated (cheap) |
| Auditor | Exfiltrates via timing / controlled-token side channels | Analyzed; residual-risk statement with a leakage estimate (bits per run through a k-field numeric schema) | **Declared residual** |
| Infra operator (us, GCP insider) | Logs into VM / swaps container image | Confidential Space: no SSH, debug-disabled attested; **image digest pinned** ([PySyft #9454](https://github.com/OpenMined/PySyft/pull/9454)); JWT-signer-rotation and digest-divergence failure modes specified in the claim set | Mitigated after PR |
| Transport (Google Drive) | Reads / tampers files | E2E encryption + signatures; transport is untrusted by design | Mitigated (syft-client) |

The digest-pinning PR is the keystone: without it, "attested container" means "some container Google booted." It's also the shape of Crucible's second job — being the stack's first hostile-environment consumer, and feeding what it finds back upstream.

---

## 8. Evaluation plan

**E1 — Main finding (the case study).** ~100–150 prompts (scale the authored scam + medical sets, keep the MultiJail backbone and benign over-refusal controls), EN + VN pairs, per-prompt provenance. 6–10 models: qwen2.5 (0.5b, 3b), phogpt-4b, seallm-v3-7b, vistral, gemma-2/3, llama-3.x, + candidates. 3 seeds; mean ± CI; bootstrapped EN↔VN gap. Judge validated vs human labels on a ~100-response subset, κ reported.

**E2 — Ensemble showcases.** 2–3 ensembles, each of 2–3 models with weights from distinct owners (up to 4 parties per audit), audited as single units through the identical pipeline, varying composition across ensembles (e.g. size-mix vs specialization-mix) — the first blind audits of composite AI systems, demonstrating N-party generality beyond a single configuration.

**E3 — Systems evaluation.** TEE overhead: same audit native vs CVM (tokens/s, end-to-end wall-clock, enclave boot + attestation latency); backend portability (CPU / H100 CC / Apple Metal — same code, three chips); one full-suite attested Confidential Space run.

**E4 — Verification & failure drills.** `crucible verify` against: a good run, a tampered scorecard, a wrong-image enclave, a vetoed job. Security claims demonstrated, not asserted.

---

## 9. Future work — living benchmarks

Everything above treats the benchmark as a *file*: the safety org hand-authors ~150 prompts once, and that CSV is the asset. That inherits the oldest problem in evaluation: **benchmarks freeze on publication day** — and a static private benchmark leaks a few bits with every published scorecard.

The forward path is a benchmark that **regenerates** on the data owner's side: the org's private collection (fraud advisories, case reports) grows as the world moves, a generation pipeline emits `benchmark@v(N+1)` without any version ever being published, and Crucible's `loader` seam resolves a versioned remote reference instead of a local file. The Scorecard grows provenance (benchmark version, pipeline hash), turning a one-off snapshot into a **safety trend**: *did the model get safer on this quarter's scams, or just on last year's?* Regeneration out-runs scorecard leakage; no version was ever public, so no model has trained on any of them. This lands as one loader-plugin swap — the seam architecture paying rent — and is deliberately out of scope for v1.

---

## Appendix — glossary

| Term | Meaning |
|---|---|
| **DO / DS** | Data Owner (lab, safety org) / Data Scientist (auditor) — PySyft party roles |
| **CVM** | Confidential VM — GCP Confidential Space instance (AMD SEV: encrypted RAM, no operator login) |
| **EAT JWT** | Entity Attestation Token — Google-signed JWT proving what image runs, with what config, on what hardware |
| **AuditJob** | Crucible's unit of work: parties + staked assets + eval code + consent policy |
| **Scorecard** | The fixed-schema signed result — the enclave's only output channel |
| **`crucible verify`** | Offline CLI that re-checks a scorecard's full attestation claim set |
