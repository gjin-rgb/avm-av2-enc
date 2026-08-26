"""Hermetic builds, fingerprinted so that two arms are comparable by construction.

A speed comparison is only about the patch if the two binaries differ *only*
because of the patch. That sounds obvious and is easy to get wrong: a debug
anchor against a release candidate, a stale CMake cache carrying a define the
other arm does not have, one arm built with an assertion-heavy configuration
because the last experiment left ``-DCMAKE_BUILD_TYPE=Debug`` in the directory.
Each of those produces a large, clean, entirely fake speedup.

So every build records a fingerprint over everything that can change the
generated code -- compiler identity and version, build type, the full sorted
CMake argument list, the preprocessor defines, and the source identity (base
SHA plus normalised patch hash). :func:`assert_comparable` refuses to let two
builds be compared unless their fingerprints differ only in source identity.

The anchor binary is cached by fingerprint. That is safe in a way that caching
a *timing* is not: a binary is a deterministic function of its inputs, so a
cache hit returns the identical artifact, while a cached timing returns a
measurement of a machine state that no longer exists.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
from dataclasses import dataclass, field

from ..util import log, proc
from ..util.io import ensure_dir, read_json, write_json_atomic

LOG = log.get("builder")

#: Build configuration that matches how CTC bitstreams are produced. Anything
#: that changes generated code belongs here, not in an ad-hoc argument list.
DEFAULT_CMAKE_ARGS = [
    "-DCMAKE_BUILD_TYPE=Release",
    "-DENABLE_TESTS=0",
    "-DENABLE_DOCS=0",
    "-DCMAKE_POLICY_VERSION_MINIMUM=3.5",
]

#: Targets to build. The binaries in this codebase are avmenc/avmdec; the
#: aomenc/aomdec names in the draft protocol are from libaom and do not exist
#: here, which is the kind of thing that should fail at configuration time
#: rather than four hours into a screening pass.
DEFAULT_TARGETS = ("avmenc", "avmdec")


class BuildError(RuntimeError):
  pass


@dataclass
class BuildSpec:
  source_dir: str
  build_dir: str
  cmake_args: list[str] = field(default_factory=lambda: list(DEFAULT_CMAKE_ARGS))
  # Preprocessor knobs the *patch itself* introduces, e.g.
  # -DTX_PART_STATIONARITY_MARGIN=2. These belong to the candidate's source
  # identity, not to the harness configuration: a threshold sweep is several
  # candidates against one anchor, so they must not make the arms
  # "incomparable". Integrity policy separately checks that every name here
  # actually appears in the patch, so an arm cannot smuggle in -DNDEBUG.
  patch_defines: dict[str, str] = field(default_factory=dict)
  targets: tuple[str, ...] = DEFAULT_TARGETS
  jobs: int = 0                  # 0 -> os.cpu_count()
  generator: str = "Ninja"
  use_ccache: bool = True
  source_identity: str = ""      # base SHA + patch hash; not a build *option*

  def effective_cmake_args(self) -> list[str]:
    args = list(self.cmake_args)
    if self.patch_defines:
      flags = " ".join(f"-D{k}={v}" for k, v in sorted(self.patch_defines.items()))
      args.append(f"-DCMAKE_C_FLAGS={flags}")
      args.append(f"-DCMAKE_CXX_FLAGS={flags}")
    if self.use_ccache and proc.have("ccache"):
      args.append("-DCMAKE_C_COMPILER_LAUNCHER=ccache")
      args.append("-DCMAKE_CXX_COMPILER_LAUNCHER=ccache")
    return sorted(set(args))

  def config_fingerprint(self) -> str:
    """Everything except the source. Two arms must agree on this exactly."""
    payload = json.dumps(
        {
            # Deliberately excludes patch_defines -- see the field comment.
            "cmake": sorted(set(self.cmake_args)),
            "ccache": bool(self.use_ccache and proc.have("ccache")),
            "targets": sorted(self.targets),
            "generator": self.generator,
            "toolchain": toolchain_identity(),
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:16]

  def source_fingerprint(self) -> str:
    payload = json.dumps(
        {
            "source": self.source_identity or "unknown",
            "patch_defines": dict(sorted(self.patch_defines.items())),
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:16]

  def fingerprint(self) -> str:
    return f"{self.config_fingerprint()}:{self.source_fingerprint()}"


@dataclass
class BuildResult:
  ok: bool
  build_dir: str
  encoder: str = ""
  decoder: str = ""
  fingerprint: str = ""
  config_fingerprint: str = ""
  wall_s: float = 0.0
  from_cache: bool = False
  log_path: str = ""
  error: str = ""
  warnings: list[str] = field(default_factory=list)


_TOOLCHAIN_CACHE: dict | None = None


def toolchain_identity() -> dict:
  """Compiler identity, cached per process. Part of the build fingerprint."""
  global _TOOLCHAIN_CACHE
  if _TOOLCHAIN_CACHE is not None:
    return _TOOLCHAIN_CACHE
  out = {}
  for name, argv in (
      ("cc", ["cc", "--version"]),
      ("cmake", ["cmake", "--version"]),
      ("ninja", ["ninja", "--version"]),
  ):
    result = proc.run(argv, timeout=60)
    first = (result.stdout or result.stderr or "").splitlines()
    out[name] = first[0].strip() if first else "missing"
  out["arch"] = os.uname().machine if hasattr(os, "uname") else "unknown"
  _TOOLCHAIN_CACHE = out
  return out


def assert_comparable(anchor: BuildResult, candidate: BuildResult) -> None:
  """Refuse to compare two arms built differently.

  This is a hard failure, not a warning. A comparison between arms with
  different build configurations is not a weaker result, it is a different
  experiment wearing the same label.
  """
  if anchor.config_fingerprint != candidate.config_fingerprint:
    raise BuildError(
        "anchor and candidate were built with different configurations "
        f"({anchor.config_fingerprint} vs {candidate.config_fingerprint}). "
        "Any speed difference between them is a measurement of the build, not "
        "of the patch."
    )


class Builder:
  """Builds encoder binaries, with a content-addressed cache for the anchor."""

  def __init__(self, cache_root: str, *, keep: int = 8):
    self.cache_root = ensure_dir(os.path.expanduser(cache_root))
    self.keep = keep

  # -- cache ---------------------------------------------------------------

  def _cache_dir(self, fingerprint: str) -> str:
    return os.path.join(self.cache_root, fingerprint.replace(":", "_"))

  def cached(self, spec: BuildSpec) -> BuildResult | None:
    directory = self._cache_dir(spec.fingerprint())
    manifest = read_json(os.path.join(directory, "manifest.json"))
    if not manifest:
      return None
    encoder = os.path.join(directory, "avmenc")
    decoder = os.path.join(directory, "avmdec")
    if not (os.path.exists(encoder) and os.path.exists(decoder)):
      return None
    # Verify the artifact is the one the manifest describes. A truncated copy
    # from an interrupted run is otherwise indistinguishable from a good one.
    if manifest.get("encoder_sha256") != _sha256(encoder):
      LOG.warning("cached encoder for %s failed its checksum; rebuilding", spec.fingerprint())
      shutil.rmtree(directory, ignore_errors=True)
      return None
    os.utime(directory, None)
    return BuildResult(
        ok=True, build_dir=directory, encoder=encoder, decoder=decoder,
        fingerprint=spec.fingerprint(),
        config_fingerprint=spec.config_fingerprint(),
        from_cache=True,
    )

  def _store(self, spec: BuildSpec, result: BuildResult) -> BuildResult:
    directory = ensure_dir(self._cache_dir(spec.fingerprint()))
    for name, path in (("avmenc", result.encoder), ("avmdec", result.decoder)):
      if path and os.path.exists(path):
        shutil.copy2(path, os.path.join(directory, name))
    encoder = os.path.join(directory, "avmenc")
    write_json_atomic(
        os.path.join(directory, "manifest.json"),
        {
            "fingerprint": spec.fingerprint(),
            "config_fingerprint": spec.config_fingerprint(),
            "source_identity": spec.source_identity,
            "cmake_args": spec.effective_cmake_args(),
            "patch_defines": spec.patch_defines,
            "toolchain": toolchain_identity(),
            "encoder_sha256": _sha256(encoder) if os.path.exists(encoder) else "",
            "built_ts": time.time(),
        },
    )
    self._evict()
    result.build_dir = directory
    result.encoder = encoder
    result.decoder = os.path.join(directory, "avmdec")
    return result

  def _evict(self) -> None:
    entries = [
        os.path.join(self.cache_root, name)
        for name in os.listdir(self.cache_root)
        if os.path.isdir(os.path.join(self.cache_root, name))
    ]
    if len(entries) <= self.keep:
      return
    entries.sort(key=os.path.getmtime)
    for stale in entries[: len(entries) - self.keep]:
      shutil.rmtree(stale, ignore_errors=True)

  # -- build ---------------------------------------------------------------

  def build(self, spec: BuildSpec, *, allow_cache: bool = True) -> BuildResult:
    if allow_cache:
      hit = self.cached(spec)
      if hit:
        LOG.info("build cache hit %s", spec.fingerprint())
        return hit

    started = time.time()
    build_dir = ensure_dir(spec.build_dir)
    log_path = os.path.join(build_dir, "av2ra-build.log")

    # A CMake cache from a different configuration is the classic source of a
    # silently mismatched arm. Detect and wipe rather than reconfigure over it.
    cache_file = os.path.join(build_dir, "CMakeCache.txt")
    stamp_file = os.path.join(build_dir, ".av2ra-config-fingerprint")
    previous = read_json(stamp_file, {}) or {}
    if os.path.exists(cache_file) and previous.get("config") != spec.config_fingerprint():
      LOG.info("wiping %s: build configuration changed", build_dir)
      shutil.rmtree(build_dir, ignore_errors=True)
      build_dir = ensure_dir(spec.build_dir)

    configure = proc.run(
        ["cmake", spec.source_dir, "-G", spec.generator, *spec.effective_cmake_args()],
        cwd=build_dir, timeout=3600,
    )
    _append_log(log_path, "CONFIGURE", configure)
    if not configure.ok:
      return BuildResult(
          ok=False, build_dir=build_dir, log_path=log_path,
          config_fingerprint=spec.config_fingerprint(),
          fingerprint=spec.fingerprint(),
          error=f"cmake configure failed:\n{configure.tail(30)}",
      )
    write_json_atomic(stamp_file, {"config": spec.config_fingerprint()})

    jobs = spec.jobs or (os.cpu_count() or 4)
    compile_cmd = (
        ["ninja", f"-j{jobs}", *spec.targets]
        if spec.generator == "Ninja"
        else ["cmake", "--build", ".", "-j", str(jobs), "--target", *spec.targets]
    )
    compiled = proc.run(compile_cmd, cwd=build_dir, timeout=14400)
    _append_log(log_path, "BUILD", compiled)
    if not compiled.ok:
      return BuildResult(
          ok=False, build_dir=build_dir, log_path=log_path,
          config_fingerprint=spec.config_fingerprint(),
          fingerprint=spec.fingerprint(),
          error=f"build failed:\n{compiled.tail(40)}",
      )

    encoder = os.path.join(build_dir, "avmenc")
    decoder = os.path.join(build_dir, "avmdec")
    missing = [p for p in (encoder, decoder) if not os.path.exists(p)]
    if missing:
      return BuildResult(
          ok=False, build_dir=build_dir, log_path=log_path,
          config_fingerprint=spec.config_fingerprint(),
          fingerprint=spec.fingerprint(),
          error=f"build reported success but produced no {', '.join(map(os.path.basename, missing))}",
      )

    result = BuildResult(
        ok=True, build_dir=build_dir, encoder=encoder, decoder=decoder,
        fingerprint=spec.fingerprint(),
        config_fingerprint=spec.config_fingerprint(),
        wall_s=time.time() - started, log_path=log_path,
        warnings=extract_warnings(compiled.stdout + compiled.stderr),
    )
    log.event(
        "build_done", fingerprint=result.fingerprint, wall_s=round(result.wall_s, 1),
        warnings=len(result.warnings),
    )
    return self._store(spec, result)


_WARNING = re.compile(r"^(.*):(\d+):(\d+): warning: (.*)$", re.MULTILINE)


def extract_warnings(text: str) -> list[str]:
  """New compiler warnings are a review signal for LLM-authored C.

  An uninitialised variable or a comparison that is always true will usually
  still produce a plausible-looking speedup, because the encoder is full of
  heuristics and a wrong one just makes different decisions. The warning is
  often the only cheap signal that the patch is not what its author intended.
  """
  return sorted({f"{m[0]}:{m[1]}: {m[3]}" for m in _WARNING.findall(text)})[:200]


def _append_log(path: str, phase: str, result: proc.RunResult) -> None:
  ensure_dir(os.path.dirname(os.path.abspath(path)) or ".")
  with open(path, "a", encoding="utf-8") as handle:
    handle.write(f"\n===== {phase} rc={result.returncode} =====\n")
    handle.write(result.stdout or "")
    handle.write(result.stderr or "")


def _sha256(path: str) -> str:
  digest = hashlib.sha256()
  with open(path, "rb") as handle:
    for chunk in iter(lambda: handle.read(1 << 20), b""):
      digest.update(chunk)
  return digest.hexdigest()
