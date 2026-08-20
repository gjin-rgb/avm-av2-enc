# Improving 0019, and a measurement finding that changes the scoreboard

Anchor `ea89c216c6`. Reading the 0018/0019 results with GPT's 0818_2 round.

---

## 1. A correction to what I told you last round, and why it matters here

Last round I wrote a rule: *promotions pass 6 of 7, conditionals deliver 0 of 5,
so prefer assignment moves and treat any new conditional as guilty.* That rule
was built on P09, P10 and P12. It should not have been.

All three report a **BD-rate of exactly 0.00% on both classes** — the encodes are
unchanged — while all six of their EncTime readings sit **above 100%**:

```
P09   A1 102.39%  BD +0.00%      A2 101.43%  BD -0.00%
P10   A1 101.76%  BD -0.00%      A2 101.62%  BD +0.00%
P12   A1 101.47%  BD +0.00%      A2 101.12%  BD +0.00%
                                 mean 101.63%
```

An unchanged bitstream cannot take 1–2% longer to produce. Either those patches
never took effect, or the BD figures are wrong. Either way they tested nothing,
and the rule I derived from them has no support.

**This matters directly for your question**, because **P12 *is* the
pyramid/leaf-gating idea** — `!boosted && !allow_screen_content_tools` at speed
1. It came back "ineffective", and on the evidence it never fired. **Your idea is
untested, not refuted.**

### The same six numbers are the cluster characterisation I keep asking for

Six null measurements averaging **+1.63% apparent slowdown** is a direct read of
the systematic bias on a patched arm. If it is real and general, every speedup in
this project is understated by roughly 1.6 points. That is a rounding error on a
+22% result and it is decisive on a +0.5% one:

| Patch | measured | bias-corrected | ratio → |
|---|---|---|---|
| `0019` A1 | +15.92% | +17.55% | 30.6 → 33.8 |
| `0019` A2 | +22.75% | +24.38% | 37.9 → 40.6 |
| `0014` A1 | +0.54% | +2.17% | — → large |
| `0014` A2 | +0.37% | +2.00% | 18.5 → 100 |

`0014` was dismissed as borderline on a +0.4% speedup. Corrected, it might be a
+2% feature. **I am not claiming it is** — I am claiming nobody can tell, and one
job settles it: run the anchor against itself, or re-run any null patch, and
measure the spread. This is now the highest-value experiment available and it
isn't a patch.

## 2. Evaluating your idea: gate by hierarchical layer

**It is the right instinct, it is well-supported by evidence already collected on
this branch, and the per-sequence data says it is aimed at the right target.**

Look at where 0019's A1 failure actually lives:

```
A1 speedup:  mean 15.90%   sd 1.93%   range 13.3–19.4    CV 12%
A1 BD-rate:  mean  0.53%   sd 0.47%   range −0.17–1.22   CV 89%
```

**The speed benefit is nearly uniform across content; the quality cost varies
sevenfold.** So the fix must separate *frames or content*, not turn a threshold
down — a threshold move scales both together and leaves the ratio roughly where
it was. That is exactly the trap 0006c fell into (speed −66%, BD −64%, ratio
*worse*).

A1 needs only a **12.5%** BD-rate reduction at constant speed. Arithmetic on the
measured per-sequence table shows how little separation is required:

```
as measured                                    ratio 30.6
exempt PierSeaSide (worst, +1.22%)             ratio 37.2  PASS
exempt PierSeaSide + BoxingPractice            ratio 42.7  PASS
```

**Exempting one sequence in eight clears the bar.**

And pyramid level is the separator this branch has already measured. When 0006c
gated orientation pruning by layer depth, the marginal ratio of the prunes that
gate removed was **12.8** — far below the bar. That is a direct measurement that
partition prunes on frames low in the pyramid are the bad ones. The mechanism is
not subtle: everything coded afterwards predicts from those frames, so a lost
partition there is paid for repeatedly, while the same loss on a leaf frame is
paid once.

That is `0020` below.

**The honest caveat:** pyramid level and content are different axes. The A1
variance I measured is *per sequence*, and I am proposing to gate *per frame*.
These coincide only if the sequences that suffer most do so because their
low-pyramid frames are where extended partitions matter — plausible, since those
frames are coded at the lowest QP and carry the most detail, but not something
the data proves. If `0020` under-delivers, a content gate (source variance at
frame level) is the next axis to try, not a further pyramid tweak.

## 3. Three patches

All build clean and apply standalone; 20 and 21 compose.

### `0020` — pyramid-gated, supersedes 0019 *(your idea)*

Disables extended partitions at speed 1 only when
`cm->current_frame.pyramid_level >= 3` — a boundary this file already uses for
pyramid-dependent decisions. Expected: less speedup than 0019,
disproportionately less BD-rate. If levels ≥3 carry roughly half the encode time
and well under half the propagated damage, A1 lands near 9–11% at 0.2–0.3% BD,
comfortably over 35. **That is a projection, not a measurement.**

`-DAVM_S1_EXT_PART_PYRAMID_MIN=4` prunes less, `=2` prunes more, `=0` reproduces
0019 exactly. Arms at 3 and 4 bracket the curve.

### `0021` — split the family instead of gating frames

Speed 2 sets `disable_uneven_4way_partitions` and `disable_ext_partitions`
together. 0019 promoted the second; this promotes the first. The uneven 4-way
shapes split a block into unequal quarters and pay off only where content
aligns to that division, so they should give up a smaller share of BD-rate than
of search time.

Either outcome is informative: if the 4-way subset is a disproportionate share
of the time, the arithmetic clears where 0019 could not; if proportionate, the
family cannot usefully be split and no further effort should go into
subdividing it.

### `0022` — scope 0019 to 1080p and below *(the floor, not the ambition)*

0019 already passes on A2 at 37.9 with **+22.75%** — the second-largest single
speedup measured in this project. Scoping the feature to the class where it
qualifies banks that today and leaves 4K untouched. Land this if 20 and 21 both
fail; prefer either of them if they succeed, since a feature working at both
resolutions is worth more than one scoped to half the test set.

## 4. Other ideas

**Two free arms nobody has run.** `P05` (= patch `0009b`) contains no speed-feature
reference at all — verified, zero `cpi->sf.` occurrences in its added code. It is
active at every preset but has only ever been measured at **Speed 4**, where it
returns +8.01% at ratio 61.6. Every pruning feature in this project has returned
more at deeper presets. **Running it at Speed 3 and Speed 2 needs no patch.** On
expected value this is still the best thing on the list.

**`0018` is retired, and the lesson is mine.** `exhaustive_searches_thresh` was
already updated upstream by PR #5263, so my patch was a no-op against the current
base. I authored it against my local tree without checking whether the feature
had moved. Before authoring any further promotion, diff the target assignment
against the actual anchor — `bin/check-rot.sh` exists for exactly this and I did
not run it.

**What I would not build yet.** The TCQ idea from two rounds ago still requires a
per-block test, and my basis for calling that pattern expensive has just
evaporated along with P09/P10/P12. That does not make it cheap — it makes it
*unmeasured*. If it is built, measure the gate's overhead first with a build
where it always returns "don't prune"; if that is slower than baseline, the idea
is dead regardless of the gating logic.

## 5. Recommended round

| Arm | Preset | Cost | Question |
|---|---|---|---|
| **anchor vs anchor** | any | 1 job | **What is the noise floor?** Everything else is a quotient with an unmeasured denominator. |
| `P05` as-is | 3 | no patch | Does +8.01% grow at a deeper preset? |
| `0020` @ pyramid 3 | 1 | patch | Does frame gating clear 35 on A1? |
| `0021` | 1 | patch | Can the extended-partition family be split? |
| `0022` | 1 | patch | Bank +22.75% on 1080p if the above fail |

If arms are scarce: the noise-floor job first, then `0020`, then `P05` at Speed 3.
