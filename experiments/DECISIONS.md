# Decision log

Append-only. Newest entry at the top. Every entry says what changed state, and
why. If a patch was killed, the reason must be recorded here so nobody spends a
CTC slot rediscovering it.

---

## 2026-08-12 — Tier 0 result: i02, i03, i05 are all BIT-EXACT

Run on the fixed harness (clean-tree assertion active, patches verified isolated
one at a time):

    config: 416x240, 6 frames, cpu-used=3, single-threaded, QP 110 and 185
    baseline                          48ebfecfc46f8cb6a43277e7739b33a6
    0002-ist-sparse-coeff-restore     48ebfecfc46f8cb6a43277e7739b33a6  BIT-EXACT
    0003-pmc-arena-allocation         48ebfecfc46f8cb6a43277e7739b33a6  BIT-EXACT
    0005-fullpel-search-memo          48ebfecfc46f8cb6a43277e7739b33a6  BIT-EXACT

All three change no encode decision. For i05 specifically this says the memo key
is correct across DRL index and MV precision — the exact thing that would have
been wrong if the patch were buggy.

**Decision: none of these three should consume a CTC round.** Their quality risk
is zero on the tested configuration, which is a stronger and far cheaper result
than a BD-rate table could give. What remains unknown for all three is *speed* —
round-1 measured 1.3%, 1.2% and 1.2%, all below the 3.5% noise floor, so their
benefit is still entirely unquantified. They go to Tier 1 (`perf stat`
instruction counts), not to CTC.

**Scope of the claim, stated precisely.** This is proof for the configuration
tested, not a universal proof. One clip at 416x240, 6 frames, two QPs and
cpu-used=3 does not exercise every block size, partition shape or reference
structure that A1 4K at the CTC preset will. A cache-key bug living only in a
path this config never reaches would not have been caught. Re-running Tier 0 on
a 4K clip at the CTC preset before final sign-off is cheap and worth doing;
until then the correct statement is "bit-exact on the tested configuration",
not "bit-exact".

---

## 2026-08-12 — Tier-0 harness had a silent contamination bug (fixed)

The first version of `bin/bitexact.sh` reverted the tree between patches with
`git checkout -- av2/ aom/ 2>/dev/null`. This repo has no `aom/` directory — AVM
renamed it `av2/` — so git rejected the entire command on the bad pathspec,
reverted **nothing**, and returned 1, with the error swallowed by `2>/dev/null`.

Consequence: patches accumulated instead of being tested in isolation. i03 would
have been measured with i02 still applied, i05 with both, and on the following
run the *baseline itself* was built with all three patches in the tree. The
script would have printed confident, precisely formatted, entirely wrong
signatures — and "bit-exact" would have been the expected output, since the
baseline and the patched builds were becoming the same binary.

Caught by noticing `git status` showed all three patches' files modified at once
during what was supposed to be a clean baseline build. Fixed by reverting with a
valid pathspec and then **asserting** the tree is clean, aborting loudly if not.

This is the same failure mode as the round-1 measurements below: a number that
looks precise, is produced by a process nobody verified, and is wrong. The
general rule now applied in the tooling: *assert the state you depend on; never
assume the command that was supposed to establish it worked.*

---

## 2026-08-12 — Round-1 measurements audited and largely retracted

**Trigger.** CTC round 1 (A1 17 frames, A2 33 frames, RA) was launched against
all ten patches. Before results returned, the round-1 evidence that justified
sending them was re-examined.

**Finding.** The round-1 timing design could not resolve the effects it
reported. Re-analysis of `patches/measurements-runtime-roundrobin.csv` with
`bin/screen_timing.py`:

    baseline mean 88.446s, sd 2.107s -> noise floor 2.38% (1 sigma)
    minimum detectable effect 3.47% (95%, n=5 per arm)

Six patches reported speedups below that floor:

| patch | reported | 95% CI | verdict |
|---|---|---|---|
| i02 | 1.3% | [-2.2%, +4.9%] | NOISE |
| i03 | 1.2% | [-3.5%, +5.9%] | NOISE |
| i04 | 0.5% | [-4.3%, +5.4%] | NOISE |
| i05 | 1.2% | [-2.1%, +4.5%] | NOISE |
| i08 | 1.9% | [-1.5%, +5.3%] | NOISE |
| i10 | 2.1% | [-1.4%, +5.6%] | NOISE |

Four were resolved above noise, but only one clears the >5% bar with its whole
interval:

| patch | speedup | 95% CI | verdict |
|---|---|---|---|
| i06 | 17.2% | [+14.3%, +20.2%] | clears the bar |
| i09 | 7.8% | [+4.2%, +11.4%] | straddles the bar |
| i01 | 6.5% | [+2.9%, +10.0%] | straddles the bar |
| i07 | 3.2% | [+0.2%, +6.1%] | real, under the bar |

**Cross-check — the two round-1 datasets contradict each other.** The paired
dataset (`measurements-runtime-paired.csv`, base and test in the same rep, which
cancels machine drift) gives per-rep speedups that disagree with the round-robin
verdicts:

| patch | paired rep1 | paired rep2 | round-robin verdict | agrees? |
|---|---|---|---|---|
| i01 | -0.6% | +5.0% | "real, 6.5%" | no — sign flips |
| i02 | +2.1% | +6.7% | NOISE | no |
| i03 | -0.2% | +6.9% | NOISE | no — sign flips |
| i04 | -5.1% | -0.5% | NOISE | consistently *slower* |
| i05 | +2.4% | +0.5% | NOISE | yes |
| i06 | +17.6% | +19.2% | +17.2% | yes |
| i07 | +2.5% | +1.6% | +3.2% | yes |
| i08 | +4.4% | +0.1% | NOISE | unstable |
| i09 | +8.7% | +0.9% | +7.8% | no — 10x spread |
| i10 | +0.9% | +9.1% | NOISE | no — 10x spread |

Two independent measurements of the same quantity disagreeing this badly means
neither is trustworthy. **i06 is the only patch that is stable across both
designs and both datasets.** i04 appears to be a slowdown, not a speedup.

**Second finding — regime mismatch.** Round 1 was measured on a single 416x240
clip, 4 frames, `--cpu-used=4`. CTC RA is A1 4K + A2, 17/33 frames, at the CTC
preset with 6 QPs. Partition and transform pruning heuristics depend directly on
block-size distribution and search depth, both of which differ substantially
between those regimes. Round-1 magnitudes should not be expected to transfer.

**Decisions.**
- Retract the six NOISE results. They are not small wins; they are
  non-measurements. `registry.csv` marks them `hold`.
- i02, i03 and i05 are *reuse* mechanisms (buffer restore, arena allocation,
  memo cache). They must be bit-exact. Their status is decided by
  `bin/bitexact.sh`, not by any CTC round — a bit-exact patch has zero quality
  risk by proof, and a diverging one has a bug. Tier 0 launched.
- All future timing moves to `perf stat -e instructions`, which the CTC
  framework already supports (`use_perf_util: true` in
  `tools/convexhull_framework/src/config.yaml`).
- i06 carries the largest speedup and, being partition pruning, the largest
  BD-rate risk at 4K. Treat a good i06 CTC result with suspicion until
  per-sequence spread is examined.

**Not yet done.** Tier 2 (shadow-mode decision regret) is specified in
`README.md` but not implemented. It is the highest-value remaining piece of
infrastructure: it measures the quality cost of every pruning heuristic
deterministically, in one encode per (clip, QP), without a CTC round.
