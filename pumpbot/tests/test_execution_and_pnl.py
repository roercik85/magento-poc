import asyncio

import pytest

from pumpbot.config import SimulationConfig
from pumpbot.execution.base import OrderRejected, OrderRequest
from pumpbot.execution.simulator import SimulatedExecutor
from pumpbot.marketdata.feed import SyntheticPumpFeed
from pumpbot.models import Fill, Position, RawMessage, Side, Signal, SignalKind


class FlatFeed:
    """Constant price, so slippage and fees are the only moving parts."""

    def __init__(self, price=100.0, depth=25_000.0):
        self._p, self._d = price, depth

    def price(self, symbol, t_ms):
        return self._p

    def depth_quote(self, symbol):
        return self._d


def sim_cfg(**kw):
    base = dict(latency_ms_median=100.0, latency_ms_sigma=0.0,
                slippage_bps_base=50.0, slippage_impact_coeff=0.0,
                assumed_depth_quote=25_000.0, taker_fee_bps=10.0,
                reject_probability=0.0)
    base.update(kw)
    return SimulationConfig(**base)


def run(coro):
    return asyncio.run(coro)


def test_buy_slippage_is_adverse():
    ex = SimulatedExecutor(sim_cfg(), FlatFeed(100.0))
    fill = run(ex.submit(OrderRequest("XUSDT", Side.BUY, quote_notional=100.0)))
    assert fill.price == pytest.approx(100.5)      # +50 bps


def test_sell_slippage_is_adverse():
    ex = SimulatedExecutor(sim_cfg(), FlatFeed(100.0))
    fill = run(ex.submit(OrderRequest("XUSDT", Side.SELL, qty=1.0)))
    assert fill.price == pytest.approx(99.5)       # -50 bps


def test_impact_scales_with_size():
    ex = SimulatedExecutor(sim_cfg(slippage_impact_coeff=250.0), FlatFeed(100.0))
    small = run(ex.submit(OrderRequest("XUSDT", Side.BUY, quote_notional=100.0)))
    big = run(ex.submit(OrderRequest("XUSDT", Side.BUY, quote_notional=20_000.0)))
    assert big.price > small.price


def test_fee_is_charged():
    ex = SimulatedExecutor(sim_cfg(), FlatFeed(100.0))
    fill = run(ex.submit(OrderRequest("XUSDT", Side.BUY, quote_notional=1000.0)))
    assert fill.fee_quote == pytest.approx(fill.qty * fill.price * 0.001)


def test_always_rejects_when_probability_is_one():
    ex = SimulatedExecutor(sim_cfg(reject_probability=1.0), FlatFeed())
    with pytest.raises(OrderRejected):
        run(ex.submit(OrderRequest("XUSDT", Side.BUY, quote_notional=10.0)))


def test_rejects_without_market_data():
    class NoFeed(FlatFeed):
        def price(self, symbol, t_ms):
            return None

    ex = SimulatedExecutor(sim_cfg(), NoFeed())
    with pytest.raises(OrderRejected, match="no market data"):
        run(ex.submit(OrderRequest("XUSDT", Side.BUY, quote_notional=10.0)))


def test_fill_price_is_taken_at_fill_time_not_signal_time():
    """The whole point of the latency model.

    Pricing a fill at the signal timestamp makes latency free, which is the
    single most common way a pump backtest lies.
    """
    feed = SyntheticPumpFeed(seed=7)
    feed.set_symbol_quality("XUSDT", 1.0)
    feed.register("XUSDT", post_ms=1_000_000.0)

    slow = SimulatedExecutor(sim_cfg(latency_ms_median=30_000.0), feed)
    fast = SimulatedExecutor(sim_cfg(latency_ms_median=1.0), feed)
    at_signal = feed.price("XUSDT", 1_000_000.0)

    slow_fill = run(slow.submit(
        OrderRequest("XUSDT", Side.BUY, quote_notional=50.0, signal_wall_ms=1_000_000.0)))
    fast_fill = run(fast.submit(
        OrderRequest("XUSDT", Side.BUY, quote_notional=50.0, signal_wall_ms=1_000_000.0)))

    assert fast_fill.price == pytest.approx(at_signal, rel=0.02)
    assert slow_fill.price > fast_fill.price      # 30 s late into a rising pump


# --- position accounting --------------------------------------------------
def make_position(entry_price=100.0, qty=10.0, entry_fee=1.0):
    raw = RawMessage(chat_id=-1, message_id=1, text="x", received_ns=0, received_wall_ms=0)
    signal = Signal.new(raw, kind=SignalKind.SYMBOL, symbol="XUSDT", base="X",
                        contract=None, chain=None, confidence=1.0, matched_by="t")
    entry = Fill(side=Side.BUY, qty=qty, price=entry_price, fee_quote=entry_fee,
                 wall_ms=0.0, latency_ms=0.0)
    return Position(position_id="p", signal=signal, symbol="XUSDT",
                    opened_wall_ms=0.0, entry=entry, qty_open=qty, peak_price=entry_price)


def test_full_exit_pnl_nets_both_fees():
    pos = make_position(entry_price=100.0, qty=10.0, entry_fee=1.0)
    pos.exits.append(Fill(side=Side.SELL, qty=10.0, price=110.0, fee_quote=1.1,
                          wall_ms=1000.0, latency_ms=0.0))
    # 1100 - 1.1 proceeds, against 1000 + 1.0 cost
    assert pos.realised_pnl_quote == pytest.approx(97.9)
    assert pos.return_pct == pytest.approx(9.79)


def test_partial_exit_carries_the_whole_entry_fee():
    """Conservative and correct: the exchange took the entry fee in full."""
    pos = make_position(entry_price=100.0, qty=10.0, entry_fee=1.0)
    pos.exits.append(Fill(side=Side.SELL, qty=4.0, price=110.0, fee_quote=0.44,
                          wall_ms=1000.0, latency_ms=0.0))
    # 440 - 0.44 proceeds, against 4*100 + 1.0 cost
    assert pos.realised_pnl_quote == pytest.approx(38.56)


def test_avg_exit_price_is_quantity_weighted():
    pos = make_position()
    pos.exits.append(Fill(Side.SELL, 2.0, 110.0, 0.0, 1000.0, 0.0))
    pos.exits.append(Fill(Side.SELL, 8.0, 120.0, 0.0, 2000.0, 0.0))
    assert pos.avg_exit_price == pytest.approx(118.0)


def test_losing_position_reports_negative():
    pos = make_position(entry_price=100.0, qty=10.0, entry_fee=1.0)
    pos.exits.append(Fill(Side.SELL, 10.0, 94.0, 0.94, 1000.0, 0.0))
    assert pos.realised_pnl_quote < 0
    assert pos.return_pct == pytest.approx(-61.94 / 10.0)
