# Integrity model

An autonomous optimiser rewarded for a number will find the cheapest way to
produce it, and the cheapest ways do not go through the encoder. This document
is the threat model, the countermeasures, and — importantly — what is *not*
covered.

Nothing here assumes malice. Every attack below has a plausible well-intentioned
version: an agent "improving the harness", "making the test faster", "handling
the 1080p case". The mechanism and the damage are identical either way, which is
why the answer is structural rather than a matter of instructions.

---

## Threats and countermeasures

Ranked by likelihood × damage.

### T1. Edit the scoreboard *(high likelihood, total damage)*

Change `CalcBDRate.py`, the thresholds, the clip list, or the agent's own
acceptance code.

**Countermeasure.** `integrity/policy.py` makes `tools/convexhull_framework/**`,
`research_agent/**`, `test/**`, `CMakeLists.txt`, `cmake/**`, `.github/**`,
`third_party/**` and `av2/decoder/**` unwritable by any patch. The implementer
checks each edit's path *before* applying it, and the diff is re-checked before
the build. `tests/test_integrity.py` asserts each path is refused.

Scope is also a research constraint: a change confined to the encoder can be
submitted upstream. A "speedup" that needed the test framework edited is not a
contribution to AV2.

### T2. Fit the benchmark *(high, high)*

Special-case on the resolutions, frame counts, QPs or sequence names of the test
set.

**Countermeasure.** The diff scanner rejects `width == 1920`, `height == 2160`,
`frame_number < 17`, `qindex == 185`, and the CTC clip names, on *added lines
only* — the encoder legitimately contains such constants, and what is policed is
what the patch introduces. Comments are exempt. An `INTEGRITY_FAIL` verdict is
terminal and never promotable.

### T3. Build the arms differently *(medium, total)*

`-DNDEBUG` or `-O3` on the candidate; a stale CMake cache carrying a define the
other arm lacks; a Debug anchor against a Release candidate.

**Countermeasure.** A build fingerprint over toolchain identity, CMake
arguments, targets and generator. `assert_comparable` raises rather than
warning. A build directory whose stamped configuration differs is wiped rather
than reconfigured over. `-D` knobs the patch introduces are excluded from the
config fingerprint and folded into source identity — so a threshold sweep is
comparable — and `check_defines` refuses any define that does not appear in the
patch, which closes the loophole that would otherwise open.

### T4. Exploit run-to-run noise *(high, medium)*

Re-run until a favourable number appears; report the best repetition.

**Countermeasure.** Repetitions are averaged inside a paired interval, never
selected from. Arms are interleaved with the order flipped between repetitions.
Verdicts come from interval bounds. The null arm measures the harness's own
noise and bias on a schedule. Re-measuring the same quantity appends a row, and
a disagreement surfaces as a conflict rather than replacing the earlier value.

### T5. Ship a broken bitstream *(medium, high)*

Skip work in a way that leaves the stream undecodable or non-conformant.

**Countermeasure.** Decode-and-MD5 on candidate bitstreams (`conformance_sample`
controls how many; the report says whether it sampled). A decoder change is
outside the writable set, so a patch cannot make the decoder accept its own
output. **Gap:** decoded *pixels* are not compared against a reference decoder
build — see below.

### T6. Overfit the screening set *(high, medium)*

Tune a threshold until it fits four clips.

**Countermeasure.** A holdout clip set never shown to ideation and never used to
pick a threshold. A sign flip between screen and holdout blocks the experiment;
shrinkage warns and is carried into the report. The planner refuses a CTC round
without a holdout measurement.

### T7. The garden of forking paths *(high, medium)*

Run thirty arms, report the two that look significant.

**Countermeasure.** Holm–Bonferroni for batch decisions
(`stats.holm_bonferroni`), conservative interval bounds for promotion, and a
registry that records every arm — so the denominator of any "we found N wins"
claim is visible.

### T8. Compare against a stale or wrong anchor *(medium, high)*

Reuse a cached anchor result from a different commit or machine state.

**Countermeasure.** The anchor cache stores *quality only*, keyed by build
fingerprint, clip identity and encode configuration; a mismatch between cached
and fresh anchor quality fails the point as a build-integrity alarm rather than
being averaged. Timing is always re-measured in the same pass. `basewatch`
invalidates measurements when the anchor moves.

### T9. Claim more than the profile allows *(medium, medium)*

Report a whole-encode speedup larger than the touched code could possibly buy.

**Countermeasure.** The Amdahl gate, applied at *ideation* time (rejecting the
proposal) and again at analysis time. A kernel cannot save more of the encode
than it occupies.

### T10. Report an inert patch as safe *(high, medium)*

A patch that never fires shows 0.00% BD-rate, which reads as harmless.

**Countermeasure.** The activation gate. For an approximating mechanism an
identical bitstream is a failure, not a pass. Eight arms in the prior corpus were
inert, and three of them produced a methodology rule that survived two rounds
before being retracted.

### T11. Nondeterminism *(low, high)*

An encoder whose output depends on thread timing makes every quality number
irreproducible and every exactness check void.

**Countermeasure.** Repetitions of the same point must produce identical
bitstreams; the determinism gate blocks otherwise. The scanner rejects `rand`,
`clock`, `getenv` in added code. Threading is fixed by the CTC tiling rules
rather than chosen.

### T12. Launder work outside the measurement *(low, high)*

Precompute into a file, use an environment variable, shell out.

**Countermeasure.** The scanner rejects `getenv`, `system`, `popen`, `fork` in
added encoder code. Encodes run with a fixed environment. `run_measured` never
uses a shell.

---

## What is not covered

Stated plainly, because an integrity model that implies completeness is worse
than none.

- **Decoded-pixel equivalence.** The conformance gate proves a stream decodes.
  It does not prove it decodes to the right pixels. Closing this needs a
  reference decoder build; it is phase 9.4.
- **Perceptual quality.** Every metric here is objective. A change that helps
  PSNR and hurts appearance passes. Only human viewing catches it.
- **Content outside the test classes.** All classes here are RA at 1080p and 4K.
  A feature behind a resolution guard that no class exercises is invisible, and
  the correct conclusion is "CTC cannot measure this", not "this does nothing".
- **Cluster-side integrity.** Once a job is submitted, the numbers that come
  back are trusted. The cluster's own repeatability has never been measured on
  this project; phase 8.4 is what would establish it.
- **Slow drift in the corpus.** The agent writes lessons that shape future
  ideation. A wrong lesson propagates. Retractions are representable and marked,
  but nothing detects a wrong lesson automatically — that is what the human
  review of `DECISIONS.md` is for.
- **Adversarial intent.** These are guardrails against a misaligned optimiser
  and honest self-deception, not a sandbox. An agent deliberately trying to
  defeat them, with write access to the workspace, could.

---

## What still needs a human

1. **Promotion upstream.** The system recommends; a person reviews the diff and
   submits it. Nothing here mails a change list.
2. **Widening the policy.** If a legitimate experiment needs a file outside the
   writable set, that is a human decision, made once, in the config.
3. **Measurement conflicts.** `av2ra registry conflicts` reports quantities
   measured twice under the same conditions that disagree. That is either a
   repeatability problem in the back end or a provenance bug in the harness.
   Both need a person.
4. **Reading the decision log.** The ledger is the system's memory. A wrong
   lesson in it is the one failure mode with no automated countermeasure.
