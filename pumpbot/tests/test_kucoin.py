import asyncio
import json

import pytest

from pumpbot.config import LiveConfig
from pumpbot.execution.base import OrderRejected, OrderRequest
from pumpbot.execution.live_kucoin import KucoinLiveExecutor
from pumpbot.marketdata.historical import HistoricalFeed
from pumpbot.marketdata.kucoin import (
    KucoinSymbols,
    KucoinTickFeed,
    KucoinTickRecorder,
)
from pumpbot.models import Side


def symbols(*rows):
    return KucoinSymbols.from_payload({"data": list(rows)})


def rule(symbol, base, quote="USDT", *, min_funds=0.1, base_increment=0.0001,
         enable=True):
    return {
        "symbol": symbol, "baseCurrency": base, "quoteCurrency": quote,
        "minFunds": str(min_funds), "baseMinSize": "0.001",
        "baseIncrement": str(base_increment), "priceIncrement": "0.00001",
        "enableTrading": enable,
    }


# --- symbol translation ----------------------------------------------------
def test_compact_tickers_resolve_to_venue_form():
    """The parser emits PEPEUSDT; KuCoin wants PEPE-USDT. Getting this wrong
    does not raise — it fails every order as 'symbol not found'."""
    s = symbols(rule("PEPE-USDT", "PEPE"), rule("BTC-USDT", "BTC"))
    assert s.resolve("PEPEUSDT") == "PEPE-USDT"
    assert s.resolve("PEPE-USDT") == "PEPE-USDT"
    assert s.resolve("PEPE/USDT") == "PEPE-USDT"
    assert s.resolve("pepeusdt".upper()) == "PEPE-USDT"


def test_unlisted_symbol_resolves_to_none():
    s = symbols(rule("BTC-USDT", "BTC"))
    assert s.resolve("SOMERANDOMTOKENUSDT") is None


def test_untradable_symbols_are_excluded():
    s = symbols(rule("HALTED-USDT", "HALTED", enable=False), rule("BTC-USDT", "BTC"))
    assert s.resolve("HALTEDUSDT") is None
    assert len(s) == 1


def test_size_rounds_down_not_up():
    """Rounding up gets the sell rejected for insufficient balance, at the
    exact moment you are trying to exit."""
    s = symbols(rule("PEPE-USDT", "PEPE", base_increment=0.01))
    assert s.round_size("PEPEUSDT", 1.239) == pytest.approx(1.23)
    assert s.round_size("PEPEUSDT", 1.0) == pytest.approx(1.0)


def test_round_size_passes_through_unknown_symbols():
    s = symbols(rule("BTC-USDT", "BTC"))
    assert s.round_size("WHATUSDT", 5.5) == 5.5


def test_min_funds_is_exposed():
    s = symbols(rule("PEPE-USDT", "PEPE", min_funds=0.5))
    assert s.min_funds("PEPEUSDT") == 0.5
    assert s.min_funds("NOPEUSDT") == 0.0


# --- tick feed -------------------------------------------------------------
def test_feed_accepts_either_symbol_form():
    f = KucoinTickFeed()
    f.on_trade("PEPE-USDT", 1.5, 10.0, 1000.0)
    assert f.price("PEPEUSDT", 1000.0) == 1.5
    assert f.price("PEPE-USDT", 1000.0) == 1.5


def test_feed_refuses_stale_marks():
    f = KucoinTickFeed(max_staleness_ms=5_000)
    f.on_trade("PEPE-USDT", 1.5, 10.0, 1000.0)
    assert f.price("PEPEUSDT", 5_900.0) == 1.5
    assert f.price("PEPEUSDT", 6_100.0) is None


def test_feed_unknown_symbol():
    assert KucoinTickFeed().price("XUSDT", 0.0) is None


# --- recorder --------------------------------------------------------------
def test_ticks_aggregate_into_one_second_bars(tmp_path):
    r = KucoinTickRecorder(tmp_path / "p.jsonl", keep_raw=False)
    r.on_trade("PEPE-USDT", 1.0, 1.0, 1_000_000.0)
    r.on_trade("PEPE-USDT", 1.5, 1.0, 1_000_400.0)
    r.on_trade("PEPE-USDT", 0.8, 1.0, 1_000_900.0)
    r.on_trade("PEPE-USDT", 2.0, 1.0, 1_001_200.0)     # next second
    assert r.flush() == 2

    rows = [json.loads(l) for l in (tmp_path / "p.jsonl").read_text().splitlines()]
    first = rows[0]
    assert first["symbol"] == "PEPEUSDT"        # stored in the form the engine trades
    assert first["high"] == 1.5
    assert first["low"] == 0.8
    assert first["close"] == 0.8
    assert first["quote_volume"] == pytest.approx(3.3)
    assert rows[1]["close"] == 2.0


def test_recorded_bars_replay_through_the_scoring_feed(tmp_path):
    """The recorder's output must be readable by the same feed a Binance
    backfill produces, or recorded sessions cannot be scored."""
    path = tmp_path / "p.jsonl"
    r = KucoinTickRecorder(path, keep_raw=False)
    for i in range(5):
        r.on_trade("PEPE-USDT", 1.0 + i, 1.0, 1_000_000.0 + i * 1000)
    r.flush()

    feed = HistoricalFeed.from_file(path)
    assert feed.symbols == ["PEPEUSDT"]
    assert feed.price("PEPEUSDT", 1_002_500.0) == 3.0


def test_window_expiry_releases_subscriptions():
    r = KucoinTickRecorder("/dev/null", window_s=60, keep_raw=False)
    r.want("PEPE-USDT", 0.0)
    assert r.expired(30_000.0) == []
    assert r.expired(61_000.0) == ["PEPE-USDT"]
    r.drop(["PEPE-USDT"])
    assert r.active == []


def test_want_extends_an_existing_window():
    r = KucoinTickRecorder("/dev/null", window_s=60, keep_raw=False)
    r.want("PEPE-USDT", 0.0)
    r.want("PEPE-USDT", 50_000.0)
    assert r.expired(61_000.0) == []
    assert r.expired(111_000.0) == ["PEPE-USDT"]


def test_raw_ticks_are_kept_separately(tmp_path):
    """Sub-second detail cannot be re-obtained later at any price."""
    r = KucoinTickRecorder(tmp_path / "p.jsonl", keep_raw=True)
    r.on_trade("PEPE-USDT", 1.0, 2.0, 1_000_000.5)
    r.flush()
    raw = [json.loads(l) for l in (tmp_path / "p.ticks.jsonl").read_text().splitlines()]
    assert raw[0]["t_ms"] == 1_000_000.5
    assert raw[0]["size"] == 2.0


def test_flush_on_empty_recorder_is_a_noop(tmp_path):
    assert KucoinTickRecorder(tmp_path / "p.jsonl").flush() == 0


# --- executor guards (no network) ------------------------------------------
def executor(**kw):
    base = dict(hard_cap_quote=10.0, symbols=symbols(rule("PEPE-USDT", "PEPE")))
    base.update(kw)
    ex = KucoinLiveExecutor(LiveConfig(), **base)
    return ex


def run(coro):
    return asyncio.run(coro)


def test_hard_cap_is_enforced_before_anything_is_sent():
    with pytest.raises(OrderRejected, match="hard cap"):
        run(executor().submit(
            OrderRequest("PEPEUSDT", Side.BUY, quote_notional=50.0)))


def test_min_funds_is_enforced():
    ex = executor(symbols=symbols(rule("PEPE-USDT", "PEPE", min_funds=5.0)))
    with pytest.raises(OrderRejected, match="below .* minimum"):
        run(ex.submit(OrderRequest("PEPEUSDT", Side.BUY, quote_notional=1.0)))


def test_unlisted_symbol_is_rejected():
    with pytest.raises(OrderRejected, match="not listed"):
        run(executor().submit(
            OrderRequest("GHOSTUSDT", Side.BUY, quote_notional=5.0)))


def test_sell_quantity_rounding_to_zero_is_rejected():
    ex = executor(symbols=symbols(rule("PEPE-USDT", "PEPE", base_increment=1.0)))
    with pytest.raises(OrderRejected, match="rounds to zero"):
        run(ex.submit(OrderRequest("PEPEUSDT", Side.SELL, qty=0.4)))


def test_dry_run_builds_the_order_but_sends_nothing():
    ex = executor(dry_run=True)
    with pytest.raises(OrderRejected, match="dry_run") as exc:
        run(ex.submit(OrderRequest("PEPEUSDT", Side.BUY, quote_notional=5.0)))
    body = json.loads(str(exc.value).split("would send ", 1)[1])
    assert body["symbol"] == "PEPE-USDT"
    assert body["type"] == "market"
    assert body["side"] == "buy"
    assert body["funds"] == "5"
    assert "size" not in body


def test_dry_run_sell_uses_size_not_funds():
    ex = executor(dry_run=True)
    with pytest.raises(OrderRejected, match="dry_run") as exc:
        run(ex.submit(OrderRequest("PEPEUSDT", Side.SELL, qty=12.34567)))
    body = json.loads(str(exc.value).split("would send ", 1)[1])
    assert body["side"] == "sell"
    assert body["size"] == "12.3456"          # rounded down to baseIncrement
    assert "funds" not in body


def test_signature_headers_are_well_formed(monkeypatch):
    """KuCoin v2 sends the passphrase HMAC'd with the secret, not in clear."""
    import base64
    import hashlib
    import hmac

    ex = executor()
    ex._secret = b"topsecret"
    ex._key = "mykey"
    ex._passphrase = base64.b64encode(
        hmac.new(b"topsecret", b"mypass", hashlib.sha256).digest()
    ).decode()

    headers = ex._headers("POST", "/api/v1/orders", '{"a":1}')
    assert headers["KC-API-KEY"] == "mykey"
    assert headers["KC-API-KEY-VERSION"] == "2"
    assert headers["KC-API-PASSPHRASE"] != "mypass"
    base64.b64decode(headers["KC-API-SIGN"])          # valid base64

    expected = base64.b64encode(hmac.new(
        b"topsecret",
        f'{headers["KC-API-TIMESTAMP"]}POST/api/v1/orders{{"a":1}}'.encode(),
        hashlib.sha256,
    ).digest()).decode()
    assert headers["KC-API-SIGN"] == expected
