# Implementation plan

This is the plan the system in this repository was built to, with each phase's
status and what remains. It replaces the four-phase roadmap in the draft guide,
which sequenced fleet operations before the research engine and before anything
that makes a result believable.

The ordering principle: **build the thing that decides a result is real before
the thing that produces results.** An agent that can generate a hundred patches
and cannot tell which of them worked produces a hundred unresolved questions.

---

## Status summary

| Phase | Scope | Status |
|---|---|---|
| 0 | Foundations: config, domain model, durable state | complete |
| 1 | Measurement: BD-rate, statistics, encodes, the ladder | complete, validated against the AVM reference |
| 2 | Integrity: policy, gates, provenance | complete, attacks covered by tests |
| 3 | Knowledge: code map, profiles, corpus, rules | complete, running against the real tree and corpus |
| 4 | The research loop: lenses, ideation, patching, analysis, planning | complete, end-to-end in simulation |
| 5 | Infrastructure boundary: providers, CTC back ends | complete; EDA path needs a Cloudtop to exercise |
| 6 | Operations: CLI, fleet, reports, dashboard | complete |
| 7 | Simulation and tests | complete: 85 tests |
| 8 | First real deployment | **not started — needs a Cloudtop, an encoder build, and clips** |
| 9 | Calibration and tuning from real results | **not started — depends on 8** |

Everything through phase 7 runs here and is covered by the test suite.
Phases 8 and 9 are the parts that cannot be done without the real environment,
and they are described below in the detail an operator needs to execute them.

---

## Phase 0 — Foundations *(complete)*

**Delivered.** Layered configuration with site profiles
(`config/default.yaml` + `config/sites/{local,sim,google}.yaml`); the domain
model (`core/models.py`); a SQLite registry with append-only measurements; an
append-only decision ledger; atomic IO; subprocess execution with per-child
`rusage`; a YAML subset parser so a missing PyYAML cannot stop a run.

**Decisions worth recording.**

- Verdicts are computed from evidence, never assigned. `Experiment.verdict`
  moves only through `apply_tier_result`.
- Measurements are append-only. Re-measuring adds a row and a disagreement
  becomes a visible conflict.
- `os.wait4` per child rather than `RUSAGE_CHILDREN` deltas — the harness runs
  encodes concurrently, and a running total would be corrupted by whichever
  sibling exited in between. That bug does not crash anything; it just makes the
  timings quietly wrong.

---

## Phase 1 — Measurement *(complete)*

**Delivered.** PCHIP in pure Python, validated against scipy to 1e-10 on 200
random cases. BD-rate reproducing `CalcBDRate.py` exactly, validated to 1e-6 on
200 random curve pairs. Student-t quantiles by continued fraction (no scipy
dependency, no tabulated-df rounding). Paired log-ratio intervals, MDE,
required-reps, Holm–Bonferroni. The CTC encode command transcribed from
`VideoEncoder.py`. Interleaved paired screening with CPU pinning. The tier
ladder and the acceptance engine.

**Validation.** The acceptance engine reproduces the recorded CTC verdicts of
six prior patches (i09b ratio 26.7 pass, i06 18.7 fail, i01 3.5 fail, 0021 62.0
pass at speed 1, …) and refuses the two "infinite ratio" artifacts the corpus
recorded as wins.

**Deliberate divergences from prior practice**, each documented in the code:

- A speedup below the reporting floor produces no ratio at all. i07 was recorded
  as ratio 0.7 from a 0.22% speedup; a 0.22% encode-time difference is below any
  repeatability the cluster has been shown to have.
- The absolute BD-rate cap warns by default rather than rejecting. The cap's real
  purpose is that BD-rate costs *add* when patches combine; a ratio of 75 bought
  at 0.3% BD-rate is a good patch that does not stack, not a failure.

---

## Phase 2 — Integrity *(complete)*

**Delivered.** Path policy, diff scanner, define checking, mechanism
cross-check, build-comparability assertion, and the runtime gates: activation,
determinism, conformance, null arm, holdout retention, Amdahl, power.

**Validation.** `tests/test_integrity.py` and `tests/test_end_to_end.py` attempt
the attacks and assert they are stopped: a patch that special-cases 1920×1080 is
blocked before it is built; a `reuse` patch that changes the bitstream is
recorded as `DIVERGES`; an inert patch is recorded as `NO_EFFECT` with the
analyst's move set to `instrument`.

**Known gap.** The conformance gate decodes bitstreams and checks the decoder
accepts them. It does not check the *decoded pixels* against a reference decoder
build, which would catch a class of bug where a stream decodes without error but
to different pixels. Doing that needs a second decoder build and is listed in
phase 9.

---

## Phase 3 — Knowledge *(complete)*

**Delivered.** A code map built by reading the tree (6,313 functions, 542 files,
330 speed-feature knobs on the reviewed commit), with symbol resolution and
near-miss repair. Profile parsing for callgrind and perf, with SIMD kernels
attributed to the algorithm they implement rather than to a "simd" bucket.
Corpus ingestion producing structured results (66 attempts, per-patch numbers,
verdicts, structural overlap by touched function). Thirty machine-readable
methodology rules, eighteen enforced by named modules. Retrieval that keeps the
cacheable prefix byte-stable.

**Validation.** Running against the real tree and the real corpus in
`tests/test_knowledge.py`: the code map rejects `search_txk_type` and suggests
`search_tx_type`; the corpus reproduces i09b's recorded ratio of 26.7 and keeps
the sign of the negative speedups; the profile reproduces the corpus's headline
finding that quantisation exceeds 25% of instructions retired.

---

## Phase 4 — The research loop *(complete)*

**Delivered.** Thirteen lenses with declared evidence requirements. Ideation
with grounding, novelty and Amdahl filters. An implementer using exact
search-and-replace with a bounded build-repair loop. An analyst with a fixed
eight-move vocabulary, marginal-ratio decomposition, exemption analysis and
prediction scoring. A planner with governor limits and written escalation cases.
A resumable tick-based loop.

**Design note.** The analyst derives its move mechanically first and only then
asks the model to refine it. A move that follows from terminal evidence — a kill
on a build failure, a promote on a result that cleared every class — cannot be
overturned by the model; the disagreement is recorded instead. A model asked to
reconsider a fact will find a reason to.

---

## Phase 5 — Infrastructure boundary *(complete; EDA untested against a live cluster)*

**Delivered.** `BlobStore`, `CodeStore`, `Notifier`, `HealthProvider`,
`CtcBackend`. Local implementations for all five. Google implementations for
CNS, CitC, `sendgmr` and `gcertstatus`. The EDA back end with per-class frame
counts, anchor reuse, arm-identity records, partial polling and comparison
parsing for both the table and CSV shapes.

**What phase 8 must verify on a Cloudtop.**

1. The exact stdout of `kick_off_av2ctc_eda.sh` — the invocation UUID is
   extracted by regex and the result folder by a looser one. If either differs,
   `EdaBackend._kick_off` needs a one-line change.
2. The exact output of `compare_eda_runs.py --csv=1`. `parse_comparison` handles
   the pipe-table and CSV shapes seen in the corpus's reports; a third shape
   needs a column-alias entry in `METRIC_ALIASES`.
3. Whether `--partial=4` returns the shape the poller expects.
4. Whether the CNS lease's read-back is sufficient in practice. It is advisory:
   `fileutil` exposes no compare-and-swap, and the window is a round trip. Point
   `coordination_root` at an NFS home directory for a hard lock if it matters.

---

## Phase 6 — Operations *(complete)*

**Delivered.** Nineteen CLI commands, all of which do the work. Fleet
coordination by TTL lease and heartbeat, with staleness reported rather than
believed. Experiment reports, leaderboard, promotion frontier, digest, and a
self-contained HTML dashboard.

---

## Phase 7 — Simulation and tests *(complete)*

**Delivered.** A synthetic encoder satisfying the real encode contract, an
effect model reproducing the corpus's awkward phenomena, a simulated cluster
with partial results, `av2ra demo`, and 85 tests.

---

## Phase 8 — First real deployment *(not started)*

This is the phase that needs a Cloudtop. In order:

**8.1 Bring up the environment.**

```bash
av2ra --site=google init
av2ra --site=google bootstrap        # clone/align ~/av2/avm to origin/av2-enc
av2ra --site=google doctor           # LOAS, disk, clips, encoder, anchor
```

Fill the `REQUIRED` fields in `config/sites/google.yaml` first. `doctor` names
what is missing rather than failing later.

**8.2 Build the anchor and calibrate the real cost.**

Build `avmenc` at the anchor and time one encode of one screening clip at the
target preset. **Write the measured number into
`config/sites/google.yaml`** as a comment. Everything downstream — how many
repetitions are affordable, how many clips a screen can carry, whether T3 runs
at all — follows from it, and the draft guide's estimate was wrong by two to
three orders of magnitude.

**8.3 Seed the knowledge base.**

```bash
av2ra --site=google ingest                                   # code map + corpus
av2ra --site=google profile --import <callgrind-output> --preset=<target>
```

A profile is not optional. Without one, `profile_hotspot`, `reuse_hoist`,
`kernel_simd` and `proxy_substitution` refuse to run, and the Amdahl gate cannot
reject an impossible claim.

**8.4 Run the null arm before anything else.**

The cheapest high-value measurement available and the one the prior effort asked
for four times without running. Locally it is automatic
(`measure.null_arm_every`). On the cluster it is one round: submit the same
commit twice as two arms. Until that number exists, every CTC ratio has an
unknown denominator error.

**8.5 One supervised experiment.**

Run `av2ra run --ticks=1` repeatedly, reading each report, until one experiment
has traversed T0–T4. Verify by hand: the patch is what the hypothesis described;
the anchor and candidate binaries have the same config fingerprint; the
activation gate says the patch fired; the speedup interval excludes zero.

**8.6 One supervised CTC round.**

Let the planner escalate, read the escalation case before it submits, and check
the parsed `CtcResult` against the raw comparison output. This is where the two
parsing assumptions in 5.1–5.2 are confirmed or fixed.

**8.7 Unsupervised operation.**

`av2ra run --ticks=50`, with `av2ra digest --send` on a daily schedule and
`av2ra status` whenever you want to look.

**Exit criteria.** Ten experiments completed, at least one CTC round parsed
correctly end to end, the null-arm number recorded, and no measurement conflicts
in `av2ra registry conflicts`.

---

## Phase 9 — Calibration *(not started; depends on 8)*

Once real results exist, the parts that can only be tuned from them:

1. **Screen-to-CTC calibration.** For every experiment with both a T3 and a T5
   result, record the pair. After ten, the correlation is measurable and the
   screening set can be judged: if it does not rank CTC outcomes, change the
   clips or the frame count rather than trusting it harder. This is the single
   highest-value thing to do after deployment, and it needs a
   `av2ra calibrate` command that does not exist yet.
2. **Lens priors.** `LensStats` shrinks toward a documented prior. After ~30
   experiments the priors should be replaced with measured rates.
3. **Prediction calibration.** The analyst scores every prediction. After enough
   of them, a systematic magnitude bias should be fed back into the ideation
   prompt.
4. **Decoded-pixel conformance.** Extend the conformance gate to compare decoded
   output against a reference decoder build.
5. **The shadow-mode tier.** `decision_statistics` currently proposes
   instrumentation as an ordinary experiment. The corpus's full design — run the
   baseline search while recording what the heuristic would have chosen, giving
   per-site regret with zero run-to-run variance — deserves to be a tier of its
   own between T2 and T3.
6. **Combination rounds.** The corpus's only composition rule is "speedups
   compound multiplicatively, BD-rate adds", validated once. With a promotion
   frontier and the `reject` cap mode, combination arms can be assembled
   properly.

---

## What would make this project fail

Named so they can be watched for:

- **The screen never correlates with CTC.** Then local screening is only a
  crash-and-activation filter, and every quality question costs a cluster round.
  The system still works; its throughput drops by an order of magnitude.
  Phase 9.1 is what detects this, and it should be done early.
- **Ideation stays generic.** Grounding and lenses raise the floor; they do not
  guarantee insight. The measurable symptom is proposals clustering in one or
  two subsystems despite the concentration cap, and the response is to write
  better lenses, not to loosen the filters.
- **The cluster's repeatability is worse than the effects being hunted.** The
  corpus contains circumstantial evidence for this and no measurement. Phase 8.4
  is what settles it. If it is true, the bar structure itself needs revisiting —
  and knowing that is worth more than another twenty patches.
