# User guide

Everything here has been run. Where a command's output is shown, it is real
output from this repository.

---

## Five minutes: see it work

No encoder, no clips, no credentials, no cluster:

```bash
cd research_agent
export PYTHONPATH=$PWD
python3 -m av2ra.cli --site=sim --workspace=/tmp/av2ra demo
```

This builds a miniature encoder repository, wires the **real** loop to a
simulated encoder and cluster, and runs a full research cycle:

```
[ 1] propose [N0001]: [decision_statistics] Gate the transform-partition search on the stationarity margin
[ 2] implement [N0001]: patch built (2 lines across 1 file(s)) in 1 attempt(s)
[ 3] screen [N0001]: local screen: 6.67% faster -6.67% [-6.84, -6.50] on instructions;
     indicative BD-rate +0.02% [+0.01, +0.04] (rank only). Local screening BD-rate is a
     rank and a tripwire, not an estimate...
[ 4] escalate [N0001]: submitted 1 arm(s): N0001. Moving on to the next hypothesis rather than waiting.
[ 5] collect [N0001]: CTC_PASS: every class cleared the bar independently:
     A1: ratio 265.1 (point) clears the speed-2 bar of 30;
     A2: ratio 260.7 (point) clears the speed-2 bar of 30 -> move 'promote'
[ 6] propose [N0002]: [decision_statistics] Reduce trellis states during the ranking pass only
...
[10] collect [N0002]: BELOW_BAR: A1: ratio 18.9 is below the speed-2 bar of 30 ...
```

and writes the artifacts a real run writes: `registry.csv`,
`ledger/DECISIONS.md`, `reports/<id>/report.md`, `reports/dashboard.html`.

Only the compiler, the encoder and the cluster are simulated. The planner,
tiers, gates, statistics, registry and reports are the production ones.

Run the test suite the same way:

```bash
python3 -m unittest discover -s tests     # 85 tests
```

---

## Setting up for real work

### 1. Choose a site

| site | storage | code | CTC | model |
|---|---|---|---|---|
| `sim` | local dir | none | simulated | offline |
| `local` | local dir | git (optional) | simulated, or reduced local CTC | live if credentials exist |
| `google` | CNS | CitC or the shared repo | Cloud EDA | live |

```bash
av2ra --site=local init
```

Fill in the `REQUIRED` fields in `config/sites/google.yaml` before using the
Google site. `av2ra doctor` names what is missing.

### 2. Get the encoder

```bash
av2ra bootstrap          # clone or align ~/av2/avm to origin/av2-enc
```

Then build it yourself once, or let the loop build the anchor on its first
screening pass.

### 3. Check the environment

```bash
av2ra doctor
```

```
[OK]        disk: 240.1 GiB free under /home/you/av2ra-workspace
[OK]        tool:git: present
[ATTENTION] perf: unavailable: complexity measurements will fall back to timing
            -> install linux-perf and set kernel.perf_event_paranoid<=2 ...
[OK]        avm repo: /home/you/av2/avm
[OK]        anchor: base is current at 493a008b9166
[ATTENTION] screening clips: 0/5 present
```

`doctor` exits 2 when something needs attention, so it works in a health check.

Fix `perf` if you can. Instruction counts are reproducible to well under 0.1%;
timing on a shared machine has a floor around 2%, which is larger than most
effects worth finding.

### 4. Seed the knowledge base

```bash
av2ra ingest
```

```
code map: {"files": 542, "functions": 6313, "speed_features": 330}
prior research: 66 attempts, 7 decisions
  historical hit rate: 2/19 resolved attempts cleared both class bars (11%)
```

Point `knowledge.prior_research` at a checkout of the shared results repository.
Without it the agent cannot check an idea against what has already failed, and
its own prior on success will be wrong by a factor of several.

### 5. Give it a profile

```bash
av2ra profile --import path/to/callgrind-annotate.txt --preset=4
```

```
  share%  function                                 subsystem
    9.95  av2_trellis_quant                        quantization
    7.47  av2_decide_states_q1_avx2                quantization
    5.86  av2_decide_states_avx2                   quantization
    4.63  __memset_avx2_unaligned_erms             libc

by subsystem:
   36.92%  quantization
   20.76%  transform
    8.89%  simd

hot functions no prior experiment has touched:
    7.47%  av2_decide_states_q1_avx2 (quantization)
    4.63%  __memset_avx2_unaligned_erms (libc)
```

A profile is not optional. Four lenses refuse to run without one, and the
Amdahl gate cannot reject an impossible claim. The prior effort produced
twenty-four patches before profiling and discovered that none of them had
touched the 47% of instructions retired that quantisation occupied.

---

## Running

```bash
av2ra run --ticks=1        # one unit of work; read the report; repeat
av2ra run --ticks=50       # unattended
av2ra run --until-idle     # until there is nothing to do
```

A tick does one thing: propose, implement, screen, escalate, or collect. State
is durable, so interrupting costs one tick.

Submitting a cluster round never blocks — the next tick starts a fresh
hypothesis in a fresh worktree.

---

## Watching

```bash
av2ra status              # fleet + top results
av2ra leaderboard         # every experiment, ranked
av2ra frontier            # non-dominated arms and open threads
av2ra report N0042        # one experiment in full
av2ra report N0042 --patch
av2ra digest --hours=24 --send
av2ra dashboard --out=/tmp/av2ra.html
```

`frontier` is the one to read when deciding what to build next. A ranked list
answers "what is best"; the frontier answers "what is not dominated", which is a
different and more useful question when two arms trade off differently.

---

## Reading a report

Every report carries the anchor it was measured against, the prediction made
*before* measuring, the tier ladder with pass/fail and reasoning, per-sequence
spread, the integrity gates, and the lessons recorded.

Three things to look at first:

1. **The tier that failed**, and its reason. `T1_exactness FAIL ... it never
   fired` means the experiment did not run, not that the patch is safe.
2. **Per-sequence spread.** A mean that hides one bad clip is a regression risk
   and also a design signal: it says a content gate exists.
3. **The prediction score.** Direction wrong means the mechanism story is
   suspect even when the numbers look fine.

---

## Steering

```bash
av2ra steer --node=ct1 --action=abort \
  --reason="the variance threshold is too noisy on 10-bit HDR; switch to transform search pruning"
```

The instruction is a durable file the node picks up on its next tick, so it
survives a restart and does not interrupt an encode mid-flight.

---

## When the anchor moves

```bash
av2ra base check
```

```
base moved 425711f3469b -> 493a008b9166 (3 commit(s), 41 file(s) changed)

upstream commits since the recorded base:
  493a008 Do not enable simple_motion_search_prune_rect (#5311)
  cf38569 Use encoder-only method for reduced-tx-part-set option (#5313)

  N0042 [APPLIES] -> revalidate
      upstream changed functions this patch hooks into (av2_rd_pick_partition):
      a clean apply proves nothing here. Re-run the cheap tiers.

2 experiment(s) need re-validation.
```

`APPLIES` is not `CORRECT`. The worked example from the prior corpus: one
upstream commit added 187 lines to a file two patches modified, and all ten
patches still reported `APPLIES`. Whether they still pruned the search they were
measured against was unknowable from that check.

`av2ra base check --accept` moves the pointer and marks the old measurements
stale. The loop halts on drift until you do.

---

## Publishing

```bash
av2ra publish --remote-path=research_agent/results --message="round 3 results"
```

Copies reports and the registry export into the configured code store — a git
checkout or a CitC client. Nothing mails a change list: promotion upstream is a
human decision.

---

## Auditing

```bash
av2ra registry conflicts
```

Reports quantities measured twice under the same conditions that disagree. That
is either a repeatability problem in the back end or a provenance bug in the
harness, and both need a person. The prior corpus ended with two different sets
of CTC numbers for the same three patches and no way to tell which was right;
this is the command that would have caught it.

```bash
av2ra rules --stage=measurement --evidence
```

Prints the methodology rules, which are enforced by code, and what each one cost
to learn.

---

## Configuration

Resolution order, last wins:

1. `config/default.yaml`
2. `config/sites/<site>.yaml`
3. `$AV2RA_WORKSPACE/config.yaml`
4. `AV2RA_*` environment variables (`AV2RA_MEASURE__REPS=5`)
5. `--set measure.reps=5`

The settings worth knowing:

| key | default | what it does |
|---|---|---|
| `measure.preset` | 2 | the speed preset under test |
| `measure.reps` | 2 | repetitions per arm; raises power, costs time |
| `measure.workers` | 2 | concurrent encodes; each pinned to `cpus_per_worker` CPUs |
| `measure.acceptance_mode` | `conservative` | `point` to rank, `conservative` to recommend |
| `measure.null_arm_every` | 10 | ticks between anchor-vs-anchor harness checks |
| `planner.ctc_slots_per_day` | 4 | the scarce-resource budget |
| `planner.require_holdout` | true | refuse a cluster round without a holdout measurement |
| `agent.llm` | `auto` | `offline` runs the loop with no model and no credentials |

---

## Command reference

| command | what it does |
|---|---|
| `init` | create the workspace and a local config |
| `doctor` | credentials, tools, disk, repo, anchor, clips, encoder |
| `bootstrap` | clone or align the AVM checkout |
| `ingest` | build the code map, read the prior corpus |
| `profile` | show or import an encoder profile |
| `base check` | anchor drift and what it invalidates |
| `run` | the autonomous loop |
| `status` | fleet status and top results |
| `leaderboard` | every experiment, ranked |
| `frontier` | non-dominated arms and open threads |
| `report` | one experiment, or a list |
| `digest` | summarise a window; `--send` to notify |
| `dashboard` | a self-contained HTML page |
| `registry` | `stats`, `export`, `conflicts` |
| `steer` | leave a durable instruction for a node |
| `publish` | push results to the code store |
| `rules` | the methodology, and what it cost |
| `gc` | remove worktrees for finished experiments |
| `demo` | a full simulated cycle with artifacts |
| `selftest` | run the test suite |

`av2-agent` is installed as an alias for `av2ra`.
