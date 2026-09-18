"""Per-run reports.

Written after every run, in Markdown (to read), JSON (to diff and to feed back
into analysis) and optionally HTML (to send to yourself).

The report leads with the numbers that can talk you out of trading: net return
after costs, median rather than mean trade, max drawdown, and how much of the
result came from a single position. A report that leads with total profit is a
sales document.
"""
from __future__ import annotations

import json
import math
import statistics
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..engine import RunResult
from ..gate import GateStatus



def significance(returns_pct: List[float]) -> Dict[str, Any]:
    """Can this run's edge be distinguished from luck?

    A one-sample t-test of per-trade returns against zero. Pump trades are fat
    tailed and this understates the tail risk, so treat |t| < 2 as "no evidence"
    rather than as a close call. The point of showing it is blunt: a run of
    thirty trades almost never clears the bar, and ten such runs in a row is a
    coin landing heads ten times, not a validated strategy.
    """
    n = len(returns_pct)
    mean = statistics.fmean(returns_pct) if n else 0.0
    if n < 2:
        return {
            "n": n, "mean": mean, "stdev": 0.0, "stderr": 0.0, "t": 0.0,
            "n_needed": None,
            "verdict": "Too few trades to say anything at all.",
        }

    stdev = statistics.stdev(returns_pct)
    stderr = stdev / math.sqrt(n)
    t = mean / stderr if stderr > 0 else 0.0

    # Trades required for |t| = 2 at the observed mean and dispersion.
    n_needed = int(math.ceil((2.0 * stdev / mean) ** 2)) if mean != 0 and stdev > 0 else None

    if abs(t) < 2.0:
        verdict = (
            f"NOT SIGNIFICANT (t = {t:.2f}). This run's edge is indistinguishable "
            f"from zero. At this mean and dispersion you would need roughly "
            f"{n_needed:,} trades to tell them apart"
            if n_needed else
            f"NOT SIGNIFICANT (t = {t:.2f}). This run's edge is indistinguishable from zero"
        )
        verdict += ". Do not fund this on the strength of this run."
    elif t > 0:
        verdict = (
            f"Significant at t = {t:.2f} over {n} trades — by this test alone. "
            "That is necessary, not sufficient: the test assumes independent, "
            "roughly normal returns, and pump trades are neither."
        )
    else:
        verdict = f"Significantly NEGATIVE at t = {t:.2f}. The strategy loses money here."

    return {
        "n": n, "mean": mean, "stdev": stdev, "stderr": stderr, "t": t,
        "n_needed": n_needed, "verdict": verdict,
    }

def _fmt(value: Optional[float], digits: int = 2, suffix: str = "") -> str:
    if value is None:
        return "n/a"
    if value == float("inf"):
        return "∞"
    return f"{value:,.{digits}f}{suffix}"


def _sign(value: float, digits: int = 2, suffix: str = "") -> str:
    return f"{value:+,.{digits}f}{suffix}"


class ReportWriter:
    def __init__(self, output_dir: str | Path, formats: List[str]) -> None:
        self.dir = Path(output_dir)
        self.formats = [f.lower() for f in formats]

    def write(self, result: RunResult, gate: Optional[GateStatus] = None) -> Dict[str, Path]:
        run_dir = self.dir / f"{result.started_at.replace(':', '')}_{result.run_id}"
        run_dir.mkdir(parents=True, exist_ok=True)
        written: Dict[str, Path] = {}

        if "markdown" in self.formats:
            path = run_dir / "report.md"
            path.write_text(render_markdown(result, gate), encoding="utf-8")
            written["markdown"] = path
        if "json" in self.formats:
            path = run_dir / "report.json"
            path.write_text(
                json.dumps(to_dict(result, gate), indent=2, default=str), encoding="utf-8"
            )
            written["json"] = path
        if "html" in self.formats:
            path = run_dir / "report.html"
            path.write_text(render_html(result, gate), encoding="utf-8")
            written["html"] = path

        return written


# ---------------------------------------------------------------------------
# serialisation
# ---------------------------------------------------------------------------
def to_dict(result: RunResult, gate: Optional[GateStatus] = None) -> Dict[str, Any]:
    trades = []
    for p in result.closed:
        trades.append(
            {
                "position_id": p.position_id,
                "symbol": p.symbol,
                "channel": p.signal.raw.channel_name,
                "chat_id": p.signal.raw.chat_id,
                "matched_by": p.signal.matched_by,
                "confidence": round(p.signal.confidence, 3),
                "entry_price": p.entry.price,
                "entry_latency_ms": round(p.entry.latency_ms, 2),
                "avg_exit_price": p.avg_exit_price,
                "pnl_quote": round(p.realised_pnl_quote, 6),
                "return_pct": round(p.return_pct, 4),
                "hold_s": round(p.hold_s, 1),
                "exit_reason": p.exit_reason.value if p.exit_reason else None,
                "partial_exits": len(p.exits),
            }
        )

    return {
        "run": {
            "run_id": result.run_id,
            "label": result.label,
            "mode": result.mode,
            "started_at": result.started_at,
            "finished_at": result.finished_at,
            "duration_s": round(result.duration_s, 3),
        },
        "funnel": {
            "messages_seen": result.messages_seen,
            "signals_parsed": result.signals_parsed,
            "signals_rejected": result.signals_rejected,
            "orders_submitted": result.orders_submitted,
            "orders_rejected": result.orders_rejected,
        },
        "pnl": {
            "starting_equity": result.starting_equity,
            "ending_equity": round(result.ending_equity, 6),
            "net_pnl_quote": round(result.net_pnl_quote, 6),
            "net_return_pct": round(result.net_return_pct, 4),
            "trades": result.trades,
            "wins": result.wins,
            "losses": result.losses,
            "win_rate": round(result.win_rate, 4),
            "profit_factor": result.profit_factor,
            "max_drawdown_pct": round(result.max_drawdown_pct, 4),
            "halted_reason": result.halted_reason,
        },
        "significance": significance([p.return_pct for p in result.closed]),
        "latency": result.latency,
        "stage_latency_ms": {k: round(v, 4) for k, v in result.stage_latency_ms.items()},
        "exits": result.exit_breakdown,
        "rejections": result.rejections,
        "channels": [c.to_dict() for c in result.channel_scores],
        "trades": trades,
        "gate": asdict(gate) if gate is not None and is_dataclass(gate) else None,
    }


# ---------------------------------------------------------------------------
# markdown
# ---------------------------------------------------------------------------
def render_markdown(result: RunResult, gate: Optional[GateStatus] = None) -> str:
    L: List[str] = []
    a = L.append

    verdict = _headline_verdict(result)
    a(f"# Run report — `{result.run_id}`")
    a("")
    a(f"**{verdict}**")
    a("")
    a("| | |")
    a("|---|---|")
    a(f"| Label | `{result.label}` |")
    a(f"| Mode | **{result.mode}** |")
    a(f"| Started | {result.started_at} |")
    a(f"| Finished | {result.finished_at} |")
    a(f"| Wall time | {_fmt(result.duration_s)} s |")
    a("")

    # -- P&L ---------------------------------------------------------
    a("## Result")
    a("")
    a("| Metric | Value |")
    a("|---|---|")
    a(f"| Starting equity | {_fmt(result.starting_equity)} |")
    a(f"| Ending equity | {_fmt(result.ending_equity)} |")
    a(f"| **Net P&L** | **{_sign(result.net_pnl_quote)}** |")
    a(f"| **Net return** | **{_sign(result.net_return_pct, suffix='%')}** |")
    a(f"| Trades | {result.trades} ({result.wins}W / {result.losses}L) |")
    a(f"| Win rate | {_fmt(100 * result.win_rate, 1, '%')} |")
    a(f"| Profit factor | {_fmt(result.profit_factor)} |")
    a(f"| Max drawdown | {_fmt(result.max_drawdown_pct, 2, '%')} |")
    if result.halted_reason:
        a(f"| ⛔ Halted | {result.halted_reason} |")
    a("")

    closed = result.closed
    if closed:
        returns = [p.return_pct for p in closed]
        pnls = [p.realised_pnl_quote for p in closed]
        best = max(closed, key=lambda p: p.realised_pnl_quote)
        concentration = (
            100.0 * best.realised_pnl_quote / sum(x for x in pnls if x > 0)
            if any(x > 0 for x in pnls)
            else 0.0
        )
        a("### Trade distribution")
        a("")
        a("| Metric | Value |")
        a("|---|---|")
        a(f"| Median trade | {_sign(statistics.median(returns), suffix='%')} |")
        a(f"| Mean trade | {_sign(statistics.fmean(returns), suffix='%')} |")
        a(f"| Best | {_sign(max(returns), suffix='%')} ({best.symbol}) |")
        a(f"| Worst | {_sign(min(returns), suffix='%')} |")
        a(f"| Median hold | {_fmt(statistics.median([p.hold_s for p in closed]), 1, ' s')} |")
        a(f"| Top winner as share of gross profit | {_fmt(concentration, 1, '%')} |")
        a("")

        a("### Is this result real?")
        a("")
        sig = significance(returns)
        a("| Metric | Value |")
        a("|---|---|")
        a(f"| Mean trade | {_sign(sig['mean'], suffix='%')} |")
        a(f"| Std dev | {_fmt(sig['stdev'], 2, '%')} |")
        a(f"| Standard error | {_fmt(sig['stderr'], 2, '%')} |")
        a(f"| t-statistic | {_fmt(sig['t'], 2)} |")
        a(f"| Trades needed for t=2 | {sig['n_needed'] if sig['n_needed'] else 'n/a'} |")
        a("")
        a(f"> **{sig['verdict']}**")
        a("")

        if statistics.fmean(returns) > 0 > statistics.median(returns):
            a(
                "> ⚠️ Mean is positive while the median is negative: the result rests "
                "on a small number of outliers. Most trades lost. Do not size up on this."
            )
            a("")
        if concentration > 60:
            a(
                f"> ⚠️ A single trade produced {concentration:.0f}% of gross profit. "
                "This run is one lucky fill, not an edge."
            )
            a("")

    # -- funnel ------------------------------------------------------
    a("## Signal funnel")
    a("")
    a("| Stage | Count |")
    a("|---|---|")
    a(f"| Messages seen | {result.messages_seen} |")
    a(f"| Parsed as signals | {result.signals_parsed} |")
    a(f"| Rejected | {result.signals_rejected} |")
    a(f"| Orders submitted | {result.orders_submitted} |")
    a(f"| Orders rejected by venue | {result.orders_rejected} |")
    a("")

    if result.rejections:
        a("### Why signals were rejected")
        a("")
        a("| Reason | Count |")
        a("|---|---|")
        for reason, count in result.rejections.items():
            a(f"| `{reason}` | {count} |")
        a("")

    # -- latency -----------------------------------------------------
    a("## Latency")
    a("")
    lat = result.latency
    if lat.get("count"):
        a("Decision path, message received → order submitted.")
        a("")
        a("| Percentile | ms |")
        a("|---|---|")
        for key in ("p50", "p90", "p99", "max"):
            a(f"| {key} | {_fmt(lat.get(key), 3)} |")
        a("")
    if result.stage_latency_ms:
        a("Mean time per stage:")
        a("")
        a("| Stage | ms |")
        a("|---|---|")
        for stage, ms in result.stage_latency_ms.items():
            a(f"| `{stage}` | {_fmt(ms, 4)} |")
        a("")
        a(
            "> In-process time is a rounding error next to Telegram fanout and the "
            "venue round trip. If entries feel late, the fix is where the process "
            "runs and how many Telegram sessions feed it — not the Python."
        )
        a("")

    # -- exits -------------------------------------------------------
    if result.exit_breakdown:
        a("## How positions closed")
        a("")
        a("| Reason | Count |")
        a("|---|---|")
        for reason, count in sorted(result.exit_breakdown.items(), key=lambda kv: -kv[1]):
            a(f"| `{reason}` | {count} |")
        a("")

    # -- channels ----------------------------------------------------
    a("## Channel scoring")
    a("")
    if not result.channel_scores:
        a(
            "_Not enough observations yet._ A channel needs "
            "`scoring.min_signals_for_score` calls before it gets a score. "
            "Run in `record` mode for longer."
        )
        a("")
    else:
        a("Ranked by composite score. All returns are **net of modelled costs**.")
        a("")
        a("| # | Channel | Calls | Hit rate | Median ret | Mean ret | Median MAE | "
          "Pre-post run | Originator | Best hold | Score |")
        a("|---|---|---|---|---|---|---|---|---|---|---|")
        for i, c in enumerate(result.channel_scores, 1):
            a(
                f"| {i} | `{c.name}` | {c.signals} | {_fmt(100*c.hit_rate,1,'%')} | "
                f"{_sign(c.median_return_pct, suffix='%')} | {_sign(c.mean_return_pct, suffix='%')} | "
                f"{_fmt(c.median_mae_pct,2,'%')} | {_fmt(c.pre_pump_pct,1,'%')} | "
                f"{_fmt(c.originator_score,2)} | "
                f"{c.best_horizon_s}s ({_sign(c.best_horizon_return_pct, 1, '%')}) | "
                f"**{_fmt(c.composite,3)}** |"
            )
        a("")
        a("### Verdicts")
        a("")
        for c in result.channel_scores:
            a(f"- **`{c.name}`** — {c.verdict}")
        a("")
        a(
            "> *Pre-post run* is how far the price had already moved in the five "
            "minutes before the call. A large value means the channel is "
            "distributing into its followers, and no execution speed fixes that."
        )
        a("")

        horizon = _hint_horizon(result)
        if horizon is not None:
            best_h, configured = horizon
            a(
                f"> ⏱ Scored at a **{configured}s** horizon, but the top channels' "
                f"calls pay best at around **{best_h}s**. A pump peaks and then "
                f"gives it all back, so scoring at the wrong horizon marks good "
                f"channels as losers and vice versa. Set `scoring.primary_horizon_s` "
                f"to {best_h} and `strategy.max_hold_s` near it."
            )
            a("")

    # -- trades ------------------------------------------------------
    if closed:
        a("## Trades")
        a("")
        a("| Symbol | Channel | Entry | Exit | Ret | P&L | Hold | Closed by | Lat |")
        a("|---|---|---|---|---|---|---|---|---|")
        for p in sorted(closed, key=lambda x: x.opened_wall_ms):
            a(
                f"| `{p.symbol}` | {p.signal.raw.channel_name} | {p.entry.price:.8g} | "
                f"{(p.avg_exit_price or 0):.8g} | {_sign(p.return_pct, suffix='%')} | "
                f"{_sign(p.realised_pnl_quote)} | {_fmt(p.hold_s,0,'s')} | "
                f"`{p.exit_reason.value if p.exit_reason else '-'}` | "
                f"{_fmt(p.entry.latency_ms,0,'ms')} |"
            )
        a("")

    # -- gate --------------------------------------------------------
    if gate is not None:
        a("## Promotion gate")
        a("")
        state = "🟢 UNLOCKED" if gate.unlocked else "🔒 LOCKED"
        a(f"**{state}** — {gate.qualifying_runs}/{gate.required} qualifying runs.")
        a("")
        a(f"> {gate.reason}")
        a("")
        if gate.recent:
            a("| Run | Finished | Mode | Trades | Net | Counts |")
            a("|---|---|---|---|---|---|")
            for r in gate.recent:
                mark = "✅" if r.counts_toward_gate else f"— ({r.excluded_because})"
                a(
                    f"| `{r.run_id}` | {r.finished_at} | {r.mode} | {r.trades} | "
                    f"{_sign(r.net_return_pct, suffix='%')} | {mark} |"
                )
            a("")
        if gate.unlocked:
            a(
                "Live mode is now *permitted*, not *recommended*. It still requires "
                "`--i-accept-live-trading-risk` and credentials in the environment, "
                "and it is capped by `execution.live.max_notional_quote_hard_cap`. "
                "Read `LEGAL.md` first."
            )
            a("")

    a("---")
    a(f"_Generated {datetime.now(timezone.utc).isoformat(timespec='seconds')} · "
      f"mode `{result.mode}` · run `{result.run_id}`_")
    return "\n".join(L)


def _hint_horizon(result: RunResult) -> Optional[tuple]:
    """Flag a mismatch between the scoring horizon and where the money is."""
    top = [c for c in result.channel_scores[:3] if c.best_horizon_s]
    if not top:
        return None
    best = statistics.median([c.best_horizon_s for c in top])
    configured = result.scoring_horizon_s
    if configured and abs(best - configured) / configured > 0.5:
        return int(best), configured
    return None


def _headline_verdict(result: RunResult) -> str:
    if result.trades == 0:
        return "No trades taken — nothing to conclude from this run."
    if result.halted_reason:
        return f"Run halted early: {result.halted_reason}"
    if result.net_return_pct > 0:
        return (
            f"Profitable: {_sign(result.net_return_pct, suffix='%')} "
            f"over {result.trades} trades."
        )
    return (
        f"Unprofitable: {_sign(result.net_return_pct, suffix='%')} "
        f"over {result.trades} trades."
    )


# ---------------------------------------------------------------------------
# html
# ---------------------------------------------------------------------------
_HTML_SHELL = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>pumpbot run {run_id}</title>
<style>
  :root {{ color-scheme: light dark; --fg:#1a1a1a; --bg:#fff; --muted:#666;
           --line:#e2e2e2; --pos:#0a7a3d; --neg:#b3261e; --code:#f5f5f5; }}
  @media (prefers-color-scheme: dark) {{
    :root {{ --fg:#e8e8e8; --bg:#121212; --muted:#9a9a9a; --line:#2e2e2e;
             --pos:#4ade80; --neg:#f87171; --code:#1d1d1d; }}
  }}
  body {{ font:15px/1.6 ui-sans-serif,system-ui,-apple-system,sans-serif;
          color:var(--fg); background:var(--bg); margin:0; padding:32px 16px; }}
  main {{ max-width:1000px; margin:0 auto; }}
  h1 {{ font-size:1.6rem; margin:0 0 4px; }}
  h2 {{ font-size:1.15rem; margin:32px 0 8px; padding-bottom:6px;
        border-bottom:1px solid var(--line); }}
  h3 {{ font-size:1rem; margin:20px 0 6px; }}
  table {{ border-collapse:collapse; width:100%; margin:8px 0 16px; font-size:14px; }}
  th,td {{ text-align:left; padding:6px 10px; border-bottom:1px solid var(--line); }}
  th {{ font-weight:600; color:var(--muted); font-size:12px;
        text-transform:uppercase; letter-spacing:.04em; }}
  code {{ background:var(--code); padding:1px 5px; border-radius:4px; font-size:13px; }}
  blockquote {{ margin:12px 0; padding:10px 14px; border-left:3px solid var(--line);
                color:var(--muted); background:var(--code); border-radius:0 6px 6px 0; }}
  .verdict {{ font-size:1.05rem; font-weight:600; margin:12px 0 24px; }}
  .pos {{ color:var(--pos); }} .neg {{ color:var(--neg); }}
  footer {{ margin-top:40px; color:var(--muted); font-size:12px; }}
</style></head>
<body><main>
{body}
</main></body></html>
"""


def render_html(result: RunResult, gate: Optional[GateStatus] = None) -> str:
    """Render the Markdown report as a self-contained HTML page.

    Uses a deliberately small Markdown subset (headings, tables, blockquotes,
    bold, inline code) rather than pulling in a dependency: the report generator
    is the only producer, so the input space is known.
    """
    md = render_markdown(result, gate)
    html_lines: List[str] = []
    in_table = False

    for line in md.split("\n"):
        stripped = line.strip()

        if stripped.startswith("|"):
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            if all(set(c) <= set("-: ") and c for c in cells):
                continue                     # separator row
            tag = "td"
            if not in_table:
                html_lines.append("<table>")
                in_table = True
                tag = "th"
            row = "".join(f"<{tag}>{_inline(c)}</{tag}>" for c in cells)
            html_lines.append(f"<tr>{row}</tr>")
            continue

        if in_table:
            html_lines.append("</table>")
            in_table = False

        if not stripped:
            continue
        if stripped == "---":
            continue
        if stripped.startswith("### "):
            html_lines.append(f"<h3>{_inline(stripped[4:])}</h3>")
        elif stripped.startswith("## "):
            html_lines.append(f"<h2>{_inline(stripped[3:])}</h2>")
        elif stripped.startswith("# "):
            html_lines.append(f"<h1>{_inline(stripped[2:])}</h1>")
        elif stripped.startswith("> "):
            html_lines.append(f"<blockquote>{_inline(stripped[2:])}</blockquote>")
        elif stripped.startswith("- "):
            html_lines.append(f"<p>• {_inline(stripped[2:])}</p>")
        elif stripped.startswith("**") and stripped.endswith("**"):
            html_lines.append(f'<p class="verdict">{_inline(stripped)}</p>')
        else:
            html_lines.append(f"<p>{_inline(stripped)}</p>")

    if in_table:
        html_lines.append("</table>")

    html_lines.append(
        f"<footer>mode <code>{result.mode}</code> · run <code>{result.run_id}</code> · "
        f"generated {datetime.now(timezone.utc).isoformat(timespec='seconds')}</footer>"
    )
    return _HTML_SHELL.format(run_id=result.run_id, body="\n".join(html_lines))


def _inline(text: str) -> str:
    import html
    import re

    out = html.escape(text, quote=False)
    out = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", out)
    out = re.sub(r"`([^`]+?)`", r"<code>\1</code>", out)
    out = re.sub(r"(?<![\w>])\+(\d[\d,]*\.?\d*%?)", r'<span class="pos">+\1</span>', out)
    out = re.sub(r"(?<![\w>])-(\d[\d,]*\.?\d*%?)", r'<span class="neg">-\1</span>', out)
    return out
