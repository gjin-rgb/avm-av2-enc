# Extending the system

The three extensions most likely to be wanted, in order of how often.

---

## Add an ideation lens

A lens is a question, the evidence it needs, and guidance on answering it.
Append to `LENSES` in `av2ra/agent/lenses.py`:

```python
Lens(
    "rate_control_lookahead",
    "Where does rate control recompute a decision the lookahead already made?",
    "Two subsystems estimate frame cost independently and neither reads the "
    "other's answer.",                      # why this lens exists, with evidence
    ("codemap", "profile"),                 # refuses to run without these
    "structural",                           # expected mechanism
    "Name both sites and the quantity duplicated. Say which is authoritative.",
    prior_success=0.15,
),
```

Three things make a lens work rather than decorate the list:

1. **The evidence requirement is real.** A lens that runs without its evidence
   produces the generic prompt wearing a costume.
2. **The rationale cites something.** It goes into the prompt verbatim, and a
   rationale that is an assertion produces proposals that are assertions.
3. **The prior is honest.** It is a pseudo-count in the planner's allocation;
   an optimistic one starves better lenses until enough real results accumulate.

---

## Add a CTC back end

Implement `CtcBackend` (`av2ra/ctc/contract.py`): `submit`, `poll`, `collect`,
optionally `cancel` and `capacity`. Register it in
`providers/factory.build_ctc_backend`.

Three contracts matter:

- **`submit` must not block.** It returns a handle; the loop moves to the next
  hypothesis. A blocking back end turns a fleet into a queue of one.
- **`poll` may return partial results.** Set `CtcClassResult.complete=False` and
  `fraction_complete`. The planner uses partials for early abort, which is how
  quota gets returned.
- **`validate` rejects a request with no question.** Inherited; do not weaken it.

`ctc/local.py` (a reduced local CTC) and `ctc/sim.py` (instant, deterministic)
are the two worked examples.

---

## Add a site

A site is one YAML file naming which provider implementation backs each of the
five interfaces. Copy `config/sites/local.yaml`, change what differs, and use
`--site=<name>`.

Nothing in the core changes. If a site needs a new *implementation* — an object
store instead of a directory, a different notifier — implement the interface in
`providers/`, add one branch in `providers/factory.py`, and keep the import
inside that branch so a machine without the dependency can still import the
package.

---

## Add a tier

Tiers are ordered in `core/models.Tier` and evaluated in `measure/tiers.py`.
A new tier needs:

1. An enum member in the right position — order decides precedence when a
   verdict is derived.
2. An `evaluate_tN(experiment, results, context) -> TierResult`.
3. A call site in `ResearchLoop._do_screen`, placed so that cheaper tiers run
   first and can kill.

The tier most worth adding is the corpus's **shadow mode**: run the full
baseline search so the bitstream is unchanged, while recording what the
heuristic *would* have chosen. It yields per-site regret with zero run-to-run
variance and one encode per operating point, and it is a cheap *necessary*
condition — high regret kills a patch outright, low regret still needs CTC. It
belongs between T2 and T3.

---

## Change the acceptance rule

`measure/acceptance.py`. `DEFAULT_BARS` holds the per-preset ratios and caps.
Two knobs exist before editing code:

- `measure.acceptance_mode`: `point` to rank, `conservative` to recommend.
- `bdrate_cap_mode`: `warn` (default) or `reject` for combination rounds.

If you change a bar, change it in configuration and record why in the ledger. A
bar that moves without a recorded reason is a bar that will move again.

---

## Add a policy rule

`integrity/policy.FORBIDDEN_PATTERNS` is a list of
`(regex, kind, why_it_matters)`. The third element goes into the message the
implementer sees on rejection, so it has to explain the *failure it produces*,
not restate the rule. "hard-coded test resolution" is useless; "special-casing
the resolutions in the test set is fitting the benchmark, not improving the
encoder" is what makes the next attempt different.

Patterns are matched against **added lines only**. The encoder legitimately
contains `clock_gettime` and sequence names in comments.

---

## Add a methodology rule

`knowledge/lessons.RULES`. A rule needs an id, a statement, **the evidence that
produced it**, the stages it applies to, and — if it is enforced — the module
that enforces it.

The distinction matters: a rule with `enforced_by` is checked by code and
appears to the model as `ENFORCED`; one without is a prior. Claiming enforcement
that does not exist is worse than claiming none, because it stops anyone
building the check.

---

## Project layout

```
research_agent/
  av2ra/
    util/          io, subprocess, logging, yaml subset
    core/          models, config, registry, ledger, basewatch, serde, ids
    measure/       pchip, bdrate, stats, encode, screen, acceptance, tiers
    integrity/     policy, gates
    buildkit/      worktree, builder
    knowledge/     codemap, profiles, corpus, lessons, retrieval
    agent/         llm, lenses, ideation, implementer, analyst, planner, loop
    providers/     base, local, google, factory
    ctc/           contract, eda, local, sim
    orchestrate/   fleet
    report/        experiment, leaderboard, dashboard
    sim/           effects, encoder, demo
    app.py cli.py
  config/          default.yaml, sites/
  docs/
  tests/
```

## Conventions

- Two-space indent, 80-column soft limit, `from __future__ import annotations`.
- Comments explain *why*, and cite the failure they prevent where one exists.
- No `except Exception: pass`. A swallowed error in a measurement harness is how
  a wrong number becomes a confident one.
- New behaviour arrives with a test that asserts on a **conclusion**, not a code
  path.
