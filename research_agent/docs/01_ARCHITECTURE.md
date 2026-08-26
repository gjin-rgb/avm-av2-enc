# Architecture

`av2ra` is an autonomous research agent for the AV2 reference encoder. It
proposes encoder-side algorithmic changes, implements them in C, measures them,
decides what the measurements mean, and records what it learned — without a
human in the inner loop, and without being able to fake a result.

This document explains the shape of the system and, for each major decision,
why it is that shape. Almost every "why" traces to a specific failure in the
prior research on this codebase; where it does, the failure is named.

---

## 1. The one-paragraph version

A **planner** decides what to do next from the state of a durable **registry**.
An **ideator** proposes a falsifiable hypothesis through one of thirteen
**lenses**, grounded in a **code map** built from the actual tree and checked
against a **corpus** of everything already tried. An **implementer** turns the
hypothesis into a patch by exact search-and-replace, inside an isolated
**worktree**, and builds it. A **tier ladder** measures the patch, cheapest
proof first, with **integrity gates** at each rung. An **analyst** converts the
evidence into one of eight moves. Only what survives everything reaches the
**CTC back end**, the one scarce resource. Every step writes to the registry and
the **ledger**, so the run is resumable and the reasoning is auditable.

---

## 2. Layering

```
                        ┌──────────────────────────────────────────┐
   creative, fallible   │  agent/      ideation, patching, analysis │
                        │              portfolio planning           │
                        └───────────────────┬──────────────────────┘
                                            │  proposes / interprets
                        ┌───────────────────▼──────────────────────┐
   must be believed     │  measure/    encodes, BD-rate, statistics │
                        │  integrity/  the gates that make it true  │
                        └───────────────────┬──────────────────────┘
                                            │  reads / writes
                        ┌───────────────────▼──────────────────────┐
   durable truth        │  core/       registry, ledger, basewatch  │
                        └───────────────────┬──────────────────────┘
                                            │
                        ┌───────────────────▼──────────────────────┐
   swappable            │  providers/  storage, code, notify, health│
                        │  ctc/        the benchmark back end        │
                        └──────────────────────────────────────────┘

   knowledge/  code map · profiles · prior corpus · methodology rules
   buildkit/   worktrees · fingerprinted builds · artifact cache
   orchestrate/ leases · heartbeats · steering
   report/     experiment reports · leaderboard · frontier · digest · dashboard
   sim/        a synthetic encoder and cluster, so the whole loop is testable
```

**The layering is a trust boundary, not a taxonomy.** The agent layer is allowed
to be creative and therefore allowed to be wrong. The measurement and integrity
layers are not: they never call into the agent, they take no instruction from
it, and the agent cannot modify them (`av2ra/integrity/policy.py` makes
`research_agent/**` unwritable by any patch). A verdict is a function of
recorded evidence, computed in `measure/`, and re-derivable from the registry by
anyone who doubts it.

---

## 3. The infrastructure boundary

The requirement: **only data storage, code storage, and CTC runs may depend on
Google-internal infrastructure.**

That boundary is four interfaces in `providers/base.py` plus one in
`ctc/contract.py`:

| Interface | Sanctioned internal form | Local form |
|---|---|---|
| `BlobStore` | CNS via `fileutil` | a directory |
| `CodeStore` | google3 CitC client, or the shared GitHub repo | a git checkout, or nothing |
| `CtcBackend` | Cloud EDA via `kick_off_av2ctc_eda.sh` | a local encode farm, or the simulator |
| `Notifier` | `sendgmr` | a log file |
| `HealthProvider` | `gcertstatus` + quota | disk, tools, CPU |

The practical test that the boundary holds:

```
$ grep -rln 'fileutil\|gcertstatus\|sendgmr\|kick_off_av2ctc' av2ra/ --include='*.py'
av2ra/core/models.py          # a docstring saying the core must NOT name these
av2ra/ctc/eda.py              # the Cloud EDA back end
av2ra/providers/base.py       # docstrings describing the boundary
av2ra/providers/factory.py    # maps a config string to an implementation
av2ra/providers/google.py     # CNS, CitC, gcertstatus, sendgmr
```

Two files *invoke* internal tooling; one selects between implementations behind
lazy imports, so a machine without the tooling can still import the package; the
other two only mention the names in prose. That is the point: the science is
portable, and only the plumbing is not.

Interfaces are deliberately narrow. `BlobStore` exposes bytes and keys, never a
filesystem path — if it exposed a path, every caller would start joining paths
and the CNS implementation would have to fake a mount.

---

## 4. The evidence ladder

Each rung answers a question the rung below cannot and costs roughly ten times
as much.

| Tier | Question | Cost | Can it kill? |
|---|---|---|---|
| **T0** build | does it compile, encode, decode, reproduce? | minutes | yes |
| **T1** exactness | did it fire — and in the way it claimed? | free (same encodes) | yes |
| **T2** complexity | is the speedup real, above the noise floor? | minutes–hours | yes |
| **T3** screen | does the RD behaviour look sane; how do variants rank? | hours | only catastrophes |
| **T4** holdout | does the effect survive on clips it never saw? | hours | yes |
| **T5** CTC | does it clear the bar on every class? | hours–a day | decisive |

Three properties of this ladder are the whole design:

**T1 means opposite things depending on the declared mechanism.** For a `reuse`
or `kernel` patch, an identical bitstream is the *proof* that quality risk is
zero, and such a patch never needs a CTC quality round. For an `approx` patch,
an identical bitstream means it **never fired** — the experiment did not run.
Eight arms in the prior corpus were inert and were read as harmless. Same
comparison, opposite meaning, which is why `mechanism` is a required field on
every hypothesis rather than a label.

**T3 is deliberately not allowed to conclude much.** Local BD-rate on short
clips had the *wrong sign* against CTC on three of ten prior patches and
understated the cost 7× on a fourth. So T3 may kill a catastrophic regression,
prove activation, and rank variants — and may not quote a magnitude or trust a
sign. The restriction is in the code (`measure/tiers.py`) and in the text of
every tier result, so no report can quietly imply otherwise.

**T4 exists because the agent is allowed to look at the screening set.** The
holdout clips are never shown to ideation and never used to pick a threshold. A
gap between T3 and T4 is a measurement of overfitting rather than of the patch;
a sign flip between them blocks the experiment outright.

---

## 5. What makes a measurement believable

Five decisions, each traceable to a documented failure.

**Paired, interleaved, pinned.** Anchor and candidate are measured in the same
pass, adjacent in time, with the arm order flipped between repetitions, each
worker pinned to a disjoint CPU set. Encode cost varies by two orders of
magnitude across clips; pairing makes that a nuisance factor that cancels rather
than noise that swamps. `tests/test_numeric.py` contains the demonstration: a
3% effect that a paired design resolves and an unpaired one cannot.

**Intervals, not point estimates.** Every effect is an `Interval` with a 95%
bound. A verdict in `conservative` mode is derived from the pessimistic end. Six
of ten reported round-1 speedups in the prior corpus were smaller than the
3.47% their design could resolve.

**A ratio needs a resolved denominator.** If the BD-rate interval spans zero or
falls below the reporting floor, the ratio is `None`. A 101-feature study in the
corpus reported 21 passes of which 12 were divisions by a rounded `0.00%`.

**Instruction counts where available.** `perf stat -e instructions` is
reproducible to well under 0.1% and immune to co-tenancy; wall-derived metrics
on a shared machine have a ~2% floor. The harness picks the least noisy metric
available and says which one it used. The prior effort asked for this five times
and never ran it.

**The harness measures itself.** `run_null_arm` compares the anchor against the
anchor on a schedule. Every ratio is a quotient, and without this the
denominator's error bar is unknown — the corpus turned two decisions on
differences of 0.1 to 1.7 ratio points it had no basis to resolve, and six inert
arms implied a systematic ~1.6-point bias nobody had measured. Bias is reported,
never subtracted.

BD-rate itself is the AVM reference algorithm exactly: PCHIP on (quality,
log-rate), 100 samples over the overlap, trapezoid integration, with explicit
non-monotonic and no-overlap rejections. PCHIP is reimplemented in pure Python
and checked against scipy, so a Cloudtop, a laptop and a CI container produce
the same number.

---

## 6. Integrity

The system gives a language model write access to a C codebase, a build, a
measurement harness and a results database, then rewards it for producing a
number. Integrity is not a nice-to-have layer; it is what makes the output
mean anything.

**Static policy** (`integrity/policy.py`) — encoder sources are writable.
The harness, the CTC metric tooling, the test framework, the build files, the
decoder and the agent's own source are not. A diff scanner rejects benchmark
fitting (hard-coded 1920/1080, frame indices, CTC QPs, sequence names),
environment dependence, randomness, assertion suppression and per-function
optimisation pragmas. Any `-D` knob passed to a build must appear in the patch,
so a sweep cannot smuggle `-DNDEBUG` onto one arm.

**Build comparability** (`buildkit/builder.py`) — a fingerprint over toolchain,
CMake arguments, targets and generator. Two arms whose configurations differ
cannot be compared: `assert_comparable` raises. A patch's own defines are
excluded from that fingerprint and folded into source identity instead, so a
threshold sweep is several candidates against one anchor rather than several
incomparable experiments.

**Runtime gates** (`integrity/gates.py`) — activation, determinism,
conformance, the null arm, the Amdahl ceiling, holdout retention, and a power
check that distinguishes "no effect" from "no power". Blocking gates stop an
experiment; non-blocking ones annotate it.

**Provenance** (`core/registry.py`) — measurements are append-only and carry
base SHA, patch hash, build fingerprint, clip set, repetition count, metric
definition, back end and job id. Re-measuring adds a row; a disagreement
surfaces as a conflict rather than being resolved by whoever wrote last. The
prior corpus ended with two different sets of CTC numbers for the same three
patches and no way to tell which was right.

The threat model and every countermeasure are in
[`07_INTEGRITY_MODEL.md`](07_INTEGRITY_MODEL.md).

---

## 7. Where creativity comes from

A single "propose an optimisation" prompt collapses onto the same four ideas.
Three mechanisms keep the portfolio wide.

**Thirteen lenses** (`agent/lenses.py`), each asking a structurally different
question and requiring different evidence. Several exist because the corpus
identified them as the highest-value unexplored directions and never built them:
`decision_statistics` (shadow-mode regret measurement), `dispatch_audit` (the
one kernel attempt found a 2.45× kernel that was unreachable), `bundle_split`
(splitting a bundled feature produced the project's best result, ratio 207.7,
where every threshold move had failed), `consistency` (apply a policy the
codebase already declares at a site that ignores it).

A lens declares what evidence it needs and ideation **refuses to run it without
that evidence**. A "profile-driven" idea generated with no profile is the
generic prompt wearing a costume.

**Grounding.** Ideation is shown, and may only name, symbols from a code map
built by reading the tree — 6,313 functions on the reviewed commit. A near-miss
is repaired from the index (`search_txk_type` → `search_tx_type`); anything else
is rejected before an experiment exists.

**Portfolio governance** (`agent/planner.py`). Caps on open experiments, on
concentration in one subsystem, and on tuning-chain depth. The corpus produced
twenty-four consecutive patches against partition and transform search while
quantisation held 47% of the profile.

Lens selection is by shrunken success rate (beta-binomial with the documented
prior as a pseudo-count), preferring a new mechanism class before repeating one.
Without shrinkage one lucky result makes a lens look like a certainty.

---

## 8. Spending the scarce resource

Local CPU is abundant; a CTC round is hours to a day and is the only measurement
that ships. Every planning decision is really about which to spend.

Escalation requires a written case (`build_escalation_case`) that names the
question and lists the cheaper evidence already exhausted. The case is
**refused** when the speedup has not been resolved locally, when no holdout
measurement exists, or when the patch is bit-exact — a proven-exact patch needs
no quality round at all, and the corpus spent three rounds discovering that its
three bit-exact patches were merely slower at 4K.

Rounds are batched, because latency is set by the slowest arm and not by their
number. Partial results are polled, and a round showing no speedup on a class
after a third of its clips is aborted to return quota.

---

## 9. Durability

The loop is a state machine over the registry, not a long-running function.
Every tick reads state, does one unit of work, writes it durably, returns. A
reboot, a killed process or an expired certificate costs one tick.

Submitting a cluster round never blocks: the handle is recorded and the next
tick starts a fresh hypothesis in a fresh worktree, which is what a human
researcher does and what turns a fleet into more than a queue of one.

Coordination across machines is a TTL lease on the blob store and a per-node
heartbeat file. There is no scheduler and no central mutable state: a node pulls
what it can lease, so adding a machine is running the agent on it. A heartbeat
older than its interval is reported stale rather than believed.

---

## 10. Testability

The simulator (`sim/`) is not a convenience, it is what makes the claims
checkable. `SimulatedEncoder` satisfies the same contract as `avmenc`, so every
tier, gate and statistic runs against it unchanged — the code under test is the
production code and only the encoder is fake.

The synthetic world reproduces the awkward parts of the real one on purpose:
inert patches, class asymmetry, per-clip spread with a heavy tail, local/CTC
sign disagreement, a noise floor, and a small systematic bias on the patched arm.
A harness that only works on well-behaved data has not been tested.

`av2ra demo` runs the whole cycle in seconds and writes the same artifacts a
real run would. `tests/test_end_to_end.py` asserts on conclusions: that a good
patch traverses all six tiers and clears the bar, that a patch which
special-cases 1920×1080 is blocked before it is ever measured, that an inert
patch is recorded as a failed experiment, that a `reuse` patch which changes the
bitstream is called a bug, that a cluster round without a holdout measurement is
refused, and that a moved anchor halts the loop.

---

## 11. Module map

| Module | Responsibility |
|---|---|
| `util/` | atomic IO, subprocess with per-child rusage, logging, a YAML subset |
| `core/models.py` | the domain: hypothesis, patch, tier result, verdict, CTC schema |
| `core/registry.py` | append-only measurements with provenance; conflict detection |
| `core/ledger.py` | dated, append-only decisions; retractions marked, never erased |
| `core/basewatch.py` | anchor drift: textual rot, semantic overlap, upstream substitution |
| `measure/pchip.py` | monotone cubic interpolation, validated against scipy |
| `measure/bdrate.py` | BD-rate exactly as AVM CTC computes it |
| `measure/stats.py` | Student-t by continued fraction, paired log-ratio CIs, MDE, Holm |
| `measure/encode.py` | the CTC command, transcribed; output parsing; perf; decode check |
| `measure/screen.py` | interleaved paired passes, CPU pinning, quality-only anchor cache |
| `measure/acceptance.py` | quadrants, bars, conservative ratios, marginal ratio, exemptions |
| `measure/tiers.py` | the ladder |
| `integrity/` | patch policy, diff scanner, runtime gates |
| `buildkit/` | worktrees with asserted state; fingerprinted builds; artifact cache |
| `knowledge/` | code map, profiles, prior corpus, methodology rules, retrieval |
| `agent/` | LLM client, lenses, ideation, implementer, analyst, planner, loop |
| `providers/`, `ctc/` | the infrastructure boundary |
| `orchestrate/` | leases, heartbeats, steering |
| `report/` | experiment reports, leaderboard, frontier, digest, dashboard |
| `sim/` | synthetic encoder, effect model, simulated cluster, demo |

---

## 12. What this architecture does not do

Stated plainly, and expanded in [`10_LIMITS.md`](10_LIMITS.md):

- It does not make the local screen predictive of CTC. It makes the screen's
  unreliability explicit and refuses to let it decide quality.
- It does not remove the human from promotion. A patch that clears every bar is
  *recommended*; a person still reviews and submits it.
- It does not measure the cluster's repeatability for you. The null arm measures
  the *local* harness; the equivalent CTC arm has to be scheduled, and the
  planner will not do it unasked because it costs a round.
- It cannot judge perceptual quality. Every metric here is objective, and a
  change that helps PSNR while hurting appearance will pass.
