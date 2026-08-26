"""The model interface: one small surface, two implementations.

Everything creative in this system -- proposing hypotheses, writing C, reading a
results table and deciding what it means -- goes through :class:`LLMClient`.
Keeping that surface tiny has three payoffs. The research logic is testable
without a network. The offline implementation lets the whole autonomous loop run
in CI and on a machine with no credentials. And prompt construction stays in one
place, which matters because prompt caching is a *prefix* match: the frozen
parts of a prompt (the methodology rules, the code map summary, the corpus
digest) must be byte-identical across calls or the cache never hits.

Structured outputs are used wherever the caller needs fields rather than prose.
A hypothesis with a missing ``mechanism`` cannot be evaluated, and parsing it out
of free text with a regex is how an agent ends up measuring the wrong thing, so
the schema is enforced by the API rather than by hope.
"""

from __future__ import annotations

import abc
import json
import os
import time
from dataclasses import dataclass, field

from ..util import log

LOG = log.get("agent.llm")

#: The default model. Opus-tier is the right trade here: a wrong hypothesis
#: costs hours of encode time, so the marginal token cost of the better model is
#: irrelevant next to the cost of the compute it directs.
DEFAULT_MODEL = "claude-opus-5"


@dataclass
class LLMUsage:
  input_tokens: int = 0
  output_tokens: int = 0
  cache_read_tokens: int = 0
  cache_write_tokens: int = 0
  calls: int = 0

  def add(self, other: "LLMUsage") -> None:
    self.input_tokens += other.input_tokens
    self.output_tokens += other.output_tokens
    self.cache_read_tokens += other.cache_read_tokens
    self.cache_write_tokens += other.cache_write_tokens
    self.calls += other.calls

  @property
  def cache_hit_rate(self) -> float:
    total = self.input_tokens + self.cache_read_tokens
    return self.cache_read_tokens / total if total else 0.0


@dataclass
class LLMResponse:
  text: str = ""
  data: dict | None = None
  usage: LLMUsage = field(default_factory=LLMUsage)
  model: str = ""
  stop_reason: str = ""
  error: str = ""

  @property
  def ok(self) -> bool:
    return not self.error


class LLMClient(abc.ABC):
  """What the research loop needs from a model."""

  name: str = "abstract"

  @abc.abstractmethod
  def complete(
      self,
      *,
      system: str,
      prompt: str,
      schema: dict | None = None,
      max_tokens: int = 16000,
      effort: str = "high",
      cache_system: bool = True,
  ) -> LLMResponse:
    ...

  def json(
      self, *, system: str, prompt: str, schema: dict, **kwargs
  ) -> tuple[dict | None, LLMResponse]:
    response = self.complete(system=system, prompt=prompt, schema=schema, **kwargs)
    return (response.data, response)


class AnthropicClient(LLMClient):
  """The real client. Uses the official SDK, structured outputs and caching."""

  name = "anthropic"

  def __init__(
      self,
      *,
      model: str = DEFAULT_MODEL,
      api_key: str | None = None,
      max_retries: int = 4,
      timeout_s: float = 900.0,
  ):
    try:
      import anthropic
    except ImportError as exc:  # pragma: no cover - environment dependent
      raise RuntimeError(
          "the 'anthropic' package is required for the live model client; "
          "install it, or run with agent.llm=offline"
      ) from exc
    self._anthropic = anthropic
    self._client = (
        anthropic.Anthropic(api_key=api_key, max_retries=max_retries, timeout=timeout_s)
        if api_key
        else anthropic.Anthropic(max_retries=max_retries, timeout=timeout_s)
    )
    self.model = model
    self.usage = LLMUsage()

  def complete(
      self,
      *,
      system: str,
      prompt: str,
      schema: dict | None = None,
      max_tokens: int = 16000,
      effort: str = "high",
      cache_system: bool = True,
  ) -> LLMResponse:
    # The system block carries the frozen context -- methodology rules, code map
    # digest, corpus digest -- and is marked for caching. The per-call prompt
    # carries only what varies, so the cached prefix survives between calls.
    system_blocks = [{"type": "text", "text": system}]
    if cache_system:
      system_blocks[0]["cache_control"] = {"type": "ephemeral"}

    request: dict = {
        "model": self.model,
        "max_tokens": max_tokens,
        "system": system_blocks,
        "messages": [{"role": "user", "content": prompt}],
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": effort},
    }
    if schema:
      request["output_config"]["format"] = {"type": "json_schema", "schema": schema}

    try:
      # Streaming: these calls run long and can emit a lot of tokens, and a
      # non-streaming request at this max_tokens risks an HTTP timeout.
      with self._client.messages.stream(**request) as stream:
        message = stream.get_final_message()
    except Exception as exc:  # network, rate limit, refusal handling below
      LOG.warning("model call failed: %s", exc)
      return LLMResponse(error=str(exc), model=self.model)

    usage = LLMUsage(
        input_tokens=getattr(message.usage, "input_tokens", 0) or 0,
        output_tokens=getattr(message.usage, "output_tokens", 0) or 0,
        cache_read_tokens=getattr(message.usage, "cache_read_input_tokens", 0) or 0,
        cache_write_tokens=getattr(message.usage, "cache_creation_input_tokens", 0) or 0,
        calls=1,
    )
    self.usage.add(usage)

    if getattr(message, "stop_reason", "") == "refusal":
      detail = getattr(message, "stop_details", None)
      return LLMResponse(
          error=f"model declined the request ({getattr(detail, 'category', 'unknown')})",
          usage=usage, model=self.model, stop_reason="refusal",
      )

    text = "".join(
        block.text for block in message.content if getattr(block, "type", "") == "text"
    )
    data = None
    if schema:
      try:
        data = json.loads(text)
      except json.JSONDecodeError:
        return LLMResponse(
            text=text, usage=usage, model=self.model,
            error="structured output did not parse as JSON",
        )
    return LLMResponse(
        text=text, data=data, usage=usage, model=self.model,
        stop_reason=getattr(message, "stop_reason", ""),
    )


class OfflineClient(LLMClient):
  """A deterministic stand-in used for tests, dry runs and credential-free hosts.

  It does not pretend to be creative. It fills the requested schema with valid,
  minimal values derived from a seed, so that every downstream stage -- patch
  application, screening, gating, analysis, reporting -- exercises real code
  paths. A test that passes here proves the *plumbing*; it proves nothing about
  research quality, and the run is labelled ``offline`` everywhere so no report
  can imply otherwise.
  """

  name = "offline"

  def __init__(self, *, responder=None, seed: str = "av2ra"):
    self.responder = responder
    self.seed = seed
    self.usage = LLMUsage()
    self.calls: list[dict] = []

  def complete(
      self,
      *,
      system: str,
      prompt: str,
      schema: dict | None = None,
      max_tokens: int = 16000,
      effort: str = "high",
      cache_system: bool = True,
  ) -> LLMResponse:
    self.calls.append({"system": system[:400], "prompt": prompt[:4000], "ts": time.time()})
    self.usage.calls += 1
    if self.responder is not None:
      produced = self.responder(system, prompt, schema)
      if isinstance(produced, dict):
        return LLMResponse(text=json.dumps(produced), data=produced, model="offline")
      return LLMResponse(text=str(produced), model="offline")
    if schema:
      data = _fill_schema(schema, f"{self.seed}:{len(self.calls)}")
      return LLMResponse(text=json.dumps(data), data=data, model="offline")
    return LLMResponse(text="(offline model: no text generated)", model="offline")


def _fill_schema(schema: dict, seed: str) -> dict:
  """Produce a minimal instance of a JSON schema. Only the subset used here."""
  import hashlib

  def _value(node: dict, path: str):
    node_type = node.get("type")
    if "enum" in node:
      options = node["enum"]
      index = int(hashlib.sha256(f"{seed}:{path}".encode()).hexdigest()[:8], 16)
      return options[index % len(options)]
    if node_type == "object":
      return {
          key: _value(sub, f"{path}.{key}")
          for key, sub in (node.get("properties") or {}).items()
      }
    if node_type == "array":
      item = node.get("items") or {"type": "string"}
      return [_value(item, f"{path}[0]")]
    if node_type == "integer":
      return 0
    if node_type == "number":
      return 0.0
    if node_type == "boolean":
      return False
    return f"offline:{path}"

  return _value(schema, "root")


def build_client(config) -> LLMClient:
  """Choose a client from configuration, degrading loudly rather than silently."""
  kind = config.get("agent.llm", "auto")
  model = config.get("agent.model", DEFAULT_MODEL)
  if kind == "offline":
    return OfflineClient()
  has_credentials = bool(
      os.environ.get("ANTHROPIC_API_KEY")
      or os.environ.get("ANTHROPIC_AUTH_TOKEN")
      or os.path.exists(os.path.expanduser("~/.config/anthropic"))
  )
  if kind == "anthropic" or (kind == "auto" and has_credentials):
    try:
      return AnthropicClient(model=model)
    except RuntimeError as exc:
      if kind == "anthropic":
        raise
      LOG.warning("%s; falling back to the offline client", exc)
  if kind == "auto":
    LOG.warning(
        "no model credentials found: running with the offline client. The loop "
        "will execute end to end but will not generate real research. Set "
        "ANTHROPIC_API_KEY or run 'ant auth login' for a live run."
    )
  return OfflineClient()
