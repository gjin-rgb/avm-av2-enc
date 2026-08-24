# The AV2 Encoder: A Systematic Architecture Study

**Codebase:** `AOMediaCodec/avm`, branch `av2-enc`, commit `d1445fc1c70f`
("Bound the two-pass wet-pass mode search with the dry pass's rd", #5304),
2026-08-24.

**Scope:** the AVM reference encoder. Every claim below is anchored to a file
and a symbol in that tree. Line numbers are given where they help; symbol names
are given everywhere, because line numbers rot and this repository moves fast —
roughly 50 commits in the two weeks before this snapshot.

---

## 0. How to read this document

### 0.1 A warning about staleness, learned the hard way

This study was produced immediately after discovering that a local working tree
was 51 commits behind `origin/av2-enc`, and that a patch series had been
authored against machinery (`x->apply_dry_pass_shortcuts`) that existed in the
CTC anchor but not in the stale tree. Everything below was re-verified against
`d1445fc` after rebasing.

**The operational rule this implies:** before trusting any structural claim in
this document, run

```bash
git fetch origin av2-enc && git rev-list --count HEAD..origin/av2-enc
```

If that number is large, re-verify the specific symbols you are about to depend
on. Prefer `grep -rn "symbol_name" av2/` over the line numbers printed here.

### 0.2 Codebase census

| Area | Files (.c/.h) | Lines |
|---|---:|---:|
| `av2/encoder/` | 201 | 197,826 |
| `av2/common/` | 138 | 120,816 |
| `av2/decoder/` | 26 | 22,992 |
| `avm_dsp/` (+ SIMD) | — | ~3.8 MB source |

The ten largest encoder translation units, which is a fair first-order map of
where the complexity lives:

```
9,865  rdopt.c              mode decision (the core RD loop)
7,306  bitstream.c          syntax writing
5,785  encoder.c            frame-level orchestration
5,712  partition_search.c   partition RD recursion
5,549  mcomp.c              motion search
4,818  encodetxb.c          coefficient coding + cost
4,464  tx_search.c          transform size / type search
4,018  pickrst.c            loop restoration parameter search
3,249  pass2_strategy.c     two-pass rate allocation
2,326  encodeframe.c        tile / superblock loop
```

Note the ~7,400-line `sms_part_none_prune_rect_tflite_model.h` and its siblings:
**neural network weights are compiled into the encoder binary.** See §21.

### 0.3 Build configuration

All the AV2 tool experiments have been merged unconditionally. `build/config/avm_config.h`
holds 44 `CONFIG_*` flags and every one of them is a *build* or *debug* switch
(`CONFIG_MULTITHREAD`, `CONFIG_DENOISE`, `CONFIG_COLLECT_RD_STATS`, …), not a
coding tool. The handful that gate encoder algorithms are:

```
CONFIG_ML_PART_SPLIT      1   ML partition-split pruning
CONFIG_DIP_EXT_PRUNING    1   matrix-intra (DIP) mode pruning
CONFIG_LANCZOS_RESAMPLE   1   resampling filter bank
CONFIG_SPATIAL_RESAMPLING 1
CONFIG_NN_V2              0   (off) second-generation NN inference path
```

This is a milestone: reading AV2 tool code no longer requires tracking which
experiment flags are on.

### 0.4 Your 15-subsystem taxonomy, assessed

The proposed decomposition is sound for the *block-level codec*. Its gap is the
**frame-level encoder** — everything that decides what to encode, at what
quality, in what order, and with what compute budget. That machinery is roughly
a third of the encoder by line count and is where a large fraction of both the
compression gain and the encode time lives.

Two structural notes on the list itself:

- **`15_subsystem_call_graphs` is not a subsystem**, it is a cross-cutting view.
  Treat it as a deliverable *format* applied to the other subsystems (§25 gives
  the graphs).
- **`13_simd_and_intrinsics` is really "the portability and optimization
  layer"** — the RTCD dispatch mechanism matters more than the intrinsics
  themselves, because it is what determines whether a new C function is even
  reachable by an optimized path.

Sections 1–15 below follow your numbering. Sections 16–24 are the subsystems I
recommend adding.

---

# PART I — The proposed 15 subsystems

## 1. I/O and formatting

**Files:** `apps/avmenc.c`, `av2/av2_cx_iface.c`, `av2/arg_defs.c`,
`common/*.c` (y4m, ivf, webm readers), `avm/avm_encoder.h`.

### The three-layer entry path

```
apps/avmenc.c              CLI parsing, container demux, frame pump
  └─ avm_codec_encode()    public codec API  (avm/avm_encoder.h)
      └─ av2_cx_iface.c    config validation, control mapping
          └─ av2_receive_raw_frame()   → lookahead queue
          └─ av2_get_compressed_data() → the encode
```

`av2_cx_iface.c` is the *policy* layer: it owns `AV2EncoderConfig`, validates
ranges (`RANGE_CHECK(extra_cfg, cpu_used, 0, 9)`), and translates ~254
command-line arguments (`av2/arg_defs.c` defines 254 `arg_def_t`) into internal
configuration. It is also where usage modes are separated (`g_usage`:
good-quality vs. realtime).

### Internal pixel format

Everything internal is **16-bit** (`uint16_t`) regardless of input bit depth.
There is no 8-bit fast path in the AV2 encoder — the `highbd_` prefix that AV1
used to distinguish paths has largely been absorbed. Supported depths are 8, 10
and 12 (`CONFIG_TESTONLY_12BIT_SUPPORT` gates the last).

### What to study here

The interesting question in this subsystem is not parsing, it is **which
configuration knobs actually reach the search**. `av2_change_config()`
(`encoder.c:1162`) is a 660-line function that fans `AV2EncoderConfig` out into
`AV2_COMMON`, `SequenceHeader`, `RATE_CONTROL` and the speed-feature framework.
Tracing a single flag from `arg_defs.c` to its consumption point is the fastest
way to understand the encoder's configuration topology.

---

## 2. Frame buffering and memory

**Files:** `av2/common/frame_buffers.c`, `av2/common/alloccommon.c`,
`av2/encoder/lookahead.c`, `av2/encoder/context_tree.c`,
`avm_scale/yv12config.c`.

### Three distinct memory regimes

**(a) Frame buffers — reference counted, pool allocated.**
`BufferPool` holds `RefCntBuffer` entries. `av2_get_frame_buffer()` /
`av2_release_frame_buffer()` implement the callback interface that also lets an
application supply its own allocator. The pool size is notable:

```c
#define REF_FRAMES 16                              // enums.h:1080
#define FRAME_BUFFERS ((REF_FRAMES + 1 + INTER_REFS_PER_FRAME) * AVM_MAX_NUM_STREAMS)
#define INTER_REFS_PER_FRAME 7                     // enums.h:1072
```

**AV2 doubles the DPB to 16 frames** (AV1 had 8) while keeping 7 active
references per frame. The extra slots buy reference-management freedom for
hierarchical GOPs and for the multi-stream operation the OBU set now supports.

**(b) Mode-info grid — per-frame, size-of-frame.**
`CommonModeInfoParams` holds `mi_grid_base`, a grid of `MB_MODE_INFO*` at 4x4
granularity, plus `tx_type_map` and the superblock-info array. Allocated by
`av2_alloc_context_buffers()`, re-allocated whenever frame size changes.

**(c) Search scratch — per-thread, per-superblock, recycled.**
`PC_TREE` and `PICK_MODE_CONTEXT` (`context_tree.h`) are the RD search's working
set. A `PC_TREE` node carries a child pointer array for *every* partition type:

```c
PICK_MODE_CONTEXT *none[REGION_TYPES];
struct PC_TREE *horizontal[REGION_TYPES][2];
struct PC_TREE *vertical[REGION_TYPES][2];
struct PC_TREE *horizontal4a[REGION_TYPES][4];   // uneven 1:2:4:1
struct PC_TREE *horizontal4b[REGION_TYPES][4];   // uneven 1:4:2:1
struct PC_TREE *vertical4a[REGION_TYPES][4];
struct PC_TREE *vertical4b[REGION_TYPES][4];
struct PC_TREE *horizontal3[REGION_TYPES][4];    // 4:1, 2:1, 4:1
struct PC_TREE *vertical3[REGION_TYPES][4];
struct PC_TREE *split[REGION_TYPES][4];
```

`REGION_TYPES == 2` doubles all of it (see §3 on SDP). This structure is
allocated and freed *per node visited* during the partition recursion. It is a
plausible target for arena allocation — though note that a previous attempt at
exactly that (single-arena `PICK_MODE_CONTEXT` allocation) was bit-exact but
measured **slower** at 4K, apparently from locality loss.

### The lookahead buffer

`av2/encoder/lookahead.c` implements a ring of source frames.
`av2_lookahead_push()` copies (or references) an input frame; the depth is
`--lag-in-frames` (CTC uses 19). Everything that needs to see the future —
GOP structure decisions, TPL, temporal filtering, first-pass statistics — reads
from this ring. `COMPRESSOR_STAGE` distinguishes the lookahead-processing stage
from the encode stage so both can consume the ring at different offsets.

---

## 3. Partition search

**Files:** `av2/encoder/partition_search.c` (5,712 lines),
`partition_strategy.c`, `partition_ml.c`, `partition_mlp.c`, `erp_ml.c`,
`var_based_part.c`, `context_tree.c`.

This is the outermost RD loop and the single largest consumer of encode time.

### 3.1 The partition set

```c
// av2/common/enums.h:502
PARTITION_NONE,
PARTITION_HORZ,
PARTITION_VERT,
PARTITION_HORZ_3,   // 3 horizontal sub-partitions, ratios 4:1, 2:1, 4:1
PARTITION_VERT_3,
PARTITION_HORZ_4A,  // 4 horizontal uneven sub-partitions (1:2:4:1)
PARTITION_HORZ_4B,  // 4 horizontal uneven sub-partitions (1:4:2:1)
PARTITION_VERT_4A,
PARTITION_VERT_4B,
PARTITION_SPLIT,
EXT_PARTITION_TYPES = PARTITION_SPLIT,
PARTITION_TYPES = PARTITION_VERT + 1,
```

Note the naming trap: `PARTITION_TYPES` is **3** (the AV1-era base set) while
`ALL_PARTITION_TYPES` is 10. Code that loops to `PARTITION_TYPES` is handling
only NONE/HORZ/VERT.

The six "extended" members — `HORZ_3`, `VERT_3`, and the four uneven 4-ways —
are AV2 additions. They are individually expensive to search and individually
cheap in coding gain; measured on CTC classes A1/A2 at Speed 1, disabling only
the four uneven 4-way members retained **39% of the whole family's speedup for
6% of its BD-rate cost**. That asymmetry is the most useful single fact about
this subsystem: *cost and benefit are not proportionally distributed within the
partition family.*

### 3.2 Superblock size

```c
#define MAX_SB_SIZE_LOG2 8          // enums.h:135
#define MAX_SB_SIZE (1 << MAX_SB_SIZE_LOG2)     // 256
BLOCK_LARGEST = BLOCK_256X256
```

**AV2 superblocks reach 256x256**, double AV1's 128x128. Combined with a
minimum block of 4x4 and the extended partition set, the recursion depth and
branching factor both grow substantially over AV1.

### 3.3 Semi-Decoupled Partitioning (SDP)

```c
SHARED_PART = 0, LUMA_PART = 1, CHROMA_PART = 2   // TREE_TYPE, enums.h:450
INTRA_REGION = 0, MIXED_INTER_INTRA_REGION = 1    // REGION_TYPE, enums.h:457
```

Luma and chroma may carry **separate partition trees**. `PC_TREE` therefore
indexes its children by `REGION_TYPE`, and `av2_rd_pick_partition` takes a
`ptree_luma` argument so that the chroma pass can be constrained by the luma
decision. `av2_get_sdp_idx(xd->tree_type)` selects which of the two partition
tree roots in `SuperBlockInfo` is live.

This roughly doubles the partition search space on intra frames, and it is why
`encode_sb_row` carries a second token pointer (`tok_chroma`) — chroma palette
information for keyframes is tokenized into a separate stream region.

### 3.4 The recursion

```
av2_rd_pick_partition()                      partition_search.c:5531
├─ init_partition_search_state_params()
├─ none_partition_search()                   PARTITION_NONE
│   └─ pick_sb_modes() → av2_rd_pick_{intra,inter}_mode_sb()
├─ prune_partitions_after_none()             early termination
├─ rectangular_partition_search()            HORZ / VERT
│   ├─ prune_rect_partitions()
│   └─ prune_rect_with_mlp()                 NN-based pruning
├─ split_partition_search()                  recursive, 4 children
│   └─ av2_ml_prune_after_split()
├─ prune_partitions_after_split()
├─ search_extended_partition()               HORZ_3/VERT_3/4A/4B
└─ prune_partitions_using_ml_results()
```

Every step writes into `PartitionSearchState`. `best_rdc` is threaded through so
that a subtree can abandon early once it exceeds the incumbent.

### 3.5 Two-pass superblock search — the "dry pass" / "wet pass"

This is the most important recent structural change and it deserves careful
study.

```c
// av2/encoder/enc_enums.h:32
SB_SINGLE_PASS,  // all contexts get updated normally
SB_DRY_PASS,     // first pass of multi-pass: does not update the contexts
SB_WET_PASS      // second pass: finalize and update the context
```

`perform_two_pass_partition_search()` (`encodeframe.c:664`):

1. **Dry pass.** Search with `min_partition_size = BLOCK_16X16` — i.e. stop
   recursing at 16x16. This produces a `PARTITION_TREE` that ranks shapes
   cheaply. Contexts are *not* updated.
2. `set_min_none_to_invalid(part_ref, BLOCK_32X32)` marks the dry pass's
   decisions for blocks ≥32x32 as trustworthy.
3. **Wet pass.** Re-run with the dry-pass tree supplied as
   `SbMultiPassParams`. Shapes ≥32x32 are taken on trust via
   `forced_partition`; unsplit large blocks are re-searched properly.

The dry pass runs with a **reduced tool set**, controlled by
`x->apply_dry_pass_shortcuts` (`block.h:1674`) and a `DryPassCfg` structure of
per-tool caps. As of `d1445fc` the flag is consulted at ~30 sites:

| File | Sites | What is reduced |
|---|---:|---|
| `rdopt.c` | 24 | motion modes, JMVD scale, CWP loop, refineMV loop, DPCM/FSC/MRL intra caps, reference-frame set |
| `tx_search.c` | 1 | `skip_trellis` in `choose_tx_size_type_from_rd` |
| `interp_search.c` | 1 | interpolation filter search |
| `partition_search.c` | 1 | RD bookkeeping |
| `encodeframe_utils.h` | 2 | where the flag and the caps are set |

**The architectural principle worth extracting:** the dry pass is a *ranking*
computation, not a *coding* computation, so any tool whose only effect is on
absolute RD magnitude (not on the ordering of shapes) can be dropped there for
free. Determining which tools are actually ranking-neutral is an empirical
question, and the answer has been counter-intuitive — warp, wedge and MV
precision, which look like magnitude-only refinements, cost **+0.58% BD-rate**
when removed from the dry pass, meaning they do reorder shapes.

A second, subtler property: **dry-pass fidelity reductions get more expensive at
faster presets.** A faster preset has already pruned more of the dry pass (less
left to remove) and trusts the dry-pass ranking further through
`forced_partition` (less downstream correction of a ranking error). Measured:
skipping the dry-pass trellis gave +3.26%/+0.14% BD at Speed 3 but
+1.23%/+0.26% at Speed 4 — *less* speed and *more* damage. This runs opposite to
the usual preset-promotion direction.

The newest upstream commit at this snapshot (#5304, "Bound the two-pass wet-pass
mode search with the dry pass's rd") extends the same idea: the dry pass's RD is
used as an upper bound to prune the wet pass's *mode* search, not just its
partition search.

### 3.6 ML-assisted pruning

Partition pruning is where the encoder's neural networks are concentrated. See
§21 for the inference infrastructure; the consumers here are
`prune_rect_with_mlp()`, `av2_ml_prune_after_split()`,
`prune_partitions_using_ml_results()`, and the `erp_ml.c` extended-recursive-
partition models.

---

## 4. Intra prediction

**Files:** `av2/common/reconintra.c`, `av2/common/intra_matrix.c`,
`av2/common/cfl.c`, `av2/encoder/intra_mode_search.c`,
`av2/encoder/intra_mode_mlp.c`, `av2/encoder/palette.c`.

### 4.1 Mode set

The luma intra mode count is explicit:

```c
#define LUMA_MODE_COUNT 61        // enums.h:919
INTRA_MODES = PAETH_PRED + 1      // the 13 "classic" directional + smooth modes
```

61 luma modes arises from the classic 13 combined with AV2's angular
refinements and the added tools below. Chroma has its own `UV_*` enum including
`UV_CFL_PRED`.

### 4.2 The AV2 additions

**DIP — matrix intra prediction** (`av2/common/intra_matrix.c`):

```c
#define DIP_ROWS 64
#define DIP_COLS 16
#define DIP_BITS 12
#define DIP_FEATURES 11
extern const uint16_t av2_intra_matrix_weights[][DIP_ROWS][DIP_COLS];
```

A learned matrix multiply from a downsampled neighbour vector to a predicted
block, quantized to 12 bits. This is AV2's analogue of VVC's MIP. The encoder
prunes DIP modes with a **TFLite model**
(`av2/encoder/intra_dip_mode_prune_tflite.h`, gated by `CONFIG_DIP_EXT_PRUNING`).

**MRL — multiple reference lines.** The `intra_mrl_cap` in `DryPassCfg` and the
`mrl_index` loop in `rdopt.c:8892` show the encoder searches several reference
line offsets per mode.

**FSC — forward skip coding.** `mbmi->fsc_mode[2]` (indexed by luma/chroma tree)
selects a coding mode that pairs with `IDTX` and takes a separate optimization
path (`av2_optimize_fsc` rather than `av2_optimize_b`). Screen-content oriented.

**DPCM.** `intra_dpcm_cap` in `DryPassCfg`; a per-block DPCM residual mode.

**CfL** (`cfl.c`) survives from AV1 but is now entangled with SDP —
`is_cfl_allowed()` must consult `xd->is_cfl_allowed_in_sdp`, because a chroma
block under a decoupled tree may not have a co-located luma reconstruction
available.

### 4.3 Encoder search

```
av2_rd_pick_intra_mode_sb()          rdopt.c:6821    (intra frames)
av2_rd_pick_intra_sby_mode()         intra_mode_search.c:1695
av2_rd_pick_intra_sbuv_mode()        intra_mode_search.c:559
av2_handle_intra_mode()              intra_mode_search.c:1145 (inter frames)
av2_search_palette_mode()            intra_mode_search.c:798
av2_intra_mlp_compute_mode_mask()    intra_mode_search.c:1393
```

Two things distinguish this from AV1's intra search. First, `av2_intra_mlp_compute_mode_mask`
uses an MLP (`intra_mode_mlp_weights.h`, 6,063 lines of weights) to compute a
*mask* of modes worth evaluating, rather than a rank ordering. Second, the
nested loop structure over `{dpcm, fsc, mrl, mode, angle}` means the raw
candidate count is large enough that the dry pass caps each dimension
independently.

Note a code-hygiene detail with real consequences: `intra_mode_search.c:384`
contains `const int skip_trellis = 0;` — hard-coded, so intra mode search always
trellises even in the dry pass. `tx_search.c` had the identical pattern until it
was made dry-pass-aware. This is a consistency gap worth investigating.

---

## 5. Inter prediction and motion estimation

**Files:** `av2/encoder/mcomp.c` (5,549), `motion_search_facade.c`,
`compound_type.c`, `interp_search.c`, `av2/common/mvref_common.c` (4,992),
`av2/common/reconinter.c` (4,135), `av2/common/warped_motion.c`,
`av2/encoder/global_motion*.c`.

### 5.1 Mode set

```c
// enums.h:847 — single reference
GLOBALMV, NEWMV, WARPMV, WARP_NEWMV,
// compound
NEAR_NEARMV, NEAR_NEWMV, ..., GLOBAL_GLOBALMV, NEW_NEWMV, JOINT_NEWMV,
// optical-flow refined compound
NEAR_NEARMV_OPTFLOW, ..., JOINT_NEWMV_OPTFLOW,
MB_MODE_COUNT
```

`WARPMV` and `WARP_NEWMV` are first-class *modes*, not motion-mode refinements —
a significant departure from AV1 where warp was only a motion mode.
`JOINT_NEWMV` codes one MV plus a scaled derivative for the second reference
(`JOINT_NEWMV_SCALE_FACTOR_CNT 5`).

### 5.2 Motion modes

```c
// enums.h:922
SIMPLE_TRANSLATION,
INTERINTRA,
WARP_CAUSAL,   // warp estimated from spatial MVs
WARP_DELTA,    // directly-signaled warp model
WARP_EXTEND,   // extension of a neighbour's warp model
MOTION_MODES
```

Note what is **absent**: `OBMC_CAUSAL`. AV1's overlapped block motion
compensation is gone; three warp variants replace it.

### 5.3 Flexible MV precision

```c
// av2/common/mv.h:75
MV_PRECISION_8_PEL, MV_PRECISION_FOUR_PEL, MV_PRECISION_TWO_PEL,
MV_PRECISION_ONE_PEL, MV_PRECISION_HALF_PEL, MV_PRECISION_QTR_PEL,
MV_PRECISION_ONE_EIGHTH_PEL, NUM_MV_PRECISIONS
```

Seven precisions from 8-pel down to 1/8-pel, **signalled per block**
(`write_pb_mv_precision`, `bitstream.c:1702`), with a per-frame allowed subset
chosen by `av2_pick_and_set_high_precision_mv()` (`mv_prec.c:497`) from
statistics gathered by `av2_collect_mv_stats()`. This is a real search-cost
multiplier: every precision is a separate motion search unless capped.

### 5.4 Search structure

```
av2_full_pixel_search()               mcomp.c:2388
  └─ per SEARCH_METHODS: DIAMOND, NSTEP, HEX, BIGDIA, SQUARE,
     FAST_HEX, FAST_DIAMOND, FAST_BIGDIA
av2_find_best_sub_pixel_tree()        mcomp.c:4243
  └─ _pruned, _pruned_more, _pruned_evenmore variants
av2_joint_amvd_motion_search()        mcomp.c:3827
av2_intrabc_hash_search()             mcomp.c:2667   (screen content, see §19)
```

Search sites are precomputed into `search_site_config` tables at init
(`av2_init_motion_compensation_{nstep,bigdia,square,hex}`), so the inner loop is
a table walk rather than a geometry computation.

### 5.5 Compound prediction

```c
COMPOUND_AVERAGE, COMPOUND_WEDGE, COMPOUND_DIFFWTD, COMPOUND_TYPES
#define MAX_CWP_NUM 5      // compound weighted prediction
#define CWP_MIN -4
#define CWP_MAX 20
#define CWP_EQUAL 8
```

**CWP** (compound weighted prediction) adds 5 signalled weight pairs beyond
simple averaging. `av2_compound_type_rd()` (`compound_type.c:1136`) searches
wedge and diff-weighted masks; `av2_handle_inter_intra_mode()` handles the
inter-intra blend.

### 5.6 Optical flow refinement (OPFL) and refineMV

```c
#define OPFL_GRAD_UNIT_LOG2 4      // 16x16 gradient units
#define REFINEMV_SUBBLOCK_WIDTH 16
#define REFINEMV_SUBBLOCK_HEIGHT 16
```

`*_OPTFLOW` modes refine a compound prediction with a decoder-side optical-flow
computation on 16x16 sub-blocks. `MACROBLOCK` carries dedicated scratch:
`opfl_vxy_bufs`, `opfl_gxy_bufs`, `opfl_dst_bufs`. This is decoder-side motion
derivation — expensive on both sides, and the encoder must mirror it exactly.

### 5.7 Reference MV construction

`av2/common/mvref_common.c` is 4,992 lines and builds the DRL (dynamic reference
list). `MAX_DRL_BITS` is frame-adaptive (`write_frame_max_drl_bits`), and there
is a **RefMV bank** (`av2_reset_refmv_bank`) — a superblock-level cache of
recently used MVs that extends the candidate list beyond spatial/temporal
neighbours. Also `write_frame_max_bvp_drl_bits` for block-vector prediction
(IntraBC).

---

## 6. Transform and secondary transform

**Files:** `av2/encoder/tx_search.c` (4,464), `hybrid_fwd_txfm.c`,
`av2_fwd_txfm2d.c`, `av2/common/av2_inv_txfm2d.c`, `av2/common/idct.c`,
`av2/common/secondary_tx.h` (5,253), `av2/common/scan.c`.

### 6.1 Transform sizes and partitioning

```c
TX_4X4 … TX_64X64, plus rectangular         // TX_SIZES_ALL
TX_PARTITION_NONE, SPLIT, HORZ, VERT,
TX_PARTITION_HORZ4, VERT4, HORZ5, VERT5     // enums.h:633
TX_PARTITION_TYPES
```

`HORZ5` / `VERT5` are AV2 additions — five-way transform partitions. Transform
partitioning is recursive within a prediction block
(`av2_pick_recursive_tx_size_type_yrd`) for inter blocks, or uniform
(`av2_pick_uniform_tx_size_type_yrd`) when the speed features say so.

### 6.2 Primary transform types

16 types: `DCT_DCT`, `ADST_DCT`, `DCT_ADST`, `ADST_ADST`, the four FLIPADST
combinations, `IDTX`, and the six 1D `{V,H}_{DCT,ADST,FLIPADST}` types.
Grouped into `EXT_TX_SET_*` sets whose availability depends on block size,
prediction mode and `reduced_tx_set_used`.

### 6.3 IST — Intra Secondary Transform

```c
#define STX_TYPES 4        // 3 kernels + none
IST_SET_SIZE, IST_REDUCED_SET_SIZE, IST_4x4_SET_SIZE, IST_8x8_SET_SIZE
static const int16_t ist_4x4_kernel[IST_4x4_SET_SIZE][STX_TYPES-1][16][IST_4x4_WIDTH];
static const int16_t ist_8x8_kernel[...][IST_8x8_HEIGHT_MAX][IST_8x8_WIDTH];
```

A non-separable secondary transform applied to the top-left 4x4 or 8x8 of the
primary transform output, selected by intra mode via
`most_probable_stx_mapping[INTRA_MODES-1][IST_SET_SIZE]`. The kernel tables
alone are ~5,000 lines.

**This multiplies the transform search.** The `search_tx_type` loop nests
`{primary_tx_type} × {IST set_idx} × {stx}` and each combination runs a full
forward transform, quantization, trellis and cost. `get_ist_max_set_id()` and
`skip_stx` bound it, but the raw candidate count is the single largest driver of
transform-search cost.

### 6.4 CCTX — cross-component transform

`CctxType` / `CCTX_TYPES` — a transform applied jointly across U and V. This is
why `av2_txfm_rd_joint_uv()` exists and why the V plane's `av2_quant` reads the
U plane's signs into `xd->tmp_sign`.

### 6.5 The transform search entry points

```
av2_txfm_search()                     tx_search.c:4478   top level
├─ av2_pick_recursive_tx_size_type_yrd()   recursive TX partition, inter
├─ av2_pick_uniform_tx_size_type_yrd()     uniform TX size
│   └─ choose_tx_size_type_from_rd()       ← dry-pass aware
├─ av2_txfm_uvrd() / av2_txfm_rd_joint_uv()
└─ search_tx_type()                   tx_search.c:2314   ← the hot loop
```

`search_tx_type` is where ~half the encoder's instructions are ultimately spent
(see §7).

---

## 7. Quantization and TCQ

**Files:** `av2/encoder/trellis_quant.c` (1,173), `av2/encoder/av2_quantize.c`,
`av2/encoder/encodemb.c`, `av2/common/quant_common.{h,c}`,
`av2/common/predefined_qm.c` (13,649).

### 7.1 Extended Q range

```c
#define QINDEX_BITS 9
#define MAXQ_OFFSET 24
#define MAXQ (255 + 4 * MAXQ_OFFSET)          // 351
#define MAXQ_10_BITS (255 + 2 * MAXQ_OFFSET)  // 303
#define QINDEX_INCR 2
```

AV2 extends qindex beyond AV1's 0–255, with bit-depth-dependent maxima. Any code
that assumes a 256-entry qindex table is wrong.

### 7.2 Quantization matrices

`predefined_qm.c` is 13,649 lines of tables. `NUM_QM_LEVELS = 16`, with defaults
`DEFAULT_QM_Y=10`, `DEFAULT_QM_U=11`, `DEFAULT_QM_V=12`. AV2 additionally
supports **custom QMs signalled in a dedicated OBU** (`OBU_QUANTIZATION_MATRIX`,
written by `write_qm_data` in `bitstream_qm.c`).

### 7.3 TCQ — Trellis Coded Quantization

This is the defining AV2 quantization change and the encoder's dominant cost
centre.

```c
// av2/common/quant_common.h:27
#define TCQ_N_STATES_LOG 3
#define TCQ_N_STATES (1 << TCQ_N_STATES_LOG)   // 8
#define TCQ_MAX_STATES 8

// In the 8-state scheme, states 0/1/4/5 use Q0 and 2/3/6/7 use Q1.
static INLINE bool tcq_quant(const int state) { return state & 2; }
```

TCQ is **a bitstream feature, not a search knob**: the decoder's dequantizer
follows a state machine driven by the parity of previously decoded coefficients,
alternating between two reconstruction grids Q0 and Q1. The encoder must run a
Viterbi search over that trellis to choose levels.

```c
// quant_common.h:74
static INLINE bool tcq_enable(int enable_tcq, int lossless, int plane,
                              TX_CLASS tx_class) {
  int dq_en = (!lossless && enable_tcq != 0);
  dq_en &= plane == 0;                 // luma only
  dq_en &= tx_class == TX_CLASS_2D;    // 2D transform classes only
  return dq_en;
}
```

`av2_trellis_quant()` (`trellis_quant.c:1247`) implements the search:
`trellis_loop_diagonal_st8` walks scan positions from `eob-1` down, evaluating 8
states per position via `av2_decide_states` / `av2_pre_quant` /
`av2_update_nbr_diagonal`, then `av2_find_best_path` back-traces.

### 7.4 A structural finding: the trellis speed features are dead for luma

A callgrind profile of this encoder attributes **~47% of retired instructions**
to trellis quantization, and `trellis_quant.c` contains **zero** `sf->` or
`cpi->sf.` references. That is not because no speed feature governs it — two do,
and both are bypassed for the plane that carries the cost.

In `search_tx_type` (`tx_search.c`):

```c
// line 2412
int perform_block_coeff_opt = 0;
if (tcq_enable(cm->features.tcq_mode, is_lossless, plane, TX_CLASS_2D)) {
  perform_block_coeff_opt = 1;                     // unconditionally ON
} else {
  perform_block_coeff_opt =                        // sf: perform_coeff_opt
      (block_mse_q8 <= coeff_opt_dist_threshold * qstep * qstep);
}
skip_trellis |= !perform_block_coeff_opt;

// line 2591
if (use_tcq) {
  skip_trellis_based_on_satd[tx_type] = skip_trellis;
} else {
  skip_trellis_based_on_satd[tx_type] =            // sf: ..._based_on_satd
      skip_trellis_opt_based_on_satd(..., coeff_opt_satd_threshold, ...);
}
```

Both `perform_coeff_opt` (tuned `0 → 2/3 → 3/5 → 4/6` across the presets in
`speed_features.c`) and `perform_coeff_opt_based_on_satd` (`0 → 1/2`) are
therefore live only for **chroma, 1D transform classes, and lossless**. Note the
first call site passes `TX_CLASS_2D` as a *literal*, so for luma it is true
regardless of the block's actual transform class.

**This is a correctness requirement, not an oversight.** Under TCQ the decoder
dequantizes with the state machine; an encoder that quantized a luma block
scalar-only and kept its scalar `dqcoeff` would drift. The gates are off for a
reason.

The consequence for anyone optimizing here: the available saving is *not*
"skip the trellis on some blocks" but "**stop running the trellis on transform
candidates that cannot win**". `av2_optimize_b` is called inside the
`{tx_type} × {set_idx} × {stx}` loop; one result survives. The code already
believes this test is worth making — it performs `RDCOST(rdmult, rate_cost, 0) >
best_rd → continue` immediately *after* the trellis.

### 7.5 Parity hiding

`PHTHRESH 4`, `parity_hiding_trellis_off()`, `ph_allowed_tx_types[]`. When the
trellis is off but parity hiding is allowed, a separate tuning pass adjusts
coefficients so the hidden parity bit is correct.

---

## 8. Reconstruction and residual

**Files:** `av2/encoder/encodemb.c`, `av2/encoder/encodetxb.c` (4,818),
`av2/common/reconinter.c`, `av2/common/reconintra.c`, `av2/common/convolve.c`.

### 8.1 The residual pipeline

```
av2_subtract_txb()          source − prediction → p->src_diff
av2_xform()                 forward transform    → p->coeff
  └─ av2_xform_dc_only()    fast path when only DC survives
av2_quant()                 → p->qcoeff, p->dqcoeff, p->eobs
  └─ quant_func_list[qparam->xform_quant_idx]
av2_optimize_b()            → trellis / RDOQ    (encodemb.c:328)
  ├─ av2_trellis_quant()      when tcq_enable()
  └─ av2_optimize_txb_new()   otherwise
av2_inverse_transform_block()  → reconstruction
```

`AV2_XFORM_QUANT_{FP,B,DC,SKIP_QUANT}` selects the scalar quantizer variant;
`av2_setup_quant()` / `av2_update_trellisq()` configure `QUANT_PARAM` including
`use_optimize_b`.

### 8.2 Skip / eob short circuits

Several early exits shape the cost profile:

- `predict_dc_only_block()` — if residual mean and variance are both below
  qstep-derived thresholds, declare the block skipped without transforming.
- `prune_tx_search_by_eob()` — kills candidates by `eob == 0` / `eob == 1`
  depending on primary type and `stx`.
- `check_primary_quant_all_zero()` — a *pre*-quantization gate that predicts
  `eob == 0` from post-primary coefficients, scoped to intra luma with IST
  enabled and no QM (`prune_intra_ist_stx_by_zero_eob`).
- `prune_sec_txfm_rd_eval()` — prunes secondary-transform evaluation from the
  secondary SSE.

### 8.3 `bob` — beginning of block

`set_bob()` computes `bob = max_eob − (leading zeros in scan order)`. AV2 codes
coefficients from both ends for some configurations; `bob` is the mirror of
`eob`.

---

## 9. In-loop filtering

**Files:** `av2/common/av2_loopfilter.c`, `av2/common/cdef*.c`,
`av2/common/restoration.c` (3,185), `av2/common/ccso.c`,
`av2/common/gdf*.c`, and encoder-side `picklpf.c`, `pickcdef.c`,
`pickrst.c` (4,018), `pickccso.c` (1,903).

### 9.1 The pipeline order

From `loopfilter_frame()` (`encoder.c:3223`) and `cdef_restoration_frame()`
(`encoder.c:3031`):

```
1. Deblocking          av2_pick_filter_level() → av2_loop_filter_frame[_mt]()
2. CDEF                av2_cdef_search()       → av2_cdef_frame()
3. CCSO                av2_ccso_search()       → ccso filter
4. Loop restoration    av2_pick_filter_restoration() → av2_loop_restoration_filter_frame[_mt]()
5. GDF                 gdf_optimize_frame()    → gdf_filter_frame()
```

Each stage is gated independently: `enable_deblocking`, `seq_params.enable_cdef`,
`enable_ccso`, `enable_restoration`, `enable_gdf`, and all are additionally
suppressed for lossless, bridge frames, and BRU-inactive frames.

### 9.2 Loop restoration: AV1's filters are gone

```c
// enums.h:1126
RESTORE_NONE,
RESTORE_PC_WIENER,      // Pixel-classified Wiener
RESTORE_WIENER_NONSEP,  // Nonseparable Wiener
RESTORE_SWITCHABLE,
RESTORE_TYPES
```

**AV2 replaces AV1's separable Wiener and self-guided (SGRPROJ) restoration
entirely.** `RESTORE_PC_WIENER` classifies each pixel (by local gradient/
direction) and applies a class-specific filter from `pc_wiener_filters.h` (1,763
lines of coefficients). `RESTORE_WIENER_NONSEP` fits a non-separable kernel per
restoration unit. `pickrst.c` is 4,018 lines because fitting these is a
least-squares problem per unit per class.

Note `DEF_UV_LR_TOOLS_DISABLE_MASK (1 << RESTORE_PC_WIENER)` — PC-Wiener is
disabled for chroma by default.

### 9.3 CCSO — Cross-Component Sample Offset

```c
#define CCSO_INPUT_INTERVAL 3
#define CCSO_PROC_BLK_LOG2 5      // 32x32 processing blocks
```

Derives a chroma (or luma) sample offset from a *classification of co-located
luma samples*. The encoder side (`pickccso.c`) builds per-class error histograms
(`ccso_pre_compute_class_err`), derives LUT offsets (`derive_lut_offset`), and
RD-searches band-offset and edge-offset variants, with per-superblock on/off and
a reuse mechanism (`sb_reuse_ccso`).

### 9.4 GDF — Guided Deep Filter

The newest filter (copyright 2025). `av2/common/gdf.c` + `gdf_block.c` +
an AVX2 kernel.

```c
enum Direction { GDF_VER, GDF_HOR, GDF_DIAG0, GDF_DIAG1, GDF_NUM_DIRS };
#define GDF_RDO_QP_NUM_LOG2 2
#define GDF_RDO_SCALE_NUM_LOG2 2
```

It carries **weight, bias, clipping and expected-coding-error tables indexed by
QP** (`gdf_get_qp_idx_base`), operates on a separately-maintained "guided frame"
(`gdf_copy_guided_frame` / `gdf_free_guided_frame`), and processes in stripes
with explicit boundary save/restore (`gdf_setup_processing_stripe_leftright_boundary`).
The structure — directional features, QP-indexed learned weights, per-block
index selection — is that of a small learned filter rather than a hand-designed
one.

### 9.5 Filter search is a per-frame RD problem

All four searches (`pick_filter_level`, `cdef_search`, `pick_filter_restoration`,
`ccso_search`) take `rdmult` and run their own RD optimizations *after* the
frame is otherwise encoded. On slow presets this is a non-trivial share of frame
time, and `LOOP_FILTER_SPEED_FEATURES` (`lpf_sf`) exists to bound it —
`cdef_pick_method`, `lpf_pick`, and the WienerNS refinement levels.

---

## 10. Entropy coding and OBU

**Files:** `avm_dsp/entenc.c`, `entdec.c`, `entcode.c`, `prob.h`,
`bitwriter*.c`; `av2/common/entropy*.{c,h}`, `av2/common/txb_common.c`,
`av2/common/hr_coding.c`; `av2/encoder/bitstream.c` (7,306),
`encodetxb.c`, `tokenize.c`, and `bitstream_{atlas,buf,ci,fgm,lcr,ops,qm}.c`.

### 10.1 The arithmetic coder

A daala-derived multi-symbol range coder.

```c
#define CDF_PROB_BITS 15
#define CDF_PROB_TOP (1 << CDF_PROB_BITS)
#define EC_MIN_PROB 4
```

`update_cdf()` (`prob.h:218`) performs the backward adaptation after each
symbol. `TOKEN_CDF_Q_CTXS 4` — coefficient CDFs are initialized from one of four
q-dependent tables.

### 10.2 PARA — probability adaptation rate adjustment

The repository ships **ParaKit**, a Python toolkit for *training* the CDF
initialization and adaptation-rate parameters (`ParaKit/`, from Apple's CWG-D115
proposal). This means AV2's entropy contexts are not hand-tuned constants but a
trained artifact with a reproducible training pipeline. `tools/py_stats/` and
`tools/aggregate_entropy_stats.py` collect the statistics;
`CONFIG_ENTROPY_STATS` and `CONFIG_PARAKIT_COLLECT_DATA` gate the instrumentation.

**For any research programme on AV2, this is a significant and under-appreciated
lever** — retraining contexts is a legitimate, tool-free source of gain.

### 10.3 Coefficient coding

`av2/common/entropy.h` shows a substantially restructured coefficient syntax vs
AV1:

```c
V_TXB_SKIP_CONTEXTS 12          TXB_SKIP_CONTEXTS 10
LF_SIG_COEF_CONTEXTS_2D 21      LF_SIG_COEF_CONTEXTS_2D_UV 8
SIG_COEF_CONTEXTS_UV 12         LEVEL_CONTEXTS_UV 4
IDTX_SIGN_CONTEXTS 9            IDTX_SIG_COEF_CONTEXTS 7
COEFF_BASE_PH_CONTEXTS 5        COEFF_BR_PH_CONTEXTS 7   (parity hiding)
TCQ_CTXS 2                      EOB_MAX_SYMS 11
CROSS_COMPONENT_CONTEXTS 3      SIG_COEF_CONTEXTS_BOB 3
```

Key changes: a separate **low-frequency (`LF_*`) context set** for the
first coefficients; separate luma and chroma context counts; dedicated `IDTX_*`
contexts; `TCQ_CTXS` conditioning on trellis state; and `*_BOB` contexts for the
beginning-of-block direction.

**`hr_coding.c`** implements the high-range escape coding: adaptive
Exp-Golomb (`get_adaptive_hr_length`, `get_adaptive_param(ctx)`) and truncated
Rice (`get_truncated_rice_length`). AV1 used a fixed Golomb order; AV2 adapts it
per context.

### 10.4 The OBU set — substantially redesigned

```c
// avm/avm_codec.h:559
OBU_SEQUENCE_HEADER = 1,    OBU_TEMPORAL_DELIMITER = 2,
OBU_MULTI_FRAME_HEADER = 3,
OBU_CLOSED_LOOP_KEY = 4,    OBU_OPEN_LOOP_KEY = 5,
OBU_LEADING_TILE_GROUP = 6, OBU_REGULAR_TILE_GROUP = 7,
OBU_METADATA_SHORT = 8,     OBU_METADATA_GROUP = 9,
OBU_SWITCH = 10,
OBU_LEADING_SEF = 11,       OBU_REGULAR_SEF = 12,
OBU_LEADING_TIP = 13,       OBU_REGULAR_TIP = 14,
OBU_BUFFER_REMOVAL_TIMING = 15,
OBU_LAYER_CONFIGURATION_RECORD = 16,
OBU_ATLAS_SEGMENT = 17,     OBU_OPERATING_POINT_SET = 18,
OBU_BRIDGE_FRAME = 19,      OBU_MULTI_STREAM_DECODER_OPERATION = 20,
OBU_RAS_FRAME = 21,         OBU_QUANTIZATION_MATRIX = 22,
OBU_FILM_GRAIN_MODEL = 23,  OBU_CONTENT_INTERPRETATION = 24,
OBU_PADDING = 25,
```

This is not AV1's OBU set with additions — it is a redesign. Frame *type* is now
carried in the OBU type itself (closed-loop key, open-loop key, leading vs
regular tile group, SEF, TIP, bridge, RAS). Sequence-level configuration is
factored into standalone OBUs (`LAYER_CONFIGURATION_RECORD`,
`OPERATING_POINT_SET`, `CONTENT_INTERPRETATION`, `QUANTIZATION_MATRIX`,
`FILM_GRAIN_MODEL`) each with its own writer module in `av2/encoder/bitstream_*.c`.

`OBU_MULTI_STREAM_DECODER_OPERATION` and `AVM_MAX_NUM_STREAMS` in the frame-buffer
sizing indicate first-class multi-stream support.

---

## 11. RDO and lambda mechanics

**Files:** `av2/encoder/rd.c` (1,774), `rd.h`, `rdopt.c` (9,865),
`rdopt_utils.h`, `model_rd.h`, `av2/common/cost.c`.

### 11.1 The cost function

```c
// av2/encoder/rd.h:37
#define RDCOST(RM, R, D) \
  (ROUND_POWER_OF_TWO(((int64_t)(R)) * (RM), AV2_PROB_COST_SHIFT) + \
   ((D) * (1 << RDDIV_BITS)))
```

Rate is carried in units of `bits << AV2_PROB_COST_SHIFT` throughout —
`av2_cost_symbol()` returns costs at that fixed-point scale, which is why raw
`rate` values in the encoder are ~512× the bit count.

### 11.2 Lambda derivation

```
av2_compute_rd_mult_based_on_qindex()   rd.c:658    base from qindex, bit-depth scaled
av2_compute_rd_mult()                   rd.c:683    × layer-depth factor, + boost factor
av2_get_adaptive_rdmult(cpi, beta)      rd.c:720    perceptual / TPL modulation
av2_initialize_rd_consts()              rd.c:1294   per-frame cost table setup
```

```c
rdmult = (rdmult * rd_layer_depth_factor[layer_depth]) >> 7;
rdmult += ((rdmult * rd_boost_factor[boost_index]) >> 7);
```

**Lambda is a function of qindex, bit depth, pyramid layer depth, and ARF boost.**
The layer-depth term is what makes high-pyramid-level frames cheap: they get a
larger rdmult, spend fewer bits, and — importantly for encoder speed work — also
carry proportionally less encode time, which caps how much any
high-layer-only optimization can win.

`mv_costs->errorperbit = AVMMAX(rdmult >> RD_EPB_SHIFT, 1)` (`RD_EPB_SHIFT 6`)
derives the motion-search lambda from the same source.

### 11.3 The mode-decision loop

```
av2_rd_pick_inter_mode_sb()                rdopt.c:9138
├─ set_params_rd_pick_inter_mode()         ref frame masks, mv candidates
├─ for each ref_frame combination:
│   └─ handle_inter_mode()                 rdopt.c:5735
│       ├─ motion search (§5)
│       ├─ av2_interpolation_filter_search()
│       ├─ av2_compound_type_rd()
│       ├─ motion-mode loop (SIMPLE/WARP_*/INTERINTRA)
│       └─ av2_txfm_search()               (§6)
├─ intra modes via av2_handle_intra_mode()
├─ winner-mode refinement (WINNER_MODE_SPEED_FEATURES)
└─ rd_pick_intrabc_mode_sb()               rdopt.c:6256 (screen content)
```

### 11.4 Model RD

`model_rd.h` provides closed-form rate/distortion estimates from residual
variance, used to rank candidates before committing to a full transform search.
`TXFM_RD_MODEL` in `MACROBLOCK` selects the fidelity level. `rd_model` and the
`tx_domain_dist` family are the encoder's main "estimate instead of compute"
mechanism and a natural place to look for speed.

### 11.5 Winner mode processing

`MODE_EVAL_TYPES` distinguishes *default*, *mode evaluation*, and *winner mode
evaluation*. Thresholds like `coeff_opt_dist_thresholds[level][MODE_EVAL_TYPES]`
are indexed by which phase is running, so the encoder can search coarsely and
then re-evaluate the winner at full fidelity. `rdopt_utils.h` (`set_mode_eval_params`)
switches the phase.

---

## 12. Speed feature framework

**Files:** `av2/encoder/speed_features.c` (1,501), `speed_features.h` (1,166).

### 12.1 Structure

`SPEED_FEATURES` aggregates 14 category structs:

```
hl_sf     HIGH_LEVEL          recode loop, frame-level decisions
tpl_sf    TPL                 temporal dependency model effort
gm_sf     GLOBAL_MOTION
part_sf   PARTITION           ~80 fields — the largest group
mv_sf     MV                  search method, subpel effort
inter_sf  INTER_MODE          ~200 fields
interp_sf INTERP_FILTER
intra_sf  INTRA_MODE
tx_sf     TX                  transform search pruning
rd_sf     RD_CALC             coeff opt, tx-domain distortion
winner_mode_sf WINNER_MODE
lpf_sf    LOOP_FILTER
rt_sf     REALTIME
flexmv_sf FLEXMV_PRECISION
```

### 12.2 How presets are assembled

```
av2_set_speed_features_framesize_independent(cpi, speed)   speed_features.c:1238
  ├─ set_good_speed_features_framesize_independent()
  │     if (speed >= 1) { ... }   ← cumulative, monotone
  │     if (speed >= 2) { ... }
  │     ... through speed >= 6
  └─ set_rt_speed_features_framesize_independent()   (realtime usage)

av2_set_speed_features_framesize_dependent(cpi, speed)     speed_features.c:1115
av2_set_speed_features_qindex_dependent(cpi, speed)        speed_features.c:1490
```

Three properties follow from this design and matter for anyone modifying it:

1. **Assignments are cumulative and monotone.** A feature set at `speed >= 2`
   stays set for 3–6 unless overwritten later. Moving an assignment from
   `speed >= N` to `speed >= N-1` ("preset promotion") applies it to one more
   preset. Empirically this is the highest-yield mechanism available: of seven
   promotion candidates tried on CTC, six passed the complexity/efficiency bar.

2. **Many features are conditioned on frame type at assignment time**, e.g.
   `is_boosted_arf2_bwd_type ? 3 : 5`. The condition is evaluated once per
   frame, not per block, so gating by frame class is nearly free.

3. **Framesize- and qindex-dependent passes run after the independent one**, so
   they can override. A change made in the wrong pass may be silently undone.

### 12.3 The threshold tables

```c
static unsigned int coeff_opt_dist_thresholds[7][MODE_EVAL_TYPES];
static unsigned int coeff_opt_satd_thresholds[3][MODE_EVAL_TYPES] = {
  { UINT_MAX, UINT_MAX, UINT_MAX },   // level 0: off
  { 97, 16, UINT_MAX },
  { 25, 10, UINT_MAX },
};
static TX_SIZE_SEARCH_METHOD tx_size_search_methods[3][MODE_EVAL_TYPES];
```

A speed feature is typically an *index into a table*, not a value. This
indirection means grepping for a feature name finds the assignment but not the
effect; you must follow the index into the table.

### 12.4 Evaluation criterion

The AVM community evaluates encoder speedups by the **complexity-to-efficiency
ratio** = speedup% ÷ BD-rate%, with preset-dependent bars (≥35 at Speed 1, ≥30
at Speed 2, ≥25 at Speed 3, ≥20 at Speed 4), and the ratio must clear the bar on
**both** CTC class A1 (4K) and A2 (1080p) independently. This is the number any
speed-feature change is judged by, and it has a non-obvious implication: a change
improves an existing feature's ratio only if its *marginal* ratio (Δspeed/ΔBD)
is below the current ratio.

---

## 13. SIMD and the portability layer

**Files:** `av2/common/av2_rtcd_defs.pl`, `avm_dsp/avm_dsp_rtcd_defs.pl`,
`build/cmake/*`, and the `x86/`, `arm/`, `mips/`, `simd/` subdirectories.

### 13.1 RTCD is the architecture, not the intrinsics

Kernels are declared in Perl:

```perl
add_proto qw/void av2_fwd_txfm2d_4x4/, "const int16_t *input, ...";
specialize qw/av2_fwd_txfm2d_4x4 sse4_1 avx2/;
```

`av2_rtcd_defs.pl` (483 lines) and `avm_dsp_rtcd_defs.pl` (1,240 lines) generate
`av2_rtcd.h` / `avm_dsp_rtcd.h` at build time. At runtime
`CONFIG_RUNTIME_CPU_DETECT` selects the best available implementation via
function pointers set by `av2_rtcd()` / `avm_dsp_rtcd()`.

**Practical consequence:** a new C function is invisible to SIMD until it is
declared in the `.pl` file. Conversely, adding a `specialize` line without the
implementation is a link error. This file is where you find out what is
*expected* to be optimized.

### 13.2 Coverage

| | sse2 | ssse3 | sse4_1 | avx2 | neon | other |
|---|---:|---:|---:|---:|---:|---:|
| `av2_rtcd_defs.pl` | 16 | 15 | 30 | 90 | 19 | 1 vsx |
| `avm_dsp_rtcd_defs.pl` | 344 | — | 6 | 271 | 15 | 2 msa, 1 dspr2 |

Two things stand out:

- **AVX2 is the primary target for AV2-specific code** (90 of ~150 specializations
  in `av2_rtcd_defs.pl`). AVX-512 is absent.
- **Arm/NEON coverage is thin** — 19 and 15 specializations against 90 and 271
  for AVX2, with a single file in `av2/encoder/arm/`. For a codec intended to
  ship on mobile, this is the largest optimization gap in the codebase.

### 13.3 File distribution

```
avm_dsp/x86       81 files      av2/common/x86    27
avm_dsp/arm       13            av2/common/arm     4
avm_dsp/simd      13            av2/encoder/x86   25
                                av2/encoder/arm    1
```

`avm_dsp/simd/` holds the portable abstraction layer (`v128_intrinsics.h` and
friends) that CDEF in particular uses to write one kernel compiled for several
targets — `cdef_block_simd.h` is the exemplar.

### 13.4 What is not vectorized

Notably absent from the specialization lists: the trellis quantizer's decision
loop (`av2_decide_states`, `av2_pre_quant`, `av2_update_nbr_diagonal`,
`av2_get_rate_dist_*`, `av2_find_best_path` are declared with `_c` suffixes and
RTCD hooks, so they are *intended* to be vectorizable), and most of the search
control flow. Given the ~47% profile share of trellis quantization, the state of
its SIMD implementations is the highest-value thing to audit in this subsystem.

---

## 14. Command line and presets

**Files:** `av2/arg_defs.c` (254 argument definitions), `apps/avmenc.c`,
`av2/av2_cx_iface.c`, `Sample.cfg`, `tools/convexhull_framework/src/config.yaml`.

### 14.1 The preset axis

`--cpu-used` ranges 0–9 (`RANGE_CHECK(extra_cfg, cpu_used, 0, 9)`), though the
speed-feature code only differentiates through `speed >= 6`. Two usage modes
(`g_usage`) select `set_good_speed_features_*` vs `set_rt_speed_features_*`.

### 14.2 The CTC invocation

The common-test-conditions command line is what most measurements actually run,
and it is worth having in front of you because several defaults differ from
casual use:

```
--codec=av2 --passes=1 --end-usage=q --qp=<Q> --threads=1
--frame-parallel=0 --lag-in-frames=19 --auto-alt-ref=1 --kf-max-dist=65
--use-fixed-qp-offsets=1 --deltaq-mode=0 --enable-tpl-model=0
--obu --psnr --i420 --cpu-used=<preset>
```

Note `--deltaq-mode=0` and `--enable-tpl-model=0`: **CTC random-access disables
both delta-Q and the TPL model.** Any work on those subsystems (§17, §18) is
therefore invisible to the standard CTC numbers — which is either a warning or
an opportunity depending on the goal.

`tools/convexhull_framework/src/config.yaml` and `VideoEncoder.py` are the
authoritative source; `use_perf_util: true` there enables `perf stat`
instruction counting, which is far more repeatable than wall clock.

---

## 15. Subsystem call graphs

Not a subsystem — a view. See §25.

---

# PART II — The subsystems missing from the taxonomy

## 16. Rate control, adaptive quantization and delta-Q

**Files:** `av2/encoder/ratectrl.c` (2,127), `pass2_strategy.c` (3,249),
`firstpass.c`, `aq_variance.c`, `aq_complexity.c`, `aq_cyclicrefresh.c`,
`segmentation.c`, `av2/common/seg_common.c`.

**Why it must be a subsystem:** nothing else decides *how many bits each frame
and each block gets*, and the answer feeds `rdmult`, which feeds every RD
decision in every other subsystem.

### 16.1 Rate control modes

```c
AVM_VBR, AVM_CBR, AVM_CQ, AVM_Q      // avm_encoder.h:176
```

CTC uses `AVM_Q` (constant quality) with fixed QP offsets, which bypasses most of
`ratectrl.c`. Production use does not.

`ratectrl.c` maintains eight qindex→minq lookup tables *per bit depth*
(`kf_low_motion_minq_8/10/12`, `arfgf_*`, `inter_minq_*`, `rtc_minq_*`), built at
init by curve-fitting (`get_minq_index(maxq, x3, x2, x1, ...)`).

### 16.2 Two-pass structure

```
av2_first_pass()                    firstpass.c:1054   per-frame stats
av2_get_second_pass_params()        pass2_strategy.c:2801
av2_calc_arf_boost()                pass2_strategy.c:588
av2_gop_bit_allocation()            pass2_strategy.c:1969
av2_twopass_postencode_update()     pass2_strategy.c:3135
av2_init_single_pass_lap()          pass2_strategy.c:3101  (lookahead-only)
```

`FIRSTPASS_STATS` per frame drives GOP-length decisions, ARF placement and
bit allocation. A single-pass "LAP" (lookahead processing) mode approximates the
same statistics from the lookahead ring.

### 16.3 Adaptive quantization

Three independent AQ modes plus delta-Q:

```c
DELTA_Q_OBJECTIVE = 1,    // modulation to improve objective quality
DELTA_Q_PERCEPTUAL = 2,
```

- `aq_variance.c` — `av2_log_block_var()`, `av2_block_wavelet_energy_level()`
  (uses `dwt.c`), `av2_compute_q_from_energy_level_deltaq_mode()`.
- `aq_complexity.c` — segment selection from complexity.
- `aq_cyclicrefresh.c` — realtime cyclic intra refresh.

### 16.4 Segmentation

```c
SEG_LVL_ALT_Q, SEG_LVL_SKIP, SEG_LVL_GLOBALMV, SEG_LVL_MAX
```

Only three segment features in AV2 (AV1 had eight). Segmentation is the delivery
mechanism for AQ; `MAX_SEGMENTS` segments each carry a qindex delta.

### 16.5 The recode loop

`encode_with_recode_loop()` (`encoder.c:3552`) re-encodes a frame at a different
q when the rate misses target:

```c
const int allow_recode = (cpi->sf.hl_sf.recode_loop != DISALLOW_RECODE);
int q = 0, q_low = 0, q_high = 0;
int loop_count = 0;
do { ... } while (loop);
```

This is a *multiplier* on encode time invisible in per-block profiling. Under
CTC's `--end-usage=q` it is normally disabled — another reason CTC timings do not
directly predict VBR encode times.

---

## 17. Temporal dependency model (TPL)

**Files:** `av2/encoder/tpl_model.c` (1,428), `tpl_model.h`.

```
av2_tpl_setup_stats()        tpl_model.c:1229
av2_mc_flow_dispenser_row()  tpl_model.c:906   forward propagation
mc_flow_synthesizer()        tpl_model.c:964   backward accumulation
av2_tpl_rdmult_setup()       tpl_model.c:1312  frame-level rdmult
av2_tpl_rdmult_setup_sb()    tpl_model.c:1366  superblock-level rdmult
av2_mc_flow_dispenser_mt()   ethread.c:1376    multithreaded variant
```

TPL performs a cheap lookahead encode over the GOP, propagates each block's
"how much do future frames depend on me" measure backwards, and converts it to a
**per-superblock rdmult modulation**. It is the mechanism by which a block that
will be referenced many times gets more bits.

It deserves separate status because it is a *distinct encode pass* with its own
threading, its own motion search, and its own speed-feature group (`tpl_sf`).

**Note again that CTC RA sets `--enable-tpl-model=0`.**

---

## 18. Lookahead, GOP structure and frame typing

**Files:** `av2/encoder/gop_structure.c`, `subgop.c`, `encode_strategy.c`
(the frame-level state machine), `lookahead.c`.

```
av2_gop_setup_structure()        gop_structure.c:412
choose_frame_source()            encode_strategy.c:443
av2_get_refresh_frame_flags()    encode_strategy.c:755
av2_get_ref_frames_enc()         encode_strategy.c:58
av2_configure_buffer_updates()   encode_strategy.c:96
denoise_and_encode()             encode_strategy.c:922
```

This subsystem decides, for each output frame: is it a key frame, an ARF, a
leaf? What is its pyramid layer? Which of the 16 DPB slots does it refresh?
Which 7 references does it use?

**`subgop.c`** implements a *scriptable* GOP structure: `SubGOPStepCfg` lets a
sub-GOP pattern be specified externally (`use_subgop_cfg`,
`get_refresh_frame_flags_subgop_cfg`). This is a research affordance with no AV1
equivalent — you can specify an exact reference structure and measure it.

`get_free_ref_map_index_multi_layer()` and `get_refresh_idx()` implement the DPB
replacement policy, which with 16 slots is a genuinely non-trivial optimization
problem.

---

## 19. Screen content coding

**Files:** `av2/encoder/palette.c`, `hash_motion.c`, `hash.c`,
`av2/encoder/mcomp.c` (IntraBC portions), `encoder.c:av2_set_screen_content_options`.

Three tools that share a detection path:

**Palette.** `av2_rd_pick_palette_intra_sby()`, k-means centroid selection
(`av2_remove_duplicates`), a color cache across blocks (`av2_index_color_cache`),
and separate luma/chroma palettes. `x->palette_pixels` and `PALETTE_BUFFER`
carry the working state.

**IntraBC (block copy).**
```
av2_intrabc_hash_search()          mcomp.c:2667
av2_pick_ref_bv()                  mcomp.c:2616   block-vector prediction
av2_find_best_sub_pixel_intraBC_dv() mcomp.c:4352
rd_pick_intrabc_mode_sb()          rdopt.c:6256
```
Backed by a hash table over 2x2-derived block hashes (`hash_motion.c`,
`av2_generate_block_hash_value`, CRC-based via `hash.c`). AV2 adds a **BVP DRL**
— a predicted-block-vector list with its own `max_bvp_drl_bits`.

**FSC (forward skip coding).** `mbmi->fsc_mode`, paired with `IDTX`, with its own
optimization path `av2_optimize_fsc` and dedicated entropy contexts
(`FSC_TX_SIZE_CONTEXTS`, `IDTX_*_CONTEXTS`).

`av2_set_screen_content_options()` (`encoder.c:2345`) does the content detection
that enables these.

---

## 20. TIP, BRU and frame-level prediction structures

Two AV2 mechanisms that live above the block level and have no AV1 analogue.

### 20.1 TIP — Temporally Interpolated Prediction

**Files:** `av2/common/tip.c` (960), `tip.h`.

```c
TIP_FRAME_DISABLED = 0,
TIP_FRAME_AS_REF,      // interpolated frame used as a reference
TIP_FRAME_AS_OUTPUT,   // interpolated frame emitted directly
TIP_FRAME_MODES
```

A frame is **synthesized by motion-field interpolation** between existing
references (`av2_setup_tip_motion_field`, `av2_setup_tip_frame`,
`av2_tip_setup_tip_frame_row`) and can then either serve as a reference
(`TIP_FRAME` is a pseudo reference index, `TIP_FRAME_INDEX = INTER_REFS_PER_FRAME + 1`)
or be output with *no residual coded at all*. `OBU_LEADING_TIP` / `OBU_REGULAR_TIP`
carry it. `enable_tip_refinemv` optionally refines the interpolated MVs.

This is a frame-rate-conversion tool inside the codec, and it changes the shape
of the encode loop — a TIP-as-output frame skips most of the block pipeline.

### 20.2 BRU — active region / block reference update

**Files:** `av2/common/bru.c`, `bru.h`.

```c
#define BRU_OFF_RATIO 50
#define MAX_ACTIVE_REGION 8
#define BRU_ENC_LOOKAHEAD_DIST_MINUS_1 1
#define BRU_ENC_REF_DELAY 1
```

`BruInfo` maintains an `active_mode_map` over superblock-sized units and up to 8
`active_region` descriptors. `cm->bru.frame_inactive_flag` suppresses *all*
in-loop filtering for a frame. The mechanism supports coding only changed
regions of a frame — a screen-sharing / static-content optimization operating at
frame granularity, with encoder-side lookahead (`bru_lookahead_update`).

---

## 21. ML-assisted encoder decisions

**Files:** `av2/encoder/ml.c`, `cnn.c` (1,072), `partition_ml.c`,
`partition_mlp.c`, `erp_ml.c`, `intra_mode_mlp.c`, plus ~20 weight headers and
6 TFLite model headers.

**Why this must be its own subsystem:** it is a distinct inference runtime with
its own numerics, its own build dependency, and its own failure modes, and it is
consulted from at least four other subsystems.

### 21.1 Three inference paths coexist

1. **Hand-rolled float MLP** — `av2_nn_predict()` (`ml.c:31`), fed by weight
   tables compiled in (`partition_model_weights.h`, `intra_mode_mlp_weights.h`
   6,063 lines, `tx_prune_model_weights.h`, `mode_prune_model_weights.h`,
   `misc_model_weights.h`, `erp_models.h` 6,104 lines).
2. **CNN** — `av2_cnn_predict()` (`cnn.c:832`) with convolve/deconvolve/
   batchnorm/activate primitives, fed by `partition_cnn_weights.h`.
3. **TFLite** — genuinely embedded TensorFlow Lite. `av2_part_prune_tflite_close()`,
   models in `sms_part_none_prune_tflite_model.h` (7,424 lines),
   `sms_part_none_prune_rect_tflite_model.h`, `sms_part_split_prune_tflite_model.h`,
   `part_split_prune_tflite.h`, `intra_dip_mode_prune_tflite.h`,
   `simple_intrapred_tflite_model_{16,32,64,128}.h`. A `partition_model` handle
   lives per-`ThreadData`.

### 21.2 Where they are consulted

| Decision | Mechanism |
|---|---|
| Partition NONE / rect pruning | TFLite (`sms_part_none_prune*`) |
| Partition split pruning | TFLite (`part_split_prune`), `CONFIG_ML_PART_SPLIT` |
| Extended recursive partition | `erp_ml.c` + `erp_models.h` |
| Rectangular pruning | `prune_rect_with_mlp()` |
| Intra mode masking | `av2_intra_mlp_compute_mode_mask()` |
| DIP mode pruning | TFLite, `CONFIG_DIP_EXT_PRUNING` |
| Transform type pruning | `tx_prune_model_weights.h` |
| Simple intra prediction | TFLite `simple_intrapred_*` |
| GOP flat-model decision | `use_flat_gop_model_params.h` |

### 21.3 Consequences to keep in mind

- **Inference cost is real encode time** and is not attributable to any coding
  tool. It shows up in profiles as `av2_nn_predict` / TFLite interpreter frames.
- **Models are trained against a particular encoder configuration.** Changing an
  upstream speed feature can silently move a model off its training distribution
  without any compile-time signal.
- **Float arithmetic in the encoder search** is acceptable (decisions are
  encoder-only) but must never leak into normative reconstruction.

---

## 22. Multithreading, tiles and parallelism

**Files:** `av2/encoder/ethread.c` (1,599), `av2/common/thread_common.c`,
`av2/common/tile_common.c`, `avm_util/avm_thread.c`.

Five *independent* parallelism mechanisms:

**1. Tile-level.** `av2_encode_tiles_mt()` — one worker per tile.
`MAX_TILE_ROWS/COLS = 64`.

**2. Row-level within tiles (row-mt).** `av2_encode_tiles_row_mt()` with
`AV2EncRowMultiThreadSync`. `av2_row_mt_sync_read/write()` implement the
wavefront dependency (a superblock needs its above-right neighbour done).
Note the `_dummy` variants used when sync is unnecessary — a common source of
confusion when reading.

**3. First-pass row-mt.** `av2_fp_encode_tiles_row_mt()`, separate worker hook
(`fp_enc_row_mt_worker_hook`).

**4. TPL row-mt.** `av2_mc_flow_dispenser_mt()`, `AV2TplRowMultiThreadSync`.

**5. Global motion.** `av2_global_motion_estimation_mt()`, `AV2GlobalMotionSync`
— one reference frame per worker.

Plus post-filter threading: `av2_loop_filter_frame_mt()`,
`av2_loop_restoration_filter_frame_mt()`.

`MultiThreadInfo` (`encoder.h:1879 (struct end)`) aggregates all of it.
`av2_compute_num_enc_workers()` sizes the pool.

**Two facts that matter for measurement.** CTC runs `--threads=1
--frame-parallel=0`, so none of this is exercised in the standard numbers.
And `av2_accumulate_frame_counts()` exists because each worker keeps private
entropy counts that must be merged — meaning entropy statistics are
thread-count-dependent unless care is taken.

---

## 23. Resize, film grain, denoise and metadata

Grouped because each is small but none belongs in the block pipeline.

**Spatial resampling** (`av2/common/resize.c`, `av2/encoder/scale.c`,
`tools/lanczos/`, `CONFIG_LANCZOS_RESAMPLE`). `av2_calculate_scaled_size()`,
`av2_resize_and_extend_frame_nonnormative()`. Superres/scaled-reference
prediction requires the convolve path to handle scale factors
(`av2/common/scale.c`).

**Film grain** (`avm_dsp/grain_synthesis.c`, `av2/encoder/bitstream_fgm.c`,
`avm_dsp/noise_model.c`). AV2 promotes film grain to its own OBU
(`OBU_FILM_GRAIN_MODEL`) with a model *list* and a selection step
(`film_grain_model_decision`). `av2_add_film_grain()` synthesizes at output.

**Denoise** (`CONFIG_DENOISE`, `apply_denoise_2d()` at `encoder.c:5281`,
`av2_noise_estimate.c`) — a pre-encode 2D denoise, distinct from temporal
filtering.

**Banding** (`av2/encoder/banding_detection.c`, `av2/common/banding_metadata.c`,
`avm_band_search()` at `encoder.c:3010`). Detects banding and emits
`OBU_METADATA_TYPE_BANDING_HINTS` — a hint to the *display* pipeline, novel in AV2.

**Metadata OBUs.** `bitstream_ci.c` (content interpretation: color, SAR),
`bitstream_lcr.c` (layer configuration record), `bitstream_ops.c` (operating
point set), `bitstream_atlas.c` (atlas segments), `bitstream_buf.c` (buffer
removal timing), `bitstream_qm.c`, `bitstream_fgm.c`.

---

## 24. Temporal filtering

**Files:** `av2/encoder/temporal_filter.c` (1,334).

```
av2_temporal_filter()                temporal_filter.c:1229
├─ tf_setup_filtering_buffer()       select frames around the target
├─ tf_motion_search()                per-block ME to each neighbour
│   └─ subblock_motion_search()
├─ tf_determine_block_partition()    16x16 vs sub-blocks
├─ tf_build_predictor()
├─ av2_highbd_apply_temporal_filter() weighted average, error-adaptive weights
└─ tf_normalize_filtered_frame()
```

A motion-compensated temporal denoise applied to ARF source frames *before*
encoding. It is a separate motion search over a separate frame set and it is
substantial encode time on ARF frames. Worth its own subsystem because it is
neither prediction nor filtering in the in-loop sense — it modifies the *source*.

---

# PART III — Cross-cutting views

## 25. Call graphs

### 25.1 Top level

```
avm_codec_encode()                              av2_cx_iface.c
└─ av2_receive_raw_frame()                      encoder.c:5316
│   └─ apply_denoise_2d() → av2_lookahead_push()
└─ av2_get_compressed_data()                    encoder.c:5441
    └─ av2_encode()                             encoder.c:5018
        └─ denoise_and_encode()                 encode_strategy.c:922
            ├─ choose_frame_source()            (lookahead)
            ├─ av2_temporal_filter()            §24, ARF frames
            ├─ av2_gop_setup_structure()        §18
            ├─ av2_get_second_pass_params()     §16
            ├─ av2_tpl_setup_stats()            §17
            ├─ av2_get_refresh_frame_flags()    §18 (DPB policy)
            └─ encode_frame_to_data_rate()      encoder.c:4536
                ├─ av2_set_quantizer()          §7
                ├─ av2_set_speed_features_*()   §12
                ├─ encode_with_recode_loop_and_filter()   encoder.c:4176
                │   ├─ encode_with_recode_loop()          encoder.c:3552
                │   │   └─ av2_encode_frame()             encodeframe.c
                │   └─ loopfilter_frame()                 §9
                └─ av2_pack_bitstream()          §10
```

### 25.2 Frame → superblock

```
av2_encode_frame()                              encodeframe.c:2287
├─ av2_set_lossless() / av2_set_frame_tcq_mode() / av2_enc_setup_ph_frame()
├─ av2_global_motion_estimation_mt()             §22
├─ av2_setup_tip_frame()                         §20 (if TIP)
├─ av2_alloc_tile_data() / av2_init_tile_data()
└─ av2_encode_tiles_mt() | av2_encode_tiles_row_mt() | single-threaded
    └─ av2_encode_tile()                        encodeframe.c:1611
        └─ av2_encode_sb_row()                  encodeframe.c:1555
            └─ encode_sb_row()  [static]        encodeframe.c
                └─ for each superblock:
                    ├─ av2_set_sb_info() / av2_reset_refmv_bank()
                    ├─ av2_set_cost_upd_freq()
                    └─ encode_rd_sb()
                        ├─ perform_two_pass_partition_search()   §3.5
                        │   ├─ perform_one_partition_pass(SB_DRY_PASS)
                        │   └─ perform_one_partition_pass(SB_WET_PASS)
                        └─ or single pass
```

### 25.3 Superblock → coefficients (the hot path)

```
av2_rd_pick_partition()                          partition_search.c:5531
└─ pick_sb_modes()                               partition_search.c
    ├─ av2_rd_pick_intra_mode_sb()               rdopt.c:6821
    └─ av2_rd_pick_inter_mode_sb()               rdopt.c:9138
        └─ handle_inter_mode()                   rdopt.c:5735
            ├─ av2_motion_search / mcomp.c       §5
            ├─ av2_interpolation_filter_search() interp_search.c:386
            ├─ av2_compound_type_rd()            compound_type.c:1136
            └─ av2_txfm_search()                 tx_search.c:4478
                └─ av2_pick_{recursive,uniform}_tx_size_type_yrd()
                    └─ choose_tx_size_type_from_rd()   ← dry-pass aware
                        └─ search_tx_type()      tx_search.c:2314
                            └─ for tx_type × set_idx × stx:
                                ├─ av2_xform()
                                ├─ av2_quant()
                                ├─ av2_optimize_b()
                                │   └─ av2_trellis_quant()  ← ~47% of instructions
                                └─ cost_coeffs() / get_tx_blk_distortion()
```

### 25.4 Where the time goes

From a callgrind profile of this encoder at `--cpu-used=4`:

- **~47%** trellis quantization (`av2_trellis_quant` and callees)
- the remainder distributed across motion search, transforms, prediction, and
  the ML inference paths

The single most useful structural observation is that the 47% is reached
through a loop that keeps one result out of many, and that the two speed
features designed to bound it are inoperative for luma (§7.4).

---

## 26. A study plan

If the goal is genuine working knowledge rather than a map, this ordering
front-loads the subsystems that constrain everything else.

**Tier 1 — the spine.** §11 (RDO/lambda), §12 (speed features), §3 (partition).
Without these, every other subsystem reads as unmotivated detail. Concretely:
trace one superblock from `av2_rd_pick_partition` to a coefficient, with a
debugger, once.

**Tier 2 — the cost centres.** §7 (quantization/TCQ), §6 (transform), §5 (ME).
These are where encode time actually is.

**Tier 3 — the frame level.** §16 (rate control), §18 (GOP), §17 (TPL), §2
(buffering). These explain why the encoder makes decisions that look wrong
locally.

**Tier 4 — the AV2-specific tools.** §20 (TIP/BRU), §9.2–9.4 (PC-Wiener, CCSO,
GDF), §19 (screen content), §4.2 (DIP/MRL/FSC). Each is self-contained; read
them when you need them.

**Tier 5 — infrastructure.** §21 (ML), §22 (threading), §13 (SIMD), §10 (entropy
+ ParaKit).

### 26.1 Instrumentation worth turning on

```
CONFIG_COLLECT_COMPONENT_TIMING   per-stage frame timing (start_timing/end_timing)
CONFIG_COLLECT_PARTITION_STATS    partition search statistics
CONFIG_COLLECT_RD_STATS           per-transform-unit RD dumps
CONFIG_ENTROPY_STATS              symbol counts for context retraining
CONFIG_SPEED_STATS                
CONFIG_THROUGHPUT_ANALYSIS        
CONFIG_INSPECTION                 per-block decision dump (tools/avm_analyzer)
CONFIG_MISMATCH_DEBUG             encoder/decoder recon divergence
CONFIG_BITSTREAM_DEBUG            
```

`tools/avm_analyzer/` and `tools/inspect-cli.js` render inspection output;
`tools/dump_obu.cc` and `tools/obu_parser.cc` give a bitstream-level view.

### 26.2 Open questions this study surfaced

These are stated as questions, not findings — each would need measurement.

1. **Why does `intra_mode_search.c:384` hard-code `skip_trellis = 0`** when
   `tx_search.c` makes the equivalent decision dry-pass aware? Is intra mode
   search intentionally exempt, or is this an oversight of the same kind that
   was already fixed once in `tx_search.c`?

2. **What is the SIMD state of the TCQ decision loop?** The RTCD hooks exist
   (`av2_decide_states_c`, `av2_pre_quant_c`, `av2_update_nbr_diagonal_c`,
   `av2_find_best_path_c`). Given the 47% profile share, whether these have AVX2
   implementations — and whether they are reached — is the highest-value audit in
   the codebase.

3. **How much encode time is ML inference?** Six TFLite models plus several MLPs
   and a CNN are consulted per partition decision. This cost is invisible in any
   tool-attributed profile.

4. **Is the NEON gap costing anything that matters?** 15–19 NEON specializations
   against 271–90 AVX2, and one file in `av2/encoder/arm/`.

5. **What would retrained entropy contexts be worth?** ParaKit exists and is
   documented; the training pipeline is reproducible. This is a gain source that
   requires no bitstream change.

6. **Does the 16-slot DPB replacement policy leave gain on the table?**
   `get_refresh_idx()` implements a heuristic over a search space that is now
   twice AV1's size, and `subgop.c` makes alternatives directly testable.

---

## Appendix A — Symbol index

| Symbol | File:line | Subsystem |
|---|---|---|
| `av2_encode` | `encoder.c:5018` | §1 |
| `av2_get_compressed_data` | `encoder.c:5441` | §1 |
| `av2_change_config` | `encoder.c:1162` | §1 |
| `av2_lookahead_push` | `lookahead.c:90` | §2, §18 |
| `av2_alloc_context_buffers` | `alloccommon.c` | §2 |
| `av2_rd_pick_partition` | `partition_search.c:5531` | §3 |
| `perform_two_pass_partition_search` | `encodeframe.c:664` | §3.5 |
| `av2_rd_pick_intra_mode_sb` | `rdopt.c:6821` | §4 |
| `av2_rd_pick_intra_sby_mode` | `intra_mode_search.c:1695` | §4 |
| `av2_intra_mlp_compute_mode_mask` | `intra_mode_search.c:1393` | §4, §21 |
| `handle_inter_mode` | `rdopt.c:5735` | §5, §11 |
| `av2_full_pixel_search` | `mcomp.c:2388` | §5 |
| `av2_compound_type_rd` | `compound_type.c:1136` | §5 |
| `av2_interpolation_filter_search` | `interp_search.c:386` | §5 |
| `av2_pick_and_set_high_precision_mv` | `mv_prec.c:497` | §5 |
| `av2_txfm_search` | `tx_search.c:4478` | §6 |
| `search_tx_type` | `tx_search.c:2314` | §6, §7 |
| `av2_trellis_quant` | `trellis_quant.c:1247` | §7 |
| `tcq_enable` | `quant_common.h:74` | §7 |
| `av2_set_quantizer` | `av2_quantize.c:635` | §7 |
| `av2_optimize_b` | `encodemb.c:328` | §8 |
| `av2_pick_filter_restoration` | `pickrst.c:3776` | §9 |
| `av2_cdef_search` | `pickcdef.c:417` | §9 |
| `av2_ccso_search` | `pickccso.c:1821` | §9 |
| `gdf_optimize_frame` | `encoder.c:2994` | §9 |
| `av2_compute_rd_mult` | `rd.c:683` | §11 |
| `av2_get_adaptive_rdmult` | `rd.c:720` | §11 |
| `av2_set_speed_features_framesize_independent` | `speed_features.c:1238` | §12 |
| `av2_first_pass` | `firstpass.c:1054` | §16 |
| `av2_get_second_pass_params` | `pass2_strategy.c:2801` | §16 |
| `encode_with_recode_loop` | `encoder.c:3552` | §16 |
| `av2_tpl_setup_stats` | `tpl_model.c:1229` | §17 |
| `av2_tpl_rdmult_setup_sb` | `tpl_model.c:1366` | §17 |
| `av2_gop_setup_structure` | `gop_structure.c:412` | §18 |
| `av2_get_refresh_frame_flags` | `encode_strategy.c:755` | §18 |
| `av2_intrabc_hash_search` | `mcomp.c:2667` | §19 |
| `av2_rd_pick_palette_intra_sby` | `palette.c:369` | §19 |
| `av2_setup_tip_frame` | `tip.c:949` | §20 |
| `av2_nn_predict` | `ml.c:31` | §21 |
| `av2_cnn_predict` | `cnn.c:832` | §21 |
| `av2_encode_tiles_row_mt` | `ethread.c:1059` | §22 |
| `av2_temporal_filter` | `temporal_filter.c:1229` | §24 |

## Appendix B — Suggested directory layout

Mapping your 15 folders plus the 9 additions:

```
01_io_and_formatting              §1
02_frame_buffering_and_memory     §2
03_partition_search               §3
04_intra_prediction               §4
05_inter_prediction_and_me        §5
06_transform_and_secondary_tx     §6
07_quantization_and_tcq           §7
08_reconstruction_and_residual    §8
09_in_loop_filtering              §9
10_entropy_coding_and_obu         §10
11_rdo_and_lambda_mechanics       §11
12_speed_features_framework       §12
13_simd_and_portability           §13   (renamed)
14_command_line_and_presets       §14
15_rate_control_and_aq            §16   NEW
16_tpl_model                      §17   NEW
17_lookahead_gop_and_frame_typing §18   NEW
18_screen_content_coding          §19   NEW
19_tip_and_bru                    §20   NEW
20_ml_assisted_decisions          §21   NEW
21_threading_and_tiles            §22   NEW
22_resize_grain_and_metadata      §23   NEW
23_temporal_filtering             §24   NEW
_call_graphs/                     §25   (cross-cutting view, not a subsystem)
_study_plan.md                    §26
```
