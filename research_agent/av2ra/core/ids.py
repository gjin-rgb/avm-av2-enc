"""Experiment identifiers.

Ids are short, sortable, and carry their origin: ``N0042`` for an autonomously
generated experiment, ``H0007`` for one a human asked for, ``S0042a`` for a
sweep arm derived from ``N0042``. Sortability matters because the registry is
read by humans in a terminal far more often than by code.
"""

from __future__ import annotations

import re
import string

_PATTERN = re.compile(r"^([A-Z])(\d{4})([a-z]*)$")
_SUFFIXES = string.ascii_lowercase


def make_id(counter: int, *, origin: str = "N") -> str:
  return f"{origin}{counter:04d}"


def variant_id(parent: str, index: int) -> str:
  """``N0042`` + 0 -> ``N0042a``. Keeps a tuning chain visibly one family."""
  match = _PATTERN.match(parent)
  if not match:
    return f"{parent}-{index}"
  letter, number, _existing = match.groups()
  if index < len(_SUFFIXES):
    return f"{letter}{number}{_SUFFIXES[index]}"
  first, second = divmod(index, len(_SUFFIXES))
  return f"{letter}{number}{_SUFFIXES[first - 1]}{_SUFFIXES[second]}"


def parse_id(value: str) -> tuple[str, int, str]:
  match = _PATTERN.match(value)
  if not match:
    raise ValueError(f"not an experiment id: {value!r}")
  letter, number, suffix = match.groups()
  return (letter, int(number), suffix)


def family_of(value: str) -> str:
  """All variants of an experiment share a family: ``N0042c`` -> ``N0042``."""
  try:
    letter, number, _ = parse_id(value)
  except ValueError:
    return value
  return f"{letter}{number:04d}"


def next_counter(existing: list[str]) -> int:
  highest = 0
  for value in existing:
    try:
      _, number, _ = parse_id(value)
    except ValueError:
      continue
    highest = max(highest, number)
  return highest + 1
