# Decision log

Append-only. Newest entry at the top. Every entry says what changed state, and
why. If a patch was killed, the reason must be recorded here so nobody spends a
CTC slot rediscovering it.

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
