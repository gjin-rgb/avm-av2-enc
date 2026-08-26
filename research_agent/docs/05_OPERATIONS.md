# Operations runbook

---

## Daily

```bash
av2ra status                 # anything stale, any conflicts?
av2ra digest --hours=24      # what moved
av2ra base check --offline   # has the anchor drifted?
```

Read `ledger/DECISIONS.md` weekly. It is the system's memory, and a wrong lesson
in it is the one failure mode with no automated countermeasure.

---

## The loop has halted

```
halt: the anchor moved: every recorded measurement is scoped to the old base
      and is not automatically valid against the new one.
```

Deliberate. Run `av2ra base check` to see the overlap set — the files upstream
changed that experiments also touch, where a clean apply means least — then
`av2ra base check --accept` to move the pointer and mark the old measurements
stale. Experiments needing re-validation are listed.

`av2ra run --ignore-drift` overrides it. Only reasonable when you have already
established the drift cannot affect the work in flight.

---

## Everything comes back NO_EFFECT

Expected when the design lacks power. The tier result names the MDE and the
repetitions needed:

```
the design cannot resolve the 3.0% effect this experiment cares about:
with 2.40% noise and 3 repetitions the smallest visible effect is 5.44%.
About 7 repetitions per arm would be needed.
```

In order of value:

1. **Get `perf` working.** Instruction counts have ~0.0004 CV against ~0.02 for
   timing. This is a fifty-fold improvement in resolving power and costs nothing
   per run.
2. **Raise `measure.reps`.** Linear cost, square-root benefit.
3. **Reduce machine noise.** Pin, stop other tenants, disable turbo. `doctor`
   warns when pinning is unavailable.

Do not lower the bar. A sub-noise effect is not a small win.

---

## Everything comes back "never fired"

The activation gate reporting an identical bitstream for an approximating patch.
The patch compiles and does nothing on this clip set.

Usual causes, in order:

- The guarded path is not reached at this preset. Check whether a speed feature
  already disables it.
- The threshold defaults to a value that never triggers.
- The screening clips do not exercise the case (screen content, high QP, small
  blocks).

The analyst returns `instrument` for this, which is the right next step:
measure when the site is entered before designing a heuristic for it.

---

## A CTC round will not submit

`av2ra report <id>` shows the escalation case with its blockers. The common ones:

| blocker | meaning |
|---|---|
| speedup not resolved locally | CTC measures quality; it cannot rescue an inconclusive timing result |
| no held-out measurement | the screening number is an upper bound that has never been checked |
| bit-exact patch | a proven-exact patch needs no quality round; if the question is 4K timing, say so explicitly |
| budget spent | `planner.ctc_slots_per_day`; arms batch better than they drip-feed |

---

## Measurement conflicts

```bash
av2ra registry conflicts
```

Two rows measuring the same quantity under the same conditions that disagree.
Never resolve by deleting a row. Establish which back-end run each came from
(`job_id` is recorded), then either fix the provenance bug or record that the
back end's repeatability is worse than assumed — the second is a finding worth
more than most patches.

---

## Disk

```bash
av2ra gc            # drop worktrees for finished experiments
```

A worktree is a full checkout plus a build tree. The manager refuses to create
one below `infra.min_free_gib`, because a build that runs out of disk halfway
links a binary that is not the code you think it is.

---

## Adding a node

```bash
av2ra --site=google --node=ct2 init
av2ra --site=google --node=ct2 bootstrap
av2ra --site=google --node=ct2 doctor
av2ra --site=google --node=ct2 run --ticks=50
```

Point it at the same workspace (or the same blob store) and it joins. There is
no registration step and no scheduler: a node pulls what it can lease.

Size `measure.workers × measure.cpus_per_worker` to leave headroom for builds
and the agent — roughly 75% of the machine. Oversubscribing makes every
measurement a measurement of the scheduler.

---

## Credentials

`doctor` warns at four hours of LOAS validity, not two. A cluster round takes
hours, and a certificate that expires mid-submission leaves a half-submitted job
set to untangle by hand. Run `gcert` when warned.

---

## Stopping a node

```bash
av2ra steer --node=ct1 --action=abort --reason="..."
```

Picked up on the next tick. To stop immediately, interrupt the process: state is
durable and re-running continues from where it stopped.

---

## Trusting a new deployment

Before believing any number from a new environment:

1. `av2ra demo` — the plumbing works.
2. `python3 -m unittest discover -s tests` — 85 tests pass here.
3. `av2ra doctor` — no attention items you have not consciously accepted.
4. One experiment through T0–T4 read by hand, including the diff.
5. The null arm's number recorded (locally automatic; on the cluster, one round
   submitting the same commit twice).

Step 5 is the one that gets skipped and the one the prior effort asked for four
times without running. Until it exists, every ratio has an unknown denominator
error, and two of that project's decisions turned on differences of 0.1 to 1.7
ratio points it had no basis to resolve.
