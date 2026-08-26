"""The command line. Every command does what it says; none of them print theatre.

The draft protocol this system replaces specified a CLI whose ``status`` command
printed a hard-coded table and whose ``study-codebase`` command printed a count
of files it had not read. Commands here either do the work or say why they
cannot, and anything that reports a number reports the conditions it was
measured under alongside it.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

from . import __version__
from .app import App, build_app
from .core.config import ConfigError, load_config
from .knowledge import lessons as lessons_mod
from .report import dashboard as dashboard_mod
from .report import experiment as experiment_report
from .report import leaderboard as leaderboard_mod
from .util.io import ensure_dir, write_text_atomic

EXIT_OK, EXIT_FAIL, EXIT_ATTENTION = 0, 1, 2


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------


def cmd_init(app: App, args) -> int:
  from .util import yamlish

  ensure_dir(app.workspace)
  for name in ("experiments", "worktrees", "buildcache", "knowledge", "reports", "logs"):
    ensure_dir(app.path(name))
  local = app.path("config.yaml")
  if not os.path.exists(local) or args.force:
    write_text_atomic(
        local,
        "# Local overrides for this machine. Never committed.\n"
        + yamlish.dumps({
            "project": {"workspace": app.workspace, "node_id": app.node_id},
            "measure": {"workers": max(1, (os.cpu_count() or 4) // 2)},
        }),
    )
  print(f"workspace ready at {app.workspace}")
  print(f"  site        : {app.config.site}")
  print(f"  avm repo    : {app.avm_repo}")
  print(f"  providers   : {app.providers.describe()}")
  print(f"  ctc backend : {app.ctc.name}")
  print(f"  model       : {app.llm.name}")
  print("\nnext: 'av2ra doctor', then 'av2ra ingest', then 'av2ra run --ticks 1'")
  return EXIT_OK


def cmd_doctor(app: App, args) -> int:
  statuses = app.providers.health.check()
  print("=" * 78)
  print("AV2 RESEARCH AGENT HEALTH")
  print("=" * 78)
  for status in statuses:
    print(status)

  repo_ok = os.path.isdir(os.path.join(app.avm_repo, ".git"))
  print(
      f"[{'OK' if repo_ok else 'ATTENTION'}] avm repo: {app.avm_repo}"
      + ("" if repo_ok else "\n         -> run 'av2ra bootstrap'")
  )
  if repo_ok:
    try:
      report = app.basewatch.check(fetch=not args.offline)
      print(f"[{'OK' if not report.moved else 'ATTENTION'}] anchor: {report.summary}")
      if report.moved:
        print(
            "         -> every recorded measurement is scoped to the old base. "
            "Run 'av2ra base check' for the overlap set."
        )
    except Exception as exc:
      print(f"[ATTENTION] anchor: could not resolve ({exc})")

  encoder = app.config.expand("measure.encoder", "")
  if encoder:
    from .measure.encode import probe_encoder

    probe = probe_encoder(encoder)
    ok = probe["exists"] and probe["runs"] and probe["has_qp"]
    print(f"[{'OK' if ok else 'ATTENTION'}] encoder: {encoder} {probe}")
    if probe["exists"] and not probe["has_qp"]:
      print(
          "         -> this binary does not accept --qp. CTC 2.0+ uses --qp, not "
          "--cq-level; check you are pointing at avmenc from this tree."
      )

  screen = app.clip_set("screen")
  missing = [c.name for c in screen.clips if not os.path.exists(c.path)]
  print(
      f"[{'OK' if not missing else 'ATTENTION'}] screening clips: "
      f"{len(screen.clips) - len(missing)}/{len(screen.clips)} present"
      + (f"\n         -> missing: {', '.join(missing[:6])}" if missing else "")
  )
  print(f"[INFO] model client: {app.llm.name}")
  print(f"[INFO] ctc backend : {app.ctc.name}")
  blocking = [s for s in statuses if not s.ok]
  return EXIT_ATTENTION if (blocking or missing or not repo_ok) else EXIT_OK


def cmd_bootstrap(app: App, args) -> int:
  from .util import proc

  origin = app.config.get("git.origin", "https://github.com/AOMediaCodec/avm.git")
  anchor = app.config.get("git.anchor_branch", "av2-enc")
  remote_ref = app.config.get("git.anchor_remote", "origin/av2-enc")
  repo = app.avm_repo
  if not os.path.isdir(os.path.join(repo, ".git")):
    ensure_dir(os.path.dirname(repo) or ".")
    print(f"cloning {origin} -> {repo}")
    if not proc.run(["git", "clone", origin, repo], timeout=7200).ok:
      print("clone failed", file=sys.stderr)
      return EXIT_FAIL
  proc.run(["git", "remote", "set-url", "origin", origin], cwd=repo)
  upstream = app.config.get("git.upstream", "")
  if upstream:
    remotes = proc.run(["git", "remote"], cwd=repo).stdout.split()
    verb = "set-url" if "upstream" in remotes else "add"
    proc.run(["git", "remote", verb, "upstream", upstream], cwd=repo)
  proc.run(["git", "fetch", "origin"], cwd=repo, timeout=3600)
  checkout = proc.run(["git", "checkout", "-B", anchor, remote_ref], cwd=repo)
  if not checkout.ok:
    print(f"could not check out {remote_ref}: {checkout.tail(6)}", file=sys.stderr)
    return EXIT_FAIL
  sha = proc.run(["git", "rev-parse", "HEAD"], cwd=repo).stdout.strip()
  app.basewatch.record_base(sha, note="bootstrap")
  print(f"{repo} is on {anchor} at {sha[:12]}")
  return EXIT_OK


def cmd_ingest(app: App, args) -> int:
  codemap = app.codemap
  print(f"code map: {json.dumps({k: v for k, v in codemap.stats().items() if k != 'subsystems'})}")
  print(f"  subsystems: {codemap.stats()['subsystems']}")
  corpus = app.corpus
  if corpus:
    stats = corpus.stats()
    print(f"prior research: {stats['attempts']} attempts, {stats['lessons']} decisions")
    print(f"  by subsystem: {stats['by_subsystem']}")
    hit = stats["hit_rate"]
    print(
        f"  historical hit rate: {hit['wins']}/{hit['resolved']} resolved attempts "
        f"cleared both class bars ({hit['rate'] * 100:.0f}%)"
    )
  else:
    print(
        "prior research: not configured. Set knowledge.prior_research to a "
        "checkout of the shared results repository -- without it the agent "
        "cannot check an idea against what has already failed."
    )
  profile = app.profile
  print(
      f"profile: {profile.source} at preset {profile.preset}, "
      f"{len(profile.entries)} entries" if profile else
      "profile: none. Run 'av2ra profile' -- ideation without a profile optimises "
      "wherever the training data suggests, not where the time is."
  )
  return EXIT_OK


def cmd_profile(app: App, args) -> int:
  from .knowledge import profiles as profiles_mod

  target = app.path("knowledge", f"profile_s{args.preset}.json")
  if args.import_path:
    with open(args.import_path, "r", encoding="utf-8", errors="replace") as handle:
      parsed = profiles_mod.parse(handle.read(), preset=args.preset)
    parsed = profiles_mod.attribute(parsed, app.codemap)
    ensure_dir(os.path.dirname(target))
    parsed.save(target)
    print(f"imported {len(parsed.entries)} entries ({parsed.source}) -> {target}")
  profile = profiles_mod.Profile.load(target)
  if not profile:
    print(
        "no profile available. Import one with --import <callgrind or perf "
        "report>, or run a profiling encode on a machine with valgrind or perf.",
        file=sys.stderr,
    )
    return EXIT_FAIL
  profile = profiles_mod.attribute(profile, app.codemap)
  print(f"profile at preset {profile.preset} ({profile.source})")
  print(f"{'share%':>8}  {'function':<40} subsystem")
  for entry in profile.top(args.top):
    print(f"{entry.share_pct:8.2f}  {entry.function:<40} {entry.subsystem}")
  print("\nby subsystem:")
  for name, share in profile.by_subsystem().items():
    print(f"  {share:6.2f}%  {name}")
  touched = set()
  if app.corpus:
    for attempt in app.corpus.attempts:
      touched.update(attempt.touched_functions)
  uncovered = profile.uncovered(touched, min_share=1.0)
  if uncovered:
    print("\nhot functions no prior experiment has touched:")
    for entry in uncovered[:12]:
      print(f"  {entry.share_pct:6.2f}%  {entry.function} ({entry.subsystem})")
  return EXIT_OK


def cmd_base(app: App, args) -> int:
  report = app.basewatch.check(fetch=not args.offline)
  print(report.summary)
  if not report.moved:
    return EXIT_OK
  print(f"\nupstream commits since the recorded base ({len(report.commit_subjects)}):")
  for subject in report.commit_subjects[:15]:
    print(f"  {subject}")
  affected = 0
  for experiment in app.registry.query(limit=300):
    if not experiment.patch.diff_text:
      continue
    risk = app.basewatch.assess_patch(
        experiment.id, experiment.patch.diff_text, experiment.patch.files,
        experiment.patch.touched_functions, report,
    )
    if risk.verdict != "ok":
      affected += 1
      print(f"\n  {experiment.id} [{risk.applies}] -> {risk.verdict}")
      print(f"      {risk.explanation}")
  print(
      f"\n{affected} experiment(s) need re-validation. A clean apply is not "
      "evidence that a patch still does what it was measured doing."
  )
  if args.accept:
    invalidated = app.registry.mark_stale(
        base_sha=report.old_sha, reason="anchor moved"
    )
    app.basewatch.record_base(report.new_sha, note="accepted by operator")
    print(f"marked {invalidated} measurement(s) stale and moved the base pointer")
  return EXIT_ATTENTION


def cmd_run(app: App, args) -> int:
  from .orchestrate.fleet import NodeState

  loop = app.loop
  node = NodeState(
      node_id=app.node_id, cpus=os.cpu_count() or 0,
      workers=int(app.config.get("measure.workers", 2)), version=__version__,
      base_sha=loop.inputs.base_sha,
  )
  drift_check = None
  if not args.ignore_drift:
    def drift_check():
      try:
        return app.basewatch.check(fetch=False).moved
      except Exception:
        return False

  for index in range(args.ticks):
    instruction = app.fleet.pending_steer(app.node_id)
    if instruction:
      print(f"[steer] {instruction['action']}: {instruction['reason']}")
      if instruction["action"] == "abort":
        break
    result = loop.tick(base_drifted=bool(drift_check and drift_check()))
    node.status = result.action
    node.experiment_id = result.experiment_id
    experiment = app.registry.get(result.experiment_id) if result.experiment_id else None
    node.subsystem = experiment.hypothesis.subsystem if experiment else ""
    node.preset = app.preset
    node.last_tick = time.time()
    app.fleet.heartbeat(node)
    print(f"[{index + 1}/{args.ticks}] {result}")
    if experiment:
      experiment_report.write(experiment, app.path("reports"))
    if result.action == "halt":
      print("\nhalted. Resolve the anchor drift, then re-run.", file=sys.stderr)
      return EXIT_ATTENTION
    if args.until_idle and result.action == "idle":
      break
  app.registry.export_csv(app.path("registry.csv"))
  return EXIT_OK


def cmd_status(app: App, args) -> int:
  stats = app.registry.stats()
  health = [str(s) for s in app.providers.health.check() if not s.ok]
  base = app.recorded_base
  extra = [
      f"experiments {stats['experiments']}",
      f"CTC slots {stats['ctc_slots']:.0f}",
  ]
  if stats["conflicts"]:
    extra.append(f"CONFLICTS {stats['conflicts']}")
  print(app.fleet.render_status(base_sha=base, extra=extra))
  if health:
    print("\nATTENTION")
    for line in health:
      print(f"  {line}")
  print()
  print(leaderboard_mod.leaderboard(app.registry, limit=12, current_base=base))
  return EXIT_OK


def cmd_leaderboard(app: App, args) -> int:
  print(leaderboard_mod.leaderboard(
      app.registry, limit=args.limit, current_base=app.recorded_base
  ))
  return EXIT_OK


def cmd_frontier(app: App, args) -> int:
  print(leaderboard_mod.promotion_frontier(
      app.registry, current_base=app.recorded_base
  ))
  return EXIT_OK


def cmd_report(app: App, args) -> int:
  if args.experiment_id in ("list", None):
    for experiment in app.registry.query(limit=args.limit, verdict=args.verdict):
      print(
          f"{experiment.id:<9} {experiment.verdict.value:<17} "
          f"{experiment.hypothesis.title[:70]}"
      )
    return EXIT_OK
  experiment = app.registry.get(args.experiment_id)
  if not experiment:
    print(f"no such experiment: {args.experiment_id}", file=sys.stderr)
    return EXIT_FAIL
  text = experiment_report.render(experiment, include_patch=args.patch)
  if args.write:
    path = experiment_report.write(experiment, app.path("reports"))
    print(f"written to {path}")
  else:
    print(text)
  return EXIT_OK


def cmd_digest(app: App, args) -> int:
  data = leaderboard_mod.collect_digest(
      app.registry, app.ledger, hours=args.hours,
      health_lines=[str(s) for s in app.providers.health.check() if not s.ok],
  )
  text = leaderboard_mod.render_digest(data)
  print(text)
  if args.send:
    ok = app.providers.notifier.send(
        f"AV2 research digest ({data.window_hours:.0f}h): "
        f"{len(data.promoted)} cleared, {len(data.killed)} killed",
        text,
    )
    print(f"\n[{'sent' if ok else 'delivery failed'}]")
  return EXIT_OK


def cmd_dashboard(app: App, args) -> int:
  html = dashboard_mod.render(
      app.registry, base_sha=app.recorded_base,
      fleet_status=app.fleet.render_status(base_sha=app.recorded_base),
  )
  path = args.out or app.path("reports", "dashboard.html")
  write_text_atomic(path, html)
  print(path)
  return EXIT_OK


def cmd_registry(app: App, args) -> int:
  if args.action == "export":
    path = app.registry.export_csv(args.out or app.path("registry.csv"))
    print(path)
    return EXIT_OK
  if args.action == "conflicts":
    conflicts = app.registry.conflicting_measurements()
    if not conflicts:
      print("no measurement conflicts")
      return EXIT_OK
    for conflict in conflicts:
      key = conflict["key"]
      print(
          f"{key[0]} {key[1]} {key[2]} preset {key[3]} {key[4]}: "
          f"spread {conflict['spread']:.4f} across {len(conflict['rows'])} rows"
      )
      for row in conflict["rows"]:
        print(
            f"    {row['value']:+.4f} backend={row['backend']} job={row['job_id']} "
            f"base={row['base_sha'][:10]} ts={time.strftime('%Y-%m-%d %H:%M', time.localtime(row['ts']))}"
        )
    return EXIT_ATTENTION
  print(json.dumps(app.registry.stats(), indent=2))
  return EXIT_OK


def cmd_steer(app: App, args) -> int:
  app.fleet.steer(args.node, args.action, args.reason)
  print(f"instruction left for {args.node}: {args.action} ({args.reason})")
  return EXIT_OK


def cmd_publish(app: App, args) -> int:
  reports = app.path("reports")
  if not os.path.isdir(reports):
    print("nothing to publish", file=sys.stderr)
    return EXIT_FAIL
  app.registry.export_csv(app.path("reports", "registry.csv"))
  location = app.providers.code.publish(
      reports, args.remote_path, args.message or f"av2ra results {time.strftime('%Y-%m-%d')}"
  )
  print(f"published to {location} via {app.providers.code.describe()}")
  return EXIT_OK


def cmd_rules(app: App, args) -> int:
  for rule in lessons_mod.RULES:
    if args.stage and args.stage not in rule.applies_to:
      continue
    marker = f"enforced by {rule.enforced_by}" if rule.enforced_by else "prior only"
    print(f"{rule.id}  {rule.title}   [{marker}]")
    print(f"      {rule.statement}")
    if args.evidence:
      print(f"      evidence: {rule.evidence}")
    print()
  return EXIT_OK


def cmd_gc(app: App, args) -> int:
  removed = []
  for path in app.worktrees.list_worktrees():
    name = os.path.basename(path)
    if not name.startswith("exp_"):
      continue
    experiment = app.registry.get(name[4:])
    if experiment and experiment.is_alive:
      continue
    app.worktrees.remove(name[4:])
    removed.append(name)
  print(f"removed {len(removed)} finished worktree(s): {', '.join(removed) or 'none'}")
  return EXIT_OK


def cmd_demo(app: App, args) -> int:
  """Run the full cycle against the simulator and leave the artifacts behind."""
  from .sim.demo import run_demo

  workspace = args.workspace_dir or app.path("demo")
  loop = run_demo(workspace, ticks=args.ticks)
  print()
  print(leaderboard_mod.leaderboard(loop.registry, limit=20))
  print()
  print(leaderboard_mod.promotion_frontier(loop.registry))
  reports = os.path.join(workspace, "reports")
  for experiment in loop.registry.query(limit=50):
    experiment_report.write(experiment, reports)
  loop.registry.export_csv(os.path.join(workspace, "registry.csv"))
  html = dashboard_mod.render(loop.registry, title="AV2 research agent (demo)")
  write_text_atomic(os.path.join(reports, "dashboard.html"), html)
  print()
  print(f"artifacts written under {workspace}:")
  print("  registry.csv           the results ledger")
  print("  ledger/DECISIONS.md    why each experiment was killed or kept")
  print("  reports/<id>/report.md one report per experiment")
  print("  reports/dashboard.html a self-contained status page")
  return EXIT_OK


def cmd_selftest(app: App, args) -> int:
  """Run the full loop against the simulated encoder and cluster."""
  import subprocess

  here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
  result = subprocess.run(
      [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
      cwd=here, capture_output=True, text=True,
  )
  print(result.stdout[-8000:])
  print(result.stderr[-8000:], file=sys.stderr)
  return EXIT_OK if result.returncode == 0 else EXIT_FAIL


# --------------------------------------------------------------------------
# parser
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
  parser = argparse.ArgumentParser(
      prog="av2ra",
      description="Autonomous research agent for the AV2 (libavm) encoder.",
  )
  parser.add_argument("--site", default=None, help="site profile (local, google, sim)")
  parser.add_argument("--workspace", default=None)
  parser.add_argument("--node", default=None, help="node id for fleet reporting")
  parser.add_argument("--set", dest="overrides", action="append", default=[],
                      metavar="key.path=value")
  parser.add_argument("--version", action="version", version=f"av2ra {__version__}")
  sub = parser.add_subparsers(dest="command")

  init = sub.add_parser("init", help="create the workspace and local config")
  init.add_argument("--force", action="store_true")
  init.set_defaults(func=cmd_init)

  doctor = sub.add_parser("doctor", help="check credentials, tools, repo, clips")
  doctor.add_argument("--offline", action="store_true", help="skip network fetches")
  doctor.set_defaults(func=cmd_doctor)

  bootstrap = sub.add_parser("bootstrap", help="clone or align the AVM checkout")
  bootstrap.set_defaults(func=cmd_bootstrap)

  ingest = sub.add_parser("ingest", help="build the code map and read prior research")
  ingest.set_defaults(func=cmd_ingest)

  profile = sub.add_parser("profile", help="show or import an encoder profile")
  profile.add_argument("--import", dest="import_path", default=None)
  profile.add_argument("--preset", type=int, default=4)
  profile.add_argument("--top", type=int, default=20)
  profile.set_defaults(func=cmd_profile)

  base = sub.add_parser("base", help="check whether the anchor has moved")
  base.add_argument("action", nargs="?", default="check", choices=["check"])
  base.add_argument("--offline", action="store_true")
  base.add_argument("--accept", action="store_true",
                    help="move the base pointer and mark old measurements stale")
  base.set_defaults(func=cmd_base)

  run = sub.add_parser("run", help="run the autonomous research loop")
  run.add_argument("--ticks", type=int, default=10)
  run.add_argument("--until-idle", action="store_true")
  run.add_argument("--ignore-drift", action="store_true")
  run.set_defaults(func=cmd_run)

  status = sub.add_parser("status", help="fleet status and top results")
  status.set_defaults(func=cmd_status)

  board = sub.add_parser("leaderboard", help="all experiments, ranked")
  board.add_argument("--limit", type=int, default=40)
  board.set_defaults(func=cmd_leaderboard)

  frontier = sub.add_parser("frontier", help="non-dominated arms and open threads")
  frontier.set_defaults(func=cmd_frontier)

  report = sub.add_parser("report", help="render one experiment report")
  report.add_argument("experiment_id", nargs="?", default="list")
  report.add_argument("--verdict", default=None)
  report.add_argument("--limit", type=int, default=50)
  report.add_argument("--write", action="store_true")
  report.add_argument("--patch", action="store_true", help="include the diff")
  report.set_defaults(func=cmd_report)

  digest = sub.add_parser("digest", help="summarise a time window")
  digest.add_argument("--hours", type=float, default=24.0)
  digest.add_argument("--send", action="store_true")
  digest.set_defaults(func=cmd_digest)

  dashboard = sub.add_parser("dashboard", help="write a self-contained HTML status page")
  dashboard.add_argument("--out", default=None)
  dashboard.set_defaults(func=cmd_dashboard)

  registry = sub.add_parser("registry", help="export or audit the results ledger")
  registry.add_argument("action", nargs="?", default="stats",
                        choices=["stats", "export", "conflicts"])
  registry.add_argument("--out", default=None)
  registry.set_defaults(func=cmd_registry)

  steer = sub.add_parser("steer", help="leave an instruction for a node")
  steer.add_argument("--node", required=True)
  steer.add_argument("--action", required=True, choices=["abort", "pause", "resume"])
  steer.add_argument("--reason", required=True)
  steer.set_defaults(func=cmd_steer)

  publish = sub.add_parser("publish", help="push reports to the configured code store")
  publish.add_argument("--remote-path", default="research_agent/results")
  publish.add_argument("--message", default=None)
  publish.set_defaults(func=cmd_publish)

  rules = sub.add_parser("rules", help="the methodology rules and where they came from")
  rules.add_argument("--stage", default=None,
                     choices=["ideation", "implementation", "measurement", "analysis", "planning"])
  rules.add_argument("--evidence", action="store_true")
  rules.set_defaults(func=cmd_rules)

  gc = sub.add_parser("gc", help="remove worktrees for finished experiments")
  gc.set_defaults(func=cmd_gc)

  demo = sub.add_parser(
      "demo", help="run a complete simulated research cycle and write its artifacts"
  )
  demo.add_argument("--ticks", type=int, default=14)
  demo.add_argument("--dir", dest="workspace_dir", default=None)
  demo.set_defaults(func=cmd_demo)

  selftest = sub.add_parser("selftest", help="run the full loop against the simulator")
  selftest.set_defaults(func=cmd_selftest)
  return parser


def main(argv: list[str] | None = None) -> int:
  parser = build_parser()
  args = parser.parse_args(argv)
  if not getattr(args, "func", None):
    parser.print_help()
    return EXIT_OK
  try:
    config = load_config(
        site=args.site, workspace=args.workspace, overrides=args.overrides
    )
  except ConfigError as exc:
    print(f"configuration error: {exc}", file=sys.stderr)
    return EXIT_FAIL
  app = build_app(config, node_id=args.node or "")
  try:
    return args.func(app, args)
  except KeyboardInterrupt:
    print("\ninterrupted; state is durable, re-run to continue", file=sys.stderr)
    return EXIT_FAIL


if __name__ == "__main__":
  sys.exit(main())
