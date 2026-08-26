"""Builds the provider bundle and the CTC back end named by the site profile.

This is the only place that maps configuration strings to implementations, so
adding a site -- a different cluster, an object store, a new notifier -- is one
entry here plus one YAML file, and no change anywhere in the core.
"""

from __future__ import annotations

import os

from ..core.config import Config
from ..ctc.contract import CtcBackend
from .base import Providers
from .local import FileNotifier, GitCodeStore, LocalBlobStore, LocalHealth


class NullCodeStore:
  """Used when nothing is published: a run that only writes locally."""

  def publish(self, local_dir: str, remote_path: str, message: str) -> str:
    return local_dir

  def fetch(self, remote_path: str, local_dir: str) -> bool:
    return False

  def describe(self) -> str:
    return "none (results stay in the local workspace)"


def build_providers(config: Config) -> Providers:
  workspace = config.workspace
  kind = config.get("infra.blobs", "local")
  if kind == "cns":
    from .google import CnsBlobStore

    blobs = CnsBlobStore(
        config.require("infra.cns_prefix"),
        local_cache=config.get("infra.cns_local_cache"),
    )
  else:
    blobs = LocalBlobStore(config.get("infra.blob_root", os.path.join(workspace, "blobs")))

  code_kind = config.get("infra.code", "none")
  if code_kind == "citc":
    from .google import CitcCodeStore

    code = CitcCodeStore(
        config.require("infra.citc_client"), config.require("infra.citc_package")
    )
  elif code_kind == "git":
    code = GitCodeStore(
        config.require("infra.git_repo"),
        remote=config.get("infra.git_remote", "origin"),
        branch=config.get("infra.git_branch", "main"),
        push=bool(config.get("infra.git_push", False)),
        author=config.get("infra.git_author", "av2ra <av2ra@localhost>"),
    )
  else:
    code = NullCodeStore()

  notifier_kind = config.get("infra.notifier", "file")
  if notifier_kind == "sendgmr":
    from .google import SendgmrNotifier

    notifier = SendgmrNotifier(
        config.require("infra.notify_recipient"), config.get("infra.notify_sender", "")
    )
  else:
    notifier = FileNotifier(os.path.join(workspace, "notifications.log"))

  health_kind = config.get("infra.health", "local")
  if health_kind == "google":
    from .google import GoogleHealth

    health = GoogleHealth(
        workspace,
        warn_hours=float(config.get("infra.loas_warn_hours", 4.0)),
        clip_cache=config.get("clips.local_cache", ""),
        min_free_gib=float(config.get("infra.min_free_gib", 20.0)),
    )
  else:
    health = LocalHealth(
        workspace, min_free_gib=float(config.get("infra.min_free_gib", 20.0))
    )

  return Providers(
      blobs=blobs, code=code, notifier=notifier, health=health, site=config.site
  )


def build_ctc_backend(config: Config, **kwargs) -> CtcBackend:
  kind = config.get("ctc.backend", "sim")
  if kind == "eda":
    from ..ctc.eda import EdaBackend, EdaConfig

    return EdaBackend(
        EdaConfig(
            kickoff_script=config.require("ctc.kickoff_script"),
            compare_script=config.require("ctc.compare_script"),
            project=config.get("ctc.project", "blade"),
            timing_accuracy=config.get("ctc.timing_accuracy", "high"),
            chunk_size=int(config.get("ctc.chunk_size", 65)),
            crosscheck=int(config.get("ctc.crosscheck", 0)),
            extra_cmake_args=config.get(
                "ctc.extra_cmake_args", "-DCMAKE_POLICY_VERSION_MINIMUM=3.5"
            ),
            state_dir=os.path.join(config.workspace, "ctc-state"),
            anchor_reuse=bool(config.get("ctc.anchor_reuse", True)),
        )
    )
  if kind == "local":
    from ..ctc.local import LocalCtcBackend

    return LocalCtcBackend(
        clips_by_testset=kwargs.get("clips_by_testset", {}),
        anchor_encoder=kwargs.get("anchor_encoder", ""),
        candidate_encoder_for=kwargs.get("candidate_encoder_for", lambda _id: ""),
        workdir=os.path.join(config.workspace, "local-ctc"),
        qps=list(config.get("ctc.local_qps", [110, 160, 185, 235])),
        max_4k_clips=int(config.get("ctc.local_max_4k_clips", 2)),
    )
  from ..ctc.sim import SimCtcBackend

  return SimCtcBackend(latency_s=float(config.get("ctc.sim_latency_s", 0.0)))
