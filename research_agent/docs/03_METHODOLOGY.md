# Methodology: what this system is allowed to conclude

Thirty rules, derived from roughly fifty prior experiments on this codebase.
Eighteen are enforced by code and cannot be argued with; the rest are injected
into the agent's prompts as priors. `av2ra rules --evidence` prints them with
the evidence behind each.

This document explains the six that matter most and how they change day-to-day
behaviour.

---

## 1. An effect below the noise floor is not a small win

**Rule L01. Enforced by `measure/acceptance.py`.**

If a confidence interval contains zero, the verdict is `NO_EFFECT` and the
report says what the design *could* have resolved. There is no "small but
promising" category, because there is no way to tell one from noise without more
data, and the appearance of one is how an agent talks itself into a CTC round.

The evidence: the prior effort's first round reported ten speedups. Its own
tooling put the noise floor at 2.38% and the minimum detectable effect at 3.47%.
Six of the ten were smaller than that. They were written into a results table as
wins, and two subsequent rounds reasoned about numbers that were never
measurements.

**What you will see.** Experiments completing with `NO_EFFECT` and a message
naming the MDE and the repetitions that would be needed. That is the system
working. The response is more repetitions or a cheaper metric, not a lower bar.

---

## 2. A local screen ranks; it does not estimate

**Rule L02. Enforced by `measure/tiers.py`.**

A local screening pass may kill a catastrophic regression, prove that a patch
fires, and order variants. It may not quote a BD-rate magnitude, and its sign
may not be trusted.

The evidence, local versus CTC on the same patches:

| patch | local BD-rate | CTC A1 |
|---|---|---|
| i01 | −0.900% | **+1.22%** |
| i04 | −0.597% | +0.11% |
| i07 | −0.338% | +0.30% |
| all-10 union | −0.630% | **+2.50%** |

Three sign flips in ten. A screen that reports a compression gain for a patch
that costs 2.5% is worse than no screen.

**What you will see.** Every T3 result carries the disclaimer in its text, and
the CTC section is the only place in a report with a quotable BD-rate.

---

## 3. Bit-exactness is a proof, and it replaces a cluster round

**Rules L03, L04, L10. Enforced by `integrity/gates.py`.**

The declared mechanism decides what an identical bitstream means:

| mechanism | identical bitstream | different bitstream |
|---|---|---|
| `reuse`, `kernel` | **proof**: quality risk is zero, judge on speed alone | **bug**: wrong cache key, stale buffer, non-equivalent kernel |
| `approx`, `structural`, `rdcost` | **failure**: it never fired, the experiment did not run | expected |

This is why `mechanism` is a required field rather than a label, and why the
planner refuses a CTC *quality* round for a proven-exact patch.

Two qualifications the code carries:

- A bit-exactness proof is scoped to the configuration tested. State the
  resolution, preset, frame count and QPs; a cache-key bug on a path that
  configuration never reaches is not caught.
- Being bit-exact says nothing about being *faster*. All three bit-exact patches
  in the prior corpus were slower at 4K.

---

## 4. Both classes pass independently

**Rule L09. Enforced by `measure/acceptance.py`.**

4K and 1080p are assessed separately and the worst one is the verdict. Ten arms
in the corpus were class-asymmetric, in both directions — one scored 80.3 on 4K
and 7.5 on 1080p; another 19.0 and 36.9 the other way.

One shared report declares three patches adopted on averaged ratios of 24.7,
62.1 and 111.2, while their binding class scores 19.0, 29.3 and 31.0 against
bars of 20, 35 and 35. All three were class failures presented as adoptions.

---

## 5. Tune only where the marginal ratio says to

**Rules L12, L13, L14. Enforced by `agent/analyst.py`.**

When a variant tightens a heuristic it gives up speed and recovers quality. The
ratio of those two deltas is the ratio of the decisions it removed, and removing
decisions improves the overall ratio **only when their marginal ratio is below
the ratio already achieved**.

The corpus's worked example, after three variants built on stories about which
blocks are unsafe (two of the three stories were wrong):

```
A1  variance floor    +2.04% speed for +0.00% BD   marginal  inf  -> worthless
A1  frame gate        +5.89% speed for +0.46% BD   marginal 12.8  -> keep it
A2  block-size floor  +8.95% speed for +0.37% BD   marginal 24.2  -> backwards
```

The variance floor had been carried through four variants on the strength of an
argument about flat blocks. It bought nothing at all.

The corollary that produced the project's best result: when a bundled feature
fails on ratio, **split it before turning its threshold down**. Disabling a whole
extended-partition family gave ratio 30.6 and failed; disabling only its uneven
4-way members kept 39% of the speed for 6% of the cost — ratio 207.7. Every
threshold move on that project slid along the trade-off; the split moved off it.

**What you will see.** The analyst returns `split` or `scope` far more often
than `tune`, and `tune` always comes with the marginal-ratio arithmetic.

---

## 6. Every cluster round answers a question nothing cheaper could

**Rule L18. Enforced by `agent/planner.py` and `ctc/contract.py`.**

An escalation must state its question and list the cheaper evidence already
exhausted. It is refused when the speedup is unresolved locally, when no holdout
measurement exists, or when the patch is bit-exact.

The evidence: one round was spent on ten patches of which eight were already
dead on evidence the project had.

---

## The tier ladder in one table

| Tier | What it establishes | Cost | May kill? |
|---|---|---|---|
| T0 build | compiles, encodes, decodes, reproduces | minutes | yes |
| T1 exactness | it fired — or is provably exact | free | yes |
| T2 complexity | the speedup is real, above the floor | minutes–hours | yes |
| T3 screen | RD behaviour is sane; variants ranked | hours | catastrophes only |
| T4 holdout | it survives on unseen clips | hours | yes |
| T5 CTC | it clears the bar on every class | hours–a day | decisive |

---

## Acceptance

Signs follow CTC: BD-rate above zero is worse; a time ratio below 100% is faster.

|  | faster | slower |
|---|---|---|
| **better compression** | Q1 absolute win — accept | Q2 — pays `Δtime / −ΔBD`, must be under the preset allowance |
| **worse compression** | Q3 — ratio `−Δtime / ΔBD`, must clear the preset bar | Q4 regression — reject |

Default bars, matching prior CTC practice on this codebase:

| preset | Q3 min ratio | absolute BD cap | Q2 max ratio |
|---|---|---|---|
| 0 | 150 | 0.03% | 20 |
| 1 | 35 | 0.15% | 10 |
| 2 | 30 | 0.20% | 5 |
| 3 | 25 | 0.30% | 2 |
| 4 | 20 | 0.40% | 1 |

Three qualifications the code adds:

- **Mode.** `point` applies the bar to the point estimate and is right for
  ranking. `conservative` applies it to the pessimistic interval bound and is
  required before recommending an upstream submission. Both numbers are always
  recorded; when they disagree the assessment says so.
- **Undefined ratios.** If the BD-rate interval spans zero or falls below the
  reporting floor, there is no ratio. Twelve of twenty-one "passes" in one prior
  study were divisions by a rounded `0.00%`.
- **The cap warns.** BD-rate costs add when patches combine, so an arm above the
  cap is flagged as *not combinable* rather than rejected. Set
  `bdrate_cap_mode=reject` when assembling a combination round, where the cap is
  exactly the right rule.

---

## Per-sequence spread is a result

**Rule L15.** Speed benefit is nearly uniform across clips (CV ~12%); quality
cost is not (CV ~89%). One or two clips routinely carry the damage while
contributing an average share of the speedup.

Twice in the corpus, exempting one clip in eight cleared a bar the mean had
failed. `exemption_analysis` computes this automatically, and the analyst
returns `scope` when it fires — because a clip that carries the damage is a
**content gate waiting to be found**, not a clip to drop from the test set.

---

## Provenance

**Rules L06, L28.** Every measurement carries its base SHA, patch hash, build
fingerprint, clip set, repetitions, metric definition, back end and job id.
Measurements are append-only; a disagreement surfaces as a conflict.

When the anchor moves, a clean apply proves the patch is textually compatible
and nothing more. `av2ra base check` reports the intersection of upstream-changed
files with patch-touched files — the set where a clean apply means least — and
flags upstream commits whose subjects match the patch's mechanism. Four
experiments in the corpus died as no-ops because the idea had already landed
upstream.
