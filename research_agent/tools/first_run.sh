#!/usr/bin/env bash
# The sequence to run on a fresh machine, in order, stopping at the first
# problem. Every step is idempotent.
#
#   tools/first_run.sh local ~/av2ra-workspace
#
# It deliberately does not run the loop. Read the demo output and `doctor`
# first: a system whose environment you have not checked will spend hours
# producing numbers you cannot use.
set -euo pipefail
site="${1:-local}"
workspace="${2:-$HOME/av2ra-workspace}"
here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
run() { "$here/tools/av2ra" --site="$site" --workspace="$workspace" "$@"; }

echo "### 1/6  the plumbing works, with nothing installed"
"$here/tools/av2ra" --site=sim --workspace="$(mktemp -d)" demo | tail -20

echo; echo "### 2/6  the test suite"
( cd "$here" && PYTHONPATH="$here" "${PYTHON:-python3}" tests/run_all.py 2>&1 | tail -3 )

echo; echo "### 3/6  workspace"
run init

echo; echo "### 4/6  encoder checkout"
run bootstrap

echo; echo "### 5/6  health  (exit 2 means something needs attention)"
run doctor || true

echo; echo "### 6/6  knowledge base"
run ingest

cat <<'NEXT'

Next, and in this order:

  1. Build avmenc at the anchor and time ONE encode of ONE screening clip at
     your target preset. Write the number down. Everything downstream depends
     on it, and the draft protocol's estimate was wrong by 2-3 orders of
     magnitude.

  2. av2ra profile --import <callgrind or perf output> --preset=<target>
     Four ideation lenses refuse to run without a profile, and the Amdahl gate
     cannot reject an impossible claim.

  3. av2ra run --ticks=1, repeatedly, reading each report, until one experiment
     has traversed T0-T4 and you have checked its diff by hand.

  4. Only then: av2ra run --ticks=50
NEXT
