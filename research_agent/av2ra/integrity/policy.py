"""What an autonomous agent is allowed to change, and why the list is short.

This system gives an LLM write access to a C codebase, a build, a measurement
harness, and a results database, then rewards it for producing a number. That
is a specification for finding the cheapest path to the number, and the
cheapest paths do not go through the encoder.

The policy below is not about trust. It is about making the *measurement*
mean what it claims to mean. A patch that edits the acceptance thresholds, or
the BD-rate implementation, or the clip list, may be entirely well-intentioned
-- an agent "improving the harness" -- and still invalidate every result the
run produces. So the harness, the metrics, the test definitions and the agent's
own source are outside the writable surface, structurally.

Scope is also a research constraint, not only a safety one: a change confined
to the encoder is a change that can be submitted upstream. A "speedup" that
required editing the test framework is not a contribution to AV2.
"""

from __future__ import annotations

import fnmatch
import os
import re
from dataclasses import dataclass, field

#: Where encoder research is allowed to happen. Everything else is read-only.
DEFAULT_WRITABLE = (
    "av2/encoder/**",
    "av2/common/**",
    "av2/*.c",
    "av2/*.h",
    "avm_dsp/**",
    "avm_util/**",
    "avm_ports/**",
    "avm_scale/**",
)

#: Never writable, in priority over the allow list. Each entry is a specific
#: way a result could stop being a result.
DEFAULT_FORBIDDEN = {
    # The measurement itself.
    "tools/convexhull_framework/**": "the CTC metric and encode-command tooling defines what a result means",
    "stats/**": "bitstream statistics tooling is measurement, not encoder",
    # The agent and its configuration.
    "research_agent/**": "the agent may not edit its own harness, policy or thresholds",
    # Test and conformance definitions.
    "test/**": "test vectors and unit tests are the correctness oracle",
    # Build and CI plumbing: a flag change is a build-configuration difference
    # masquerading as an encoder change.
    "CMakeLists.txt": "build configuration must be identical across arms",
    "cmake/**": "build configuration must be identical across arms",
    "*.cmake": "build configuration must be identical across arms",
    ".github/**": "CI configuration is not encoder research",
    "third_party/**": "vendored dependencies are out of scope for encoder research",
    # The decoder: an encoder-side experiment that needs a decoder change is
    # proposing a bitstream syntax change, which is a different process
    # (a CWG proposal) and must not arrive as a speed patch.
    "av2/decoder/**": "a decoder change means the bitstream changed: that is a syntax proposal, not an encoder optimisation",
}

#: Patterns that make a measurement invalid or a patch unshippable, regardless
#: of where they appear. Each carries the failure it produces.
FORBIDDEN_PATTERNS: list[tuple[str, str, str]] = [
    (
        r"\bgetenv\s*\(",
        "environment-dependent behaviour",
        "an encoder whose decisions depend on the environment cannot be measured "
        "reproducibly and cannot be submitted upstream",
    ),
    (
        r"\b(system|popen|execl|execv|fork)\s*\(",
        "process execution from inside the encoder",
        "the encoder must not shell out; this is never part of a coding tool",
    ),
    (
        r"\b(rand|random|drand48|arc4random)\s*\(",
        "nondeterminism",
        "a randomised encoder produces a different bitstream every run, which "
        "destroys bit-exactness checks and makes BD-rate irreproducible",
    ),
    (
        r"\b(time|clock|gettimeofday|clock_gettime)\s*\(",
        "wall-clock dependence in encoder decisions",
        "decisions that depend on how fast the machine is are not reproducible; "
        "if this is only instrumentation it belongs behind a debug macro",
    ),
    (
        r"#\s*define\s+assert\b|#\s*define\s+NDEBUG\b",
        "assertion suppression",
        "silencing assertions hides the crash the patch would otherwise cause",
    ),
    (
        r"#\s*pragma\s+GCC\s+optimi[sz]e|__attribute__\s*\(\s*\(\s*optimize",
        "per-function optimisation override",
        "an optimisation pragma changes codegen for one arm only: the resulting "
        "speed difference is a compiler-flag difference, not an algorithm",
    ),
    (
        r"\b(width|w)\s*==\s*(1920|3840)\b|\b(height|h)\s*==\s*(1080|2160)\b",
        "hard-coded test resolution",
        "special-casing the resolutions in the test set is fitting the benchmark, "
        "not improving the encoder",
    ),
    (
        r"frame_number\s*[<>=]=?\s*(1[0-9]|2[0-9]|3[0-9])\b",
        "hard-coded test frame count",
        "the screening set runs 17 or 33 frames; a threshold at that boundary is "
        "fitting the harness",
    ),
    (
        r"\bq(index|p)?\s*==\s*(110|135|160|185|210|235)\b",
        "hard-coded CTC QP",
        "the CTC QP ladder is the test, not a property of the content",
    ),
    (
        r"(RitualDance|BoxingPractice|Crosswalk|FoodMarket|OldTownCross|Tango|"
        r"TimeLapse|DinnerScene|Boat_1920|Neon1224|NocturneDance|PierSeaSide)",
        "test sequence name in encoder source",
        "referring to a test clip by name is the most direct possible form of "
        "benchmark fitting",
    ),
    (
        r"\bsse_to_psnr\b|\bcalc_psnr\b|\bavm_calc_ssim\b",
        "modification of quality measurement",
        "changing how PSNR is computed changes the score without changing the "
        "encoder",
    ),
]


@dataclass
class PolicyViolation:
  kind: str
  detail: str
  why_it_matters: str
  path: str = ""
  line: int = 0
  severity: str = "blocker"      # blocker | warning

  def __str__(self) -> str:
    where = f"{self.path}:{self.line}" if self.path else "patch"
    return f"[{self.severity}] {where}: {self.kind} -- {self.detail}"


@dataclass
class PatchPolicy:
  writable: tuple[str, ...] = DEFAULT_WRITABLE
  forbidden: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_FORBIDDEN))
  max_changed_files: int = 12
  max_changed_lines: int = 1500
  allow_new_files: bool = True

  def path_allowed(self, path: str) -> tuple[bool, str]:
    normalised = path.lstrip("./")
    for pattern, reason in self.forbidden.items():
      if _match(normalised, pattern):
        return (False, reason)
    for pattern in self.writable:
      if _match(normalised, pattern):
        return (True, "")
    return (
        False,
        "outside the writable surface (encoder sources only); if this file "
        "genuinely needs to change, a human must widen the policy",
    )


def _match(path: str, pattern: str) -> bool:
  if pattern.endswith("/**"):
    prefix = pattern[:-3]
    return path == prefix or path.startswith(prefix + "/")
  return fnmatch.fnmatch(path, pattern)


def check_paths(files: list[str], policy: PatchPolicy) -> list[PolicyViolation]:
  violations = []
  for path in files:
    allowed, reason = policy.path_allowed(path)
    if not allowed:
      violations.append(
          PolicyViolation(
              kind="path outside policy",
              detail=f"{path} may not be modified",
              why_it_matters=reason,
              path=path,
          )
      )
  if len(files) > policy.max_changed_files:
    violations.append(
        PolicyViolation(
            kind="patch too broad",
            detail=f"{len(files)} files changed, limit is {policy.max_changed_files}",
            why_it_matters=(
                "a wide patch cannot be attributed: if it wins you do not know "
                "which part won, and if it loses you cannot bisect it cheaply"
            ),
            severity="warning",
        )
    )
  return violations


_ADDED = re.compile(r"^\+(?!\+\+)(.*)$")
_FILE_HEADER = re.compile(r"^\+\+\+ b/(.+)$")


def scan_diff(diff_text: str, *, extra_patterns: list[tuple[str, str, str]] | None = None) -> list[PolicyViolation]:
  """Scan only the *added* lines of a diff for forbidden constructs.

  Scanning added lines rather than whole files matters: the encoder legitimately
  contains ``clock_gettime`` in its own timing code and sequence names in
  comments. What is being policed is what this patch introduces.
  """
  patterns = list(FORBIDDEN_PATTERNS) + list(extra_patterns or [])
  compiled = [(re.compile(p), kind, why) for p, kind, why in patterns]
  violations: list[PolicyViolation] = []
  current_file = ""
  line_no = 0
  for raw in diff_text.splitlines():
    header = _FILE_HEADER.match(raw)
    if header:
      current_file = header.group(1)
      line_no = 0
      continue
    if raw.startswith("@@"):
      match = re.search(r"\+(\d+)", raw)
      line_no = int(match.group(1)) if match else 0
      continue
    added = _ADDED.match(raw)
    if not added:
      if not raw.startswith("-"):
        line_no += 1
      continue
    body = added.group(1)
    line_no += 1
    stripped = body.strip()
    if stripped.startswith("//") or stripped.startswith("*") or stripped.startswith("/*"):
      continue
    for regex, kind, why in compiled:
      if regex.search(body):
        violations.append(
            PolicyViolation(
                kind=kind,
                detail=stripped[:160],
                why_it_matters=why,
                path=current_file,
                line=line_no,
            )
        )
  return violations


def check_defines(patch_defines: dict[str, str], diff_text: str) -> list[PolicyViolation]:
  """Every ``-D`` knob passed to the build must be introduced by the patch.

  Without this, a parameter sweep is an open channel for passing arbitrary
  compiler defines to one arm only -- ``-DNDEBUG`` on the candidate is a large,
  clean, entirely fake speedup.
  """
  violations = []
  for name in sorted(patch_defines):
    if not re.search(rf"^\+.*\b{re.escape(name)}\b", diff_text, re.MULTILINE):
      violations.append(
          PolicyViolation(
              kind="undeclared build define",
              detail=f"-D{name} is passed to the build but never appears in the patch",
              why_it_matters=(
                  "a define the patch does not reference cannot be part of the "
                  "hypothesis; it is a build-flag difference between the arms"
              ),
          )
      )
  return violations


def check_mechanism(mechanism: str, files: list[str]) -> list[PolicyViolation]:
  """Cross-check the declared mechanism against what the patch actually touches."""
  violations = []
  touches_simd = any(
      "/x86/" in f or "/arm/" in f or f.endswith(("_avx2.c", "_sse2.c", "_sse4.c", "_neon.c"))
      for f in files
  )
  if mechanism == "kernel" and not touches_simd:
    violations.append(
        PolicyViolation(
            kind="mechanism mismatch",
            detail="declared as a kernel rewrite but touches no SIMD source",
            why_it_matters=(
                "the mechanism decides which tier proves the patch; a mislabelled "
                "patch gets the wrong proof"
            ),
            severity="warning",
        )
    )
  if mechanism == "reuse" and touches_simd:
    violations.append(
        PolicyViolation(
            kind="mechanism mismatch",
            detail="declared as reuse but rewrites SIMD kernels",
            why_it_matters="a kernel rewrite needs kernel-level equivalence tests",
            severity="warning",
        )
    )
  return violations
