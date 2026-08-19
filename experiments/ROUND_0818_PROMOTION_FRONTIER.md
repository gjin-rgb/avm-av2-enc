# The promotion frontier: what worked, what didn't, and what's left

Reading the 0013–0016 results together with GPT's 0818_2 round. Anchor
`ea89c216c6`.

---

## 1. The result that should drive everything now

Two mechanisms have been tried repeatedly across both patch sets, and their
track records could hardly be more different.

**Moving an existing speed-feature assignment to a slower preset — 6 of 7 pass:**

| Patch | Move | Result | Ratio (bar) |
|---|---|---|---|
| `0001` tx_stats | s4→s3 | +6.12% / +0.18% | 34.0 (25) ✅ |
| `0002` dc_blk_pred L1 | s4→s3 | +3.08% / +0.00% | ~308 (25) ✅ |
| `0013` dc_blk_pred L2 | s4→s3 | +5.22% / +0.04% | **130.5** (25) ✅ |
| `0014` warp_diamond | s4→s3 | +0.54% / −0.03% | 18.5 (25) borderline |
| `0015` tx_stats | s3→s2 | +4.46% / +0.13% | 34.3 (30) ✅ A2 |
| `P11` ccso_early_term | s2→s1 | **+22.37%** / +0.36% | 62.1 (35) ✅ |
| `P14` wienerns_iters | s2→s1 | +2.78% / +0.025% | 111.2 (35) ✅ |

**Adding a conditional gate to a hot path — 0 of 5 deliver, 4 outright slower:**

| Patch | Result |
|---|---|
| `0012` CCSO tolerance | no change at all |
| `P09` frame-aware CCSO | −2.33% / −1.41% **slower** |
| `P10` leaf exh-MV | −1.73% / −1.59% **slower** |
| `P12` leaf disable-ext-part | −1.45% / −1.11% **slower** |
| `0016` remove 3 features | −1.72% / −2.00% (cost 1.9%; I predicted ~0) |

The explanation is mundane and worth internalising: **an assignment move costs
nothing at runtime, while a conditional is evaluated on every block whether or
not it fires.** In a search this hot, a per-block test that fires rarely loses
money on the tests that fail. P10 and P12 are the cleanest demonstrations —
they are "smarter" versions of features that *pass* as plain assignments
(`mv_exh_thresh` at 60.3, `disable_ext_partitions` at 53.4), and adding the
intelligence made both slower than doing nothing.

**Practical rule for this project: prefer promotions. Treat any new per-block
conditional as guilty until proven innocent, and measure its cost before its
benefit.**

## 2. Where I was wrong

`0016` was my patch and my prediction for it was wrong in a specific, learnable
way. I screened three speed-4 features whose reported time savings were −0.00%,
+0.15% and −0.31%, concluded the net was "roughly +0.16% in the favourable
direction", and predicted removal would be roughly free. It cost **1.9%**.

The error was summing three numbers each of which sat inside a ±1% noise band.
Summing noisy near-zero measurements does not average the noise away when you
are summing *estimates of different quantities* — it compounds the uncertainty
while I treated it as a point estimate. The patch is still worth landing (it
recovers −0.14% BD-rate for 1.9% time, a 13:1 quality-recovery rate), but it is
a quality patch that costs real time, not the free win I described.

`0015` I predicted would fail. It passed on A2 at ratio 34.3 and missed on A1 at
25.1. Being wrong in that direction is fine, but it is worth noting the
extrapolation I based the prediction on (two points, degradation factor 0.69)
was roughly right about the *ratio* — I simply drew the pass/fail line at the
wrong class.

## 3. The free arm nobody has run

`P05` is patch `0009b`, and **its code references no speed feature at all** —
verified: zero occurrences of `cpi->sf.` in the added lines. It is active at
every preset.

It has been measured only at Speed 4, where it returns **+8.01% at ratio 61.6**.
Nobody has measured it at Speed 3 or Speed 2, where the transform search it
prunes is deeper and every pruning feature in this project has returned more.

**This needs no patch. It is two measurement arms on an existing binary, and on
the evidence it may be the largest single gain still available.** I would run
this before any of the patches below.

## 4. Three new patches — the remaining promotion frontier

Of the nine features that genuinely clear their bars, six have now been
promoted. Three have not, and all three are here.

### `0017` — DC-block prediction level 2 to speed 2 *(strongest)*

Measured at three operating points, never close to its bar: 77.0 at speed 4,
~308 at speed 3 level 1, **130.5** at speed 3 level 2 (+5.22% on 4K at four
hundredths of a percent of BD-rate). Speed 2's bar is 30. Even applying the
worst degradation factor observed (×0.69), 130.5 lands near 90.

Supersedes `0013` — apply instead of it, not on top.

### `0018` — exhaustive-MV threshold to speed 1 *(marginal, deliberately)*

+7.23% / +0.12%, ratio 60.3 at speed 2. Speed 1's bar is 35; ×0.69 gives 41.6.
Clears, but not by much.

### `0019` — extended-partition disabling to speed 1 *(marginal, large)*

+20.84% / +0.39%, ratio 53.4 — the second-largest single time saver in the
encoder after CCSO, which P11 already promoted for +22.37%. ×0.69 gives 36.8
against a bar of 35.

**Both `0018` and `0019` are near the edge on purpose.** The promotion frontier
has paid six times out of seven and will stop paying somewhere. These two arms
locate it. A failure on `0019` is arguably the more valuable outcome, because it
comes with a large speedup attached and the BD-rate cost then tells us what
extended partitions are worth at speed 1 — a number nobody has, and one that
bounds every future partition-pruning idea at that preset.

## 5. Recommended round

| Arm | Preset | Cost | Question |
|---|---|---|---|
| **`P05` as-is** | **3** | **no patch** | Does the +8.01% grow at a deeper preset? |
| **`P05` as-is** | **2** | **no patch** | And deeper still? |
| `0017` | 2 | patch | Does the 130.5 ratio survive one more promotion? |
| `0019` | 1 | patch | Largest remaining prize; marginal ratio |
| `0018` | 1 | patch | Where does the frontier stop? |

If arms are scarce, the two `P05` measurement arms come first — they need no
new code and test the largest measured effect in the set at presets where every
comparable feature has done better.

## 6. What I would not build

**More conditional gates.** Five have been tried and none delivered. Until
something explains why the four slower ones lost time, a sixth is a bad bet.

**The TCQ idea I proposed last round**, at least not yet. It requires adding a
per-block test on the coefficient level counts — exactly the pattern that has
failed five times. If it is built, its overhead must be measured *before* its
pruning benefit, on a build where the gate always returns "don't prune". If that
build is slower than baseline, the idea is dead regardless of how good the
gating logic is.

## 7. Measurement note, again

`0016`'s outcome (predicted ~0, measured −1.9%) and the `0006e` anomaly last
round (0.73% and 2.96% from the same patch two commits apart) both come back to
the same gap: nobody has measured this cluster's EncTime repeatability. Every
ratio in this project is a quotient whose denominator has an unknown error bar.
One job — the same configuration run twice — makes every future number here
interpretable, and it is still the highest-value experiment available that
isn't a patch.
