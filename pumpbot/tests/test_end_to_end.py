import asyncio
import json

import pytest

from pumpbot.config import Config, LadderRung
from pumpbot.gate import PromotionGate, config_fingerprint
from pumpbot.reporting.report import ReportWriter, render_html, render_markdown, significance
from pumpbot.runner import SimulationRunner, synthetic_messages


def base_config(tmp_path, **risk_kw) -> Config:
    cfg = Config()
    cfg.mode = "simulate"
    cfg.strategy.take_profit_ladder = [
        LadderRung(4.0, 0.4), LadderRung(9.0, 0.35), LadderRung(20.0, 0.25)
    ]
    cfg.risk.max_trades_per_run = 50
    for k, v in risk_kw.items():
        setattr(cfg.risk, k, v)
    cfg.gate.state_file = str(tmp_path / "gate.json")
    cfg.reporting.output_dir = str(tmp_path / "runs")
    return cfg


def simulate(cfg, *, seed=1, count=800):
    messages, ground_truth = synthetic_messages(count=count, seed=seed)
    runner = SimulationRunner(cfg, messages, seed=seed, symbol_quality=ground_truth)
    return asyncio.run(runner.run())


def test_pipeline_produces_trades_and_a_coherent_result(tmp_path):
    result = simulate(base_config(tmp_path))
    assert result.messages_seen == 800
    assert result.signals_parsed > 0
    assert result.trades > 0
    assert result.wins + result.losses == len(result.closed)
    # Equity must reconcile with the sum of realised trade P&L.
    expected = result.starting_equity + sum(p.realised_pnl_quote for p in result.closed)
    assert result.ending_equity == pytest.approx(expected, rel=1e-9)


def test_every_position_is_closed_by_the_end(tmp_path):
    result = simulate(base_config(tmp_path))
    assert all(p.closed for p in result.positions)
    assert all(p.exit_reason is not None for p in result.positions)


def test_runs_are_deterministic(tmp_path):
    a = simulate(base_config(tmp_path), seed=3)
    b = simulate(base_config(tmp_path), seed=3)
    assert a.net_pnl_quote == pytest.approx(b.net_pnl_quote)
    assert a.trades == b.trades


def test_different_seeds_diverge(tmp_path):
    a = simulate(base_config(tmp_path), seed=3)
    b = simulate(base_config(tmp_path), seed=4)
    assert a.net_pnl_quote != pytest.approx(b.net_pnl_quote)


def test_max_trades_is_respected(tmp_path):
    result = simulate(base_config(tmp_path, max_trades_per_run=5))
    assert result.trades <= 5


def test_concurrency_limit_is_respected(tmp_path):
    cfg = base_config(tmp_path, max_concurrent_positions=1)
    result = simulate(cfg)
    # With a cap of one, no two positions may overlap in time.
    spans = sorted(
        (p.opened_wall_ms, p.exits[-1].wall_ms) for p in result.closed
    )
    for (_, end), (start, _) in zip(spans, spans[1:]):
        assert start >= end - 1e-6


def test_channel_scores_are_produced(tmp_path):
    result = simulate(base_config(tmp_path), count=1500)
    assert result.channel_scores
    assert all(0.0 <= c.composite <= 1.0 for c in result.channel_scores)
    # Ranked descending.
    composites = [c.composite for c in result.channel_scores]
    assert composites == sorted(composites, reverse=True)


def test_scorer_recovers_the_planted_channel_ranking(tmp_path):
    """The synthetic scenario plants channel 1 as the best and channel 5 as
    pure exit liquidity. The scorer is never told; it has to find it."""
    result = simulate(base_config(tmp_path), count=4000, seed=11)
    by_name = {c.name: c for c in result.channel_scores}
    assert "alpha_originator" in by_name, "best channel was not scored at all"
    assert "exit_liquidity_vip" in by_name
    assert by_name["alpha_originator"].composite > by_name["exit_liquidity_vip"].composite
    assert "CANDIDATE" in by_name["alpha_originator"].verdict
    assert "AVOID" in by_name["exit_liquidity_vip"].verdict


def test_reports_are_written_in_every_format(tmp_path):
    cfg = base_config(tmp_path)
    result = simulate(cfg)
    gate = PromotionGate(cfg.gate, config_fingerprint(cfg))
    gate.record_run(run_id=result.run_id, label="t", finished_at=result.finished_at,
                    mode="simulate", trades=result.trades,
                    net_return_pct=result.net_return_pct,
                    net_pnl_quote=result.net_pnl_quote)

    writer = ReportWriter(cfg.reporting.output_dir, ["markdown", "json", "html"])
    paths = writer.write(result, gate.status())

    assert set(paths) == {"markdown", "json", "html"}
    for path in paths.values():
        assert path.exists() and path.stat().st_size > 0

    md = paths["markdown"].read_text(encoding="utf-8")
    assert "# Run report" in md
    assert "## Channel scoring" in md
    assert "## Promotion gate" in md

    payload = json.loads(paths["json"].read_text(encoding="utf-8"))
    assert payload["pnl"]["trades"] == result.trades
    assert payload["gate"]["required"] == cfg.gate.required_profitable_runs

    html = paths["html"].read_text(encoding="utf-8")
    assert html.startswith("<!DOCTYPE html>")
    assert "<table>" in html
    assert html.count("<table>") == html.count("</table>")


def test_report_renders_with_zero_trades(tmp_path):
    """A no-trade run must still produce a readable report, not a crash."""
    cfg = base_config(tmp_path, max_trades_per_run=0)
    result = simulate(cfg)
    assert result.trades == 0
    md = render_markdown(result, None)
    assert "No trades taken" in md
    assert render_html(result, None).startswith("<!DOCTYPE html>")


def test_significance_flags_a_thin_result():
    s = significance([5.0, -4.0, 6.0, -5.0, 3.0])
    assert abs(s["t"]) < 2.0
    assert "NOT SIGNIFICANT" in s["verdict"]


def test_significance_needs_at_least_two_trades():
    assert "Too few trades" in significance([5.0])["verdict"]


def test_gate_stays_locked_until_ten_qualifying_runs(tmp_path):
    cfg = base_config(tmp_path)
    fingerprint = config_fingerprint(cfg)
    unlocked_at = None
    for i in range(20):
        gate = PromotionGate(cfg.gate, fingerprint)
        gate.record_run(run_id=f"r{i}", label="t", finished_at=f"t{i}", mode="simulate",
                        trades=10, net_return_pct=1.0, net_pnl_quote=10.0)
        if gate.status().unlocked and unlocked_at is None:
            unlocked_at = i + 1
    assert unlocked_at == 10
