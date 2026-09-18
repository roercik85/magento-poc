import pytest

from pumpbot.config import LadderRung, StrategyConfig
from pumpbot.models import ExitReason, Fill, Position, RawMessage, Side, Signal, SignalKind
from pumpbot.strategy.pump import PumpStrategy


def make_position(entry_price: float = 100.0, qty: float = 10.0, opened_ms: float = 0.0):
    raw = RawMessage(chat_id=-1, message_id=1, text="x", received_ns=0, received_wall_ms=opened_ms)
    signal = Signal.new(
        raw, kind=SignalKind.SYMBOL, symbol="XUSDT", base="X",
        contract=None, chain=None, confidence=0.9, matched_by="test",
    )
    entry = Fill(side=Side.BUY, qty=qty, price=entry_price, fee_quote=0.0,
                 wall_ms=opened_ms, latency_ms=0.0)
    return Position(
        position_id="p1", signal=signal, symbol="XUSDT", opened_wall_ms=opened_ms,
        entry=entry, qty_open=qty, peak_price=entry_price,
    )


@pytest.fixture
def strategy():
    return PumpStrategy(
        StrategyConfig(
            take_profit_ladder=[
                LadderRung(gain_pct=4.0, fraction=0.4),
                LadderRung(gain_pct=9.0, fraction=0.35),
                LadderRung(gain_pct=20.0, fraction=0.25),
            ],
            stop_loss_pct=6.0,
            trailing_stop_pct=5.0,
            max_hold_s=180,
            max_entry_chase_pct=8.0,
        )
    )


def test_stop_loss_closes_everything(strategy):
    pos = make_position()
    out = strategy.evaluate(pos, 93.0, 1_000)
    assert len(out) == 1
    assert out[0].reason is ExitReason.STOP_LOSS
    assert out[0].fraction == 1.0


def test_stop_loss_not_triggered_above_threshold(strategy):
    assert strategy.evaluate(make_position(), 95.0, 1_000) == []


def test_ladder_fires_once_per_rung(strategy):
    pos = make_position()
    first = strategy.evaluate(pos, 105.0, 1_000)
    assert len(first) == 1
    assert first[0].reason is ExitReason.TAKE_PROFIT
    assert first[0].fraction == 0.4
    # Same price again: the rung is spent.
    assert strategy.evaluate(pos, 105.0, 2_000) == []


def test_ladder_skips_ahead_on_a_gap(strategy):
    """A single tick through several rungs must fire all of them.

    Pump spikes gap. If the price goes from +1% to +25% between two ticks,
    firing only the first rung leaves most of the position riding the decay.
    """
    pos = make_position()
    out = strategy.evaluate(pos, 125.0, 1_000)
    assert [i.fraction for i in out] == [0.4, 0.35, 0.25]


def test_trailing_stop_arms_only_after_first_rung(strategy):
    pos = make_position()
    # +3%, then a large drop: below the first rung, so no trailing stop yet.
    strategy.evaluate(pos, 103.0, 1_000)
    assert not pos.trailing_armed
    out = strategy.evaluate(pos, 100.5, 2_000)
    assert out == []

    # Clear the first rung, then drop more than the trailing distance.
    strategy.evaluate(pos, 106.0, 3_000)
    assert pos.trailing_armed
    out = strategy.evaluate(pos, 100.0, 4_000)
    assert len(out) == 1
    assert out[0].reason is ExitReason.TRAILING_STOP


def test_time_exit_is_the_backstop(strategy):
    pos = make_position()
    assert strategy.evaluate(pos, 100.5, 179_000) == []
    out = strategy.evaluate(pos, 100.5, 181_000)
    assert len(out) == 1
    assert out[0].reason is ExitReason.TIME_EXIT


def test_stop_loss_takes_priority_over_time_exit(strategy):
    pos = make_position()
    out = strategy.evaluate(pos, 90.0, 200_000)
    assert out[0].reason is ExitReason.STOP_LOSS


def test_entry_chase_guard(strategy):
    ok, _ = strategy.entry_allowed(100.0, 105.0)
    assert ok
    ok, why = strategy.entry_allowed(100.0, 112.0)
    assert not ok
    assert "12.00%" in why


def test_entry_allowed_without_reference(strategy):
    ok, _ = strategy.entry_allowed(None, 105.0)
    assert ok


def test_closed_position_is_inert(strategy):
    pos = make_position()
    pos.closed = True
    assert strategy.evaluate(pos, 50.0, 999_999) == []
