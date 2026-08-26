# av2ra — an autonomous research agent for the AV2 encoder

`av2ra` proposes encoder-side algorithmic changes to the AV2 reference encoder
(`libavm`), implements them in C, measures them, decides what the measurements
mean, and records what it learned — without a human in the inner loop, and
without being able to fake a result.

```bash
cd research_agent
tools/av2ra --site=sim --workspace=/tmp/av2ra demo
```

That runs a complete research cycle in seconds against a simulated encoder and
cluster, and writes the artifacts a real run writes. No encoder, no clips, no
credentials, no cluster.

```
[ 1] propose [N0001]: [decision_statistics] Gate the transform-partition search on the stationarity margin
[ 2] implement [N0001]: patch built (2 lines across 1 file(s)) in 1 attempt(s)
[ 3] screen [N0001]: local screen: 6.67% faster -6.67% [-6.84, -6.50] on instructions; ...
[ 4] escalate [N0001]: submitted 1 arm(s). Moving on to the next hypothesis rather than waiting.
[ 5] collect [N0001]: CTC_PASS: every class cleared the bar independently -> move 'promote'
```

---

## What is different about it

Most of the design follows from one observation: on this codebase, **the
measurement is harder than the idea.** The prior research effort — roughly fifty
experiments, three CTC rounds, a 101-feature ablation study — produced fewer
than ten results that cleared the acceptance bar on both test classes, and lost
far more time to measurement failures than to bad ideas. Six of ten reported
speedups in its first round were smaller than the noise floor of the design that
produced them. Three of ten local screens had the *wrong sign* against the real
benchmark. Eight arms never fired and were read as harmless. Four ideas died as
no-ops because upstream had already implemented them.

So this system is built measurement-first:

- **An evidence ladder**, cheapest proof first. A bit-exactness proof costs
  minutes and replaces a cluster round outright. A local screen is allowed to
  kill and to rank, and is *forbidden* from quoting a BD-rate.
- **Intervals, not point estimates.** An effect whose interval contains zero is
  `NO_EFFECT`, never a small win. A ratio whose denominator is unresolved is
  `None`, never infinity.
- **Integrity gates.** The agent cannot edit the harness, the metrics, the test
  framework or its own thresholds; cannot special-case the test set; cannot
  compare arms built differently; and is checked against a holdout clip set it
  never sees.
- **A memory with provenance.** Every measurement carries its anchor SHA, patch
  hash, build fingerprint, clip set, metric definition and job id. Measurements
  are append-only, so a disagreement surfaces as a conflict instead of being
  overwritten.
- **Thirty methodology rules**, eighteen enforced by code, each citing the
  failure that produced it. `av2ra rules --evidence`.

And research-first:

- **Thirteen ideation lenses**, each asking a structurally different question and
  requiring different evidence. Four of them exist because the prior corpus
  named them as the highest-value unexplored directions and never built them.
- **Grounding in the real tree.** Ideation may only name symbols from a code map
  built by reading the source (6,313 functions on the reviewed commit). A
  plausible-but-absent name is repaired or rejected, never implemented.
- **Profile-driven targeting** with an Amdahl gate that rejects impossible
  claims before anything is built.
- **Portfolio governance** that stops the agent producing twenty-four
  consecutive patches in one subsystem while half the profile goes untouched.

---

## The infrastructure boundary

Only **data storage, code storage and CTC runs** may depend on Google-internal
infrastructure. Everything else runs unchanged on a laptop.

```
$ grep -rln 'fileutil\|gcertstatus\|sendgmr\|kick_off_av2ctc' av2ra/ --include='*.py'
av2ra/core/models.py          # a docstring saying the core must NOT name these
av2ra/ctc/eda.py              # the Cloud EDA back end
av2ra/providers/base.py       # docstrings describing the boundary
av2ra/providers/factory.py    # the one place that maps a config string to an implementation
av2ra/providers/google.py     # CNS, CitC, gcertstatus, sendgmr
```

Two files *invoke* internal tooling (`providers/google.py`, `ctc/eda.py`); one
selects between implementations (`providers/factory.py`); the other two only
mention the names in prose. Delete the first two and the factory's two branches
and the system still runs, losing CNS and the cluster and keeping everything
else.

| site | storage | code | CTC | model |
|---|---|---|---|---|
| `sim` | local dir | none | simulated | offline |
| `local` | local dir | git | simulated or reduced local | live if credentials exist |
| `google` | CNS | CitC / shared repo | Cloud EDA | live |

---

## Documentation

| | |
|---|---|
| [Critical review of the draft guides](docs/00_CRITICAL_REVIEW.md) | what the drafts got right, five blockers, ten major defects, all checked against the tree |
| [Architecture](docs/01_ARCHITECTURE.md) | the shape of the system and why each part is that shape |
| [Implementation plan](docs/02_IMPLEMENTATION_PLAN.md) | phases 0–7 complete, 8–9 specified for the first real deployment |
| [Methodology](docs/03_METHODOLOGY.md) | what the system is allowed to conclude, and what it cost to learn |
| [User guide](docs/04_USER_GUIDE.md) | setup, running, reading a report, every command |
| [Operations runbook](docs/05_OPERATIONS.md) | halts, no-effect results, conflicts, adding nodes |
| [Google deployment](docs/06_GOOGLE_DEPLOYMENT.md) | the three sanctioned touchpoints, and what to verify first |
| [Integrity model](docs/07_INTEGRITY_MODEL.md) | twelve threats, their countermeasures, and what is not covered |
| [The research loop](docs/08_AGENT_LOOP.md) | ideation, patching, analysis, planning, learning |
| [Extending](docs/09_EXTENDING.md) | new lenses, back ends, sites, tiers, rules |
| [Limits](docs/10_LIMITS.md) | what is untested, unbuilt, and structurally out of reach |
| [Appendix: prior-research synthesis](docs/appendix_prior_research_synthesis.md) | the raw evidence the methodology was derived from |

---

## Status

| | |
|---|---|
| Code | ~14,500 lines of Python, no required third-party dependency |
| Tests | 85, including the full loop end to end and the real `avmenc` |
| Validated against | `CalcBDRate.py` (1e-6 over 200 curve pairs), scipy PCHIP and Student-t, six recorded CTC verdicts, the real AVM tree, the real prior-research corpus |
| Not yet exercised | Cloud EDA against a live cluster, CNS, a full-scale CTC screening pass |

`numpy`, `scipy` and `PyYAML` are used when present and are not required: PCHIP,
the Student-t distribution and a YAML subset are implemented in pure Python and
tested against the reference implementations.

---

## Quick reference

```bash
av2ra init                 # create the workspace
av2ra doctor               # credentials, tools, disk, anchor, clips, encoder
av2ra bootstrap            # clone or align the AVM checkout
av2ra ingest               # code map + prior research
av2ra profile --import=... # targeting and the Amdahl ceiling
av2ra run --ticks=50       # the autonomous loop
av2ra status               # fleet and top results
av2ra frontier             # what is not dominated, and what to build next
av2ra report N0042         # one experiment in full
av2ra base check           # has the anchor moved, and what does that invalidate
av2ra registry conflicts   # quantities measured twice that disagree
av2ra rules --evidence     # the methodology, and what it cost
```
