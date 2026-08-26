# Publishing this package

This archive is the complete `av2ra` project: the agent, its configuration, its
tests, and twelve documents. Nothing in it depends on where it lives.

## Already pushed

The same tree is on branch `claude/av2-research-agent-3b62ax` of
`AOMediaCodec/avm`, under `research_agent/`:

    git fetch origin claude/av2-research-agent-3b62ax
    git checkout claude/av2-research-agent-3b62ax
    cd research_agent && tools/av2ra --site=sim --workspace=/tmp/av2ra demo

## Putting it in chengchen-google/avm-patches

The session that produced this could not be granted push access to that
repository, so it was not pushed there. To do it:

    git clone https://github.com/chengchen-google/avm-patches.git
    cd avm-patches
    cp -r /path/to/av2ra-research-agent research_agent/av2ra
    git add research_agent/av2ra
    git commit -m "Add av2ra: an autonomous research agent for the AV2 encoder"
    git push

Two notes on that layout:

- The three draft guides already in `research_agent/` are the *input* to this
  work, not files it supersedes. `docs/00_CRITICAL_REVIEW.md` reviews them by
  name; keeping them side by side is what makes the review checkable.
- `research_agent/av2_research_agent/` (the phase 0/1 demo) is replaced rather
  than extended. The critical review documents exactly which of its commands
  print fixed strings, and which of its ideas were kept.

## One packaging hazard worth knowing

The AVM tree's `.gitignore` contains a bare `build/` rule, so a Python package
named `av2ra/build` is silently dropped when this project is committed inside
it. The package is therefore named `av2ra/buildkit`. If you vendor this
somewhere with its own ignore rules, check that nothing was quietly excluded:

    find . -type f | git check-ignore --stdin -v

The failure mode is not a build error — it is an import error in a fresh clone,
long after the commit looked fine.

## What is in here

    README.md                        start here
    PUBLISHING.md                    this file
    docs/00_CRITICAL_REVIEW.md       what the drafts got right and wrong, checked against the tree
    docs/01_ARCHITECTURE.md          the shape of the system, and why
    docs/02_IMPLEMENTATION_PLAN.md   phases 0-7 complete; 8-9 specified for a real deployment
    docs/03_METHODOLOGY.md           what the system may conclude, and what that cost to learn
    docs/04_USER_GUIDE.md            setup, running, reading a report, every command
    docs/05_OPERATIONS.md            runbook
    docs/06_GOOGLE_DEPLOYMENT.md     the three sanctioned internal touchpoints
    docs/07_INTEGRITY_MODEL.md       twelve threats, countermeasures, and what is not covered
    docs/08_AGENT_LOOP.md            ideation, patching, analysis, planning, learning
    docs/09_EXTENDING.md             new lenses, back ends, sites, tiers, rules
    docs/10_LIMITS.md                what is untested, unbuilt, and out of reach
    docs/appendix_prior_research_synthesis.md   the evidence the methodology came from
    av2ra/                           ~14,500 lines of Python, no required dependency
    config/                          defaults and three site profiles
    tests/                           85 tests
    tools/av2ra                      run from a checkout without installing
    tools/first_run.sh               the sequence to run on a fresh machine
    BUILD  pyproject.toml            google3 and PyPI packaging

## Verifying it before trusting it

    cd av2ra-research-agent
    tools/av2ra --site=sim --workspace=/tmp/av2ra demo    # a full cycle in seconds
    PYTHONPATH=. python3 tests/run_all.py                 # 85 tests

Tests needing an AVM checkout or a built `avmenc` skip rather than fail, so the
suite passes anywhere. On a machine with both, it additionally checks BD-rate
against `CalcBDRate.py`, the code map against the real tree, and the encode path
against the real `avmenc`.
