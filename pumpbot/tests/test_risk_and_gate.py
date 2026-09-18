import pytest

from pumpbot.config import Config, GateConfig, RiskConfig
from pumpbot.gate import PromotionGate, config_fingerprint
from pumpbot.models import RawMessage, RejectReason, Signal, SignalKind
from pumpbot.risk.manager import RiskManager


def sig(symbol="PEPEUSDT", chat_id=-1001, wall_ms=0.0):
    raw = RawMessage(chat_id=chat_id, message_id=1, text="x",
                     received_ns=0, received_wall_ms=wall_ms)
    return Signal.new(raw, kind=SignalKind.SYMBOL, symbol=symbol, base=symbol[:-4],
                      contract=None, chain=None, confidence=0.9, matched_by="test")


def cfg(**kw):
    base = dict(starting_equity=1000.0, position_notional_quote=50.0,
                max_concurrent_positions=2, max_trades_per_run=5,
                daily_drawdown_stop_pct=10.0, symbol_cooldown_s=900)
    base.update(kw)
    return RiskConfig(**base)


# --- risk -----------------------------------------------------------------
def test_allows_a_clean_signal():
    rm = RiskManager(cfg())
    ok, reason, _ = rm.check(sig(), 0.0)
    assert ok and reason is None


def test_cooldown_blocks_the_same_symbol():
    rm = RiskManager(cfg())
    rm.on_position_opened("PEPEUSDT", 0.0)
    ok, reason, _ = rm.check(sig(), 60_000.0)
    assert not ok and reason is RejectReason.COOLDOWN
    # ...and releases after the window.
    ok, _, _ = rm.check(sig(), 901_000.0)
    assert ok


def test_cooldown_is_per_symbol():
    rm = RiskManager(cfg())
    rm.on_position_opened("PEPEUSDT", 0.0)
    ok, _, _ = rm.check(sig("BONKUSDT"), 1_000.0)
    assert ok


def test_max_concurrent_positions():
    rm = RiskManager(cfg(max_concurrent_positions=2))
    rm.on_position_opened("AUSDT", 0.0)
    rm.on_position_opened("BUSDT", 0.0)
    ok, reason, _ = rm.check(sig("CUSDT"), 0.0)
    assert not ok and reason is RejectReason.MAX_POSITIONS


def test_max_trades_per_run():
    rm = RiskManager(cfg(max_trades_per_run=2, max_concurrent_positions=10))
    rm.on_position_opened("AUSDT", 0.0)
    rm.on_position_closed(1.0)
    rm.on_position_opened("BUSDT", 0.0)
    rm.on_position_closed(1.0)
    ok, reason, _ = rm.check(sig("CUSDT"), 0.0)
    assert not ok and reason is RejectReason.MAX_TRADES


def test_drawdown_halts_the_run():
    rm = RiskManager(cfg(daily_drawdown_stop_pct=10.0))
    rm.on_position_opened("AUSDT", 0.0)
    rm.on_position_closed(-150.0)          # -15% from peak
    assert rm.state.halted
    ok, reason, _ = rm.check(sig("BUSDT"), 0.0)
    assert not ok and reason is RejectReason.DRAWDOWN_STOP


def test_drawdown_measures_from_the_peak_not_the_start():
    rm = RiskManager(cfg(daily_drawdown_stop_pct=10.0))
    rm.on_position_opened("AUSDT", 0.0)
    rm.on_position_closed(+500.0)          # equity 1500, peak 1500
    rm.on_position_opened("BUSDT", 0.0)
    rm.on_position_closed(-200.0)          # equity 1300: still up on the run...
    assert rm.state.equity == 1300.0
    assert rm.state.halted                 # ...but -13.3% off the peak
    assert "13.3" in rm.state.halt_reason


def test_blocked_symbols():
    rm = RiskManager(cfg(blocked_symbols=["PEPE"]))
    ok, reason, _ = rm.check(sig("PEPEUSDT"), 0.0)
    assert not ok and reason is RejectReason.BLOCKED_SYMBOL


def test_unknown_channel_is_rejected_when_a_minimum_is_set():
    rm = RiskManager(cfg(min_channel_score=0.5), channel_scores={-1002: 0.9})
    ok, reason, _ = rm.check(sig(chat_id=-1001), 0.0)
    assert not ok and reason is RejectReason.LOW_CHANNEL_SCORE
    ok, _, _ = rm.check(sig(chat_id=-1002), 0.0)
    assert ok


def test_no_minimum_means_everything_passes():
    rm = RiskManager(cfg(min_channel_score=0.0))
    ok, _, _ = rm.check(sig(chat_id=-9999), 0.0)
    assert ok


# --- gate -----------------------------------------------------------------
@pytest.fixture
def gate_cfg(tmp_path):
    return GateConfig(
        required_profitable_runs=3, require_consecutive=True,
        min_trades_per_run=5, min_net_return_pct=0.0,
        state_file=str(tmp_path / "gate.json"),
    )


def add(gate, *, net, trades=10, mode="simulate", run_id="r"):
    return gate.record_run(run_id=run_id, label="t", finished_at="now", mode=mode,
                           trades=trades, net_return_pct=net, net_pnl_quote=net)


def test_gate_unlocks_after_the_required_streak(gate_cfg):
    g = PromotionGate(gate_cfg, "fp1")
    for i in range(2):
        add(g, net=1.0, run_id=f"r{i}")
    assert not g.status().unlocked
    add(g, net=1.0, run_id="r2")
    assert g.status().unlocked


def test_a_loss_resets_the_streak(gate_cfg):
    g = PromotionGate(gate_cfg, "fp1")
    add(g, net=1.0, run_id="a")
    add(g, net=1.0, run_id="b")
    add(g, net=-2.0, run_id="c")
    add(g, net=1.0, run_id="d")
    assert g.status().qualifying_runs == 1


def test_thin_runs_do_not_count(gate_cfg):
    """Ten one-trade runs is a coin flipped ten times, not a validated edge."""
    g = PromotionGate(gate_cfg, "fp1")
    for i in range(5):
        r = add(g, net=5.0, trades=1, run_id=f"t{i}")
        assert not r.counts_toward_gate
    assert not g.status().unlocked
    assert g.status().qualifying_runs == 0


def test_live_runs_never_count(gate_cfg):
    g = PromotionGate(gate_cfg, "fp1")
    for i in range(3):
        r = add(g, net=5.0, mode="live", run_id=f"l{i}")
        assert not r.counts_toward_gate
    assert not g.status().unlocked


def test_streak_does_not_transfer_across_configs(gate_cfg):
    g = PromotionGate(gate_cfg, "fp1")
    for i in range(3):
        add(g, net=2.0, run_id=f"x{i}")
    assert g.status().unlocked
    # Same history, different strategy parameters: the evidence does not carry.
    g2 = PromotionGate(gate_cfg, "fp2")
    assert not g2.status().unlocked
    assert g2.status().qualifying_runs == 0


def test_live_needs_the_explicit_flag_even_when_unlocked(gate_cfg):
    g = PromotionGate(gate_cfg, "fp1")
    for i in range(3):
        add(g, net=2.0, run_id=f"y{i}")
    with pytest.raises(PermissionError, match="i-accept-live-trading-risk"):
        g.assert_live_allowed(False)
    g.assert_live_allowed(True)          # does not raise


def test_live_blocked_while_locked(gate_cfg):
    g = PromotionGate(gate_cfg, "fp1")
    with pytest.raises(PermissionError, match="gated"):
        g.assert_live_allowed(True)


def test_gate_state_survives_a_restart(gate_cfg):
    g = PromotionGate(gate_cfg, "fp1")
    add(g, net=1.0, run_id="p0")
    reloaded = PromotionGate(gate_cfg, "fp1")
    assert reloaded.status().qualifying_runs == 1


def test_fingerprint_changes_with_strategy_params():
    a = Config()
    b = Config()
    b.strategy.stop_loss_pct = 12.0
    assert config_fingerprint(a) != config_fingerprint(b)


def test_fingerprint_ignores_cosmetics():
    a = Config()
    b = Config()
    b.run_label = "something else"
    b.reporting.formats = ["json"]
    assert config_fingerprint(a) == config_fingerprint(b)
