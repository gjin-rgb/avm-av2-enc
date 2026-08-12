#!/bin/bash
# Tier 0: prove that a "reuse" patch changes no encode decision.
#
# Patches that cache, memoize, restore buffers or re-arrange allocation do not
# approximate anything -- they change how work is stored and reused, not which
# candidate wins. Every such patch MUST produce a byte-identical bitstream:
#
#   BIT-EXACT -> quality risk is zero by proof. Judge it on speed alone and
#                never spend a CTC round on it.
#   DIVERGES  -> the refactor changed a decision. That is a bug in the patch
#                (a wrong cache key, a stale buffer), not a quality tradeoff.
#
# Costs minutes. Replaces a full CTC round for this class of patch.
#
# Usage:
#   experiments/bin/bitexact.sh [patch ...]
# Defaults to the three reuse-class patches in patches/.
#
# Env overrides:
#   CLIP     input y4m           WIDTH/HEIGHT/FPS  clip geometry
#   QPS      QP list to test     PRESET            --cpu-used value
#   FRAMES   frame count         OUTDIR            scratch dir

set -u
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO" || exit 1

CLIP="${CLIP:?set CLIP to a y4m input}"
WIDTH="${WIDTH:-416}"; HEIGHT="${HEIGHT:-240}"; FPS="${FPS:-30}"
PRESET="${PRESET:-3}"; FRAMES="${FRAMES:-8}"
# Two QPs from the CTC RA ladder. Different QPs give different coefficient
# density and search depth, so a cache-key bug that only manifests at one
# operating point is still caught.
QPS="${QPS:-110 185}"
OUTDIR="${OUTDIR:-$REPO/experiments/.bitexact}"
ENC="$REPO/build/avmenc"
LOG="$OUTDIR/run.log"

PATCHES=("$@")
if [ ${#PATCHES[@]} -eq 0 ]; then
  PATCHES=(patches/0002-ist-sparse-coeff-restore.patch
           patches/0003-pmc-arena-allocation.patch
           patches/0005-fullpel-search-memo.patch)
fi

mkdir -p "$OUTDIR"; : > "$LOG"

# Mirrors tools/convexhull_framework/src/VideoEncoder.py. Single-threaded and
# fixed QP offsets so the output is deterministic by construction -- otherwise
# a threading nondeterminism would masquerade as a patch divergence.
ARGS="--verbose --codec=av2 -v --psnr --obu --frame-parallel=0 --threads=1 \
--cpu-used=$PRESET --limit=$FRAMES --skip=0 --passes=1 --end-usage=q --i420 \
--use-fixed-qp-offsets=1 --deltaq-mode=0 --enable-tpl-model=0 \
--fps=$FPS/1 -w $WIDTH -h $HEIGHT --lag-in-frames=19 --auto-alt-ref=1 \
--kf-max-dist=65"

# Revert the source tree, and then PROVE it reverted.
#
# The first version of this function was `git checkout -- av2/ aom/ 2>/dev/null`.
# This repo has no aom/ directory (AVM renamed it av2/), so git rejected the
# whole command on the bad pathspec, reverted nothing, and returned 1 -- while
# the 2>/dev/null hid the error. Patches then accumulated across iterations:
# i03 got measured with i02 still applied, i05 with both, and on the following
# run the "baseline" itself was built with all three patches in the tree. The
# script would have printed confident, precisely formatted, entirely wrong
# signatures.
#
# The lesson generalizes beyond this script: an unverified cleanup step is not a
# cleanup step. Assert the state you depend on instead of trusting that the
# command meant to establish it did.
#
# Scoped to av2/ so that a run cannot revert edits to the tooling under
# experiments/ while it executes.
clean_tree() {
  git checkout -- av2/ >>"$LOG" 2>&1
  if ! git diff --quiet -- av2/ || ! git diff --cached --quiet -- av2/; then
    echo "FATAL: av2/ still dirty after revert; refusing to continue." >&2
    git diff --stat -- av2/ >&2
    exit 2
  fi
}

build_enc()  { make -C "$REPO/build" -j"$(nproc)" avmenc >>"$LOG" 2>&1; }

encode_sig() {
  local tag="$1" qp sig=""
  for qp in $QPS; do
    rm -f "$OUTDIR/${tag}_qp${qp}.obu"
    "$ENC" $ARGS --qp="$qp" -o "$OUTDIR/${tag}_qp${qp}.obu" "$CLIP" >>"$LOG" 2>&1
    [ -s "$OUTDIR/${tag}_qp${qp}.obu" ] || { echo "ENCODE_FAIL"; return 1; }
    sig="${sig}$(md5sum "$OUTDIR/${tag}_qp${qp}.obu" | cut -d' ' -f1)"
  done
  printf '%s' "$sig" | md5sum | cut -d' ' -f1
}

echo "clip=$CLIP ${WIDTH}x${HEIGHT} frames=$FRAMES preset=$PRESET qps='$QPS'"
echo "establishing baseline on clean tree..."
clean_tree
build_enc || { echo "BASELINE BUILD FAILED, see $LOG"; exit 1; }
BASE_SIG=$(encode_sig base) || { echo "BASELINE ENCODE FAILED, see $LOG"; exit 1; }
echo

printf '%-42s %-11s %s\n' "patch" "verdict" "signature"
printf '%-42s %-11s %s\n' "(baseline)" "-" "$BASE_SIG"

rc=0
for pfile in "${PATCHES[@]}"; do
  name="$(basename "$pfile" .patch)"
  clean_tree
  if ! git apply --check "$pfile" 2>>"$LOG"; then
    printf '%-42s %-11s %s\n' "$name" "APPLY_FAIL" "-"; rc=1; continue
  fi
  git apply "$pfile"
  if ! build_enc; then
    printf '%-42s %-11s %s\n' "$name" "BUILD_FAIL" "-"; rc=1; clean_tree; continue
  fi
  sig=$(encode_sig "$name")
  if [ "$sig" = "$BASE_SIG" ]; then
    printf '%-42s %-11s %s\n' "$name" "BIT-EXACT" "$sig"
  else
    printf '%-42s %-11s %s\n' "$name" "DIVERGES" "$sig"; rc=1
  fi
  clean_tree
done

echo
echo "BIT-EXACT -> zero quality risk by proof; no CTC round needed."
echo "DIVERGES  -> the refactor changed an encode decision; that is a bug."
exit $rc
