"""Monotone piecewise cubic Hermite interpolation, in pure Python.

BD-rate is the number the whole system is judged on, so the interpolation
underneath it must be *the same* interpolation the AV2 CTC tooling uses --
``scipy.interpolate.pchip_interpolate``, called from
``tools/convexhull_framework/src/CalcBDRate.py`` in the AVM tree.

Re-implementing it here is deliberate. The agent has to produce identical
numbers on a Cloudtop with google3's scipy, on a laptop with no scipy at all,
and inside a CI container with a frozen dependency set. A BD-rate that silently
changes definition with the environment is worse than no BD-rate, because the
experiment registry accumulates numbers across machines and compares them.

``test_pchip.py`` asserts agreement with scipy to 1e-12 whenever scipy is
importable, so this file cannot drift away from the reference unnoticed.

Derivative estimation follows Fritsch & Carlson (1980) as implemented by scipy:
a weighted harmonic mean at interior knots, and the three-point edge rule at the
ends, both clamped so the interpolant cannot overshoot.
"""

from __future__ import annotations

from typing import Sequence


def _sign(x: float) -> int:
  if x > 0.0:
    return 1
  if x < 0.0:
    return -1
  return 0


def _edge_derivative(h0: float, h1: float, m0: float, m1: float) -> float:
  """One-sided derivative at an endpoint (scipy's ``_edge_case``).

  A plain one-sided difference would let the end segment overshoot, which on an
  RD curve shows up as a BD-rate that swings on the highest-QP point alone.
  """
  d = ((2.0 * h0 + h1) * m0 - h0 * m1) / (h0 + h1)
  if _sign(d) != _sign(m0):
    return 0.0
  if _sign(m0) != _sign(m1) and abs(d) > abs(3.0 * m0):
    return 3.0 * m0
  return d


def pchip_derivatives(x: Sequence[float], y: Sequence[float]) -> list[float]:
  """Shape-preserving slopes at each knot."""
  n = len(x)
  if n != len(y):
    raise ValueError("x and y must have the same length")
  if n < 2:
    raise ValueError("need at least two points to interpolate")
  if n == 2:
    slope = (y[1] - y[0]) / (x[1] - x[0])
    return [slope, slope]

  h = [x[k + 1] - x[k] for k in range(n - 1)]
  if any(hk <= 0.0 for hk in h):
    raise ValueError("x must be strictly increasing")
  m = [(y[k + 1] - y[k]) / h[k] for k in range(n - 1)]

  d = [0.0] * n
  for k in range(1, n - 1):
    m0, m1 = m[k - 1], m[k]
    if _sign(m0) * _sign(m1) > 0:
      # Weighted harmonic mean: the shorter interval gets the larger say, so a
      # tightly spaced pair of QPs cannot be dragged around by a distant one.
      w1 = 2.0 * h[k] + h[k - 1]
      w2 = h[k] + 2.0 * h[k - 1]
      d[k] = (w1 + w2) / (w1 / m0 + w2 / m1)
    else:
      # Local extremum (or a flat run): a zero slope is the only choice that
      # keeps the interpolant monotone through the knot.
      d[k] = 0.0

  d[0] = _edge_derivative(h[0], h[1], m[0], m[1])
  d[n - 1] = _edge_derivative(h[n - 2], h[n - 3], m[n - 2], m[n - 3])
  return d


def pchip_interpolate(
    x: Sequence[float], y: Sequence[float], xi: Sequence[float]
) -> list[float]:
  """Evaluate the monotone cubic through ``(x, y)`` at each point of ``xi``.

  ``x`` must be strictly increasing. Points outside ``[x[0], x[-1]]`` are
  extrapolated with the end cubic, matching scipy; callers in this package
  never do that, because BD-rate is defined only on the overlap interval.
  """
  d = pchip_derivatives(x, y)
  n = len(x)
  out: list[float] = []
  for t in xi:
    # Binary search for the owning interval; linear scan would dominate the
    # cost at the 100-sample resolution CalcBDRate.py uses per curve pair.
    lo, hi = 0, n - 2
    if t <= x[0]:
      k = 0
    elif t >= x[n - 1]:
      k = n - 2
    else:
      while lo <= hi:
        mid = (lo + hi) // 2
        if t < x[mid]:
          hi = mid - 1
        elif t >= x[mid + 1]:
          lo = mid + 1
        else:
          break
      k = min(max(mid, 0), n - 2)

    h = x[k + 1] - x[k]
    s = (t - x[k]) / h
    s2, s3 = s * s, s * s * s
    h00 = 2.0 * s3 - 3.0 * s2 + 1.0
    h10 = s3 - 2.0 * s2 + s
    h01 = -2.0 * s3 + 3.0 * s2
    h11 = s3 - s2
    out.append(
        h00 * y[k] + h10 * h * d[k] + h01 * y[k + 1] + h11 * h * d[k + 1]
    )
  return out
