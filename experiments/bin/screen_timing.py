#!/usr/bin/env python3
"""Decide whether a measured encode-time difference is real or is server noise.

This exists because the first round of speed-up measurements on this branch
reported six "speedups" that were entirely inside the run-to-run noise of the
machine they were measured on. The noise floor was 2.4% (1 sigma) and the
smallest difference the design could resolve was 3.5%, yet differences as small
as 0.5% were written down as wins.

The rule this tool enforces: a speedup that is not larger than the measurement
system can resolve is not a speedup, it is an unmeasured quantity. Such patches
must never be promoted to a CTC round -- CTC rounds cost a day and can only
answer the quality question, not rescue a timing measurement that was never
conclusive.

Input CSV needs three columns: a run-index column, a `tag` column where the
baseline is called `base`, and a metric column. Any monotone cost metric works;
instruction counts from `perf stat` are strongly preferred over wall clock
because they are deterministic to well under 0.1% and immune to co-tenancy on a
shared server.

Usage:
    screen_timing.py results.csv [--metric user_s] [--min-effect 5.0]
"""

import argparse
import csv
import math
import statistics as st
import sys
from collections import defaultdict

# Two-sided t critical values at 95%, indexed by degrees of freedom. Small-n
# measurement is the normal case here (a CTC-adjacent encode is expensive), so
# using a normal approximation would materially understate the intervals.
_T95 = {1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447, 7: 2.365,
        8: 2.306, 9: 2.262, 10: 2.228, 11: 2.201, 12: 2.179, 13: 2.160,
        14: 2.145, 15: 2.131, 16: 2.120, 17: 2.110, 18: 2.101, 19: 2.093,
        20: 2.086, 25: 2.060, 30: 2.042, 40: 2.021, 60: 2.000, 120: 1.980}


def t95(df):
    if df < 1:
        return float("inf")
    if df in _T95:
        return _T95[df]
    keys = sorted(k for k in _T95 if k <= df)
    return _T95[keys[-1]] if keys else 1.96


def welch(a, b):
    """Return (mean_diff, half_width_95, df) for mean(a) - mean(b)."""
    na, nb = len(a), len(b)
    if na < 2 or nb < 2:
        return st.mean(a) - st.mean(b), float("inf"), 0
    va, vb = st.variance(a), st.variance(b)
    se = math.sqrt(va / na + vb / nb)
    if se == 0:
        return st.mean(a) - st.mean(b), 0.0, na + nb - 2
    # Welch-Satterthwaite: the two arms often have very different spread,
    # because a patch that prunes work also prunes variance.
    df = (va / na + vb / nb) ** 2 / (
        (va / na) ** 2 / (na - 1) + (vb / nb) ** 2 / (nb - 1))
    return st.mean(a) - st.mean(b), t95(int(df)) * se, df


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csv_path")
    ap.add_argument("--metric", default=None,
                    help="metric column (default: last numeric column)")
    ap.add_argument("--tag-col", default="tag")
    ap.add_argument("--min-effect", type=float, default=5.0,
                    help="speedup%% we actually care about; used to report "
                         "whether the design has the power to see it")
    args = ap.parse_args()

    rows = [r for r in csv.DictReader(open(args.csv_path))]
    if not rows:
        sys.exit("no rows")

    metric = args.metric
    if metric is None:
        for k, v in rows[0].items():
            if k != args.tag_col:
                try:
                    float(v)
                    metric = k
                except (TypeError, ValueError):
                    continue
        if metric is None:
            sys.exit("no numeric column found; pass --metric")

    data = defaultdict(list)
    for r in rows:
        tag = (r.get(args.tag_col) or "").strip()
        raw = (r.get(metric) or "").strip()
        if not tag or not raw:
            continue
        try:
            data[tag].append(float(raw))
        except ValueError:
            continue

    if "base" not in data:
        sys.exit("no rows tagged 'base'; the baseline arm must be present and "
                 "must be interleaved with the test arms, not run once up front")

    base = data["base"]
    bm, n = st.mean(base), len(base)
    bsd = st.stdev(base) if n > 1 else 0.0

    print(f"metric               : {metric}")
    print(f"baseline             : mean={bm:.3f}  sd={bsd:.3f} "
          f"({100 * bsd / bm if bm else 0:.2f}%)  n={n}")

    # Minimum detectable effect for a two-arm comparison at equal n. This is the
    # number that decides whether the experiment was worth running at all.
    if n > 1 and bm:
        mde = 100 * t95(2 * n - 2) * bsd * math.sqrt(2.0 / n) / bm
        print(f"noise floor (1 sigma): {100 * bsd / bm:.2f}%")
        print(f"min detectable effect: {mde:.2f}%  (95%, equal n={n})")
        if mde > args.min_effect:
            need = math.ceil(2 * (t95(2 * n - 2) * bsd / bm * 100
                                  / args.min_effect) ** 2)
            print(f"  UNDERPOWERED for the {args.min_effect:.1f}% effect you "
                  f"care about; need about n={need} reps per arm.")
    print()

    hdr = f"{'tag':<8}{'mean':>10}{'sd':>9}{'delta%':>9}{'95% CI':>18}  verdict"
    print(hdr)
    print("-" * len(hdr))

    promote, reject = [], []
    for tag in sorted(k for k in data if k != "base"):
        v = data[tag]
        m = st.mean(v)
        sd = st.stdev(v) if len(v) > 1 else 0.0
        diff, hw, _ = welch(base, v)          # positive diff == test is faster
        pct = 100 * diff / bm if bm else 0.0
        lo, hi = (100 * (diff - hw) / bm, 100 * (diff + hw) / bm) if bm else (0, 0)

        if lo > args.min_effect:
            verdict, bucket = "PROMOTE", promote
        elif lo > 0:
            verdict, bucket = "real,small", reject
        elif hi < 0:
            verdict, bucket = "SLOWER", reject
        else:
            verdict, bucket = "NOISE", reject
        bucket.append(tag)
        print(f"{tag:<8}{m:>10.3f}{sd:>9.3f}{pct:>8.2f}%"
              f"  [{lo:>6.2f}%,{hi:>6.2f}%]  {verdict}")

    print()
    print(f"PROMOTE to CTC ({len(promote)}): {', '.join(promote) or '-'}")
    print(f"HOLD          ({len(reject)}): {', '.join(reject) or '-'}")
    print()
    print("A 'NOISE' verdict is not a small win. It is no measurement at all: "
          "add reps or\nswitch to perf instruction counts before spending a CTC "
          "round on it.")


if __name__ == "__main__":
    main()
