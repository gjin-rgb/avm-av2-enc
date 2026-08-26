"""Wiring: turn a configuration into a working system, once, in one place.

Every command needs some subset of registry, providers, code map, corpus,
profile, worktrees, builder, CTC back end and loop. Constructing them ad hoc per
command is how a CLI ends up with six slightly different notions of where the
workspace is. :class:`App` builds each piece lazily and caches it, so a command
that only needs the registry does not pay for a code map, and two commands that
both need one share it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import cached_property

from .agent.analyst import Analyst
from .agent.ideation import IdeationInputs, Ideator
from .agent.implementer import Implementer
from .agent.lenses import LensStats
from .agent.llm import build_client
from .agent.loop import LoopConfig, ResearchLoop
from .agent.planner import Governor, Planner
from .buildkit.builder import Builder
from .buildkit.worktree import WorktreeManager
from .core.basewatch import BaseWatch
from .core.config import Config
from .core.ledger import Ledger
from .core.registry import Registry
from .integrity.policy import PatchPolicy
from .knowledge import codemap as codemap_mod
from .knowledge import corpus as corpus_mod
from .knowledge import profiles as profiles_mod
from .knowledge.lessons import LessonStore
from .measure.encode import ClipSpec
from .measure.screen import ClipSet
from .orchestrate.fleet import Fleet
from .providers.factory import build_ctc_backend, build_providers
from .util import log
from .util.io import ensure_dir


@dataclass
class App:
  config: Config
  node_id: str = ""
  _cache: dict = field(default_factory=dict)

  def __post_init__(self) -> None:
    self.node_id = self.node_id or self.config.get("project.node_id", "local")
    ensure_dir(self.workspace)
    log.configure(self.workspace, node_id=self.node_id,
                  verbose=bool(self.config.get("project.verbose", False)))

  # -- paths ---------------------------------------------------------------

  @property
  def workspace(self) -> str:
    return self.config.workspace

  @property
  def avm_repo(self) -> str:
    return self.config.avm_repo

  def path(self, *parts: str) -> str:
    return os.path.join(self.workspace, *parts)

  # -- core services -------------------------------------------------------

  @cached_property
  def registry(self) -> Registry:
    return Registry(self.path("registry.sqlite"))

  @cached_property
  def ledger(self) -> Ledger:
    return Ledger(self.path("ledger"))

  @cached_property
  def providers(self):
    return build_providers(self.config)

  @cached_property
  def fleet(self) -> Fleet:
    return Fleet(self.providers.blobs)

  @cached_property
  def worktrees(self) -> WorktreeManager:
    return WorktreeManager(
        self.avm_repo, self.path("worktrees"),
        min_free_gib=float(self.config.get("infra.min_free_gib", 15.0)),
    )

  @cached_property
  def builder(self) -> Builder:
    return Builder(
        self.path("buildcache"), keep=int(self.config.get("build.cache_keep", 8))
    )

  @property
  def recorded_base(self) -> str:
    """The anchor a report should cite, readable without a git checkout.

    Reporting commands run on machines that have the workspace but not the
    encoder tree -- a laptop reading yesterday's results, for instance -- so
    they must not fail merely because ``avm_repo`` is absent.
    """
    from .util.io import read_json

    return (read_json(self.path("base.json"), {}) or {}).get("base_sha", "")

  @cached_property
  def basewatch(self) -> BaseWatch:
    return BaseWatch(
        self.worktrees, self.path("base.json"),
        self.config.get("git.anchor_remote", "origin/av2-enc"),
    )

  # -- knowledge -----------------------------------------------------------

  @cached_property
  def codemap(self):
    path = self.path("knowledge", "codemap.json")
    base = ""
    try:
      base = self.worktrees.resolve(self.config.get("git.anchor_remote", "HEAD"))
    except Exception:
      base = ""
    cached = codemap_mod.CodeMap.load(path)
    if cached and (not base or cached.base_sha == base):
      return cached
    built = codemap_mod.build(self.avm_repo, base_sha=base)
    ensure_dir(os.path.dirname(path))
    built.save(path)
    return built

  @cached_property
  def corpus(self):
    root = self.config.expand("knowledge.prior_research", "")
    if not root or not os.path.isdir(root):
      return None
    return corpus_mod.ingest(root)

  @cached_property
  def profile(self):
    path = self.path("knowledge", f"profile_s{self.preset}.json")
    loaded = profiles_mod.Profile.load(path)
    if loaded:
      return profiles_mod.attribute(loaded, self.codemap)
    imported = self.config.expand("knowledge.import_profile", "")
    if imported and os.path.exists(imported):
      with open(imported, "r", encoding="utf-8", errors="replace") as handle:
        parsed = profiles_mod.parse(handle.read(), preset=self.preset)
      parsed = profiles_mod.attribute(parsed, self.codemap)
      ensure_dir(os.path.dirname(path))
      parsed.save(path)
      return parsed
    return None

  @cached_property
  def lesson_store(self) -> LessonStore:
    store = LessonStore()
    for lesson in self.ledger.lessons():
      store.add(lesson)
    return store

  # -- configuration-derived values ---------------------------------------

  @property
  def preset(self) -> int:
    return int(self.config.get("measure.preset", 2))

  def clip_set(self, role: str) -> ClipSet:
    section = self.config.get(f"clips.{role}", {}) or {}
    directory = self.config.expand("clips.local_cache", "~/clips/av2")
    clips = []
    for entry in section.get("sequences", []) or []:
      clips.append(
          ClipSpec(
              name=entry.get("id") or entry.get("filename", "clip"),
              path=os.path.join(directory, entry.get("filename", "")),
              width=int(entry.get("width", 1920)),
              height=int(entry.get("height", 1080)),
              fps_num=int(entry.get("fps_num", 30)),
              fps_denom=int(entry.get("fps_denom", 1)),
              bit_depth=int(entry.get("bit_depth", 10)),
              file_class=entry.get("class", "A2"),
          )
      )
    return ClipSet(
        name=section.get("name", role), role=role, clips=clips,
        qps=[int(q) for q in section.get("qps", [160, 185, 210])],
        frames=int(section.get("frames", 17)),
        test_cfg=section.get("config", "RA"),
    )

  @cached_property
  def ctc(self):
    return build_ctc_backend(self.config)

  @cached_property
  def llm(self):
    return build_client(self.config)

  @cached_property
  def loop(self) -> ResearchLoop:
    governor = Governor(
        max_open_experiments=int(self.config.get("planner.max_open", 6)),
        max_per_subsystem_fraction=float(self.config.get("planner.max_subsystem_fraction", 0.4)),
        max_tuning_depth=int(self.config.get("planner.max_tuning_depth", 3)),
        ctc_slots_per_day=float(self.config.get("planner.ctc_slots_per_day", 4.0)),
        ctc_min_arms_per_round=int(self.config.get("planner.ctc_min_arms", 2)),
        require_holdout_before_ctc=bool(self.config.get("planner.require_holdout", True)),
    )
    policy = PatchPolicy(
        max_changed_files=int(self.config.get("policy.max_changed_files", 12)),
        max_changed_lines=int(self.config.get("policy.max_changed_lines", 1500)),
    )
    loop_config = LoopConfig(
        workspace=self.workspace,
        anchor_ref=self.config.get("git.anchor_remote", "origin/av2-enc"),
        preset=self.preset,
        reps=int(self.config.get("measure.reps", 2)),
        workers=int(self.config.get("measure.workers", 2)),
        cpus_per_worker=int(self.config.get("measure.cpus_per_worker", 2)),
        testsets=list(self.config.get("ctc.testsets", ["a1", "a2"])),
        configs=list(self.config.get("ctc.configs", ["ra"])),
        node_id=self.node_id,
        run_holdout=bool(self.config.get("measure.run_holdout", True)),
        run_null_arm_every=int(self.config.get("measure.null_arm_every", 10)),
        conformance_sample=int(self.config.get("measure.conformance_sample", 4)),
        acceptance_mode=self.config.get("measure.acceptance_mode", "conservative"),
        build_jobs=int(self.config.get("build.jobs", 0)),
    )
    inputs = IdeationInputs(
        codemap=self.codemap, profile=self.profile, corpus=self.corpus,
        lesson_store=self.lesson_store,
        base_sha=self.codemap.base_sha if self.codemap else "",
    )
    stats = LensStats()
    return ResearchLoop(
        config=loop_config, registry=self.registry, ledger=self.ledger,
        planner=Planner(self.registry, governor),
        ideator=Ideator(self.llm, stats=stats),
        implementer=Implementer(
            self.llm, self.worktrees, self.builder, policy=policy,
            max_attempts=int(self.config.get("agent.max_implementation_attempts", 3)),
            build_jobs=loop_config.build_jobs,
        ),
        analyst=Analyst(self.llm), worktrees=self.worktrees, builder=self.builder,
        ctc=self.ctc, inputs=inputs,
        screen_set=self.clip_set("screen"), holdout_set=self.clip_set("holdout"),
        lesson_store=self.lesson_store, lens_stats=stats,
        decoder=self.config.expand("measure.decoder", ""),
    )


def build_app(config: Config, *, node_id: str = "") -> App:
  return App(config=config, node_id=node_id)
