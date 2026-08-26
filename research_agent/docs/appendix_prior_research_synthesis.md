# Appendix: synthesis of the prior research corpus

This is the raw analysis of `chengchen-google/avm-patches` that the methodology
in this project was derived from. It is included unedited because it is
evidence, not conclusion: every rule in `av2ra rules` traces to a numbered
lesson here, and anyone who disagrees with a rule should be able to read what it
was derived from and disagree with that instead.

`$CORPUS` below is the root of the prior-research checkout. Line references are
to the files as they stood when this was written; the corpus is append-only, so
they should remain valid.

The condensed, machine-readable form is `av2ra/knowledge/lessons.py`
(30 rules, 18 enforced by code). This document is the long version.

---

**Corpus root** (all paths below are under it, absolute):
`$CORPUS` — referred to as `$P`.
AVM checkout: `/home/user/avm`.

---

# 1. METHODOLOGY LESSONS

These are the rules the corpus actually paid for. Each is a hard rule candidate; I flag which ones the corpus itself later violated.

### M1 — A sub-noise "speedup" is a non-measurement, not a small win
`$P/Claude/experiments/README.md:17-22`: `noise floor (1 sigma): 2.38% / min detectable effect: 3.47% (95%, n=5 per arm)` … "Six of the ten reported 'speedups' (i02 1.3%, i03 1.2%, i04 0.5%, i05 1.2%, i08 1.9%, i10 2.1%) are smaller than the smallest effect the design could resolve. **They are not small wins; they are non-measurements.**"
Enforced in code: `$P/Claude/experiments/bin/screen_timing.py:119-127` computes MDE = `t95(2n-2)·sd·sqrt(2/n)/mean` and prints `UNDERPOWERED … need about n=<N> reps per arm`; verdict ladder at `:143-150` (`PROMOTE` only if CI-low > min-effect; else `real,small` / `SLOWER` / `NOISE`).

### M2 — Two independent timing designs that disagree mean *neither* is usable
`$P/Claude/experiments/DECISIONS.md:455-475`: the round-robin and paired datasets "**contradict each other on seven of ten patches**"; i09 and i10 have 10× spread between reps; i04 is a consistent slowdown in paired data while round-robin called it noise. "**i06 is the only patch that is stable across both designs and both datasets.**" CTC later confirmed the pessimistic reading.

### M3 — Local BD-rate on a small clip is sign-unreliable, not merely imprecise
Round 1 local (416×240, 4 frames, 4 QPs, `$P/Claude/reports/round1-patch-notes.md:28-40`) vs CTC (`$P/Claude/experiments/registry.csv:14-21`):

| patch | local BD | CTC A1 BD | error |
|---|---|---|---|
| i01 | **−0.900%** | **+1.22%** | sign flip, 2.1 pp |
| i04 | −0.597% | +0.11% | sign flip |
| i07 | −0.338% | +0.30% | sign flip |
| i06 | +0.105% | +0.75% (A1) / +1.29% (A2) | 7–12× under |
| all-10 | −0.630% | **+2.50%** | sign flip, 3.1 pp |

Local runtime was equally wrong: all-10 measured −27.78% locally, +20.34% on CTC A1. Rule: **local screening may rank and may prove activation; it may never estimate BD-rate or quote a magnitude.**

### M4 — Bit-exactness is a proof tier that replaces a CTC round for reuse-class patches
`$P/Claude/experiments/README.md:45-56`: "For any patch whose mechanism is *reuse* … the bitstream must be byte-identical. BIT-EXACT → quality risk is zero *by proof*. Never spend a CTC round on it; judge on speed alone. DIVERGES → the refactor changed an encode decision. **That is a bug in the patch, not a quality tradeoff to be measured.**" Tier 0 called i02/i03/i05's `+0.00%` correctly "in 25 minutes … before the CTC round returned" (`$P/Claude/ASSESSMENT-AND-PLAN.md:157-160`). Mechanism taxonomy is recorded per patch: `registry.csv:9-10` — `reuse` = caching/allocation/restore, MUST be bit-exact; `approx` = prunes candidates, has a real quality cost.

### M5 — Bit-exactness is scoped to the configuration tested
`$P/Claude/experiments/CLAUDE.md:36-39`: "i02, i03 and i05 are proven bit-exact **on the tested configuration only** (416x240, 6 frames, cpu-used=3, QP 110/185). That is not universal proof; a cache-key bug in a path that config never reaches would not have been caught." Two QPs are used deliberately: `$P/Claude/experiments/bin/bitexact.sh:32-34` — "Different QPs give different coefficient density and search depth, so a cache-key bug that only manifests at one operating point is still caught."

### M6 — Assert the state you depend on; never assume the cleanup ran
`$P/Claude/experiments/bin/bitexact.sh:59-70` and `DECISIONS.md:396-417`: the first harness reverted with `git checkout -- av2/ aom/ 2>/dev/null`; AVM has no `aom/`, so git rejected the whole pathspec, reverted **nothing**, and the error was swallowed. Patches accumulated; the "baseline" was eventually built with all three applied — and "bit-exact" would have been the *expected* output. "The general rule now applied in the tooling: **assert the state you depend on; never assume the command that was supposed to establish it worked.**"

### M7 — Base drift: textual rot is loud, semantic rot is silent and invalidates numbers
`$P/Claude/README.md:39-60` and `$P/Claude/bin/check-rot.sh:12-30`: upstream moved `a341351 → 425711f` (one commit, "non-RD partition evaluation for real-time mode (#5236)", +187 lines in `av2/encoder/partition_search.c`). Patches 0006/0007 modify that file. **All ten still reported `APPLIES`.** "`APPLIES` was true and told us nothing." Rules: `BASE` is pinned and tracked (`$P/Claude/BASE` = `425711f3469b466139ed748f4bb86b93b579d8af`); `check-rot.sh` prints the *intersection* of upstream-changed files and series-touched files (`check-rot.sh:62-65`) because "the overlap set is where a clean apply is least trustworthy"; **a base change invalidates every recorded number until Tier 0 + Tier 1 are re-run.**

### M8 — The cluster's own EncTime repeatability was never measured, and it bit the project three times
`$P/Claude/0006E_VS_TWO_PASS_ANALYSIS.md:234-277`: same patch, byte-identical 3-way diff, two commits apart, neither touching partition search → `45dc128163: A1 +0.73%/0.11% (ratio 6.6)` vs `ea89c216: A1 +2.96%/0.11% (ratio 26.9)`. "**The BD-rate is the tell.** On A1 it is identical across the two bases (0.11% and 0.11%) while the measured time saving quadrupled. A patch making the same decisions cannot save four times the work." Conclusion: "the uncertainty band on this cluster's EncTime is wider than the effect being measured … the same failure that has now bitten this project three times." The fix has been demanded four times and never run: **one anchor-vs-anchor arm** (`$P/Claude/ROUND_0819_EXT_PARTITION.md:163`, `$P/Claude/ROUND_0821_TRELLIS.md:251-259`).

### M9 — Null arms are free noise-floor estimators; use them
`$P/Claude/ROUND_0819_EXT_PARTITION.md:14-30`: P09/P10/P12 all report `BD-rate exactly 0.00% on both classes` while **all six EncTime readings sit above 100%**, mean 101.63%. "An unchanged bitstream cannot take 1–2% longer to produce." Six null measurements = a direct read of systematic bias on a patched arm: "if it is real and general, every speedup in this project is understated by roughly 1.6 points." Same estimator applied to the 101-feature study (`$P/Claude/SPEED_FEATURE_STUDY_REVIEW.md:32-46`): 40 features with 0.00% BD on both classes give per-preset **bias that flips sign** — Speed 2 −0.529%, Speed 3 −0.440%, Speed 4 **+0.222%** — "The resolution limit is about ±1%."

### M10 — Division by a rounded zero is not an infinite ratio
`$P/Claude/SPEED_FEATURE_STUDY_REVIEW.md:62-84`: "**12 of the 21 reported passes are division-by-noise artifacts. The true count is 9.**" Same error inside the project's own registry: `$P/Claude/experiments/registry.csv:16-17` records `inf` for i08/i04 on A2; `ASSESSMENT-AND-PLAN.md:63-67` corrects it — "those come from BD-rates of −0.01% and −0.03% … a rounding artifact dividing a near-zero speedup by a near-zero quality change. **They are not win-wins.**" GPT reaches the same verdict independently (`$P/GPT/0817/…:64-70`): "Treat these entries as **precision-limited**, not passes."

### M11 — Both classes must pass independently; averages hide class failures
`$P/shared/0819_N02_N06_…md:6` states it as an acceptance criterion: "**Both Class A1 and Class A2 must pass the qualification bar individually** … average performance does not substitute for a single-class failure." `$P/GPT/0817/…:58-62` argued it earlier. **The corpus violates its own rule repeatedly**: `$P/shared/0818_2_…md:33-41` declares P08 "Adopt" on an averaged 24.7× while its A1 ratio is **19.0 < bar 20**; P11 "Must-Adopt" at averaged 62.1 while A2 is **29.3 < bar 35**; P14 "Adopt" at 111.2 while A2 is **31.0 < 35**. `$P/shared/0817_…md:17-21` computes every ratio from combined averages.

### M12 — The bar is a ratio and it is preset-dependent
`Ratio = (100% − EncTime%) / ΔPSNR-YUV BD-rate%`, `$P/Claude/av2_cloud_eda_assessment_report.md:6`; bars **S1 ≥35, S2 ≥30, S3 ≥25, S4 ≥20** (`$P/Claude/experiments/registry.csv:7`). The draft guide adds S0 ≥150 plus absolute BD caps (≤0.03/0.15/0.20/0.30/0.40%) and a 4-quadrant classifier including a *quality-side* bar for Q2 patches (`$P/research_agent/av2_internal_agentic_rd_system_implementation_guide.md:200-203`). Consequence recorded at `$P/Claude/experiments/DECISIONS.md:204-209`: patch 06's speed and BD are flat across presets while the bar rises 20→35, so "**Speeds 1-3 are unreachable and should not consume slots.**"

### M13 — Speedup rises at slower presets, but the ratio can fall faster than the bar rises
`$P/Claude/experiments/DECISIONS.md:355-359`: "+30.74% at Speed 2 vs +20.34% at Speed 4 on A1 for essentially unchanged BD-rate. These heuristics prune redundancy that only exists in deeper searches." Counter-lesson at `$P/Claude/ROUND_0817_OPPORTUNITIES.md:33-36`: the same promotion took `prune_tx_type_using_stats` from ratio 49.0 (S4) to 34.0 (S3) — "**the ratio can fall while the speedup rises.**" Degradation factor ×0.69 was then used as a planning prior (`ROUND_0818_PROMOTION_FRONTIER.md:88-98`).

### M14 — Dry-pass / two-pass inverts the preset direction
`$P/Claude/ROUND_0821_TRELLIS.md:17-32`: 0025 gave *less* speed and *more* damage at S4 than S3 (+1.23%/+0.26% vs +3.26%/+0.14% on A1). "The dry pass at Speed 4 already prunes harder … And the wet pass at Speed 4 trusts the dry pass more … **Dry-pass fidelity reductions get more expensive at faster presets, because there is less downstream correction.** That is the opposite of the direction preset promotion normally runs."

### M15 — Marginal-ratio analysis beats hypotheses about which blocks are unsafe
`$P/Claude/experiments/DECISIONS.md:28-63` — the single most transferable technique in the corpus. For each incremental change, `Δspeed / ΔBD` gives the marginal ratio of the prunes it removed; a change raises the overall ratio **only when its marginal ratio is below the ratio already achieved**:

```
A1  variance floor            +2.04% speed for +0.00% BD   marginal  inf   → worthless
A1  frame gate                +5.89% speed for +0.46% BD   marginal 12.8   → keep the gate
A2  var floor + rect floor 32 +8.95% speed for +0.37% BD   marginal 24.2   → floor was BACKWARDS
A2  frame gate + ext floor 32 +3.36% speed for +0.46% BD   marginal  7.3
```
"Every 06 variant so far was built on a story about which blocks are unsafe, and **two of the three stories were wrong.**" And the physical reading that followed: "A mistaken prune on a 128x128 block commits sixty-four times the area to a worse partition … **Large blocks are not less measurable — they are more expensive to get wrong.**"

### M16 — Split a bundle, don't turn its threshold down
`$P/Claude/ROUND_0820_RESULTS.md:16-35`. `0019` disabled the whole extended-partition family: +15.92%/+0.52%, ratio 30.6, failed. `0021` disabled **only the uneven 4-way members**: kept **39% of the speed for 6% of the quality cost** → ratio 207.7 (A1) / 111.7 (A2), the project's best result. "**Cost and benefit are not proportionally distributed inside a feature group** … when a bundled feature fails on ratio, the first move should be to split it and find the cheap members, not to turn its threshold down. Every threshold move on this project has slid along the trade-off; this split moved off it."
The counter-example is explicit: 06c cut A2 speed 66% for a 64% BD cut → ratio got *worse*, 14.4 → 13.5 (`DECISIONS.md:106-112`).

### M17 — Per-sequence spread is a first-class result, and it tells you whether a split can work
`$P/Claude/experiments/CLAUDE.md:56-57`: "Report **per-sequence spread**, not just the mean. A good average hiding one bad sequence is a regression risk, not a win."
Its analytic use, `$P/Claude/ROUND_0819_EXT_PARTITION.md:59-78`: `A1 speedup mean 15.90% sd 1.93% CV 12%` vs `A1 BD mean 0.53% sd 0.47% range −0.17…1.22 CV 89%` — "The speed benefit is nearly uniform across content; the quality cost varies sevenfold," and "**Exempting one sequence in eight clears the bar**" (30.6 → 37.2). Same for 0025 (`ROUND_0821_TRELLIS.md:36-56`): speed/BD per-sequence correlation `r = −0.07 (A1), −0.20 (A2)`; the three worst sequences carry 66% of the BD but only 35% of the speedup; exempting `Crosswalk` alone takes 22.8 → 29.6.

### M18 — Assignment moves are cheap; new per-block conditionals are guilty until proven innocent — *and this rule was later retracted*
Stated `$P/Claude/ROUND_0818_PROMOTION_FRONTIER.md:13-45`: promotions 6/7 pass, conditional gates 0/5 deliver. "**an assignment move costs nothing at runtime, while a conditional is evaluated on every block whether or not it fires.**"
Retracted `$P/Claude/ROUND_0819_EXT_PARTITION.md:9-26`: three of the five (P09/P10/P12) were inert (0.00% BD with >100% EncTime) — "they tested nothing, and the rule I derived from them has no support." **Memory rule: a conclusion built on arms that never fired must be marked retracted, not carried forward.** The surviving operational form (`ROUND_0818:132-137`): "**measure the gate's overhead first**, on a build where it always returns 'don't prune'; if that build is slower than baseline, the idea is dead regardless of the gating logic."

### M19 — A patch that is bit-identical is a no-op; say so before the round, not after
`$P/Claude/ROUND_0820_RESULTS.md:45-49` (0024): "I said in its header: *'if the CTC arm comes back bit-identical the patch is a no-op and should be dropped rather than tuned.'* It did. Dropped. **Flagging the verification gap up front cost one arm instead of a round of tuning something that never ran.**" Also `$P/Claude/PROFILE_DRIVEN_OPPORTUNITIES.md:116-132`: "**I could not make this fire on my test clip.** … That is exactly the signature I criticised in P09/P10/P12, and I am not going to present it as a speedup on that basis."

### M20 — Diff the target assignment against the *actual anchor* before authoring a promotion
`$P/Claude/ROUND_0819_EXT_PARTITION.md:145-150`: "`0018` is retired, and the lesson is mine. `exhaustive_searches_thresh` was already updated upstream by PR #5263, so my patch was a no-op against the current base. **I authored it against my local tree without checking whether the feature had moved.**" Same class: `$P/GPT/0820/…:15-23` (N07/N08 already upstream at `247c63a3c`, `a5d3d1c3a`); `$P/GPT/rebased_round3_screening_and_eda_launch_report.md:30-40` (R3-09's entire measured speedup belonged to already-merged `ca23f90edc`).

### M21 — Union-first for go/no-go, then attribution
`$P/Claude/experiments/README.md:111-124`: "**Run the union first, not last** … the union answers it in *one arm*. Ten one-at-a-time arms cost ten times as much and cannot answer it at all." Then the combination arithmetic that killed the family (`DECISIONS.md:340-353`): speedups compound multiplicatively, BD-rate adds; backing i06 out of all-10 leaves "nine patches collectively buying about a third of i06's speedup at more than twice its quality cost … **the combination is i06 plus ballast.**"

### M22 — CTC-slot economics
`$P/Claude/experiments/README.md:126-137`: encode the anchor **once** and reuse it across rounds; "arms within a round are independent, so round *latency* is set by the slowest arm, not the number of arms. **Prefer few rounds with many arms** … Every patch killed at Tier 0/1/2 is an arm that does not have to be scheduled." And `CLAUDE.md:41`: "Your job is to make sure every CTC slot answers a question nothing cheaper could have."

### M23 — Profile before theorising; a feature-ablation map is not a hotspot map
`$P/Claude/PROFILE_DRIVEN_OPPORTUNITIES.md:10-38`: trellis quantisation is **~47% of instructions retired**; "quantisation is larger than partition search, mode search and the loop filters **combined**. And **every patch this project has produced … has attacked something else.** Twenty-four patches into this effort, we have been optimising around the edges of the thing that actually dominates." Method note at `:154-159`: the 101-feature study "measure[s] *what a feature is worth* rather than *where time is spent*. Those are different questions, and the gap between them is why 47% of the encoder went unexamined for twenty-four patches." Cost: **25 minutes of wall clock, no CTC slot** (`:161`). Raw profile: `$P/Claude/PROFILE_5d628d8_cpu4.txt` (191.08 Gi total; `av2_trellis_quant` 9.95%, `av2_decide_states_q1_avx2` 7.47%, `__memset_avx2_unaligned_erms` 4.63%).

### M24 — "No speed-feature references in the hot file" is a hypothesis, not a finding
`$P/Claude/ROUND_0821_TRELLIS.md:114-158` corrects M23's sharpest claim: two speed features *do* govern per-block trellis (`perform_coeff_opt`, `perform_coeff_opt_based_on_satd`) but `tcq_enable(..., plane, TX_CLASS_2D)` is passed `TX_CLASS_2D` as a **literal**, so for luma it is always true and both gates are bypassed. "Luma 2D, which holds essentially all of the coefficient mass and all of the trellis time, is exempt at every preset. **This is not an oversight to reverse** — under TCQ the decoder dequantises with the state machine; an encoder that quantised a luma block scalar-only and kept its scalar `dqcoeff` would drift."

### M25 — Consistency fixes beat new heuristics
`$P/Claude/ROUND_0820_RESULTS.md:65-106`: `choose_tx_size_type_from_rd` declares `skip_trellis = x->apply_dry_pass_shortcuts ? 1 : 0` while `tx_type_rd` in the same file hard-coded `skip_trellis = 0`. "**This is a consistency fix, not a new heuristic.** It applies the policy the dry pass already declares, at a site that wasn't honouring it." Also explains why 0023 failed: the dry pass was already skipping trellis on its main path, so 0023 "wasn't attacking quantisation at all. It was cutting ranking fidelity."

### M26 — Write down the falsifier before the run
`$P/Claude/ROUND_0821_TRELLIS.md:235-236` (0027): "**If BD-rate comes back above roughly 0.3% at shift 3, that premise is wrong** — the trellis is reordering candidates, not merely improving them — and the right response is to retire the patch, not tune it, because the only safer setting (shift 2) cannot pay for its own overhead." Also `$P/Claude/ROUND_0821_TRELLIS.md:94-104`: the arm is pre-analysed as `f_s/f_b ≥ 1.07` with the required concentration per threshold (1.9× vs 1.1×), which selects the default before spending the slot.

### M27 — Bracket a threshold curve with two arms; never take a single step past a peak
`$P/Claude/experiments/DECISIONS.md:14-26`: MARGIN 4 → 23.6/25.2, MARGIN 2 → **26.7/51.0 (peak)**, MARGIN 1 → 14.7/11.8. "The decay I said had to arrive eventually arrived at exactly the next step. **Adopt 09b. No further tuning of this parameter is warranted** — the curve has been bracketed on both sides and the top is measured, not inferred." Design rule: expose the knob as `-D<MACRO>=N` so the known-good point is reproducible with no patch edit (`registry.csv:30`: `-DTX_PART_STATIONARITY_MARGIN=2 restores the 09b operating point exactly`; also `AVM_S1_EXT_PART_PYRAMID_MIN`, `AVM_TX_DRY_TRELLIS_MIN_DIM`, `AVM_TX_PRE_TRELLIS_GATE_SHIFT`, `CCSO_EARLY_TERM_TOLERANCE`).

### M28 — Do not sum noisy near-zero estimates
`$P/Claude/ROUND_0818_PROMOTION_FRONTIER.md:48-59`: three features reported −0.00%, +0.15%, −0.31% time; predicted net ≈ free; **actual cost 1.9%**. "Summing noisy near-zero measurements does not average the noise away when you are summing *estimates of different quantities* — it compounds the uncertainty while I treated it as a point estimate."

### M29 — Score your own predictions, in both directions
`$P/Claude/ROUND_0820_RESULTS.md:133-148` is a literal prediction scorecard (0021 right, 0024 right, 0023 wrong mechanism, 0020 "direction right, magnitude off by 5×"). `$P/Claude/ASSESSMENT-AND-PLAN.md:19-21`: "**my round-1 screening was not good enough to justify the CTC round it triggered**, and the process changes in this repo matter more than any individual patch in it."

### M30 — Resolution guards mean CTC *cannot see* a feature; that is not evidence it is dead
`$P/Claude/SPEED_FEATURE_STUDY_REVIEW.md:154-168`: Tests 1/30/32 sit behind `!is_720p_or_larger`; deleting them "would regress small-resolution encoding invisibly, because no test in this suite can see it. **The correct conclusion is 'CTC cannot measure these', not 'these do nothing'.**"

### M31 — Speed features can be self-defeating: worse prediction ⇒ more downstream work
`$P/Claude/ROUND_0817_OPPORTUNITIES.md:40-46`: "**a speed feature that degrades prediction quality can be self-defeating**, because larger residuals make downstream transform and partition search more expensive, not less." Evidence: restoring smooth intra was **−1.08% BD *and* +1.25% faster** (`$P/shared/0817_…md:19`).

### M32 — Right subsystem, wrong lever
`$P/Claude/ROUND_0817_OPPORTUNITIES.md:48-64` (0012, CCSO): the headroom argument was right, "**the choice of lever was wrong.** … filtered costs evidently differ from the running best by far more than 1%, so widening the tolerance to 1.01 almost never flips the comparison." GPT's `0006` got +1.13% from the same function by changing **search order**: "when a search is already terminating early, what matters is *what it has evaluated before it stops*, not how narrowly it decides to stop."

### M33 — An upstream mechanism can *substitute* for your patch, not stack with it
`$P/Claude/0006E_VS_TWO_PASS_ANALYSIS.md:140-152`: 0006e predicts which shapes are worth evaluating from source structure; the two-pass dry pass finds out by measuring. "**A measured proxy dominates a heuristic one** … they are not complementary optimisations that stack, they are competing solutions to one problem, and the better one landed." Plus the leverage inversion (`:114-138`): under two-pass an 0006e prune *steers a shape the expensive pass then trusts* — "the savings became cheap … and the errors became expensive," visible as **BD-rate going UP while pruning less**.

### M34 — Gate anti-correlation between your patch and the base
`$P/Claude/0006E_VS_TWO_PASS_ANALYSIS.md:103-112`: two-pass runs only on inter frames; 0006e's frame gate disables pruning at `layer_depth <= 0` (key frames). "**The frames where two-pass is off are exactly the frames where 0006e turns itself off.** The two gates are almost perfectly anti-correlated, and neither knows about the other."

### M35 — Prefer `perf stat -e instructions` over wall clock — demanded 5×, never done
`$P/Claude/experiments/README.md:58-68` ("reproducible to well under 0.1% … immune to co-tenancy, frequency scaling and scheduler drift"); repeated at `DECISIONS.md:490-492`, `SPEED_FEATURE_STUDY_REVIEW.md:56-60` and `:256-262` ("roughly 40 of the 101 results here are currently unusable for want of it"), `ROUND_0817_OPPORTUNITIES.md:179-186`. The framework already supports it: `use_perf_util: true` at `/home/user/avm/tools/convexhull_framework/src/config.yaml:66` (read at `Config.py:114`).

### M36 — Local timing hygiene that did work
`$P/Claude/reports/round1-patch-notes.md:70-86`: wall clock unusable (71 s vs 97 s for the same binary); user CPU repeatable ~1% back-to-back but drifting over a sweep; the workable protocol was **round-robin (every binary once per round) pooled with paired runs, reduced by minimum not mean, with equal sample counts per arm**. GPT's independent version (`$P/GPT/AV2_0824_STANDALONE_SPEED_RESEARCH/validation/README.md:24-29`): "Pin baseline and candidate to isolated cores, run them concurrently, and **swap cores on the next pair** … do not infer whole-encoder speed from this number."

### M37 — Amdahl gate before a cloud run
`$P/GPT/AV2_0824_STANDALONE_SPEED_RESEARCH/…Report.md` §3.5: "a `38.8%` kernel-time reduction yields a `2%` whole-encode time reduction only if this kernel accounts for at least about `5.2%` of baseline encode time (`0.02 / 0.388`). **This gives a concrete profile gate before a costly cloud run.**" It was right to be cautious: the CTC result was A1 +1.20%, A2 +0.34% (`$P/shared/0824_0002_…md:21-22`).

### M38 — Tier 2 (shadow-mode decision regret) is specified and still unbuilt
`$P/Claude/experiments/README.md:73-99`: run the full baseline search so the bitstream is unchanged, while recording what the heuristic *would* have chosen → `Σregret/ΣRD` and `skipped/total` per decision site, **zero run-to-run variance, one encode per (clip,QP), attributable per patch even with several patches enabled**. Honest limit: "a cheap **necessary** condition, not a sufficient one — high regret kills a patch outright, low regret still has to be confirmed by CTC." Status: `DECISIONS.md:497-500` — "**the highest-value remaining piece of infrastructure**," never implemented.

### M39 — Ledger discipline
`$P/Claude/experiments/CLAUDE.md:51-58`: on results — write numbers into `registry.csv`, confirm `BASE` matches the SHA they were measured on, append a dated entry to `DECISIONS.md` (what was killed, kept, why), report per-sequence spread, commit. "**This repo is the only thing that survives you.**" `DECISIONS.md:1-5`: "Append-only… If a patch was killed, the reason must be recorded here so nobody spends a CTC slot rediscovering it." First command of any session: `bin/check-rot.sh /path/to/avm-checkout` (`CLAUDE.md:12-14`).

### M40 — Known internal inconsistency the new system must not inherit
The bit-exact three have **two different sets of CTC numbers** in the corpus: `registry.csv:19-21` (i02 A1 −2.34%, i03 −2.50%, i05 −1.39%; A2 +0.41/+0.07/−1.01 *partial*) vs `$P/Claude/av2_cloud_eda_assessment_report.md:29-32` (Patch 02 A1 −2.43%, Patch 03 −2.34%, Patch 05 −1.38%; A2 +0.47/+0.22/−1.62). Nobody reconciled them. A results ledger needs a single writer and an invocation-ID provenance field per row.

---

# 2. RESULTS LEDGER

Convention: **speedup% = 100 − EncTime%**; BD = PSNR-YUV BD-rate (positive = quality cost); ratio = speedup/BD. Bars: S1 35, S2 30, S3 25, S4 20.

### 2.1 Claude series — anchor `d6b40b789381601440e4ce2cc1164cd57e8c3c7d` (A1 = 4K 17f, A2 = 1080p 33f)

| id | mechanism | preset | A1 sp/BD (ratio) | A2 sp/BD (ratio) | verdict |
|---|---|---|---|---|---|
| i01 | tx-type RD saturation exit (`tx_search.c`) | 4 | +4.27 / +1.22 (3.5) | +5.05 / +0.88 (5.7) | DISCARD — BD 4–6× too high |
| i02 | IST sparse coeff restore (*reuse*) | 2 | −2.34 / **0.00** | +0.41ᵖ / 0.00 | DISCARD — bit-exact but slower at 4K |
| i03 | PICK_MODE_CONTEXT arena alloc (*reuse*) | 2 | −2.50 / **0.00** | +0.07ᵖ / 0.00 | DISCARD — same |
| i04 | sub-pel curvature gate (`mcomp.c`) | 4 | −1.31 / +0.11 | +0.05 / −0.03 | DISCARD — slowdown |
| i05 | full-pel search memo (*reuse*) | 2 | −1.39 / **0.00** | −1.01ᵖ / 0.00 | DISCARD — memo key correct, hit rate never repays |
| i06 | orientation partition pruning (source row/col-mean profile variance) | 4 | +14.00 / +0.75 (18.7) | +18.52 / +1.29 (14.4) | IMPROVE — only patch with magnitude |
| i06 | (same) | 3 / 2 / 1 | +12.85/0.86 (14.9); +14.77/0.81 (18.2); +14.85/0.96 (15.5) | +18.21/1.48 (12.3); +18.81/1.50 (12.5); +18.97/1.48 (12.8) | fails every preset |
| i07 | parent-normalised RD-density partition termination | 4 | +0.22 / +0.30 (0.7) | +2.84 / +0.76 (3.7) | DISCARD |
| i08 | DRL dispersion budget | 4 | −0.45 / +0.04 | +0.91 / −0.01 (`inf`✗) | DISCARD — A2 "inf" is a rounding artifact |
| i09 | tx-partition residual stationarity (margin 4) | 4 | +2.60 / +0.11 (**23.6**) | +2.27 / +0.09 (**25.2**) | PROMOTE (S4 only) |
| i09 | (same) | 3 / 2 / 1 | **−0.27** (slower); +0.79/0.09 (8.8); +0.59/0.06 (9.8) | +1.38/0.06 (23.0); +3.37/0.13 (25.9); +2.95/0.10 (29.5) | S4 only |
| i10 | adaptive ME search range | 4 | −0.14 / −0.04 | −0.71 / −0.20 | DISCARD — slower both |
| i02+03+05 | bit-exact suite | 2 | −2.55 / 0.00 | −1.17ᵖ / 0.00 | DISCARD |
| ALL10 | union | 4 / 3 / 2 | +20.34/2.50 (8.1); +25.18/3.12 (8.1); +30.74/2.55 (12.1) | +24.00/3.45 (7.0); +29.25/3.85 (7.6); — | DISCARD — "i06 plus ballast" |
| i09b | lazy eval + MARGIN 4→2 | 4 | +4.01 / +0.15 (**26.7**) | +3.57 / +0.07 (**51.0**) | **ADOPT-FINAL** (Pareto peak) |
| i09c | MARGIN 2→1 | 4 | +4.40 / +0.30 (14.7) | +4.71 / +0.40 (11.8) | DISCARD — past the knee |
| i06b | + absolute variance floor + frame gate | — | never CTC'd | — | superseded |
| i06c | + resolution-scaled block floor (16@4K / 32@1080p) | 4 | +6.07 / +0.29 (**20.9**) | +6.21 / +0.46 (13.5) | PASS-A1-ONLY |
| i06c+i09b | combination | 4 | +11.87 / +0.54 (**22.0**) | +9.80 / +0.72 (13.6) | best shipping candidate of round 2 |
| i06d | un-blunted (frame gate removed) | 4 | +11.96 / +0.75 (15.9) | +9.57 / +0.92 (10.4) | DISCARD |
| i06d+i09c | combination | 4 | +17.57 / +1.08 (16.3) | +14.38 / +1.11 (13.0) | DISCARD |
| i06e | size-graded (var floor deleted, gate restored, anisotropy ×2@32-63 ×4@64+) | 4 | +2.82 / 0.10 (28.2) | +5.20 / 0.10 (52.0) | passes on this anchor |

ᵖ = partial run (~85–89%).

### 2.2 i06e across anchors — the base-drift ledger (`$P/Claude/0006E_VS_TWO_PASS_ANALYSIS.md:55-56, 234-236`)

| anchor | A1 | A2 | note |
|---|---|---|---|
| `d6b40b7893` | +2.82 / 0.10 (28.2) | +5.20 / 0.10 (52.0) | pass both |
| `45dc128163` (two-pass #5253) | +0.73 / 0.11 (6.6) | +1.64 / 0.15 (10.9) | fail both — 3 structural mechanisms + suspect timing |
| `ea89c216` (+2 commits, none touching partition search) | **+2.96 / 0.11 (26.9)** | **+2.81 / 0.09 (31.2)** | pass both — irreconcilable with the row above |
| `ea89c216` as GPT `P06` | +3.05 / 0.11 (27.7) | +2.89 / 0.09 (32.1) | independent confirmation |

### 2.3 GPT 0817 series — anchor `fe1bfdee54` (`$P/shared/0817_…md:17-25`)

| id | mechanism | preset | A1 sp/BD | A2 sp/BD | verdict |
|---|---|---|---|---|---|
| 0001 | promote `prune_tx_type_using_stats=1` S4→S3 | 3 | +6.55 / +0.15 | +5.68 / +0.22 | **WINNER** (avg ratio 34.0) |
| 0002 | promote `dc_blk_pred_level=1` S4→S3 | 3 | +3.39 / −0.01 | +2.76 / +0.01 | **WINNER** (~308) |
| 0003 | frame-adaptive TCQ on L0/L1 | 3 / 4 | −4.99 / −0.89; −2.95 / −0.98 | −6.25 / −0.96; −3.96 / −1.14 | quality booster, **slower** |
| 0004 | restore smooth intra | 3 | +1.77 / **−1.28** | +0.72 / **−0.88** | **PARETO REPAIR** (faster *and* better) |
| 0005 | compound prune level 2→1 | 4 | −2.93 / −0.80 | −8.74 / −0.87 | quality booster, slower |
| 0006 | CCSO BO-first ordering under early exit | 2 | +0.64 / −0.14 | +1.61 / −0.10 | clean small win |
| 0007 | all-combined | 3 / 4 | +1.58 / −2.11; −5.80 / −3.08 | +3.43 / −1.63; −10.70 / −2.80 | quality engine, not speed |

### 2.4 Claude 0011–0016 — anchor `ea89c216c6` (`$P/shared/Claude_eda_report_patches_0013_to_0016.md:14-17`)

| id | mechanism | preset | A1 sp/BD (ratio) | A2 sp/BD (ratio) | verdict |
|---|---|---|---|---|---|
| 0011 | delete 2 pure-loss S3 assignments | 3 | never run standalone | — | (mechanism confirmed by GPT 0004) |
| 0012 | `CCSO_EARLY_TERM_TOLERANCE` 1.001→1.01 | 2 | no change at all | no change | DISCARD — wrong lever |
| 0013 | `dc_blk_pred_level=2` @ S3 | 3 | +5.22 / +0.04 (**130.5**) | +1.99 / −0.00 (pure gain) | **LAND** |
| 0014 | warp diamond @ S3 | 3 | +0.54 / −0.03 | +0.37 / +0.02 (18.5) | borderline / below noise |
| 0015 | tx-stat pruning @ S2 | 2 | +4.02 / +0.16 (25.1 ✗bar 30) | +4.46 / +0.13 (**34.3**) | PASS A2 only |
| 0016 | remove 3 self-defeating S4 features | 4 | −1.72 / −0.08 | −2.00 / −0.14 | quality patch costing 1.9% time (13:1 recovery) |

### 2.5 GPT 0818_2 — anchor `ea89c216` (`$P/shared/0818_2_…md:27-47`)

| id | mechanism | preset | A1 sp/BD (ratio) | A2 sp/BD (ratio) | strict per-class verdict |
|---|---|---|---|---|---|
| P05 | tx stationarity margin 2 (= i09b) | 4 | +7.68 / +0.14 (54.9) | +8.34 / +0.13 (64.2) | **PASS both** — largest clean win |
| P06 | orientation size-graded (= 0006e) | 4 | +3.05 / +0.11 (27.7) | +2.89 / +0.09 (32.1) | **PASS both** |
| P08 | intra top-2 all contexts | 4 | +7.81 / +0.41 (**19.0**) | +7.02 / +0.19 (36.9) | **FAILS A1** (report says "Adopt") |
| P11 | promote CCSO early-term to S1 | 1 | +33.32 / +0.33 (101.0) | +11.42 / +0.39 (**29.3**) | **FAILS A2** (report says "Must-Adopt") |
| P14 | WienerNS zero-refine @ S1 | 1 | +4.62 / +0.02 (231.0) | +0.93 / +0.03 (**31.0**) | **FAILS A2** |
| P09 | frame-aware CCSO tolerance | 2 | −2.33 / 0.00 | −1.41 / −0.00 | INERT (never fired) |
| P10 | leaf exhaustive-MV threshold | 2 | −1.73 / −0.00 | −1.59 / +0.00 | INERT |
| P12 | leaf disable ext partitions | 1 | −1.45 / 0.00 | −1.11 / 0.00 | INERT |

### 2.6 GPT 0819 N-series (`$P/shared/0819_N02_N06_…md:31-36`)

| id | mechanism | preset | A1 (ratio) | A2 (ratio) | verdict |
|---|---|---|---|---|---|
| N06 | intra top-3 shortlist | 3 | +3.23 / +0.11 (29.4) | +3.72 / +0.09 (41.3) | **QUALIFIED — adopt** |
| N02 | `dc_blk_pred_level=1` @ S2 | 2 | +2.41 / +0.03 (80.3) | **+0.15** / +0.02 (7.5) | DISQUALIFIED — 9/19 A2 clips *slower* |

### 2.7 Claude 0018–0027 — anchors `ea89c216c6`, `5d628d840b`, `d1445fc1c7`

| id | mechanism | preset | A1 sp/BD (ratio) | A2 sp/BD (ratio) | verdict |
|---|---|---|---|---|---|
| 0018 | exhaustive-MV thresh → S1 | 1 | 0.00 (EncTime 100.93) | 0.00 (100.20) | **NO-OP** — already upstream (PR #5263) |
| 0019 | disable extended partitions @ S1 | 1 | +15.92 / +0.52 (30.6 ✗) | +22.75 / +0.60 (**37.9**) | fails A1; per-seq BD range −0.17…+1.22 |
| 0020 | pyramid-gated (`pyramid_level>=3`) ext-part | 1 | +2.02 / +0.00 | +0.71 / −0.00 | passes, prize small — leaf frames are cheap |
| 0021 | disable **uneven 4-way only** (`HORZ_4A/4B`,`VERT_4A/4B`) | 1 | +6.23 / +0.03 (**207.7**) | +6.70 / +0.06 (**111.7**) | **BEST RESULT OF THE PROJECT** |
| 0022 | scope 0019 to ≤1080p | 1 | never run | — | fallback, unspent |
| 0023 | dry-pass tool reduction (warp/precision/wedge) | 3 | +6.16 / +0.59 (10.4) | +6.14 / +0.57 (10.8) | FAIL — bought speed by degrading ranking |
| 0024 | tx-type ranking without trellis | 4 | 0.00 / 0.00 | 0.00 / 0.00 | **NO-OP** — call site not reached on CTC RA |
| 0025 | honour dry pass in `tx_type_rd` (`skip_trellis`) | 3 | +3.26 / +0.14 (23.3) | +4.23 / +0.17 (24.9) | near-miss on both |
| 0025 | (same) | 4 | +1.23 / +0.26 (4.7) | +3.86 / +0.20 (19.3) | FAIL — wrong preset for the family |
| 0026 | 0025 split by tx block size ≥16 | 3 | +1.17 / +0.26 (4.5) | +2.01 / +0.23 (8.7) | RETIRE — split lost speed **without** recovering BD |
| 0027 | pre-trellis RD gate, shift 3 (14% margin) | 3 | +3.79 / +0.09 (**42.1**) | +3.03 / +0.15 (20.2) | best current candidate; misses A2 |
| 0027 | shift 3 | 4 | +1.14 / +0.04 (28.5) | +0.92 / +0.10 (9.2) | fails A2 |
| 0027 | shift 4 (7% margin) | 3 | +4.71 / +0.17 (27.7) | +5.40 / +0.37 (14.6) | fails A2 — BD grew faster than speed |
| 0027 | shift 4 | 4 | +1.34 / +0.03 (44.7) | +2.06 / +0.26 (7.9) | fails A2 |
| 0027 | shift 2 (33% margin), local only | 3 | −2.68% (gate costs ~2.7% of encode) | — | the patch's own floor |

### 2.8 GPT 0820 / 0824 — anchors `5d628d840b`, `d1445fc1c7`

| id | mechanism | preset | A1 | A2 | verdict |
|---|---|---|---|---|---|
| 0820/0001 | non-DCT TCQ rank-then-refine (12.5% window) | 4 | **−1.07** / −0.04 | −0.25 / +0.00 | REJECT — 7/8 A1 and 13/19 A2 clips regressed; local screen had said +10.58% |
| LR-PYR (upstream `2b00abb`) | restoration-unit-size pruning by pyramid level | 4 | +4.379 / 0.00 (published) | +2.421 / 0.00 (published) | not independently re-run |
| 0824/0002 TF-SSE2 | hoist invariants + 32×32→64×64 SSE2 temporal filter; fix dispatch check (`TF_BLOCK_SIZE == BLOCK_32X32` never matched `BLOCK_64X64`) | 4 | **+1.20 / 0.00** | +0.34 / 0.00 | bit-exact (kernel 1.60× vs SSE2, 2.45× vs C); encoder gain small |

### 2.9 Rebased round 3 (GPT, anchor `d6b40b7893`) — `$P/GPT/eda_rebased_round3_final_results_report.md:14-16`

| id | mechanism | A1 | A2 | verdict |
|---|---|---|---|---|
| R3-04 | key-frame intra top-2 | +1.80 / +0.22 | +2.09 / −0.05 | clean but sub-threshold |
| R3-02 | diversity inter beam | −6.82 / −0.18 | −6.11 / −0.10 | quality engine, +6.5% cost |
| R3-05 | R3-01+02+04 stack | −0.99 / +0.06 | −1.55 / +0.02 | stack ruined by R3-02 |
| R3-09 | CCSO seeded order | −0.03 | +0.16 | effect belonged to already-merged `ca23f90edc` |
| R3-07 / R3-08 | two-pass backports | crashed A2 / +0.4% A1 | — | bundled unmerged PRs |

### 2.10 101-feature ablation study — anchor `fe1bfdee54`, 208 runs

Baseline preset ladder (`$P/shared/AV2_Speed_Feature_Tradeoff_Report.md:35-38`): S1 +1.32%/41.26% EncTime (A1), S2 +5.94%/16.62%, S3 +11.35%/13.14%, S4 +14.83%/10.33%.
Claimed 21/101 pass; corrected to **9** (`$P/Claude/SPEED_FEATURE_STUDY_REVIEW.md:85-99`) or **6** under GPT's stricter denominator+per-class rule (`$P/GPT/0817/…:100-107`). The nine survivors:

| feature | preset (bar) | saving | BD | ratio |
|---|---|---|---|---|
| `ccso_early_term` | 2 (30) | +31.07 | +0.36 | **86.3** — 31% of encode time |
| `wienerns_iters=0` | 2 (30) | +18.48 | +0.10 | **184.8** — already at floor |
| `disable_ext_partitions` | 2 (30) | +20.84 | +0.39 | 53.4 |
| `mv_exh_thresh` | 2 (30) | +7.23 | +0.12 | 60.3 |
| `prune_2d_txfm` | 2 (30) | +1.38 | +0.04 | 34.5/39.3 |
| `best_rd_chroma` | 3 (25) | +0.74 | +0.02 | 37.0 |
| `prune_tx_type_using_stats` | 4 (20) | +4.90 | +0.10 | 46.6/49.0 |
| `dc_blk_pred_level` | 4 (20) | +3.08 | +0.04 | 77.0 |
| `warp_search_dia` | 4 (20) | +1.65 | +0.08 | 20.6 |

"**CCSO, Wiener NS and extended-partition disabling alone account for ~70% of all measured speed-feature value.**" Destructive shortcuts: `skip_intra_dip_search` +1.05% BD for +2.45% (2.3); `disable_tcq` +1.31% for +13.09% (10.0); `prune_comp_using_best_single_mode_ref` +1.13% for +8.53% (7.5). Pure-loss: `disable_smooth_intra` +1.08% BD for **−0.26%** time; `enable_winner_mode_for_coeff_opt` +1.12% for **−0.56%**.

**Everything that has ever cleared both class bars, in one list:** i09/i09b (P05), 0021, 0013 (A2 pure-gain), N06, i06e/P06, 0004+0002+0001-class promotions (on averaged numbers), LR-PYR (published). Everything else either failed a class, failed the bar, was inert, or was slower.

---

# 3. FAILURE TAXONOMY

12 distinct modes; counts are distinct arms/patches/features in the corpus.

| # | mode | count | exemplars & signature |
|---|---|---|---|
| **F1** | **Sub-noise non-measurement reported as a result** | 6 patches + 12 study "passes" + 40 study rows | i02/i03/i04/i05/i08/i10 (`experiments/README.md:20-22`); ratios formed by dividing by a rounded 0.00% BD (`SPEED_FEATURE_STUDY_REVIEW.md:62-84`). *Signature: |effect| < MDE, or BD rounds to 0.00.* |
| **F2** | **Regime inversion — small-clip intuition inverted at 4K/CTC preset** | 3 | All three bit-exact reuse patches were **slower** at 4K: memo lookup unpaid by hit rate, one big arena worse for locality than many cache-resident allocations, sparse-restore bookkeeping worse than a bulk clear (`DECISIONS.md:318-338`). *"Future ideas should be sanity-checked at 4K before they are written, not after."* |
| **F3** | **BD cost structurally too high; gap not tunable** | 7 | i01 (3.5/5.7), i07 (0.7/3.7), 0023 (10.4/10.8), 0026 (4.5/8.7), study #34 DIP (2.3), #35 disable_tcq (10.0), #85 compound (7.5). *Signature: ratio ≤ ½ bar.* |
| **F4** | **Net slowdown — added work exceeds removed work** | 13 | i04, i08(A1), i10, 0820/0001 (proxy became an extra pass), GPT-0003, GPT-0005, R3-02, R3-05, 0027@shift2 (gate costs 2.7% of encode), plus 3 local-only rejects (DC-only quant fast path ~0.5% slower, `eob==1` TCQ ctx bypass <1%, dry-pass trellis-consistency 17.56→20.98 s). |
| **F5** | **Inert patch — never fired, tested nothing** | 8 | 0024 (`prune_tx_type_est_rd && num_allowed>2` not reached on CTC RA), 0018 (feature moved upstream), 0012 (knob not binding), R3-09, P09/P10/P12 (0.00% BD with 101–102% EncTime), 0006e under two-pass (`forced_partition != PARTITION_INVALID` early-returns for every block ≥32×32). *Signature: BD exactly 0.00% on both classes for an `approx` patch.* |
| **F6** | **Self-defeating speed feature — costs quality AND time** | 4 | study #61 `disable_smooth_intra`, #72 `win_coeff_opt`, #94 `coeff_opt_satd`, #97 `cdef_pick_lvl3`. Removal is a *quality* patch (0016: −0.14% BD for +1.9% time). |
| **F7** | **Measurement invalidated by base drift** | 5 | 0006e's 2.2-ratio-point swing across two irrelevant commits; `a341351→425711f` semantic rot with all-`APPLIES`; 0018 vs PR #5263; R3-09 vs `ca23f90edc`; R3-07/08 vs PR #5253. |
| **F8** | **Blunt threshold move slides along the tradeoff** | 4 | 06c (A2 14.4→13.5 with speed −66%/BD −64%), 09c (26.7→14.7), 0027 shift 3→4 (A2 20.2→14.6), 0026 (23.3/24.9→4.5/8.7). *"Threshold moves change the operating point, not the frontier."* |
| **F9** | **Right subsystem, wrong lever** | 4 | 0012 tolerance vs 0006 ordering; 0023 tool-set vs 0025 trellis; 0024 wrong call site; DIP gradient-anisotropy vs the row/column-mean profile that actually worked. |
| **F10** | **Class-asymmetric result** | 10 | 0019 (A1 30.6 / A2 37.9), 0015 (25.1/34.3), N02 (80.3/7.5), 06c (20.9/13.5), 06c+09b (22.0/13.6), 0027@s3 (42.1/20.2), 0025@s4 (4.7/19.3), P08 (19.0/36.9), P11 (101.0/29.3), P14 (231.0/31.0). Both directions occur — 4K-only and 1080p-only. |
| **F11** | **Plan-side arithmetic/projection error (code fine, forecast wrong)** | 7 | 4 study proposals each fail their own arithmetic (9.8 vs ">35", 22.4 vs "≥30", 15.8 vs "≥25"); 0016 predicted ~0, cost 1.9%; 0020 predicted 9–11%, got 2.02%; 0015 predicted fail, passed A2. |
| **F12** | **Substituted by upstream — the idea landed better elsewhere** | 4 | 0006e vs two-pass #5253, N07 #5273, N08 #5261, 0018 #5263. *"That is a good outcome for the encoder even though it is a negative result for the patch."* |

Aggregate hit rate the new system should calibrate on: **round 1 = 1 promote / 1 improve / 8 discard from 10** (`ASSESSMENT-AND-PLAN.md:11-13`); across ~27 Claude patches + ~20 GPT patches + 101 ablations, **fewer than 10 things have ever cleared both class bars.**

---

# 4. THE EDA / CTC INTERFACE

**Provenance caveat:** `kick_off_av2ctc_eda.sh` and `compare_eda_runs.py` are **not** in the AVM tree — `grep -rl "kick_off_av2ctc_eda\|compare_eda_runs\|bdRateExtend\|edacloud"` over `/home/user/avm` returns nothing. They are google3 paths. Everything below is what the corpus reveals.

### 4.1 Submission
`$P/research_agent/av2_internal_agentic_rd_system_implementation_guide.md:210-222`:
```bash
/google/src/cloud/chengchen/AV2_research_agent/google3/experimental/users/kslu/eda/kick_off_av2ctc_eda.sh \
  <experiment_id> <path_to_worktree> '-DCMAKE_POLICY_VERSION_MINIMUM=3.5' \
  --crosscheck=0 --project=blade --chunk_size=65 --timing_accuracy=high \
  --test_configs=ra --testsets=a1,a2 --frame_count=33 \
  --extra_params='--cpu-used=<target_preset>'
```
- **Positional**: experiment id, worktree path, extra CMake args (the `CMAKE_POLICY_VERSION_MINIMUM=3.5` string implies an old-CMake-policy build fix is required).
- `--project=blade` — the cluster; every shared report says `--timing_accuracy=high` on `blade` (`$P/shared/0817_…md:4`, `0818_2:5`, `0819_N02_N06:5`, `0820_0001:6`, `$P/GPT/eda_rebased_round3_final_results_report.md:5`).
- `--chunk_size=65` matches in-tree `gop_size: 65` (`/home/user/avm/tools/convexhull_framework/src/config.yaml:92`); the study attributes part of the per-preset timing bias to "different machine pools, different run lengths, **chunking**" (`SPEED_FEATURE_STUDY_REVIEW.md:44`) and notes TPL is `<3%` of "**multi-chunk** RA execution time" (`AV2_Speed_Feature_Tradeoff_Report.md:193`). Chunking is the parallelism unit and a timing-noise source.
- `--crosscheck=0` — crosscheck (presumably decode verification) is **off** in the draft command. The corpus never records a crosscheck result; bitstream verification was done locally instead (`aomdec --md5` gate, guide Phase 2; `avmdec` decode checks, `ROUND_0821_TRELLIS.md:213`).
- **`--frame_count=33` is a bug in the draft**: every report runs A1 at **17 frames** and A2 at **33 frames** in the same job set. One scalar cannot express that.
- The preset is passed through `--extra_params='--cpu-used=N'`, i.e. a preset is an *arm*, not a sweep.

### 4.2 Retrieval
`guide:227-230`: `compare_eda_runs.py <anchor_eda_folder> <candidate_eda_folder> --download=1 --status=1 --csv=1`. Comparison is **folder-vs-folder**, anchor first — so the anchor run is a durable, reusable artifact (matches M22: encode the anchor once). `--status=1` implies job-status polling; `--partial=4` exists for partial-result triage (`guide:261`, `:12`) and enables early abort to preserve quota (`guide:299-300`).

### 4.3 Result folder / job identity
From the traceability matrices (`$P/shared/Claude_eda_report_patches_0013_to_0016.md:23-32`, `eda_report_patches_0018_0019.md:22-26`, `eda_report_patches_0020_to_0024.md:29-37`, `eda_report_patch_0025.md:26-31`, `eda_report_patches_0026_0027.md:29-37`):
- **One invocation UUID per (arm × testset)** — A1 and A2 are separate jobs. Console: `http://edacloud/invocations/<uuid>`.
- **Test tags** are hand-structured and carry meaning: `av2_enc_<anchor6>_base`, `p0818_13_dc_pred_s3`, `p0820_21_dis_4way_s1`, `p0824_26_tx_split16_s3`, `p0825_27_shift3_s4` → `p<date>_<patchno>_<slug>_s<preset>`.
- **Baselines are per-preset**: separate anchor invocations for S1/S2/S3/S4 (`Claude_eda_report_patches_0013_to_0016.md:25-28`), reused verbatim across later rounds on the same anchor (the S3/S4 baseline UUIDs `757fcc44…`/`14bad88f…` appear in both the 0020–0024 and 0025 reports).
- Each row also records the **candidate git SHA** (e.g. `dddb2e93d6`, `940b7e4c07`) — arm identity = (anchor SHA, patch SHA, preset, testset, invocation UUID).
- Raw text comparisons are written to files like `eda_comparisons_0824_0002_vs_d1445f/vs_d1445f_0002_s4_tf_sse2_A1_spd4.txt` (`$P/shared/0824_0002_…md:105`).

### 4.4 Metrics and aggregation
- Per-sequence columns, exactly as reported: **PSNR-Y, PSNR-U, PSNR-V, PSNR-YUV, SSIM, MS-SSIM, VMAF, VMAF-NEG, EncTime, DecTime** (`$P/shared/0824_0002_…md:107-117` shows the full set; most reports use the PSNR-Y/U/V/YUV, SSIM, VMAF, EncTime subset).
- In-tree metric list for cross-reference: `VMAF_Y, VMAF_Y-NEG, PSNR_Y, PSNR_U, PSNR_V, SSIM_Y(dB), MS-SSIM_Y(dB), PSNR-HVS, CIEDE2000, APSNR_Y/U/V, CAMBI` (`/home/user/avm/tools/convexhull_framework/src/CalcQtyWithVmafTool.py:26-39`). **PSNR-HVS, CIEDE2000 and CAMBI are computed by the framework and never appear in any report.**
- **EncTime is a percentage of anchor (100% = anchor)**; speedup = 100 − EncTime. DecTime is reported but never gated on.
- Aggregation (`$P/Claude/av2_cloud_eda_assessment_report.md:15-20`): runtime uses "codec internal wall-clock time (`Summary: <time>s` from `stats.log`), aggregating sequence-level speedups using **log-scale geometric means** exp((1/N)Σ ln(T_test/T_anchor)) per AOM CTC standard"; BD-rate uses "monotonic piecewise cubic Hermite interpolating polynomial (`pchip`) via `bdRateExtend` with standard **(14,1,1) YUV weighting**."
- **Documented discrepancy**: an intermediate custom script used multi-threaded CPU user time and **(6,1,1)** weights with polynomial fitting, producing different numbers; all final numbers were regenerated with `compare_eda_runs.py`. The in-tree `round1-patch-notes.md:26` also uses `(6·Y+U+V)/8`. **Two YUV weightings exist in this corpus — always record which.**
- In-tree BD-rate reference (`/home/user/avm/tools/convexhull_framework/src/CalcBDRate.py:62-121`): pchip on (log-rate, quality), 100 samples over the overlapping quality interval, trapezoid integration, **returns `(-1, "Error: Non-monotonic")` if either RD curve is non-monotonic**, VMAF curves get a non-monotonic filter first. Note `use_pchip_interpolation: false` at `config.yaml:72` while EDA uses pchip — the in-tree default and the EDA path disagree.

### 4.5 Test set (verified in-tree, CTC v9)
`/home/user/avm/tools/convexhull_framework/src/AV2CTCVideo.py:41-62` — **A1 = 8 clips 4K**: BoxingPractice, Crosswalk, FoodMarket2, Neon1224, NocturneDance, PierSeaSide(_v2 in CTC 9.0), Tango, TimeLapse. `:64-88` — **A2 = 19 clips 1080p** (incl. two portrait: GregoryScarf 1080×1920, Vertical_bees 1080×1920; one square-ish: ToddlerFountain 1080×1080). RA QPs `[110,135,160,185,210,235]` (`config.yaml:98`). Reports run A1 @17 frames, A2 @33 frames (a CTC-subset convention, not the in-tree `RA: 130`).

### 4.6 Partial results, failures, timing accuracy
- Partial runs are usable and are labelled: registry rows carry `+0.41%(partial)` at ~85–89% completion (`registry.csv:19-21`, `DECISIONS.md:320-322`).
- Jobs do fail: "**I found that there are failures for speed 1. Some jobs failed**" (`$P/Claude/av2_cloud_eda_assessment_report.md:69`); R3-07 "errored/crashed on Class A2" (`$P/GPT/rebased_round3_…:40`). Reports otherwise advertise "100% success rate" for 16–18 job batches (`$P/shared/0817_…md:11`, `0818_2:12`).
- **Throughput observed**: 18 jobs (0817), 16 jobs (0818_2), 6 jobs (round 3) per round — i.e. roughly **8–9 arms × 2 testsets** per round is achievable; a round is "hours to a day" (`experiments/README.md:3`).
- `--timing_accuracy=high` is used universally, yet **its actual repeatability has never been measured** (M8). This is the single largest unknown in the interface.

### 4.7 Acceptance rules encoded in the interface
`guide:200-203` gives the 4-quadrant classifier: Q1 (BD≤0, time≤0) unconditional accept; Q2 (quality gain, slower) ratio = Δtime/−ΔBDR with caps ≤20/10/5/2/1 for S0..S4; **Q3 (speedup, BD cost) ratio ≥150/35/30/25/20 with absolute BD caps ≤0.03/0.15/0.20/0.30/0.40%**; Q4 (worse both) immediate rejection. The BD caps are *not* used anywhere in the reports and would additionally disqualify e.g. 0019 (+0.52/+0.60 vs cap 0.15) and 0023.

---

# 5. WHAT PRIOR ROUNDS NEVER TRIED

**Measurement infrastructure that was specified and never built** (highest expected value, zero CTC cost):
1. **Anchor-vs-anchor arm** — the same commit submitted twice as two arms. Demanded in `ROUND_0819:163`, `ROUND_0821:251-259`, `0006E_VS_TWO_PASS:284-287`. Every ratio in the corpus is a quotient whose denominator's error bar is unknown; two decisions in round 0821 turned on 0.1–1.7 ratio points.
2. **Tier 1: `perf stat -e instructions`** — never once run, despite `use_perf_util: true` already in the framework (`config.yaml:66`).
3. **Tier 2: shadow-mode decision regret** (`experiments/README.md:73-99`) — fully specified, never implemented. Would give `Σregret/ΣRD` and `skipped/total` per decision site, deterministic, one encode per (clip,QP), attributable across simultaneously-enabled patches.
4. **Tier 3: reduced CTC** (A2 + two A1 clips, 4 of 6 QPs) — defined at `experiments/README.md:101-104`, never used.
5. **Tier 0 at 4K/CTC preset** — bit-exactness was only ever proven at 416×240/cpu-used=3 (`CLAUDE.md:36-39`); the owed re-run is "cheap and still owed."
6. **Per-sequence BD-rate spread as a gate, not a comment** — computed post hoc twice (0019, 0025) and never used to *select* an arm.

**Encoder territory nothing has touched:**
7. **The 47% itself** — a *search-stage reduction of the trellis* (fewer states / shorter lookahead during RD search, full strength for the final decision), i.e. giving the TCQ path the `perform_coeff_opt`/`enable_winner_mode_for_coeff_opt` distinction it never received. Named as "the real prize" and explicitly deferred (`PROFILE_DRIVEN_OPPORTUNITIES.md:136-146`). 0025/0026/0027 only nibble at call-site policy.
8. **Every other trellis call site** — "0025 fixes one hard-coded `skip_trellis = 0` … the same question ('is this call site ranking or deciding?') should be asked of every trellis invocation in `tx_search.c`. **0025 is the first answer, not the last**" (`ROUND_0820_RESULTS.md:121-125`).
9. **The 5.3% memset / 4.63% `__memset_avx2_unaligned_erms`** — "large enough to deserve a proper look at *which* buffers and *how often*" (`PROFILE_DRIVEN_OPPORTUNITIES.md:148-152`). Only i02 attacked it, from the wrong end.
10. **`av2_is_dv_valid` at 1.84%** and `av2_optimize_fsc_block` at 2.51% (`PROFILE_5d628d8_cpu4.txt:19,23`) — never mentioned in any report.
11. **The 0021 split method applied to other bundles** — "a demonstrated method with a 207.7 ratio behind it, and it is cheap: each split is an assignment move" (`ROUND_0820_RESULTS.md:127-132`). Candidates from the study never split: `disable_ext_partitions` (partially done), `ccso_early_term`, `mv_exh_thresh`, `prune_2d_txfm`.
12. **CCSO candidate *ordering* beyond BO-first** — CCSO is ~31% of encode time, ordering is the proven lever (GPT 0006), and "order the remaining filter configurations by expected cost" was recommended twice and never built (`ROUND_0817:168-171`).
13. **Prediction-aware / residual-based orientation profile** — flagged in three consecutive rounds as the deeper fix for A2 and never built: "the profile is measured on the **source**, and A2 spends most of its 33 frames on inter blocks where the residual, not the source, decides whether a cut pays" (`DECISIONS.md:79-82`, `:156-160`, `ASSESSMENT-AND-PLAN.md:106-113`).
14. **Two-pass-native opportunity (c)** — gate the wet-pass *re-search of unsplit large blocks* with the orientation signal, replacing the authors' block-size cap. "This is the one place on the new base where the orientation signal has a clear comparative advantage, and it is a **different patch** from 0006e rather than a retuning of it" (`0006E_VS_TWO_PASS_ANALYSIS.md:183-209`). Never built.
15. **TCQ gated on marginal coefficients** — `|level| == 1` count as the gate signal instead of `eob > 16` or temporal layer: "those are the positions where a trellis decision actually changes the rate, whereas large-magnitude coefficients are insensitive to it" (`SPEED_FEATURE_STUDY_REVIEW.md:246-254`, `ROUND_0817:156-166`). Never built; the one TCQ attempt (0820/0001) attacked a different point and failed.
16. **Existing DIP TFLite gate calibration** — GPT's recommendation was to *instrument and sweep the classifier that already exists* (keep rate, DIP-winner rate when rejected, block size, QP, update type, RD margin) rather than add a second gate (`$P/GPT/0817/…:171`). No counters were ever added.
17. **A1-exempting / content-adaptive scoping** — 0022 (scope 0019 to ≤1080p) was written and never run; per-sequence data shows exempting 1 clip in 8 clears the bar for both 0019 and 0025. No arm has ever tested a content-gated (e.g. frame-level source variance) rather than pyramid-level gate — explicitly named as the next axis if 0020 under-delivered (`ROUND_0819:94-97`), and 0020 did under-deliver.
18. **Presets 0 and 5+, and configurations other than RA** — everything is RA + S1–S4 + A1/A2. No LD, AI, AS, STILL; no classes A3/A4/A5/B1/B2/G1/G2/E, which the framework defines (`AV2CTCVideo.py:33-38`). Sub-720p behaviour is structurally invisible (M30).
19. **Decoder-side and DecTime** — DecTime is in every result table and has never been read.
20. **SIMD/kernel work** — only one attempt ever (TF-SSE2), and it found a **dead dispatch check** (`TF_BLOCK_SIZE == BLOCK_32X32` vs `BLOCK_64X64`) that had been silently routing every encode through the C path. Nobody has audited the other RTCD dispatch predicates for the same class of bug.
21. **Cross-patch interaction beyond simple stacking** — the only composition rule ever used is "speedups compound multiplicatively, BD-rate adds," validated once (all-10) and never revisited; no arm has measured whether two *passing* patches stay passing together since 06c+09b.

---

# 6. CRITIQUE OF THE EXISTING "MEMORY" (relevant, since the new system would inherit it)

`$P/research_agent/av2_research_agent/knowledge/prior_failures.md` and `prior_successes.md` are **not knowledge**. Each entry is a filename plus the first ~200 characters of the report, verbatim, including the YAML-ish header (`prior_failures.md:5-13, 15-23, 25-33 …`). The same report appears in both files. No result, mechanism, verdict, anchor or ratio survives ingestion. `knowledge/README.md:22-40` claims "Catalog of 32+ accepted speedup/gain mechanisms" and "27+ rejected hypotheses" — the files contain neither. `av2agent/prior_research.py:29-37` shows `list_prior_experiments()` returns *directory-entry counts*.

The corpus already contains the right data structure: `$P/Claude/experiments/registry.csv` (typed columns: id, mechanism∈{reuse,approx}, t0_bitexact, ctc_speed, a1/a2 speedup+bd+ratio, status, notes) plus an append-only `DECISIONS.md`. The new system's memory should be that schema, extended with: anchor SHA, patch SHA, invocation UUIDs, per-sequence vector (not just the mean), partial-completion fraction, YUV weighting, tier at which the patch died, and a `retracted_by` field (for M18-class reversals). Rows must be immutable and re-validated on every `BASE` change (M7).