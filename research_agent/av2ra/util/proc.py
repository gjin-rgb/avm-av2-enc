"""Subprocess execution with the properties a measurement harness needs.

Three things matter here and none of them are the default:

1. **Resource accounting.** ``run_measured`` returns the child's own CPU time
   from ``os.wait4``. Wall clock on a shared Cloudtop has a noise floor of a
   couple of percent (the prior research on this codebase measured 2.4%), which
   is larger than most of the effects being hunted.
2. **No shell.** Every call takes an argv list. Paths in this system come from
   config files and from an LLM; a shell in that path is a code-execution
   surface, and it also breaks silently on filenames with spaces.
3. **Timeouts that actually kill.** A hung encode holds a worker slot forever.
   The child runs in its own process group so the whole tree dies with it.
"""

from __future__ import annotations

import os
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass, field


@dataclass
class RunResult:
  argv: list[str]
  returncode: int
  stdout: str
  stderr: str
  wall_s: float
  user_s: float = 0.0
  sys_s: float = 0.0
  max_rss_kib: int = 0
  timed_out: bool = False
  cwd: str | None = None
  env_overrides: dict[str, str] = field(default_factory=dict)

  @property
  def ok(self) -> bool:
    return self.returncode == 0 and not self.timed_out

  @property
  def cpu_s(self) -> float:
    return self.user_s + self.sys_s

  def tail(self, n: int = 40) -> str:
    """Last ``n`` lines of stderr then stdout -- what a build error looks like."""
    text = (self.stderr or "") + (self.stdout or "")
    lines = text.splitlines()
    return "\n".join(lines[-n:])


def run(
    argv: list[str],
    *,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    timeout: float | None = None,
    check: bool = False,
    input_text: str | None = None,
) -> RunResult:
  """Run ``argv`` and capture output. Never uses a shell."""
  merged_env = dict(os.environ)
  if env:
    merged_env.update(env)
  started = time.time()
  try:
    proc = subprocess.Popen(
        argv,
        cwd=cwd,
        env=merged_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.PIPE if input_text is not None else None,
        text=True,
        start_new_session=True,
    )
  except FileNotFoundError as exc:
    return RunResult(argv, 127, "", str(exc), 0.0, cwd=cwd)
  timed_out = False
  try:
    out, err = proc.communicate(input=input_text, timeout=timeout)
  except subprocess.TimeoutExpired:
    timed_out = True
    _kill_tree(proc)
    out, err = proc.communicate()
  result = RunResult(
      argv=list(argv),
      returncode=proc.returncode,
      stdout=out or "",
      stderr=err or "",
      wall_s=time.time() - started,
      timed_out=timed_out,
      cwd=cwd,
      env_overrides=dict(env or {}),
  )
  if check and not result.ok:
    raise ProcessError(result)
  return result


def run_measured(
    argv: list[str],
    *,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    timeout: float | None = None,
    cpu_affinity: list[int] | None = None,
    nice: int | None = None,
    stdout_path: str | None = None,
) -> RunResult:
  """Run ``argv``, returning *that child's* CPU time and peak RSS.

  The rusage comes from :func:`os.wait4` on the specific pid, not from
  ``RUSAGE_CHILDREN``. The difference matters: this harness runs many encodes
  concurrently in one process, and ``RUSAGE_CHILDREN`` is a running total over
  every child the process has ever reaped, so a before/after delta would be
  corrupted by whichever sibling happened to exit in between. That bug does not
  crash anything -- it just makes the timings quietly wrong, which is the worst
  kind of bug in a measurement system.

  ``cpu_affinity`` pins the child to a fixed set of logical CPUs. Pinning is
  what makes repeated timings comparable: without it the scheduler migrates an
  encode between a physical core and its hyperthread sibling mid-run and the
  measured time moves by more than the effect under test.
  """
  merged_env = dict(os.environ)
  if env:
    merged_env.update(env)

  def _preexec() -> None:  # runs in the child, after fork, before exec
    os.setsid()
    if nice is not None:
      try:
        os.nice(nice)
      except OSError:
        pass
    if cpu_affinity:
      try:
        os.sched_setaffinity(0, set(cpu_affinity))
      except (AttributeError, OSError):
        pass

  # Redirect to real files rather than pipes. An encode with --verbose can emit
  # more than a pipe buffer holds, and a pipe nobody is draining deadlocks the
  # child; draining it from a thread would add scheduling noise to the very
  # measurement being taken.
  out_file = (
      open(stdout_path, "w+", encoding="utf-8", errors="replace")
      if stdout_path
      else tempfile.TemporaryFile("w+", encoding="utf-8", errors="replace")
  )
  err_file = tempfile.TemporaryFile("w+", encoding="utf-8", errors="replace")

  started = time.time()
  try:
    proc = subprocess.Popen(
        argv, cwd=cwd, env=merged_env, stdout=out_file, stderr=err_file,
        preexec_fn=_preexec,
    )
  except (FileNotFoundError, PermissionError) as exc:
    out_file.close()
    err_file.close()
    return RunResult(list(argv), 127, "", str(exc), 0.0, cwd=cwd)

  timed_out = False
  deadline = (started + timeout) if timeout else None
  pid = proc.pid
  status = 0
  usage = None
  while True:
    try:
      waited_pid, status, usage = os.wait4(pid, os.WNOHANG)
    except ChildProcessError:
      waited_pid, status, usage = pid, 0, None
      break
    if waited_pid == pid:
      break
    if deadline is not None and time.time() > deadline:
      timed_out = True
      _kill_tree(proc)
      try:
        _, status, usage = os.wait4(pid, 0)
      except ChildProcessError:
        usage = None
      break
    time.sleep(0.02)

  # Tell Popen the child is already reaped so its destructor stays quiet.
  proc.returncode = _exit_code(status)

  out_file.seek(0)
  err_file.seek(0)
  stdout_text = out_file.read()
  stderr_text = err_file.read()
  out_file.close()
  err_file.close()

  return RunResult(
      argv=list(argv),
      returncode=proc.returncode,
      stdout=stdout_text,
      stderr=stderr_text,
      wall_s=time.time() - started,
      user_s=usage.ru_utime if usage else 0.0,
      sys_s=usage.ru_stime if usage else 0.0,
      max_rss_kib=usage.ru_maxrss if usage else 0,
      timed_out=timed_out,
      cwd=cwd,
      env_overrides=dict(env or {}),
  )


def _exit_code(status: int) -> int:
  if os.WIFEXITED(status):
    return os.WEXITSTATUS(status)
  if os.WIFSIGNALED(status):
    return -os.WTERMSIG(status)
  return status


def _kill_tree(proc: subprocess.Popen) -> None:
  try:
    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
  except (ProcessLookupError, PermissionError, OSError):
    try:
      proc.kill()
    except OSError:
      pass


class ProcessError(RuntimeError):

  def __init__(self, result: RunResult):
    self.result = result
    super().__init__(
        f"command failed ({result.returncode}): {' '.join(result.argv[:6])}"
        f"\n{result.tail(30)}"
    )


def have(binary: str) -> bool:
  """True if ``binary`` is on PATH. Used for optional tools like ``perf``."""
  from shutil import which

  return which(binary) is not None
