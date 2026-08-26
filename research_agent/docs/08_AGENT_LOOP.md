# The research loop

The part the draft guides never specified. This document describes how a
hypothesis is generated, implemented, judged, and turned into something the next
hypothesis benefits from.

---

## The tick

The loop is a state machine over the registry, not a long-running function.

```
   ┌─ collect ──────  a cluster round finished → assess, analyse, learn
   │
   ├─ escalate ─────  an arm earned a round → submit, do NOT wait
   │
   ├─ screen ───────  a patch exists → run the ladder
   │
   ├─ implement ────  a proposal exists → write C, build
   │
   └─ propose ──────  nothing else to do → generate a hypothesis
```

Priority is top to bottom: **finishing beats starting.** An experiment that is
measured but not analysed is compute already spent that has produced no
knowledge.

Each tick reads state, does one unit of work, writes durably, returns. A reboot
costs one tick.

---

## Ideation

### Why lenses

A single "propose an optimisation" prompt returns the same four ideas — prune
the partition search, skip transform types, early-terminate the loop filter —
because that is what the training data says video encoders do. The prior effort
produced twenty-four patches that way and never touched the 47% of instructions
retired that quantisation occupied.

Thirteen lenses each ask a structurally different question:

| lens | question | prior |
|---|---|---|
| `decision_statistics` | what would the data say if we measured the decisions? | 0.35 |
| `dispatch_audit` | is there a faster path that already exists but is unreachable? | 0.30 |
| `consistency` | where does a call site ignore a policy the code already declares? | 0.28 |
| `bundle_split` | which member of a bundle carries the cost? | 0.25 |
| `self_defeating` | which enabled feature costs both time and quality? | 0.24 |
| `marginal_tuning` | where does the marginal ratio say the threshold is wrong? | 0.22 |
| `content_gate` | what separates the clips this helps from the ones it damages? | 0.20 |
| `cross_preset_transfer` | which feature would also pay at a slower preset? | 0.20 |
| `kernel_simd` | which hot kernel runs scalar C or a narrow vector width? | 0.20 |
| `proxy_substitution` | which expensive exact cost could a cheaper proxy replace? | 0.18 |
| `profile_hotspot` | which large untouched profile share can be made cheaper? | 0.18 |
| `failure_postmortem` | which failure was the lever, not the mechanism? | 0.16 |
| `reuse_hoist` | what is recomputed that could be computed once? | 0.15 |

Four of them exist because the corpus named them as the highest-value
unexplored directions and never built them.

A lens declares the evidence it needs and ideation **refuses to run it without
that evidence**. `profile_hotspot` without a profile is the generic prompt
wearing a costume.

Selection is by shrunken success rate — beta-binomial with the documented prior
as a four-observation pseudo-count — preferring a new mechanism class before
repeating one. Without shrinkage one lucky result makes a lens look like a
certainty.

### What a hypothesis must contain

Enforced by the output schema:

- **Mechanism**, honestly. It decides which tier proves the patch, and a
  mislabelled patch gets the wrong proof.
- **Target functions**, from the code map. Checked; a near-miss is repaired
  (`search_txk_type` → `search_tx_type`), anything else is rejected.
- **A prediction**: expected speedup and BD-rate, as numbers. The analyst grades
  both. An idea whose author cannot say what it should buy cannot be wrong, and
  an agent that is never wrong never learns.
- **Kill criteria**: the result that retires the idea rather than prompting
  another round of tuning. The corpus records that writing this before a run
  cost one arm instead of a round.
- **Why nobody has done this already**, answered against the prior-attempt list.

### Three filters

**Grounding.** Symbols must exist. Two prior experiments were authored against
functions that had moved upstream; one consumed a cluster arm.

**Novelty.** Structural, by touched function and file, against the corpus and
this system's own registry — not by wording. Re-deriving a dead idea in
different words is the default behaviour of a model asked the same question
twice. Overlap with a prior *failure* on the same functions is not an automatic
rejection (one CCSO idea failed on tolerance and later succeeded on ordering)
but the proposal must account for it.

**Amdahl.** A claimed whole-encode speedup larger than the touched code's
profile share, plus 15% slack, is rejected before implementation.

---

## Implementation

The model edits by **exact search-and-replace**, not by emitting a diff. A diff
carries line numbers and hunk headers that a model gets subtly wrong often
enough to matter, and one that fails to apply after ten minutes of context
assembly is a wasted experiment. Exact anchors either match or they do not, and
the repair prompt can say which anchor was not found.

The context is the real source around each target function, with line numbers,
read from the worktree.

The build-repair loop is bounded at three attempts and is not a free retry.
Compiler errors are fed back — filtered to the error lines, not five thousand
lines of ninja output. After the budget, the experiment fails as `BUILD_FAIL`
and is recorded, because an idea that cannot be expressed in three attempts is
usually one whose author does not understand the code it touches.

Policy runs before the build, so a violating patch never reaches a compiler.
Between attempts the tree is reverted **and the revert is verified** — the prior
corpus lost a round to a revert that silently did nothing.

---

## Analysis

Eight moves, and every result maps to exactly one:

| move | when |
|---|---|
| `kill` | mechanism wrong, or ratio at or under half the bar — not tunable |
| `tune` | **only** when a marginal-ratio calculation supports it |
| `split` | the change bundles behaviours and the cost is concentrated |
| `scope` | it works on one class, preset or content type, and the evidence says where |
| `escalate` | local evidence exhausted; only CTC can answer what remains |
| `promote` | cleared the bar on every class independently |
| `instrument` | measure the decisions rather than guess at a threshold |
| `hold` | blocked on something external |

The move is derived **mechanically first**, then the model is asked to refine it.
A move that follows from terminal evidence — a kill on a build failure, a
promote on a result that cleared every class — cannot be overturned; the
disagreement is recorded. A model asked to reconsider a fact will find a reason.

Three analyses run on every result:

**Marginal ratio** against the parent arm. `(speed given up) / (quality
recovered)` is the ratio of the decisions a variant removed; removing them helps
only when that is *below* the ratio already achieved. This ended two rounds of
guesswork on the prior project and showed that a "variance floor" carried
through four variants bought 2.04% of speed for 0.00% BD-rate.

**Exemption analysis** on per-sequence spread. Which single clip, if exempted,
most improves the ratio, and what share of the damage and the benefit it
carries. Twice, exempting one clip in eight cleared a bar the mean had failed.
That returns `scope`, because such a clip is a content gate waiting to be found.

**Prediction scoring.** Direction and magnitude against the hypothesis's
forecast. A wrong direction warns that the mechanism story is suspect even when
the numbers look good; a magnitude off by more than 3× flags the lens's
calibration.

---

## Planning

Two resources three orders of magnitude apart: local CPU, and cluster rounds.

**Governor limits**, each addressing a documented degeneration:

| limit | default | prevents |
|---|---|---|
| max open experiments | 6 | starting instead of finishing |
| max fraction per subsystem | 0.4 | twenty-four consecutive patches in one area |
| max tuning depth | 3 | infinite threshold chains |
| CTC slots per day | 4 | burning the scarce resource |
| min arms per round | 2 | drip-feeding rounds whose latency is set by one arm |
| holdout required | true | escalating an unchecked screening number |

**Escalation** requires a written case naming the question and the cheaper
evidence exhausted. Refused when the speedup is unresolved locally, when no
holdout exists, or when the patch is bit-exact — a proven-exact patch needs no
quality round, and the corpus spent three discovering that its bit-exact patches
were merely slower at 4K.

**Early abort.** A round a third complete showing no speedup on a class is
cancelled to return quota. The bar is deliberately high: a partial round is a
biased sample, since the fastest clips finish first.

---

## Learning

Every completed experiment writes a dated entry to the ledger: what changed
state, why, and one durable lesson. Lessons are injected into future ideation
prompts.

The ledger is append-only and retractions are **marked, not erased**. The
corpus contains a rule derived from three arms that never fired, carried forward
for two rounds before anyone noticed. A memory that cannot represent its own
retractions repeats them.

---

## Context engineering

Prompt caching is a prefix match, so context is split:

- **Frozen system block** (~34 KB, cacheable, byte-identical across calls):
  methodology rules, code map digest, speed-feature ladder, profile digest,
  corpus digest, decision digest.
- **Volatile user message**: the lens, the focus, recent results, what to avoid.

`ContextPack.fingerprint` makes the stability checkable, and a test asserts two
different prompts produce the same prefix. Getting this backwards means paying
full input price on every call for context that never changed.

---

## Running without a model

`agent.llm=offline` runs the entire loop with no credentials and no network. The
offline client fills the requested schema with valid minimal values, so every
downstream stage — patch application, screening, gating, analysis, reporting —
exercises real code. A pass proves the *plumbing*; it proves nothing about
research quality, and every run is labelled `offline` so no report can imply
otherwise.
