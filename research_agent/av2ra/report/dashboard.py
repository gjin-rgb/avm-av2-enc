"""A single self-contained HTML page showing the state of the research.

Self-contained because it has to be readable from a laptop with no access to
the workspace it describes: one file, no external assets, no network. It is
generated on demand rather than served, so there is no daemon to keep alive and
nothing to break while the agent is doing the work that matters.

What it shows is chosen to make the failure modes visible: stale bases, unresolved
effects, per-clip spread, and the difference between a screening number and a CTC
number. A dashboard that only shows the leaderboard would make an autonomous
system look more decisive than the evidence allows.
"""

from __future__ import annotations

import html
import time

from ..core.models import Tier, Verdict
from ..core.registry import Registry
from .leaderboard import rows

_CSS = """
:root { --bg:#fbfbfa; --fg:#1a1a19; --muted:#6b6b66; --line:#e4e4e0;
        --good:#1a6b3c; --bad:#a02020; --warn:#8a6d1a; --card:#ffffff; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#151513; --fg:#e8e8e4; --muted:#9a9a94; --line:#2e2e2a;
          --good:#5fbf88; --bad:#e07070; --warn:#d4b458; --card:#1e1e1b; }
}
* { box-sizing: border-box; }
body { margin:0; padding:2rem 1.5rem 4rem; background:var(--bg); color:var(--fg);
       font:15px/1.55 ui-sans-serif,-apple-system,Segoe UI,Roboto,sans-serif; }
.wrap { max-width: 1180px; margin: 0 auto; }
h1 { font-size:1.5rem; margin:0 0 .25rem; letter-spacing:-0.01em; }
h2 { font-size:1.05rem; margin:2.5rem 0 .75rem; letter-spacing:-0.005em; }
.sub { color:var(--muted); margin:0 0 2rem; font-size:.9rem; }
.cards { display:grid; grid-template-columns:repeat(auto-fit,minmax(140px,1fr)); gap:.75rem; }
.card { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:.85rem 1rem; }
.card .n { font-size:1.6rem; font-weight:600; letter-spacing:-0.02em; }
.card .l { color:var(--muted); font-size:.78rem; text-transform:uppercase; letter-spacing:.04em; }
.scroll { overflow-x:auto; border:1px solid var(--line); border-radius:10px; background:var(--card); }
table { border-collapse:collapse; width:100%; font-size:.86rem; }
th,td { text-align:left; padding:.5rem .7rem; border-bottom:1px solid var(--line); white-space:nowrap; }
th { color:var(--muted); font-weight:600; font-size:.75rem; text-transform:uppercase; letter-spacing:.04em; }
tr:last-child td { border-bottom:none; }
td.num { text-align:right; font-variant-numeric:tabular-nums; }
.good { color:var(--good); font-weight:600; } .bad { color:var(--bad); font-weight:600; }
.warn { color:var(--warn); font-weight:600; }
.tag { display:inline-block; padding:.1rem .45rem; border:1px solid var(--line);
       border-radius:5px; font-size:.72rem; color:var(--muted); }
.note { color:var(--muted); font-size:.82rem; margin:.6rem 0 0; max-width:72ch; }
"""

_GOOD = {Verdict.PROMOTE.value, Verdict.CTC_PASS.value, Verdict.SCREEN_PASS.value}
_BAD = {
    Verdict.BUILD_FAIL.value, Verdict.CONFORMANCE_FAIL.value,
    Verdict.INTEGRITY_FAIL.value, Verdict.DIVERGES.value, Verdict.REGRESSION.value,
}


def render(registry: Registry, *, base_sha: str = "", fleet_status: str = "",
           title: str = "AV2 research agent") -> str:
  stats = registry.stats()
  table = rows(registry, current_base=base_sha)
  table.sort(key=lambda r: (r.ratio is None, -(r.ratio or 0.0)))
  stale = sum(1 for r in table if r.stale)

  parts = [
      f"<title>{html.escape(title)}</title>",
      f"<style>{_CSS}</style>",
      '<div class="wrap">',
      f"<h1>{html.escape(title)}</h1>",
      f'<p class="sub">anchor <code>{html.escape(base_sha[:12] or "unknown")}</code> '
      f'&middot; generated {time.strftime("%Y-%m-%d %H:%M")} &middot; '
      "every number is scoped to the anchor it was measured against</p>",
      '<div class="cards">',
  ]
  for label, value, tone in (
      ("experiments", stats["experiments"], ""),
      ("measurements", stats["measurements"], ""),
      ("cleared the bar", stats["by_verdict"].get(Verdict.PROMOTE.value, 0)
       + stats["by_verdict"].get(Verdict.CTC_PASS.value, 0), "good"),
      ("CTC slots spent", f"{stats['ctc_slots']:.0f}", ""),
      ("stale vs anchor", stale, "warn" if stale else ""),
      ("measurement conflicts", stats["conflicts"], "bad" if stats["conflicts"] else ""),
  ):
    parts.append(
        f'<div class="card"><div class="n {tone}">{value}</div>'
        f'<div class="l">{label}</div></div>'
    )
  parts.append("</div>")

  if fleet_status:
    parts += ["<h2>Fleet</h2>", f"<div class='scroll'><pre style='margin:0;padding:1rem;font-size:.8rem'>{html.escape(fleet_status)}</pre></div>"]

  parts += [
      "<h2>Experiments</h2>",
      '<div class="scroll"><table><thead><tr>'
      "<th>id</th><th>title</th><th>lens</th><th>subsystem</th><th>mech</th>"
      "<th>tier</th><th>verdict</th><th>ratio</th><th>bar</th>"
      "<th>speed %</th><th>BD %</th></tr></thead><tbody>",
  ]
  for row in table[:120]:
    experiment = row.experiment
    verdict = experiment.verdict.value
    tone = "good" if verdict in _GOOD else ("bad" if verdict in _BAD else "")
    parts.append(
        "<tr>"
        f"<td><code>{html.escape(experiment.id)}</code></td>"
        f"<td>{html.escape(experiment.hypothesis.title[:64])}"
        + (' <span class="tag">stale base</span>' if row.stale else "")
        + "</td>"
        f"<td><span class='tag'>{html.escape(experiment.hypothesis.lens)}</span></td>"
        f"<td>{html.escape(experiment.hypothesis.subsystem)}</td>"
        f"<td>{html.escape(experiment.hypothesis.mechanism.value)}</td>"
        f"<td>{html.escape(row.tier)}</td>"
        f"<td class='{tone}'>{html.escape(verdict)}</td>"
        f"<td class='num'>{_num(row.ratio, 1)}</td>"
        f"<td class='num'>{_num(row.bar, 0)}</td>"
        f"<td class='num'>{_num(row.speedup, 2)}</td>"
        f"<td class='num'>{_num(row.bdrate, 2)}</td>"
        "</tr>"
    )
  parts.append("</tbody></table></div>")
  parts.append(
      '<p class="note">Ratio is speedup% per 1% BD-rate given up. A ratio taken '
      "from a local screen ranks candidates; it is not an estimate of the "
      "patch's BD-rate, which only the CTC tier provides. Both test classes must "
      "clear the bar independently: an average that passes while one class fails "
      "is a class failure.</p>"
  )
  parts.append("</div>")
  return "\n".join(parts)


def _num(value, places: int) -> str:
  return "-" if value is None else f"{value:.{places}f}"
