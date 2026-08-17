# Critical review of the 101-feature AV2 speed-feature study

Reviewing `shared/AV2_Speed_Feature_Tradeoff_Report.md` (anchor `fe1bfdee54`,
CTC RA, Class A1 4K / A2 1080p, 208 runs).

The study is the most useful artifact produced on this project so far: a
per-feature causal map of the whole encoder is exactly the right thing to build,
and the isolated-branch methodology is sound in design. This review concerns
what the numbers can and cannot support, and it reaches three conclusions that
change the recommendations:

1. **12 of the 21 reported passes are division-by-noise artifacts.** The true
   count is 9.
2. **All four proposals fail their own stated arithmetic.** None reaches the
   ratio it projects; three land below the bar they target.
3. **The recommendation to delete 35+ "dead" assignments is unsafe** and would
   silently regress sub-720p encoding, which CTC never measures.

It also identifies what I believe is the largest unexploited opportunity in the
data, which the study's framing could not surface, and ships two patches.

---

## 1. What the measurements can resolve

The study reports time savings to two decimals and computes ratios from them. To
know what those digits mean, we need the noise floor — and the data contains a
clean estimator for it.

**A feature whose BD-rate is 0.00% on both classes changed nothing measurable in
the bitstream, so its true time saving is zero.** Any nonzero value reported for
such a feature is measurement error. There are 40 of them:

| Preset | n | mean reported saving | sd | range |
|---|---|---|---|---|
| Speed 2 | 11 | **−0.529%** | 0.317% | −1.04 … +0.00 |
| Speed 3 | 20 | **−0.440%** | 0.162% | −0.71 … −0.08 |
| Speed 4 | 9 | **+0.222%** | 0.160% | −0.10 … +0.42 |

Two things follow.

**There is a systematic per-preset bias, not just random scatter.** The means are
nowhere near zero and they *flip sign* between Speed 3 and Speed 4. Whatever
causes it — different machine pools, different run lengths, chunking — it offsets
every time-saving figure in the study by roughly −0.5% at Speeds 2–3 and +0.2% at
Speed 4.

**The resolution limit is about ±1%.** Combining bias and scatter, no feature
whose true saving is under roughly 1% can be distinguished from zero here. That
invalidates a specific and load-bearing claim: the ~35 features reported with
"negative speedup" are not evidence that those features *slow the encoder down*.
Correcting the Speed-3 bias turns a reported −0.44% into approximately zero,
which is the expected result for a feature that does nothing on this content.

This is worth stating plainly because the same error was made on this project
before: round 1 of the patch work reported six speedups that sat below a 3.5%
minimum detectable effect, and CTC later showed two of them were slowdowns. The
fix is the same one recommended then — `perf stat -e instructions` is
deterministic to well under 0.1% and immune to machine variation, and the CTC
framework already supports it via `use_perf_util: true`.

## 2. The corrected scoreboard: 9 passes, not 21

A ratio is speedup divided by BD-rate loss. When BD-rate rounds to −0.00%, that
division produces "∞ → PASS" from what is actually a null result. Twelve entries
qualify:

    #20  s2_skip_rep_newmv       TS +1.09%  BD -0.01%
    #43  s3_auto_mv_step         TS +0.18%  BD -0.01%
    #54  s3_dis_sb_mv_cost       TS +0.17%  BD -0.00%
    #76  s4_part_breakout_dist   TS +0.41%  BD -0.00%
    #78  s4_subpel_pruned_more   TS +0.22%  BD -0.00%
    #79  s4_gm_downsample        TS +0.27%  BD -0.00%
    #82  s4_alt_ref_fp           TS +0.25%  BD -0.00%
    #84  s4_prune_inter_tpl3     TS +0.09%  BD -0.00%
    #88  s4_tpl_prune_mv2        TS +0.16%  BD -0.00%
    #89  s4_tpl_subpel_half      TS +0.28%  BD -0.00%
    #90  s4_tpl_search_bigdia    TS +0.42%  BD -0.00%
    #100 s4_newmv_drl_limit      TS +0.98%  BD -0.04%

Every Speed-4 entry here has a reported saving below the +0.22% Speed-4 bias, so
after correction their true savings are approximately zero. These are not
win-wins; they are unmeasured.

**The features that genuinely clear their bars:**

| Preset (bar) | Feature | Saving | BD | Ratio |
|---|---|---|---|---|
| 2 (30) | `ccso_early_term` | +31.07% | +0.36% | **86.3** |
| 2 (30) | `disable_ext_part` | +20.84% | +0.39% | 53.4 |
| 2 (30) | `mv_exh_thresh` | +7.23% | +0.12% | 60.3 |
| 2 (30) | `prune_2d_txfm` | +1.38% | +0.04% | 34.5 |
| 2 (30) | `wienerns_iters` | +18.48% | +0.10% | **184.8** |
| 3 (25) | `best_rd_chroma` | +0.74% | +0.02% | 37.0 |
| 4 (20) | `prune_tx_stats` | +4.90% | +0.10% | 49.0 |
| 4 (20) | `warp_search_dia` | +1.65% | +0.08% | 20.6 |
| 4 (20) | `dc_blk_pred` | +3.08% | +0.04% | **77.0** |

Nine features, an 8.9% pass rate rather than 20.8%. The qualitative conclusion —
in-loop filtering and macro-partition pruning carry the real speedup — survives
and is if anything strengthened: **CCSO, Wiener NS and extended-partition
disabling alone account for ~70% of all measured speed-feature value.**

## 3. The four proposals

Each proposal states a BD-rate recovery and a speedup retention, then claims a
resulting ratio. Those claims do not follow from those inputs.

| Proposal | Stated inputs | Ratio implied | Claimed | Bar | Verdict |
|---|---|---|---|---|---|
| 1 DIP gating | recover 0.85 of 1.05%, keep 80% of 2.45% | **9.8** | ">35.0" | 25 | fails |
| 2 TCQ adaptive | recover 0.90 of 1.31%, keep 9.2 of 13.09% | **22.4** | "≥30.0" | 25 | fails |
| 4 MV collinearity | recover 0.70 of 1.13%, keep 6.8 of 8.53% | **15.8** | "≥25.0" | 20 | fails |

To actually clear its bar, Proposal 1 must recover **93%** of the DIP loss (not
81% as stated), Proposal 2 **72%**, and Proposal 4 **70%**. Those are the real
targets and they are much harder than the text implies.

Beyond arithmetic:

**Proposal 1's mechanism is the one I already found inadequate.** It proposes
gating on per-pixel gradient anisotropy, `|Gx−Gy|/(Gx+Gy)`. That is exactly what
patch 06 on this branch tried first for partition pruning, and the reason it was
abandoned is documented in the patch: fine texture excites the horizontal and
vertical gradients about equally, so on ordinary detailed content their ratio
stays near 1 no matter how the block is actually structured. What worked instead
was the variance of the *row-mean and column-mean profiles*, which measures
structure at the scale a decision can exploit rather than at pixel scale. If DIP
gating is attempted, it should use that formulation from the start. Note also
that the whole prize is +2.45% of runtime, the smallest of the four.

**Proposal 4's premise appears to be incorrect.** It argues that near-collinear
MVs pointing the same temporal direction make compound prediction "mathematically
redundant". Compound prediction's main gain is not motion diversity but *noise
averaging*: averaging two predictors reduces residual energy even when the two
MVs are nearly identical, which is why compound-average modes win on flat,
noisy content. Pruning on collinearity would remove exactly the cases where
averaging is most effective. I would not pursue this without first measuring how
often the winning compound mode has near-collinear MVs.

**Proposal 3 solves a problem that does not need solving.** Test 61
(`disable_smooth_intra`) costs +1.08% BD-rate and saves *no time* (−0.26%, itself
inside the noise band). Test 72 (`enable_winner_mode_for_coeff_opt`) costs +1.12%
for −0.56%. These are not tradeoffs requiring smarter replacements — they are
features that take quality and give nothing back. **Deleting the two assignments
recovers the same ~1.08% and ~1.12% with no new code, no new threshold and no new
failure mode.** That is patch 11 below, and it is the single best
quality-per-effort action in the entire study.

**Proposal 2 is the one worth pursuing**, but for reasons the report understates:
it is the largest remaining lever at +13.09% of runtime, and 22.4 is close enough
to 25 that a better gating signal could close it. See §5.

## 4. The "delete 35 dead assignments" recommendation is unsafe

Two distinct groups are being conflated.

**Resolution-guarded features (Tests 1, 30, 32) are not dead code.** They sit
behind `!is_720p_or_larger`, so they are inactive on CTC A1 and A2 — but they are
*active on sub-720p content*, which CTC never exercises. Deleting them would
regress small-resolution encoding invisibly, because no test in this suite can
see it. The correct conclusion is "CTC cannot measure these", not "these do
nothing".

**Everything else in the group is unproven rather than proven dead.** A feature
with a true saving of 0.3% is indistinguishable from zero at this noise floor. If
even a third of the 35 are worth 0.3%, deleting them costs several percent of
cumulative runtime for no benefit.

Removing them may still be right for maintainability — 35 fewer assignments is a
real simplification — but it should be justified by *code inspection* proving
each is subsumed or unreachable, not by a delta this design cannot resolve. The
one claim of that kind already in the report (`speed_features.c:268`
unconditionally overwriting an earlier assignment) is exactly the right form of
evidence, and is actionable today.

## 5. Where the remaining opportunity actually is

The study asks one question of every feature: *does removing it hurt?* It never
asks the complementary one: *could it be pushed further?* A feature passing at
three times its bar is not a success to leave alone — it is unclaimed speedup.

That second question has already paid on this branch. Transform-partition
stationarity pruning was passing at ratio 23.6; loosening its threshold took
speedup from +2.60% to +4.01% while the ratio **improved** to 26.7, because the
original value had been chosen conservatively rather than measured. (The next
step decayed to 14.7 — the curve is real and has a peak, which is why sweeping
matters.)

Applying that lens to the corrected scoreboard, ranked by unclaimed headroom ×
size of the prize:

| Feature | Ratio / bar | Headroom | Runtime share | Tunable? |
|---|---|---|---|---|
| **`ccso_early_term`** | 86.3 / 30 | **2.9×** | **31%** | **yes — a hard-coded 1.001** |
| `wienerns_iters` | 184.8 / 30 | 6.2× | 18% | no — already at floor (0) |
| `dc_blk_pred` | 77.0 / 20 | 3.9× | 3% | no — already at max level 2 |
| `mv_exh_thresh` | 60.3 / 30 | 2.0× | 7% | yes |
| `disable_ext_part` | 53.4 / 30 | 1.8× | 21% | partially |
| `prune_tx_stats` | 49.0 / 20 | 2.5× | 5% | yes |

**CCSO is the standout and it is not close.** It has the largest runtime share,
nearly 3× headroom, and its aggressiveness is governed by a single hard-coded
scalar — a 0.1% cost tolerance that effectively means "never terminate unless
tied". Wiener NS has more headroom on paper but its iteration count is already
0 and cannot go lower. That is patch 12.

## 6. Patches delivered

Both are standalone, build clean, and touch different files, so they can be run
separately or together.

### `0011-remove-pure-loss-speed-features.patch`

Removes the two Speed-3 assignments that cost >1% BD-rate each while saving no
measurable time. Expected: **−1.0% to −2.2% BD-rate at approximately zero time
cost.** This is a quality patch — it will not move the speedup number and should
be judged on BD-rate with the requirement that encode time not regress beyond
noise. Both sites are kept behind `-DAVM_KEEP_DISABLE_SMOOTH_INTRA=1` and
`-DAVM_KEEP_WINNER_COEFF_OPT=1` for A/B without editing the patch.

### `0012-ccso-early-term-tolerance.patch`

Names the hard-coded CCSO termination tolerance `CCSO_EARLY_TERM_TOLERANCE` and
loosens it from 1.001 to 1.01. One step along a curve, not a tuned value — the
ratio must decay eventually, and finding where it crosses 30 is what a sweep is
for. Sweep with `-DCCSO_EARLY_TERM_TOLERANCE=1.005` etc.;
`-DCCSO_EARLY_TERM_TOLERANCE=1.001` restores today's behaviour exactly. Run at
Speed 2.

## 7. Recommended next round

| Arm | Preset | Question |
|---|---|---|
| `0011` | 3 | Does deleting two pure-loss features recover ~1–2% BD-rate for free? |
| `0012` @ 1.005 | 2 | Does the near step hold the ratio above 30? |
| `0012` @ 1.01 | 2 | Does the further step, and where does decay begin? |

Two `0012` arms bracket the curve for one extra arm and avoid the mistake made
with the stationarity margin, where a single step past the peak cost a whole
round to discover.

**Not recommended without further work:** Proposals 1 and 4, on the arithmetic
and mechanism grounds in §3.

**Worth designing properly:** Proposal 2 (TCQ). It needs 72% of the 1.31% loss
recovered while keeping 70% of the 13.09% saving. The report's proposed gate is
`eob > 16`, which is a crude proxy — TCQ's benefit is concentrated where
quantization decisions are *marginal*, not merely where coefficients are
numerous. A sharper signal is the count of coefficients quantizing to |level| ==
1: those are the positions where a trellis decision actually changes the rate,
whereas large-magnitude coefficients are insensitive to it. Gating on that count
rather than raw eob targets the same blocks TCQ was designed for, at a fraction
of the invocations. That is worth a patch, and I would build it next if wanted.

## 8. A note on the measurement itself

Before the next 100-branch study, moving timing from wall clock to `perf stat -e
instructions` would remove the per-preset bias and the ±1% floor entirely, and
make sub-1% features measurable for the first time. It costs nothing in test
design — the framework already supports it — and roughly 40 of the 101 results
here are currently unusable for want of it.
