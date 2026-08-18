# Where the remaining speedup is, after the 0817 results

Reading the GPT 0817 patch results together with the 101-feature study, my own
two patches, and the four rounds of 06/09 work on this branch.

---

## 1. What the 0817 results establish

The single most useful thing in them is not any individual patch — it is that
**two of the winners share one mechanism**, and that mechanism generalises.

| Patch | What it does | Result |
|---|---|---|
| `0001` | `prune_tx_type_using_stats` promoted **speed 4 → 3** | +6.12% / +0.18% — ratio 34.0 |
| `0002` | `dc_blk_pred_level` promoted **speed 4 → 3** | +3.08% / +0.00% — ratio ~308 |
| `0004` | restore smooth intra | +1.25% **and** −1.08% BD gain |
| `0006` | CCSO BO-first ordering | +1.13% / −0.12% BD gain |

Both big winners are **preset promotions of features the 101-feature study had
already measured as efficient at speed 4.** Nothing about either feature was
redesigned. The study measured every feature *at its own preset* and asked "does
removing it hurt?" — it never asked "should this be on at a slower preset too?"
That unasked question produced +9.2% of compounded speedup in one round.

Two further facts worth carrying:

- **Speedup grows as the preset slows.** `prune_tx_type_using_stats` gave +4.90%
  at speed 4 and +6.12% at speed 3. The same pattern appeared on this branch
  earlier: an identical patch set gained +30.7% at speed 2 against +20.3% at
  speed 4 for near-identical BD-rate. Pruning removes more where the search is
  deeper.
- **But the ratio can fall while the speedup rises.** That same promotion took
  the ratio from 49.0 down to 34.0 — BD-rate grew faster than time saved. This
  matters for how far promotion can be pushed, and it is the reason patch 15
  below is expected to fail.

## 2. What my two patches showed

**`0011` worked and is now confirmed twice.** It removed two speed-3
assignments that cost >1% BD-rate while saving no measurable time. GPT's `0004`
independently reproduces half of it (+1.25% speed *and* −1.08% BD). The class of
defect is real: **a speed feature that degrades prediction quality can be
self-defeating**, because larger residuals make downstream transform and
partition search more expensive, not less. That is the mechanism the 0817 report
gives for why restoring smooth intra was *faster*.

**`0012` did nothing — no speed change, no quality change.** My reasoning was
that CCSO passes at ratio 86.3 against a bar of 30, so ~3× headroom was
unclaimed, and the aggressiveness was one hard-coded scalar (`1.001`). The
headroom argument was right; **the choice of lever was wrong.** The termination
test is

    1.001 * final_filtered_cost > last_best_cost

and filtered costs evidently differ from the running best by far more than 1%
in almost every case, so widening the tolerance to 1.01 almost never flips the
comparison. The knob was not the binding constraint.

GPT's `0006` got +1.13% from the same function by changing **search order**
instead — evaluating the cheap BO-only candidate first so early termination
starts from a finite bound. That is the correct lever: when a search is
already terminating early, what matters is *what it has evaluated before it
stops*, not how narrowly it decides to stop.

I have not proposed another CCSO patch here. The next one should follow 0006's
direction — order the remaining candidates by expected cost — and I would want
to read the search loop properly before writing it rather than guess a second
time.

## 3. Four new patches

All four build clean, apply standalone against the anchor, and compose with each
other.

### `0013` — DC-block prediction level 2 at speed 3  *(highest expected value)*

GPT `0002` promoted the **conservative** level 1 and measured +3.08% speedup at
+0.00% BD-rate: a ratio near **308 against a bar of 25**, over twelve times the
required headroom. Level 2 is what speed 4 already runs; it additionally applies
DC-block prediction during winner-mode evaluation.

This is not a new operating point for the encoder, only a new preset for an
existing one, and its measured BD-rate cost at speed 4 was +0.04%. The plausible
outcomes are dominated by how much extra time level 2 saves rather than by
quality risk.

*Caveat, learned twice on this branch:* large headroom makes a step likely to
succeed, not safe. Loosening a comfortably-passing threshold worked for
stationarity pruning (23.6 → 26.7, speedup +54%) and then collapsed at the very
next step (→ 14.7). `-DAVM_S3_DC_BLK_PRED_LEVEL=1` restores the measured
level-1 point exactly.

### `0014` — warp search diamond at speed 3

The **only genuine speed-4 pass not yet promoted** (ratio 20.6). Same mechanism
as 0001 and 0002.

Thinnest margin of the three: 20.6 measured against a speed-3 bar of 25. If time
and BD-rate scale together on promotion — as they roughly did for tx-stat
pruning (49.0 → 34.0) — the ratio barely moves and it fails. It clears only if
the extra warp search at speed 3 is disproportionately redundant. Kept as its
own arm rather than bundled with 0013 so a marginal result stays attributable.

### `0015` — transform-statistics pruning promoted to speed 2  *(expected to fail)*

This is the only feature measured at two presets, so it is the only one where
the promotion curve can be extrapolated:

    speed 4   +4.90% / +0.10%   ratio 49.0   (bar 20)
    speed 3   +6.12% / +0.18%   ratio 34.0   (bar 25)

BD-rate grew faster than time saved. A third promotion of similar character
lands near 25 against a speed-2 bar of **30**.

**I expect this arm to fail, and I think it is still worth running.** The
extrapolation assumes the two quantities keep their growth ratio, and they need
not: the transform-type search at speed 2 evaluates a wider type set, not merely
a deeper version of speed 3's, so statistics-based pruning may remove
proportionally more of it. That is a mechanism, not wishful arithmetic, and no
cheaper experiment settles it. Running it also locates where the promotion curve
turns over, which is worth knowing before promoting anything else.

### `0016` — remove three self-defeating speed-4 features  *(quality, not speed)*

The `0011` screen applied to speed 4:

    Test 94  perform_coeff_opt_based_on_satd   BD +0.15%   time -0.00%
    Test 95  multi_winner_mode_type (inter)    BD +0.36%   time +0.15%
    Test 97  cdef_pick_method = LVL3           BD +0.15%   time -0.31%

Test 97 is the clearest: the LVL3 CDEF search is **both slower and worse** than
the LVL1 search speed 2 already selects. Together: ~+0.66% BD-rate recovered at
a net time change of roughly +0.16% in the favourable direction — every
individual time figure being inside the speed-4 noise band.

Judge on BD-rate, requiring encode time not to regress beyond noise. Given the
0004 mechanism (better predictions shorten downstream search), a small speedup
would not be surprising.

## 4. Recommended round

| Arm | Preset | Question |
|---|---|---|
| `0001+0002+0004` | 3 | **Run this first.** The 0817 report's "golden combination" is projected, not measured: ~+9–10% speed at ~−0.90% BD. It is the current shipping candidate and nothing else should be judged before it is confirmed. |
| `0013` | 3 | Does level 2 claim the 12× headroom level 1 left? *(supersedes 0002 — do not stack)* |
| `0014` | 3 | Does the last un-promoted speed-4 pass survive promotion? |
| `0016` | 4 | ~0.66% BD recovered for free? |
| `0015` | 2 | Where does the promotion curve turn over? *(expected to fail)* |

If arms are scarce, drop `0015` first and `0014` second. `0013` and `0016` are
the two I would spend slots on.

## 5. What I would build next, and what I would not

**Would build — TCQ, gated on marginal coefficients.** Still the largest single
lever at +13.09% of runtime. GPT `0003` re-enabled TCQ on L0/L1 and got −0.9%
BD-rate for a 5.6% *slowdown* — a quality booster, not a speed feature, because
temporal layer alone does not identify where TCQ earns its cost. TCQ's benefit
is concentrated where quantization decisions are **marginal**, and the sharpest
available signal is the count of coefficients quantizing to **|level| == 1**:
those are the positions where a trellis decision actually changes the rate,
whereas large-magnitude coefficients are insensitive to it. Gating on that count
rather than on `eob > 16` or on temporal layer targets the blocks TCQ was
designed for at a fraction of the invocations. This is the one remaining idea
with double-digit potential.

**Would build — CCSO candidate ordering,** following `0006` rather than my
failed `0012`. Order the remaining filter configurations by expected cost so
early termination has seen the cheap wins first. CCSO is ~31% of encode time and
`0006` showed ordering is the lever that works there.

**Would not build — the DIP and MV-collinearity proposals** from the original
study, for the reasons in `SPEED_FEATURE_STUDY_REVIEW.md`: both fail their own
stated arithmetic, DIP's proposed gradient-anisotropy signal is the formulation
patch 06 already found inadequate, and the collinearity premise overlooks that
compound prediction's main gain is noise averaging rather than motion diversity.

## 6. One measurement note

The promotion mechanism means features will increasingly be evaluated at presets
they were not measured at. That makes the ±1% timing floor in the original study
more costly, not less — a promoted feature worth 0.8% is currently unmeasurable.
`perf stat -e instructions` is deterministic to well under 0.1%, and the CTC
framework already supports it (`use_perf_util: true`). It costs nothing in test
design and would make the whole promotion sweep resolvable.
