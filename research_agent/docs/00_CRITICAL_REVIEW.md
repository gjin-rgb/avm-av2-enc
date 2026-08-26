# Critical review of the draft AV2 agentic R&D guides

**Reviewed:**

- `av2_internal_agentic_rd_system_implementation_guide.md` (the implementation guide)
- `av2_agentic_system_user_guide.md` (the user guide)
- `AV2_Encoder_Deep_Architecture_Study_5d628d840.md` (the architecture study)
- `research_agent/av2_research_agent/` (the phase 0 / phase 1 code)

**Method.** Every claim that could be checked was checked against something
executable: the AVM tree at `/home/user/avm`, a release build of `avmenc`
produced for this review, the CTC tooling in
`tools/convexhull_framework/src/`, and the prior research corpus in
`chengchen-google/avm-patches`. Where a number appears below, it was measured
or read out of a file, not recalled.

---

## Summary

The drafts get the *operational* layer substantially right and the *scientific*
layer substantially wrong, and the code implements neither.

What the guides describe well is a fleet: how to onboard a Cloudtop, how to keep
credentials alive, how to keep a human in the loop, how to route work to a
cluster. That framing is correct and this project keeps most of it.

What the guides do not describe is the research: how a hypothesis is generated,
what evidence is required before spending anything, what makes a measured
difference believable, or what stops an autonomous optimiser from optimising the
measurement instead of the encoder. The one part of the scientific layer that is
specified in detail — the local screening protocol and the four-quadrant
acceptance matrix — is specified in a way that the prior research on this exact
codebase already demonstrated does not work.

The phase 0/1 code is a demonstration of a CLI, not an implementation of the
guide. Nine of its commands print fixed strings.

The most valuable document in the whole package is not any of the three guides.
It is the prior research corpus the guides treat as an input to be keyword-scanned:
`Claude/experiments/DECISIONS.md`, `registry.csv`, `bin/check-rot.sh` and
`experiments/bin/screen_timing.py` contain a hard-won methodology that the guides
do not incorporate and the demo code discards.

---

## 1. Blockers

### B1. The screening protocol's encoder command does not run

`§6.2` specifies `--cq-level=<qp>`. This encoder has no such flag:

```
$ avmenc --help | grep -c -- --cq-level
0
$ avmenc --help | grep -c -- --qp
1
```

CTC 2.0 and later pass `--qp`; `--cq-level` is the pre-2.0 spelling and also the
libaom spelling. `tools/convexhull_framework/src/VideoEncoder.py:80-83` confirms
the version split.

The same section calls the binaries `./aomenc` and `aomdec`. This codebase builds
`avmenc` and `avmdec`; `ninja aomenc` fails with `unknown target 'aomenc', did
you mean 'avmenc'?`. The prior corpus's own `bitexact.sh` uses `build/avmenc`,
so the guide is inconsistent with working tooling that already exists.

The command is also missing `-w`, `-h`, `--fps` and `--input-bit-depth`, which
`VideoEncoder.py` passes on every CTC encode, and omits the tile/thread policy,
which CTC derives from resolution (`--tile-columns=1 --threads=2` for 1080p,
`--tile-columns=2 --threads=4` for 4K). Threading is part of the definition of
the test: a patch measured single-threaded against a two-thread anchor produces
a speedup that is an artifact of the harness.

**Fixed here** by transcribing `EncodeWithAOM_AV2` directly
(`av2ra/measure/encode.py`), and by probing the binary at start-up
(`av2ra doctor`) rather than discovering the mismatch four hours into a pass.

### B2. The screening-time estimates are wrong by two to three orders of magnitude

`§5` claims a 12-encode screening pass of 1080p/17-frame clips completes in
**1.5–2.0 minutes** on 24 vCPUs.

Measured on a 2.8 GHz Xeon with a `-O3 -DNDEBUG` AVX2 build of this encoder:

| clip | preset | frames | encoder-reported time |
|---|---|---|---|
| 176x144 | `--cpu-used=4` | 8 | 111.9 s (≈14 s/frame) |
| 176x144 | `--cpu-used=9` | 4 | 20.7 s (≈5 s/frame) |

176×144 is 1/82 the pixel count of 1080p. Even granting a Cloudtop core three
times this throughput and perfect scaling, a single 1080p 17-frame encode at
`--cpu-used=2` is an hours-long job, not a seconds-long one. The prior corpus
corroborates this indirectly: its own round-1 measurements were taken on
**416×240, 4 frames** — a choice nobody makes when 1080p is cheap.

This is a blocker rather than an inaccuracy because the entire phase-2 design
rests on it: "hermetic on-node screening" of the full CTC-shaped clip set is not
a two-minute inner loop, and a system built as though it were will spend its
first day discovering that.

**Fixed here** by a *ladder* rather than a single screen: a bit-exactness proof
(minutes), an instruction-count complexity screen on short clips, then a
CTC-shaped RD screen, then the cluster — with the cheap rungs allowed to kill
and the expensive ones reserved for what survives.

### B3. The local screening BD-rate is treated as decisive; the corpus proves it is not even sign-reliable

`§6`/`§7` compute BD-rate from 4 clips × 3 QPs × 17 frames locally and feed it
straight into the acceptance matrix.

The prior effort did exactly this and recorded the result
(`Claude/reports/round1-patch-notes.md` against `experiments/registry.csv`):

| patch | local BD-rate | CTC A1 BD-rate | error |
|---|---|---|---|
| i01 | **−0.900%** | **+1.22%** | sign flip, 2.1 points |
| i04 | −0.597% | +0.11% | sign flip |
| i07 | −0.338% | +0.30% | sign flip |
| i06 | +0.105% | +0.75% | understated 7× |
| all-10 union | **−0.630%** | **+2.50%** | sign flip, 3.1 points |

Three of ten had the wrong sign. A screen that reports a compression *gain* for
a patch that costs 2.5% is worse than no screen, because it is acted on.

**Fixed here** by restricting what a local screen is permitted to conclude
(`av2ra/measure/tiers.py`): it may kill a catastrophic regression, prove
activation, and rank variants. It may not quote a BD-rate magnitude or trust a
local sign. Every report says so in the text of the tier result.

### B4. There is no defence against the agent optimising the measurement

The guides give an LLM write access to the source tree, the build, the harness
and the results database, then reward it for producing a number. The word
"integrity" does not appear; neither does "overfit", "conformance", or any
constraint on which files a patch may touch.

Concrete, cheap attacks this permits, all of which produce large clean
"speedups":

- special-case on `cm->width == 1920`, on `frame_number < 17`, or on a CTC QP;
- edit `CalcBDRate.py`, the thresholds YAML, or the clip list;
- pass `-DNDEBUG` or `-O3` to the candidate build only;
- skip work in a way that leaves the bitstream undecodable;
- tune a threshold until it fits the four screening clips.

**Fixed here** by a policy layer and a set of runtime gates
(`av2ra/integrity/`), covered by tests that attempt each attack. The path policy
makes the harness, the metric tooling, the test framework, the build files and
the agent's own source unwritable; the diff scanner catches benchmark fitting;
the build fingerprint refuses to compare arms built differently; a held-out clip
set the agent never sees detects overfitting.

### B5. "Algorithmic innovation only; parameter tuning is strictly forbidden"

`§Core Principle 9`. The intent is right — an agent that hill-climbs
`speed_features.c` presets is not doing research — but the rule as written
forbids the single most productive technique in the corpus.

`DECISIONS.md` records the tuning chain that found the peak of one heuristic:

```
MARGIN 4   A1 ratio 23.6   A2 25.2
MARGIN 2   A1 ratio 26.7   A2 51.0   <- peak, passes both classes
MARGIN 1   A1 ratio 14.7   A2 11.8   <- decay
```

That is three arms of pure parameter variation, and it is what turned a marginal
patch into the project's only clean two-class pass. The same document contains
the *marginal ratio* method, which requires parameter variation by construction.

**Fixed here** by replacing the blanket ban with two narrower rules: a
parameter must be exposed as a `-D` knob so an operating point is reproducible
without editing the patch; and a tuning step is only taken when a marginal-ratio
calculation supports it (`av2ra/agent/analyst.py`). Tuning a threshold *without*
that calculation is what slides along the trade-off, and that is what is
actually forbidden.

---

## 2. Major defects

### M1. The four-quadrant matrix classifies point estimates

`§7` assigns a quadrant from the signs of ΔBDR and ΔTime. No interval, no noise
floor, no power. The prior effort's own tooling
(`experiments/bin/screen_timing.py`) already computes a minimum detectable
effect and refuses to call a sub-noise difference a win — and its README records
that **six of ten** reported round-1 speedups were smaller than the 3.47% the
design could resolve.

Fixed: `av2ra/measure/acceptance.py` takes intervals, reports `NO_EFFECT` when
neither axis is resolved, and derives the pass/fail flag from the pessimistic
end of each interval in `conservative` mode.

### M2. The ratio can be a division by the reporting precision

The matrix's Q3 ratio is `−ΔTime / ΔBDR`. When ΔBDR rounds to `0.00%`, that is
not an infinite ratio, it is an undefined one. The corpus contains this error at
scale: a 101-feature study reported 21 passes of which
**12 were divisions by a rounded zero** (`SPEED_FEATURE_STUDY_REVIEW.md`), and
`registry.csv` itself records `inf` for two patches whose BD-rate was −0.01% and
−0.03%.

Fixed: the ratio is `None` when the denominator's interval spans zero or falls
below the reporting floor, and the verdict says so.

### M3. Nothing requires the two test classes to pass independently

The guide's bars are per preset, not per class. Ten arms in the corpus are
class-asymmetric in both directions (ratio 80.3 on 4K against 7.5 on 1080p;
19.0 against 36.9 the other way). One shared report declares three patches
"Adopt" on averaged ratios of 24.7, 62.1 and 111.2 while their binding class
scores 19.0, 29.3 and 31.0 against bars of 20, 35 and 35.

Fixed: `assess_all_classes` evaluates each class separately and reports the
worst; an average that passes while a class fails is a class failure.

### M4. Anchor caching is specified in a way that invalidates the timings

`§Core Principle 5` caches anchor baselines per commit "halving local screening
compute to candidate-only runs".

Half of that is right. Rate and PSNR are deterministic functions of source,
binary and flags — caching them is free and correct. **Timing is not.** A cached
anchor time was measured in a different machine state, so comparing today's
candidate against it reintroduces exactly the drift that interleaving exists to
remove.

Fixed: `av2ra/measure/screen.py` caches anchor *quality* (and uses a mismatch as
a build-integrity alarm) and always re-measures anchor *time* in the same pass,
interleaved with the candidate and with the arm order flipped between repetitions.

### M5. The BD-rate definition does not match CTC

`§6.3` specifies "3-point piecewise cubic polynomial interpolation". The AVM
reference (`tools/convexhull_framework/src/CalcBDRate.py`) uses **PCHIP**
(shape-preserving monotone cubic Hermite) on (quality, log-rate), 100 samples
across the overlapping quality interval, trapezoid integration, with explicit
non-monotonicity and no-overlap rejections. A different interpolant produces a
different number on the same data.

Fixed: `av2ra/measure/bdrate.py` reproduces the reference exactly, and a test
asserts agreement to 1e-6 across 200 random curve pairs. PCHIP is reimplemented
in pure Python (`av2ra/measure/pchip.py`) and validated against scipy, so the
definition does not change with the environment.

### M6. `--frame_count=33` cannot express the frame counts actually used

The EDA invocation in `§8` passes one scalar. Every report in the corpus runs
**A1 at 17 frames and A2 at 33**. Using 33 for both doubles the cost of the 4K
half of every round for numbers nobody compares against.

Fixed: `av2ra/ctc/contract.py` derives the frame count per test set and the EDA
back end submits one job set per class.

### M7. There is no null arm, so every ratio has an unknown denominator

The corpus asked for an anchor-vs-anchor arm four times and never ran it, and
paid for it: the same patch, byte-identical, measured two commits apart gave
ratio 6.6 and 26.9 with identical BD-rate. Separately, six *inert* arms — 0.00%
BD-rate on both classes — all read **slower** than the anchor, implying a
systematic ~1.6-point bias on the patched arm.

Fixed: `av2ra.integrity.gates.run_null_arm` measures the anchor against itself
on a schedule and reports the harness's own noise floor and bias in the
experiment record. Bias is reported, never subtracted: a biased harness needs
fixing, not correcting for.

### M8. An inert patch is treated as a safe patch

Eight arms in the corpus never fired. Their 0.00% BD-rate was read as "harmless"
rather than "untested", and three of them produced a methodology rule that was
carried for two rounds before being retracted, because the arms it rested on had
tested nothing.

Fixed: for an approximating patch, an identical bitstream is a **failure**
(`Verdict.NO_EFFECT`, with the analyst's move set to `instrument`). For a
reuse or kernel patch it is the required proof. Same comparison, opposite
meaning, decided by the declared mechanism — which is why mechanism is a
required field.

### M9. No profile, so no targeting

The guides never mention profiling. The corpus produced **twenty-four patches**
before anyone profiled the encoder; when they did, in 25 minutes and with no
cluster time, quantisation was ~47% of instructions retired — larger than
partition search, mode search and the loop filters combined — and no patch had
touched it.

Fixed: a profile is a first-class input. It targets ideation, supplies the
Amdahl ceiling that rejects impossible claims before implementation, and drives
a coverage report of hot functions no experiment has ever touched.

### M10. Nothing checks whether upstream already did it

Four experiments in the corpus died as no-ops because the idea had landed
upstream; three were discovered after cluster time was spent.

Fixed: `av2ra/core/basewatch.py` reports the intersection of upstream-changed
files with patch-touched files, flags upstream commits whose subjects match the
patch's mechanism, and distinguishes "still applies" from "still correct".

---

## 3. What the guides get right, and this project keeps

- **Non-blocking multitasking.** Submitting a cluster round and immediately
  moving to the next hypothesis in a fresh worktree is exactly right, and it is
  the single most important throughput decision in the design.
- **Isolated worktrees with out-of-source builds.** Correct, and the corpus has
  a painful worked example of what happens without them.
- **Anchor discipline.** Pinning a baseline commit and treating a base change as
  significant is right; the guides under-specify what it invalidates, not whether
  it matters.
- **The 4-quadrant framing.** The *shape* is right and is kept; what changes is
  that it consumes intervals rather than point estimates.
- **Ratio bars per preset.** The S1≥35 / S2≥30 / S3≥25 / S4≥20 ladder matches
  prior CTC practice and is kept as the default.
- **Human-in-the-loop steering and a daily digest.** Kept, implemented as durable
  instructions a node picks up on its next tick rather than as signals.
- **Fleet onboarding as one command.** Kept.
- **Proactive credential health.** Kept, with the alert threshold raised from
  2 hours to 4: a cluster round takes hours, and a certificate that expires
  mid-submission leaves a half-submitted job set to untangle by hand.

---

## 4. The phase 0 / phase 1 code

The code does not implement the guide it accompanies. Specifically:

| Command | Claim | What it does |
|---|---|---|
| `status` | "live status of all Cloudtop agents" | prints a hard-coded line: `Cloudtop 1 IDLE_READY None (Phase 1 Done)` |
| `study-codebase` | "generate documentation for all 15 subsystems" | prints `[OK] 15 Subsystem Architecture Treatises verified` after `glob`-ing a directory |
| `ingest-prior-research` | "parse and index historical research" | classifies a report as a success if it contains "gain" and a failure if it contains "loss"; the same report lands in both lists |
| `knowledge --query` | search | `grep` over markdown, capped at 15 hits |
| `generate_subsystem_docs` | count treatises | `return len(files) if files else 15` — returns 15 when the directory is empty |

`prior_successes.md` and `prior_failures.md` are the output of that ingester:
each entry is a filename plus the first ~200 characters of a report, YAML header
included. `knowledge/README.md` claims a "catalog of 32+ accepted speedup
mechanisms" and "27+ rejected hypotheses"; the files contain neither. No result,
mechanism, verdict, anchor or ratio survives ingestion.

`prior_insights.md` is four hard-coded sentences written into the file by
`knowledge_engine.py:150-171`, presented as if derived from the corpus.

The parts worth keeping are the config schema (the YAML shape is sensible and is
kept, extended) and `GitManager.create_worktree` (the right idea; reimplemented
here with state assertions, because the corpus contains a worked example of an
unverified revert silently accumulating three patches on top of each other).

---

## 5. The architecture study

Read as a *map*, it is useful: the subsystem decomposition matches the tree, and
it names real files. Read as a *source of targets*, it is dangerous in one
specific way — it describes functions by role rather than by symbol, and a model
asked to act on it will produce plausible names that do not exist. The
concrete failure is easy to reproduce: `search_txk_type` sounds exactly right
and is not in the tree; the real symbol is `search_tx_type`.

This project therefore never lets prose reach ideation as a source of symbols.
`av2ra/knowledge/codemap.py` indexes the actual tree (6,313 functions, 542 files,
330 speed-feature knobs on the reviewed commit) and ideation is only shown, and
only permitted to name, symbols from that index. A near-miss is repaired from the
index; anything else is rejected before an experiment is created.

---

## 6. What was missing entirely

Ranked by what it would have cost the prior effort:

1. **The research loop itself.** Ideation, patch synthesis, analysis, portfolio
   allocation, and learning. The guides describe the states an agent moves
   through and never what it does inside them.
2. **Integrity.** See B4.
3. **An experiment memory with provenance.** The corpus ends with two different
   sets of CTC numbers for the same three patches in two documents and no way to
   tell which is right.
4. **Calibration.** No mechanism scores a prediction against its outcome, so
   ideation cannot improve. Every hypothesis here states an expected speedup and
   BD-rate and is graded on both.
5. **A holdout set.** The screening clips are the clips the agent tunes against.
   Without a set it never sees, "it works on the screening set" is unfalsifiable.
6. **Decision-level instrumentation.** The corpus specifies a shadow mode —
   run the full baseline search while recording what a heuristic *would* have
   chosen, yielding per-site regret with zero run-to-run variance — calls it the
   highest-value remaining infrastructure, and never builds it. It is a first
   class ideation lens here (`decision_statistics`).
7. **An honest cost model.** See B2.

---

## 7. Assessment

The drafts are a good operations manual for a system whose science is
unspecified, attached to code that implements neither. The prior research corpus
they were meant to build on already contains most of the missing science, in the
form of forty documented failures and the rules derived from them.

The rest of this project is that science, implemented: thirty of those rules are
machine-readable (`av2ra rules`), eighteen of them are enforced by code, and the
test suite checks the enforcement against the results that produced the rules.
