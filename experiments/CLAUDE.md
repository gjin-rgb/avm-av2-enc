# Orientation for a future session

You are almost certainly a fresh Claude session with no memory of this work.
This directory is the handover. Read it in this order and you will be current in
about a minute.

1. `registry.csv` — where every patch stands, across every tier. Start here.
2. `DECISIONS.md` — why patches were killed or kept. Read the last entry first.
3. `README.md` — the protocol and the reasoning behind it.

## Ground truth you must not re-derive or re-litigate

- The patches live in `/patches` as standalone `.patch` files against
  `av2-enc`. They are **not** applied to the source tree; the tree is clean.
- Round-1 timings in `patches/measurements-*.csv` are **wall clock on a shared
  host with a 2.4% noise floor and a 3.5% minimum detectable effect.** Six of
  the ten reported speedups are below that floor and are non-measurements. Do
  not quote them as speedups. Re-run `bin/screen_timing.py` on that CSV if you
  need to see this for yourself.
- Round-1 was measured on 416x240, 4 frames, `--cpu-used=4`. The CTC target is
  A1 (4K) + A2 RA at 17 / 33 frames, QPs 110/135/160/185/210/235. Assume nothing
  transfers between those two regimes until measured.
- The user runs CTC themselves on their own server; rounds take hours to a day.
  Your job is to make sure every CTC slot answers a question that nothing
  cheaper could have answered.

## The one rule

Before proposing that any patch go to a CTC round, check it has cleared the
cheaper tiers. A patch whose speedup has never been resolved above the noise
floor must not consume a CTC slot — fix the measurement first (Tier 1,
`perf stat -e instructions`), because CTC measures quality and cannot rescue an
inconclusive timing result.

## When CTC results arrive

1. Write the numbers into `registry.csv` (`t4_bdrate`, `t4_worst`,
   `t4_speedup`).
2. Append a dated entry to `DECISIONS.md` saying what was killed, kept, and why.
3. Report **per-sequence spread**, not just the mean. A good average that hides
   one bad sequence is a regression risk, not a win.
4. Commit. This directory is the only thing that survives you.
