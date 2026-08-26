"""av2ra -- an autonomous research agent for the AV2 (libavm) encoder.

The package is layered so that the parts which must be believed (measurement,
integrity) do not depend on the parts which are allowed to be creative (the
LLM-driven agent loop), and so that nothing in the core depends on a specific
compute or storage infrastructure.

    av2ra.util        small, dependency-free helpers
    av2ra.core        domain model, experiment registry, decision ledger
    av2ra.measure     encodes, metrics, BD-rate, statistics, tiered evaluation
    av2ra.buildkit    worktrees, hermetic builds, artifact caching
    av2ra.integrity   the gates that make an autonomous result trustworthy
    av2ra.knowledge   code map, profiles, prior-research corpus, retrieval
    av2ra.agent       ideation, patch synthesis, analysis, portfolio planning
    av2ra.providers   storage / code / notification back ends (local, google)
    av2ra.ctc         the abstract CTC job contract and its back ends
    av2ra.orchestrate durable queue, leases, node state machine, fleet
    av2ra.report      experiment reports, leaderboard, frontier, digest
    av2ra.sim         a simulated encoder, so the whole loop is testable
"""

__version__ = "1.0.0"
