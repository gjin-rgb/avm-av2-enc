"""Ideation lenses: thirteen different ways to ask "what should we try next?".

A single "propose an optimisation" prompt collapses. It returns the same four
ideas -- prune the partition search, skip some transform types, early-terminate
the loop filter -- because those are what the training data says video encoders
do. The prior effort on this codebase produced twenty-four patches that way and
never touched the 47% of instructions retired that quantisation actually
occupied.

Each lens below asks a *structurally different* question, needs different
evidence, and tends to produce a different mechanism class. Several exist
because the corpus identified them as the highest-value unexplored directions
and then never built them. The planner allocates attention across lenses
explicitly, so the portfolio cannot silently collapse into one.

A lens is not a prompt template with the serial numbers filed off: it declares
what evidence it requires (``needs``), and ideation refuses to run it when that
evidence is missing. A "profile-driven" idea generated without a profile is just
the generic prompt wearing a costume.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Lens:
  name: str
  question: str
  rationale: str
  needs: tuple[str, ...]
  mechanism_hint: str
  guidance: str
  prior_success: float = 0.1        # base rate used by the planner
  cost_hint: str = "normal"

  def brief(self) -> str:
    return f"{self.name}: {self.question}"


LENSES: tuple[Lens, ...] = (
    Lens(
        "profile_hotspot",
        "Which function holding a large, untouched share of the profile can be made cheaper?",
        "Twenty-four patches were written before anyone profiled this encoder. "
        "Quantisation turned out to be ~47% of instructions retired -- more than "
        "partition search, mode search and the loop filters combined -- and not "
        "one patch had touched it.",
        ("profile", "codemap"),
        "approx",
        "Start from the ranked profile. Name the specific function and the "
        "specific work inside it that is redundant. State the Amdahl ceiling: "
        "the whole-encode saving cannot exceed the function's profile share.",
        prior_success=0.18,
    ),
    Lens(
        "dispatch_audit",
        "Is there a faster path that already exists but is never reached?",
        "A SIMD temporal-filter kernel in this codebase was 2.45x faster than "
        "the C reference and had been unreachable for an unknown length of time, "
        "because a dispatch predicate compared TF_BLOCK_SIZE against the wrong "
        "block size. Nobody has audited the other dispatch predicates.",
        ("codemap",),
        "dispatch",
        "Look for guards whose two sides disagree: an RTCD predicate testing a "
        "constant that no longer matches, a feature flag checked against a value "
        "the enum no longer produces, a fast path behind a condition that is "
        "always false. This class of finding is bit-exact and nearly free.",
        prior_success=0.30,
        cost_hint="cheap",
    ),
    Lens(
        "consistency",
        "Where does the code declare a policy that some call site does not honour?",
        "One function derived skip_trellis from the dry-pass shortcut flag while "
        "another in the same file hard-coded it to zero. Applying the existing "
        "policy at the second site was a clean win; a neighbouring patch that "
        "invented a new rule failed.",
        ("codemap",),
        "structural",
        "This is not a new heuristic. Find a policy the encoder already states "
        "-- a speed-feature flag, a shortcut mode, a winner-mode distinction -- "
        "and a site that ignores it. Explain why the site was missed.",
        prior_success=0.28,
    ),
    Lens(
        "bundle_split",
        "Which member of a bundled feature carries the cost, and which carries the benefit?",
        "Disabling a whole extended-partition family gave ratio 30.6 and failed. "
        "Disabling only its uneven 4-way members kept 39% of the speed for 6% of "
        "the quality cost: ratio 207.7, the best result the project ever "
        "produced. Cost and benefit are not proportionally distributed inside a "
        "feature group.",
        ("codemap", "corpus"),
        "approx",
        "Name the bundle, enumerate its members, and predict which members are "
        "cheap. Prefer an assignment-only change: a conditional is evaluated on "
        "every block whether or not it fires.",
        prior_success=0.25,
    ),
    Lens(
        "marginal_tuning",
        "Where does the measured marginal ratio say the current threshold is wrong?",
        "Three successive variants of one heuristic were each built on a story "
        "about which blocks are unsafe; two of the three stories were wrong. The "
        "marginal ratios answered the question directly and showed a 'variance "
        "floor' carried for four rounds bought 2.04% of speed for 0.00% BD-rate.",
        ("results", "corpus"),
        "approx",
        "Compute (speed given up)/(quality recovered) for each prior variant. "
        "Propose the next arm only where that marginal ratio is below the ratio "
        "already achieved, and bracket the parameter with arms on both sides of "
        "the best known value. Expose the parameter as a -D knob.",
        prior_success=0.22,
    ),
    Lens(
        "content_gate",
        "Which content property separates the clips this heuristic helps from the ones it damages?",
        "Speed benefit is nearly uniform across clips (CV ~12%) while quality "
        "cost varies sevenfold (CV ~89%). Exempting one clip in eight has twice "
        "been enough to clear a bar the mean failed -- which says a content gate "
        "exists and is worth building.",
        ("results",),
        "approx",
        "Use the per-sequence table. Identify what the damaged clips have in "
        "common and propose a gate computed from information the encoder already "
        "has at that point. Do not gate on resolution, frame index or QP: those "
        "are properties of the test set, not of the content.",
        prior_success=0.20,
    ),
    Lens(
        "cross_preset_transfer",
        "Which speed feature enabled at a faster preset would also pay at a slower one?",
        "A pruning heuristic removed +30.7% at speed 2 against +20.3% at speed 4 "
        "for essentially unchanged BD-rate: deeper searches contain more "
        "redundancy. But the ratio can fall while the speedup rises, and for a "
        "change to a ranking pass the direction reverses entirely.",
        ("codemap",),
        "approx",
        "Name the speed-feature assignment and the presets involved. Say "
        "explicitly whether the change affects a ranking pass -- if it does, "
        "expect it to be more expensive at faster presets, not less.",
        prior_success=0.20,
    ),
    Lens(
        "self_defeating",
        "Which enabled speed feature costs both time and quality?",
        "Degrading prediction quality makes residuals larger, which makes "
        "downstream transform and partition search more expensive. Restoring "
        "smooth intra prediction was -1.08% BD-rate and 1.25% faster at once. "
        "Four such features were found in one ablation study.",
        ("codemap", "corpus"),
        "structural",
        "Propose *removing* or narrowing a feature. The claim to test is that "
        "the feature's local saving is smaller than the downstream work its "
        "worse prediction creates.",
        prior_success=0.24,
    ),
    Lens(
        "reuse_hoist",
        "What is recomputed that could be computed once?",
        "Reuse changes are provable: the bitstream must be identical, so quality "
        "risk is zero by proof and no cluster round is needed. But all three "
        "reuse patches in the prior corpus were *slower* at 4K -- a memo lookup "
        "unpaid by its hit rate, an arena worse for locality than the "
        "allocations it replaced.",
        ("profile", "codemap"),
        "reuse",
        "State the invariant being hoisted and why it is invariant. Then argue "
        "the 4K case specifically: what is the hit rate, and what does the "
        "lookup or the extra memory cost when the working set no longer fits in "
        "cache?",
        prior_success=0.15,
        cost_hint="cheap",
    ),
    Lens(
        "kernel_simd",
        "Which hot kernel is running scalar C, or a narrower vector width than the machine has?",
        "The one kernel attempt in the prior corpus found a dead dispatch and a "
        "2.45x speedup. Kernel work is bit-exact, so it is cheap to prove and "
        "cannot cost quality -- but it is bounded hard by Amdahl.",
        ("profile", "codemap"),
        "kernel",
        "Give the Amdahl arithmetic before anything else: kernel share of the "
        "profile times the fraction removed. If that is under about 0.5% "
        "whole-encode, say so and drop the idea.",
        prior_success=0.20,
    ),
    Lens(
        "proxy_substitution",
        "Which expensive exact cost could be replaced by a cheaper proxy that ranks the same way?",
        "When a search is already terminating early, what matters is what it "
        "evaluated before it stopped, not how narrowly it decides to stop. One "
        "patch got +1.13% from changing search *order* in a function where "
        "changing the *tolerance* had achieved nothing.",
        ("profile", "codemap"),
        "approx",
        "Distinguish ranking from deciding. A cheaper proxy is safe where the "
        "result only orders candidates and an expensive exact cost still makes "
        "the final decision; it is dangerous where the proxy *is* the decision.",
        prior_success=0.18,
    ),
    Lens(
        "failure_postmortem",
        "Which failed idea failed for a fixable reason rather than a fundamental one?",
        "Failures divide into 'the mechanism was wrong' and 'the lever was "
        "wrong'. The second kind is worth re-attacking; the first is not. One "
        "CCSO idea failed on tolerance and then succeeded on ordering.",
        ("corpus", "results"),
        "approx",
        "Name the failed experiment, state which failure mode it hit, and "
        "explain what specifically is different this time. If the failure was "
        "'BD cost structurally too high', do not re-attack it.",
        prior_success=0.16,
    ),
    Lens(
        "decision_statistics",
        "What would the data say if we measured the decisions instead of guessing about them?",
        "A shadow mode -- run the full baseline search so the bitstream is "
        "unchanged, while recording what the heuristic *would* have chosen -- "
        "gives per-site regret with zero run-to-run variance and one encode per "
        "operating point. It was specified in the prior work, called the highest "
        "-value remaining infrastructure, and never built.",
        ("codemap",),
        "structural",
        "Propose an instrumentation patch first: record the candidate the "
        "heuristic would have skipped and the RD cost of the one that won. The "
        "output is a regret distribution, not a speedup. The heuristic is "
        "designed from that distribution afterwards.",
        prior_success=0.35,
        cost_hint="instrumentation",
    ),
)

LENSES_BY_NAME = {lens.name: lens for lens in LENSES}


def available(context_keys: set[str]) -> list[Lens]:
  """Lenses whose evidence requirements this context actually satisfies."""
  return [lens for lens in LENSES if set(lens.needs) <= context_keys]


def missing_evidence(lens: Lens, context_keys: set[str]) -> list[str]:
  return [need for need in lens.needs if need not in context_keys]


@dataclass
class LensStats:
  """Realised performance per lens. Feeds the planner's allocation."""

  proposed: dict[str, int] = field(default_factory=dict)
  screened: dict[str, int] = field(default_factory=dict)
  passed: dict[str, int] = field(default_factory=dict)

  def record(self, lens: str, *, stage: str) -> None:
    target = {"proposed": self.proposed, "screened": self.screened, "passed": self.passed}[stage]
    target[lens] = target.get(lens, 0) + 1

  def rate(self, lens: str) -> float:
    """Posterior success rate, shrunk toward the lens's documented prior.

    Beta-binomial shrinkage with the prior as a two-observation pseudo-count.
    Without it, one lucky first result makes a lens look like a 100% winner and
    the planner pours everything into it.
    """
    prior = LENSES_BY_NAME[lens].prior_success if lens in LENSES_BY_NAME else 0.1
    tried = self.proposed.get(lens, 0)
    won = self.passed.get(lens, 0)
    pseudo = 4.0
    return (won + prior * pseudo) / (tried + pseudo)
