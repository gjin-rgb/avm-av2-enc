"""A small YAML reader for this package's own configuration files.

PyYAML is present on a Cloudtop and in google3 (``//third_party/py/yaml``) but
not in every container this system is expected to run in, and a research agent
that cannot start because of a config parser is a research agent that does not
run. :func:`load` uses PyYAML when it is importable and otherwise falls back to
the subset parser below.

The subset is deliberately narrow -- nested mappings, block lists, scalars,
quoted strings, comments, and nothing else. No anchors, no multi-line scalars,
no flow mappings beyond the empty ``{}``/``[]``. ``tests/test_yamlish.py``
parses every shipped config file with both implementations and asserts they
agree, so the fallback cannot drift away from real YAML for the files that
matter.
"""

from __future__ import annotations

import json
import re
from typing import Any

try:  # pragma: no cover - exercised by whichever environment runs the tests
  import yaml as _pyyaml
except ImportError:  # pragma: no cover
  _pyyaml = None

_INT = re.compile(r"^[+-]?\d+$")
_FLOAT = re.compile(r"^[+-]?(\d+\.\d*|\.\d+|\d+)([eE][+-]?\d+)?$")


def _scalar(text: str) -> Any:
  text = text.strip()
  if not text:
    return None
  if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
    return text[1:-1]
  low = text.lower()
  if low in ("null", "~"):
    return None
  if low == "true":
    return True
  if low == "false":
    return False
  if _INT.match(text):
    return int(text)
  if _FLOAT.match(text):
    return float(text)
  if text in ("{}", "[]"):
    return {} if text == "{}" else []
  if text.startswith("[") and text.endswith("]"):
    inner = text[1:-1].strip()
    return [_scalar(part) for part in inner.split(",")] if inner else []
  return text


def _strip_comment(line: str) -> str:
  out, quote = [], None
  for index, char in enumerate(line):
    if quote:
      out.append(char)
      if char == quote:
        quote = None
      continue
    if char in "\"'":
      quote = char
      out.append(char)
      continue
    if char == "#" and (index == 0 or line[index - 1] in " \t"):
      break
    out.append(char)
  return "".join(out).rstrip()


def loads(text: str) -> Any:
  """Parse the supported YAML subset."""
  if _pyyaml is not None:
    return _pyyaml.safe_load(text) or {}
  lines = []
  for raw in text.splitlines():
    stripped = _strip_comment(raw)
    if not stripped.strip() or stripped.strip() == "---":
      continue
    indent = len(stripped) - len(stripped.lstrip(" "))
    lines.append((indent, stripped.strip()))
  value, index = _parse_block(lines, 0, lines[0][0] if lines else 0)
  del index
  return value


def _parse_block(lines: list[tuple[int, str]], start: int, indent: int) -> tuple[Any, int]:
  if start >= len(lines):
    return ({}, start)
  container: Any = [] if lines[start][1].startswith("- ") or lines[start][1] == "-" else {}
  index = start
  while index < len(lines):
    line_indent, content = lines[index]
    if line_indent < indent:
      break
    if line_indent > indent:  # handled by the recursive calls below
      index += 1
      continue
    if isinstance(container, list):
      if not (content.startswith("- ") or content == "-"):
        break
      item = content[2:].strip() if content.startswith("- ") else ""
      if not item:
        child, index = _parse_block(lines, index + 1, _next_indent(lines, index, indent))
        container.append(child)
        continue
      if ":" in item and not item.split(":", 1)[0].strip().startswith(("\"", "'")):
        # An inline mapping opening a list item: "- key: value" plus siblings
        sub_lines = [(indent + 2, item)]
        cursor = index + 1
        while cursor < len(lines) and lines[cursor][0] > indent:
          sub_lines.append(lines[cursor])
          cursor += 1
        child, _ = _parse_block(sub_lines, 0, indent + 2)
        container.append(child)
        index = cursor
        continue
      container.append(_scalar(item))
      index += 1
      continue

    if ":" not in content:
      index += 1
      continue
    key, _, rest = content.partition(":")
    key = key.strip().strip("\"'")
    rest = rest.strip()
    if rest:
      container[key] = _scalar(rest)
      index += 1
      continue
    child_indent = _next_indent(lines, index, indent)
    if child_indent is None:
      container[key] = None
      index += 1
      continue
    child, index = _parse_block(lines, index + 1, child_indent)
    container[key] = child
  return (container, index)


def _next_indent(lines: list[tuple[int, str]], index: int, indent: int) -> int | None:
  if index + 1 >= len(lines):
    return None
  nxt_indent = lines[index + 1][0]
  return nxt_indent if nxt_indent > indent else None


def load(path: str) -> Any:
  with open(path, "r", encoding="utf-8") as handle:
    text = handle.read()
  if path.endswith(".json"):
    return json.loads(text)
  return loads(text)


def dumps(obj: Any) -> str:
  """Serialise back to YAML when PyYAML is available, else to JSON.

  Configuration is only ever *written* by ``av2ra init``; JSON is valid YAML
  for the shapes this package uses, so the fallback stays loadable.
  """
  if _pyyaml is not None:
    return _pyyaml.safe_dump(obj, default_flow_style=False, sort_keys=False)
  return json.dumps(obj, indent=2, sort_keys=False)
