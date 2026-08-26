# Deploying on Google infrastructure

Only three things here depend on Google-internal infrastructure: **data
storage, code storage, and CTC runs.** Everything else runs identically on a
laptop. Two files carry that dependency —
`av2ra/providers/google.py` and `av2ra/ctc/eda.py` — reached only through the
interfaces in `providers/base.py` and `ctc/contract.py`, and selected by two
lazily-imported branches in `providers/factory.py`. Deleting them leaves a
working system.

```
$ grep -rln 'fileutil\|gcertstatus\|sendgmr\|kick_off_av2ctc' av2ra/ --include='*.py'
av2ra/core/models.py          # a docstring saying the core must NOT name these
av2ra/ctc/eda.py              # the Cloud EDA back end
av2ra/providers/base.py       # docstrings describing the boundary
av2ra/providers/factory.py    # maps a config string to an implementation
av2ra/providers/google.py     # CNS, CitC, gcertstatus, sendgmr
```

---

## What is used, and why each is the sanctioned choice

| Need | Tool | Why |
|---|---|---|
| artifact + lease storage | `fileutil` on CNS | the supported CLI; avoids linking a client library into a process that must also run outside google3 |
| credential health | `gcertstatus` | the only supported way to read LOAS2 validity |
| code publication | `g4` into a CitC client, or git into the shared repo | mailing a change list stays a human action |
| notification | `sendgmr` | delivery from a Cloudtop without an SMTP setup |
| CTC runs | `kick_off_av2ctc_eda.sh` + `compare_eda_runs.py` | the existing internal path; wrapped, not replaced |

---

## Setup

### 1. Fill in the site profile

`config/sites/google.yaml` has `REQUIRED` markers. `av2ra doctor` names what is
missing rather than failing four hours into a pass.

```yaml
infra:
  cns_prefix: "/cns/<cell>/home/<you>/av2ra"
  notify_recipient: "<you>@google.com"
ctc:
  kickoff_script: "/google/src/head/depot/google3/experimental/users/<owner>/eda/kick_off_av2ctc_eda.sh"
  compare_script: "/google/src/head/depot/google3/experimental/users/<owner>/aom_tools/compare_eda_runs.py"
knowledge:
  prior_research: "~/work/av2/agent/prior_research/avm-patches"
clips:
  local_cache: "~/clips/av2_a2"
  cns_source: "/cns/<cell>/home/on2-prod/av2ctc/a2_2k"
```

### 2. Bring the node up

```bash
gcert
av2ra --site=google init
av2ra --site=google bootstrap
av2ra --site=google doctor
```

### 3. Sync clips

The screening set is data storage — sanctioned. `sync_clips_from_cns` fetches
the configured sequences to local NVMe; they are hard-linked into worktrees
rather than copied, because a copy per worktree fills the disk the builds need.

### 4. Seed knowledge

```bash
av2ra --site=google ingest
av2ra --site=google profile --import <callgrind-output> --preset=<target>
```

---

## The CTC contract

The core hands the back end a `CtcRequest` — experiment id, base SHA, patch
reference, presets, test sets, configs, and **the question the run answers** —
and receives a `CtcResult`. Nothing in that exchange names a cluster, a
scheduler or a shell script. The EDA back end translates.

Three corrections to the draft invocation are implemented, each of which costs
real cluster time if left alone:

**Per-class frame counts.** The draft passes one `--frame_count=33`. Every
report in the corpus runs A1 at 17 frames and A2 at 33. One scalar cannot
express that, and 33 everywhere doubles the cost of the 4K half of every round.
`frames_for()` derives it and the back end submits one job set per class.

**Anchor reuse.** An anchor job set for a given (base SHA, preset, test set) is
a durable artifact; the corpus reuses the same invocation UUIDs across rounds.
Recorded in `~/.av2ra/eda/anchors.json` and reused, halving every round after
the first on a given base.

**Arm identity.** Each arm is recorded as (anchor SHA, patch SHA, preset, test
set, invocation UUID) — the tuple the corpus's own traceability matrices use.
Without it two rounds' numbers cannot be reconciled, which is how that project
ended with two different CTC results for the same three patches.

### Verify on first use

Two parsing assumptions need confirming against the live tools:

1. **Invocation id extraction.** `EdaBackend._kick_off` takes the first UUID in
   the script's output. If the format differs, it is a one-line change.
2. **Comparison output.** `parse_comparison` handles the pipe-table shape seen
   in the shared reports and the CSV shape from `--csv=1`. A third shape needs a
   `METRIC_ALIASES` entry.

Do this during the first supervised round (phase 8.6), while you can compare the
parsed `CtcResult` against the raw text.

---

## Leases on CNS

`CnsBlobStore.acquire_lease` is **advisory**, and says so in its docstring.
`fileutil` exposes no compare-and-swap, so the implementation writes and reads
back; the race window is a round trip.

That is acceptable here because a lost race costs duplicated local work and is
caught at registry insert time by id collision, whereas a *missed* lease would
deadlock the fleet. If you need a hard lock, point the coordination root at a
filesystem supporting `O_EXCL` — an NFS home directory works — and use the local
store for leases while keeping CNS for artifacts.

---

## Publishing and IP hygiene

`av2ra publish` copies reports and the registry export into the configured code
store. If that store is a public GitHub repository, note what a report contains:

- **Always:** the patch diff, measured numbers, the anchor SHA, the reasoning.
- **Configuration-dependent:** CNS paths, invocation UUIDs, internal script
  paths, hostnames — these appear in provenance fields and job ids.

Two mitigations, neither automatic:

1. Point `infra.code` at a CitC client for internal publication and use git only
   for the artifacts you have read.
2. Before a public push, review the reports. Job ids and CNS prefixes are the
   fields to check.

The system deliberately does not scrub automatically. A scrubber that silently
removes provenance produces reports whose numbers cannot be traced, which is the
failure mode the registry exists to prevent.

---

## Cost model

Measure it on your machine before planning around it (phase 8.2). What is known:

- On a 2.8 GHz Xeon with a `-O3` AVX2 build, this encoder needs roughly
  **14 s/frame at 176×144, `--cpu-used=4`**. 1080p is 82× the pixels.
- A CTC round is **hours to a day**; the corpus observed 6–18 job sets per round.
- Arms within a round are independent, so latency is set by the slowest arm.
  **Prefer few rounds with many arms.**

The draft guide's "12 encodes in 1.5 minutes" is not achievable on any machine
this encoder has been measured on, and a plan built on it will discover that on
its first day.
