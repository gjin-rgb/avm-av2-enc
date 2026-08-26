# Limits, and what is not done

Stated plainly, because a system that implies completeness is harder to use
safely than one that names its edges.

---

## Not exercised against real infrastructure

**The Cloud EDA path has never talked to a cluster.** Its argument construction,
anchor reuse and result parsing are built from the invocation in the draft guide
and the result tables in the prior corpus's reports. Two things need confirming
on first use, during a supervised round:

1. The exact stdout of `kick_off_av2ctc_eda.sh` — the invocation UUID is
   extracted by regex.
2. The exact output of `compare_eda_runs.py --csv=1` — two shapes are handled; a
   third needs a column-alias entry.

**CNS storage has never been exercised.** `fileutil` is not available here. The
lease implementation is advisory and says so: `fileutil` exposes no
compare-and-swap, so the window is a round trip.

**No full CTC-scale local run.** The encoder built for this work needs roughly
14 s/frame at 176×144 on the available hardware, which puts a real 1080p
screening pass in the hours. The encode path is validated against the real
`avmenc` on small clips — parsing, decoding, determinism, timing — and the
full-scale behaviour is inferred from that.

---

## Not built

**Screen-to-CTC calibration.** The single most valuable thing to build after
deployment. Every experiment with both a T3 and a T5 result is a data point on
whether local screening predicts anything; after ten, the correlation is
measurable. Without it, the screening set's usefulness is assumed rather than
known. Phase 9.1.

**Shadow-mode decision regret.** Specified in the prior corpus, called the
highest-value remaining infrastructure, never built there and not built here.
It exists as an ideation lens that proposes instrumentation, which is weaker
than a tier that measures regret directly.

**Decoded-pixel conformance.** The gate proves a bitstream decodes. It does not
prove it decodes to the right pixels; that needs a reference decoder build.

**Combination rounds.** The corpus's only composition rule — speedups compound
multiplicatively, BD-rate adds — was validated once. Combining two passing
patches is not automated, though the promotion frontier and the `reject` cap
mode are the pieces it would need.

**Presets 5+, and configurations other than RA.** Everything here targets RA at
speeds 0–4 on A1 and A2, matching prior practice. LD, AI, AS and classes A3–A5,
B1, B2, G1, G2 are representable in the config and untested.

---

## Structural limits

**Local screening cannot be made predictive by this system.** It makes the
unreliability explicit and refuses to let a local screen decide quality. If the
correlation turns out to be zero, every quality question costs a cluster round
and throughput drops by an order of magnitude. The system still works; it works
slower.

**Objective metrics only.** PSNR, SSIM, VMAF. A change that helps the metric and
hurts appearance passes every gate here.

**The test set defines what is visible.** A feature behind a resolution guard no
class exercises is invisible, and the correct conclusion is "CTC cannot measure
this", not "this does nothing". Nothing here can distinguish those
automatically.

**Cluster-side numbers are trusted.** Once a job is submitted, what comes back
is believed. The cluster's repeatability has never been measured on this
project; one round submitting the same commit twice would establish it, and the
planner will not spend that round unasked.

**A wrong lesson propagates.** The ledger shapes future ideation. Retractions
are representable and marked, but nothing detects a wrong lesson automatically.
Reading `DECISIONS.md` is the countermeasure.

**Guardrails, not a sandbox.** The integrity layer defends against a misaligned
optimiser and honest self-deception. An agent deliberately trying to defeat it,
with write access to the workspace, could.

---

## Deliberate non-goals

**Automatic upstream submission.** The system recommends; a person reviews the
diff and submits. Code review is the point at which a human should still be in
the loop.

**Automatic provenance scrubbing before publication.** A scrubber that silently
removes CNS paths and job ids produces reports whose numbers cannot be traced,
which is the failure the registry exists to prevent. Review before a public
push instead.

**Bitstream syntax changes.** A patch that needs a decoder change is proposing a
syntax change, which is a standards process, not a speed patch. The decoder is
outside the writable set.

---

## Honest expectations

The prior corpus is the best available prior on what this kind of work yields:
roughly fifty attempts, of which fewer than ten ever cleared both class bars —
an **11% hit rate**, and that with a human researcher in the loop.

This system does not raise that rate by being cleverer. It raises the *value* of
the same rate by:

- killing dead ideas on minutes of local compute instead of a day of cluster
  time;
- refusing to record a non-measurement as a result, which is what turned six of
  ten round-1 "speedups" into two wasted rounds;
- keeping a memory with provenance, so a dead end is not rediscovered;
- looking where the profile says the time is, rather than where the training
  data says encoders are usually optimised.

If it produces one clean two-class pass per fifty experiments, it is performing
exactly as the history predicts. The improvement to look for is in the cost of
the forty-nine.
