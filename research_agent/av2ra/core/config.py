"""Layered configuration: defaults, then a site profile, then local overrides.

A "site" is the one thing that changes when this system moves between
environments -- laptop, Cloudtop, CI. Everything infrastructure-specific lives
in ``config/sites/<site>.yaml`` and is reached only through the provider
interfaces; the core reads ``project``, ``measure``, ``policy`` and ``agent``
sections that mean the same thing everywhere.

Resolution order, last wins:
  1. ``config/default.yaml`` (shipped)
  2. ``config/sites/<site>.yaml`` (shipped; site chosen by ``--site`` or ``AV2RA_SITE``)
  3. ``$AV2RA_WORKSPACE/config.yaml`` (per-machine local overrides, never committed)
  4. ``AV2RA_*`` environment variables
  5. explicit ``--set key.path=value`` arguments
"""

from __future__ import annotations

import copy
import os
from dataclasses import dataclass
from typing import Any

from ..util import yamlish

PACKAGE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROJECT_ROOT = os.path.dirname(PACKAGE_ROOT)
CONFIG_DIR = os.path.join(PROJECT_ROOT, "config")


class ConfigError(RuntimeError):
  pass


def _deep_merge(base: dict, overlay: dict) -> dict:
  out = copy.deepcopy(base)
  for key, value in (overlay or {}).items():
    if isinstance(value, dict) and isinstance(out.get(key), dict):
      out[key] = _deep_merge(out[key], value)
    else:
      out[key] = copy.deepcopy(value)
  return out


@dataclass
class Config:
  data: dict
  site: str = "local"
  sources: list[str] = None  # type: ignore[assignment]

  def __post_init__(self) -> None:
    if self.sources is None:
      self.sources = []

  def get(self, path: str, default: Any = None) -> Any:
    """Fetch by dotted path: ``config.get("measure.reps", 2)``."""
    node: Any = self.data
    for part in path.split("."):
      if not isinstance(node, dict) or part not in node:
        return default
      node = node[part]
    return node

  def require(self, path: str) -> Any:
    value = self.get(path, _MISSING)
    if value is _MISSING:
      raise ConfigError(
          f"required configuration '{path}' is not set (site={self.site}; "
          f"sources: {', '.join(self.sources) or 'none'})"
      )
    return value

  def set(self, path: str, value: Any) -> None:
    node = self.data
    parts = path.split(".")
    for part in parts[:-1]:
      node = node.setdefault(part, {})
    node[parts[-1]] = value

  def section(self, name: str) -> dict:
    value = self.get(name, {})
    return value if isinstance(value, dict) else {}

  @property
  def workspace(self) -> str:
    return os.path.expanduser(self.get("project.workspace", "~/av2ra-workspace"))

  @property
  def avm_repo(self) -> str:
    return os.path.expanduser(self.get("project.avm_repo", "~/av2/avm"))

  def expand(self, path: str, default: str = "") -> str:
    return os.path.expanduser(str(self.get(path, default)))


_MISSING = object()


def _coerce_env(text: str) -> Any:
  lowered = text.lower()
  if lowered in ("true", "false"):
    return lowered == "true"
  try:
    return int(text)
  except ValueError:
    pass
  try:
    return float(text)
  except ValueError:
    return text


def load_config(
    *,
    site: str | None = None,
    workspace: str | None = None,
    overrides: list[str] | None = None,
    config_dir: str | None = None,
) -> Config:
  """Assemble the effective configuration and record where each part came from."""
  directory = config_dir or CONFIG_DIR
  site_name = site or os.environ.get("AV2RA_SITE", "local")
  sources: list[str] = []

  data: dict = {}
  default_path = os.path.join(directory, "default.yaml")
  if os.path.exists(default_path):
    data = _deep_merge(data, yamlish.load(default_path) or {})
    sources.append(default_path)

  site_path = os.path.join(directory, "sites", f"{site_name}.yaml")
  if os.path.exists(site_path):
    data = _deep_merge(data, yamlish.load(site_path) or {})
    sources.append(site_path)
  elif site:
    raise ConfigError(f"unknown site '{site_name}': {site_path} does not exist")

  workspace_dir = (
      workspace
      or os.environ.get("AV2RA_WORKSPACE")
      or data.get("project", {}).get("workspace")
      or "~/av2ra-workspace"
  )
  workspace_dir = os.path.expanduser(workspace_dir)
  data.setdefault("project", {})["workspace"] = workspace_dir

  local_path = os.path.join(workspace_dir, "config.yaml")
  if os.path.exists(local_path):
    data = _deep_merge(data, yamlish.load(local_path) or {})
    sources.append(local_path)

  # AV2RA_MEASURE__REPS=5 -> measure.reps = 5
  for key, value in sorted(os.environ.items()):
    if not key.startswith("AV2RA_") or key in ("AV2RA_SITE", "AV2RA_WORKSPACE"):
      continue
    path = key[len("AV2RA_"):].lower().replace("__", ".")
    node = data
    parts = path.split(".")
    for part in parts[:-1]:
      node = node.setdefault(part, {})
    node[parts[-1]] = _coerce_env(value)
    sources.append(f"env:{key}")

  config = Config(data=data, site=site_name, sources=sources)
  for override in overrides or []:
    if "=" not in override:
      raise ConfigError(f"--set expects key.path=value, got {override!r}")
    path, _, raw = override.partition("=")
    config.set(path.strip(), _coerce_env(raw.strip()))
    config.sources.append(f"cli:{path.strip()}")
  return config
