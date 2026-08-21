# Round 0820: 0021 is the project's best result, and the profile finally paid

Anchor `5d628d8`. Results for 0020–0024, plus one new patch built from them.

---

## 1. Results

| Patch | Preset | A1 | A2 | Verdict |
|---|---|---|---|---|
| **`0021`** uneven-4-way | 1 | +6.23% / +0.03% → **207.7** | +6.70% / +0.06% → **111.7** | **PASS both, land it** |
| `0020` pyramid-gated | 1 | +2.02% / +0.00% | +0.71% / −0.00% | passes, but small |
| `0023` dry-pass tools | 3 | +6.16% / +0.59% → 10.4 | +6.14% / +0.57% → 10.8 | fail |
| `0024` tx-rank no-trellis | 4 | 0.00% / 0.00% | 0.00% / 0.00% | **no-op, retire** |

### `0021` is the best result this project has produced

Ratios of 207.7 and 111.7 against a bar of 35 — 3× to 6× over. And the reason
generalises, which matters more than the number:

```
                        speed kept    BD cost kept
A1:  4-way subset of        39%            6%
A2:  the ext-part family    29%           10%
```

`0019` disabled the whole extended-partition family: +15.92% / +0.52%, ratio
30.6, **failed**. Disabling only the uneven 4-way members kept **39% of the
speed for 6% of the quality cost**.

**Cost and benefit are not proportionally distributed inside a feature group.**
That is the lesson worth carrying: when a bundled feature fails on ratio, the
first move should be to split it and find the cheap members, not to turn its
threshold down. Every threshold move on this project has slid along the
trade-off; this split moved off it.

### `0020` — your pyramid idea worked, but the prize is small

It passes both classes at ~0.00% BD, so gating by pyramid level is safe exactly
as predicted. But +2.02% / +0.71% against `0019`'s +15.92% / +22.75% means
pyramid ≥3 frames carry only a small share of encode time — they are the cheap
frames, which is *why* pruning them is safe. The idea was directionally right
and the mechanism is confirmed; the ceiling is just low.

### `0024` — no-op, exactly as flagged

I said in its header: *"if the CTC arm comes back bit-identical the patch is a
no-op and should be dropped rather than tuned."* It did. Dropped. Flagging the
verification gap up front cost one arm instead of a round of tuning something
that never ran.

### `0023` — bought speed by degrading the ranking

+6.15% for +0.58% BD-rate, ratio ~10.6 against a bar of 25. My premise was that
warp, wedge and MV precision don't change which *shape* wins. They do, by
0.58%. To clear 25 the BD would have to fall 58% at constant speed.

It has three independent switches, so it is decomposable in one round
(`-DAVM_DRY_PASS_WARP_MODES=1`, `-DAVM_DRY_PASS_PRECISIONS=3`,
`-DAVM_DRY_PASS_WEDGE=1`). **But I would not spend three arms on it.** Even if
one sub-change carries most of the BD-rate, the remainder has to reach ratio 25
from 10.6, and the 0021 lesson says look for a cheap member — here all three
members are ranking-fidelity changes, which is the expensive kind.

## 2. The profile paid off — patch `0025`

The callgrind profile said trellis quantisation is **~47% of instructions**, and
that `trellis_quant.c` has **zero** speed-feature references. `0023` and `0024`
were both indirect attempts to get at it and both missed. This one is direct.

`choose_tx_size_type_from_rd` states the dry pass's policy explicitly:

```c
// Dry pass: skip trellis (no RDOQ) - light quant only.
const int skip_trellis = x->apply_dry_pass_shortcuts ? 1 : 0;
```

But `tx_type_rd`, in the same file, had:

```c
const int skip_trellis = 0;
```

hard-coded. **The trellis ran at full strength there even during the dry pass** —
whose entire purpose is to rank shapes cheaply. `tx_type_rd` is reached from
`try_tx_block_no_split`, once per transform block per transform-partition
candidate: one of the densest trellis call sites in the encoder.

This is a **consistency fix, not a new heuristic**. It applies the policy the
dry pass already declares, at a site that wasn't honouring it.

**Measured locally, paired, 3 reps, alternating two saved binaries:**

```
+6.86%, +9.16%, +5.00%     mean +7.01%, sd 2.08%, all same sign
```

The bitstream changes (md5 `eeebfc00` vs `ab4e2d32`), so there is a real quality
cost that only CTC can price. A 192x128 4-frame clip is not a CTC result and the
magnitude will move at 4K — what this establishes is that it fires, and that the
effect is large enough to deserve an arm.

Note this is also why `0023` failed the way it did: the dry pass was *already*
skipping trellis on its main path, so `0023` wasn't attacking quantisation at
all. It was cutting ranking fidelity in mode search and paying for it in
BD-rate. `0025` removes work the code already intended to skip.

## 3. Recommended round

| Arm | Preset | Question |
|---|---|---|
| **`0021`** | 1 | **Land it.** Already qualified; no further arm needed unless you want a combination. |
| **`0025`** | 3 or 4 | Does the ~7% local speedup survive at 4K, and what does it cost in BD-rate? |
| `0025 + 0021` | 1 | The shipping combination, if both hold. |
| `0020` | — | Land or drop on its own merits; it passes but adds ~1–2%. |

Retire `0024`. Park `0023` unless arms are cheap.

## 4. What I would build next

**More trellis call sites.** `0025` fixes one hard-coded `skip_trellis = 0`. The
profile says quantisation is 47%, and the dry pass is only part of the search —
the same question ("is this call site ranking or deciding?") should be asked of
every trellis invocation in `tx_search.c`. `0025` is the first answer, not the
last.

**Apply the 0021 split to other bundled features.** The 101-feature study lists
several features that fail on ratio as a bundle. Splitting them the way `0021`
split the extended partitions is now a demonstrated method with a 207.7 ratio
behind it, and it is cheap: each split is an assignment move, not a new
conditional.

## 5. Scorecard on my own predictions this round

Worth recording, since I have been wrong in both directions before.

- **`0021`**: I wrote *"either outcome is informative... if the 4-way subset is a
  disproportionate share of the time, the arithmetic clears where 0019 could
  not."* It was, and it did — by 6×.
- **`0024`**: I flagged it as unverified and said to drop it if bit-identical.
  Correct call, and the flag saved a tuning round.
- **`0023`**: I expected the three tool reductions to be ranking-neutral. They
  were not. The specific error was assuming the dry pass's *stated* purpose
  (rank shapes) meant its tool set had slack — it had already been tuned once,
  by the commit that introduced it.
- **`0020`**: predicted "9–11% speedup at 0.2–0.3% BD". Actual: +2.02% at 0.00%.
  The direction was right, the magnitude was off by 5×; I overestimated how much
  encode time leaf frames carry.
