import asyncio

import pytest

from pumpbot.marketdata.solana import SolanaTokens, TokenPair, _pair_from
from pumpbot.risk.token_safety import SafetyLimits, TokenSafetyChecker


def run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------
# a session stub: canned JSON per URL substring
# --------------------------------------------------------------------------
class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    async def json(self):
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeSession:
    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def get(self, url, params=None):
        self.calls.append((url, params or {}))
        for needle, payload in self.routes.items():
            if needle in url:
                value = payload(params or {}) if callable(payload) else payload
                return FakeResponse(value)
        return FakeResponse({})


def raw_pair(mint, symbol, *, liq=100_000.0, vol=50_000.0, buys=400, sells=400,
             price=1.0, created=None, pair="POOL"):
    return {
        "chainId": "solana", "dexId": "raydium", "pairAddress": pair,
        "priceUsd": str(price), "pairCreatedAt": created,
        "baseToken": {"address": mint, "symbol": symbol},
        "liquidity": {"usd": liq},
        "volume": {"h24": vol, "h1": vol / 24},
        "txns": {"h24": {"buys": buys, "sells": sells},
                 "h1": {"buys": buys // 24, "sells": sells // 24}},
        "priceChange": {"h1": 0.0},
    }


# --- parsing ---------------------------------------------------------------
def test_pair_parsing():
    p = _pair_from(raw_pair("MINT1", "pepe", liq=1234.5, vol=99.0, buys=3, sells=4))
    assert p.mint == "MINT1"
    assert p.symbol == "PEPE"
    assert p.liquidity_usd == 1234.5
    assert p.txns_h24 == 7


def test_pair_without_a_mint_is_dropped():
    assert _pair_from({"baseToken": {"symbol": "X"}}) is None


def test_manufactured_depth_is_recognised():
    """$249M of claimed liquidity, $3.99 of daily volume, two trades — the
    shape of a pool built to look deep."""
    p = _pair_from(raw_pair("FAKE", "BONK", liq=249_000_000.0, vol=3.99,
                            buys=1, sells=1))
    assert p.looks_inactive


def test_an_active_pair_is_not_flagged():
    assert not _pair_from(raw_pair("REAL", "BONK")).looks_inactive


# --- resolution ------------------------------------------------------------
def test_a_contract_address_is_taken_as_given():
    """It names the instrument, which is why the channels post them."""
    session = FakeSession({"/tokens/MINT1": {"pairs": [raw_pair("MINT1", "PEPE")]}})
    r = run(SolanaTokens(session).resolve(contract="MINT1"))
    assert r.ok
    assert r.mint == "MINT1"
    assert r.source == "contract"


def test_a_contract_with_no_pair_still_resolves_but_says_so():
    session = FakeSession({"/tokens/MINT1": {"pairs": []}})
    r = run(SolanaTokens(session).resolve(contract="MINT1"))
    assert r.mint == "MINT1"
    assert "no DEX pair" in r.reason


def test_a_unique_active_ticker_resolves():
    session = FakeSession({"/search": {"pairs": [raw_pair("MINT1", "PEPE")]}})
    r = run(SolanaTokens(session).resolve(ticker="PEPE"))
    assert r.ok
    assert r.mint == "MINT1"


def test_two_active_mints_under_one_ticker_is_ambiguous():
    """Guessing between them is how you buy an impersonator."""
    session = FakeSession({"/search": {"pairs": [
        raw_pair("MINT1", "HEDGE", vol=60_000.0),
        raw_pair("MINT2", "HEDGE", vol=55_000.0),
    ]}})
    r = run(SolanaTokens(session).resolve(ticker="HEDGE"))
    assert not r.ok
    assert r.ambiguous
    assert r.mint is None
    assert "2 different mints" in r.reason


def test_many_inactive_mints_are_all_rejected():
    """Fourteen mints called HEDGE, none trading — exactly what the search
    returns for a channel's tickers."""
    session = FakeSession({"/search": {"pairs": [
        raw_pair(f"MINT{i}", "HEDGE", liq=8_000.0, vol=0.0, buys=1, sells=1)
        for i in range(14)
    ]}})
    r = run(SolanaTokens(session).resolve(ticker="HEDGE"))
    assert not r.ok
    assert "14 mint(s)" in r.reason


def test_claimed_liquidity_alone_does_not_resolve_a_ticker():
    """Depth is the number a scam inflates; transactions are the one it
    cannot."""
    session = FakeSession({"/search": {"pairs": [
        raw_pair("FAKE", "BONK", liq=249_000_000.0, vol=3.99, buys=1, sells=1),
    ]}})
    assert not run(SolanaTokens(session).resolve(ticker="BONK")).ok


def test_pairs_named_differently_are_ignored():
    session = FakeSession({"/search": {"pairs": [raw_pair("MINT1", "PEPECOIN")]}})
    r = run(SolanaTokens(session).resolve(ticker="PEPE"))
    assert not r.ok
    assert "no Solana pair" in r.reason


def test_resolution_without_either_input():
    r = run(SolanaTokens(FakeSession({})).resolve())
    assert not r.ok
    assert "neither" in r.reason


# --- ohlcv -----------------------------------------------------------------
def test_ohlcv_is_normalised_and_sorted():
    session = FakeSession({"/ohlcv/": {"data": {"attributes": {"ohlcv_list": [
        [1_700_000_120, 3.0, 3.1, 2.9, 3.05, 100.0],
        [1_700_000_060, 2.9, 3.0, 2.8, 3.0, 50.0],
    ]}}}})
    rows = run(SolanaTokens(session).ohlcv("POOL"))
    assert [r[0] for r in rows] == [1_700_000_060_000.0, 1_700_000_120_000.0]
    assert rows[0][4] == 3.0


def test_malformed_candles_are_skipped():
    session = FakeSession({"/ohlcv/": {"data": {"attributes": {"ohlcv_list": [
        [1_700_000_060, 2.9, 3.0, 2.8, 3.0, 50.0],
        ["bad"],
    ]}}}})
    assert len(run(SolanaTokens(session).ohlcv("POOL"))) == 1


# --------------------------------------------------------------------------
# safety
# --------------------------------------------------------------------------
def quote_routes(buy=None, sell=None):
    def handler(params):
        if params.get("inputMint", "").startswith("EPjFWdd"):    # USDC in = buy
            return buy if buy is not None else {"error": "no route"}
        return sell if sell is not None else {"error": "no route"}

    return {"/quote": handler}


def test_no_buy_route_is_untradable():
    chk = TokenSafetyChecker(FakeSession(quote_routes()))
    v = run(chk.check("MINT1", notional_usd=10.0))
    assert not v.tradable
    assert not v.safe
    assert "UNTRADABLE" in v.verdict


def test_a_honeypot_is_caught_by_quoting_the_sell():
    """Buys route, the sell does not. Nothing in liquidity, volume or holder
    counts shows this, and it takes the whole position."""
    chk = TokenSafetyChecker(FakeSession(quote_routes(
        buy={"outAmount": "1000000", "priceImpactPct": "0.01"},
        sell=None,
    )))
    v = run(chk.check("MINT1", notional_usd=10.0))
    assert v.tradable
    assert not v.sellable
    assert "HONEYPOT" in v.verdict


def test_a_clean_round_trip_passes():
    chk = TokenSafetyChecker(FakeSession(quote_routes(
        buy={"outAmount": "1000000", "priceImpactPct": "0.001"},
        sell={"outAmount": "9950000", "priceImpactPct": "0.001"},
    )))
    v = run(chk.check("MINT1", notional_usd=10.0))
    assert v.tradable and v.sellable
    assert v.round_trip_pct == pytest.approx(-0.5)
    assert v.safe


def test_a_taxed_token_fails_on_round_trip_cost():
    """Buying and immediately selling loses 40%: transfer tax, or depth so
    thin that the order is the market."""
    chk = TokenSafetyChecker(FakeSession(quote_routes(
        buy={"outAmount": "1000000", "priceImpactPct": "0.01"},
        sell={"outAmount": "6000000", "priceImpactPct": "0.01"},
    )))
    v = run(chk.check("MINT1", notional_usd=10.0))
    assert v.sellable
    assert v.round_trip_pct == pytest.approx(-40.0)
    assert not v.safe
    assert "round trip costs 40.0%" in v.verdict


def test_price_impact_over_the_limit_fails():
    chk = TokenSafetyChecker(FakeSession(quote_routes(
        buy={"outAmount": "1000000", "priceImpactPct": "0.12"},
        sell={"outAmount": "9900000", "priceImpactPct": "0.01"},
    )))
    v = run(chk.check("MINT1", notional_usd=10.0,
                      limits=SafetyLimits(max_buy_impact_pct=3.0)))
    assert not v.safe
    assert "moves the price 12.0%" in v.verdict


def test_thin_pair_metrics_fail_even_with_a_clean_quote():
    pair = TokenPair(
        mint="MINT1", symbol="X", pair_address="P", dex="raydium",
        price_usd=1.0, liquidity_usd=800.0, volume_h24=20.0, volume_h1=0.0,
        txns_h24=3, txns_h1=0, created_at_ms=None, price_change_h1=0.0,
    )
    chk = TokenSafetyChecker(FakeSession(quote_routes(
        buy={"outAmount": "1000000", "priceImpactPct": "0.001"},
        sell={"outAmount": "9990000", "priceImpactPct": "0.001"},
    )))
    v = run(chk.check("MINT1", notional_usd=10.0, pair=pair))
    assert not v.safe
    assert any("liquidity" in f for f in v.failures)
    assert any("nobody to sell to" in f for f in v.failures)


def test_a_brand_new_pool_is_rejected():
    import time

    pair = TokenPair(
        mint="MINT1", symbol="X", pair_address="P", dex="raydium",
        price_usd=1.0, liquidity_usd=50_000.0, volume_h24=50_000.0,
        volume_h1=5_000.0, txns_h24=500, txns_h1=50,
        created_at_ms=(time.time() - 300) * 1000.0, price_change_h1=0.0,
    )
    chk = TokenSafetyChecker(FakeSession(quote_routes(
        buy={"outAmount": "1000000", "priceImpactPct": "0.001"},
        sell={"outAmount": "9990000", "priceImpactPct": "0.001"},
    )))
    v = run(chk.check("MINT1", notional_usd=10.0, pair=pair))
    assert any("minutes old" in f for f in v.failures)


def test_the_sell_is_quoted_for_the_full_position():
    """A smaller probe can route where the whole position cannot."""
    session = FakeSession(quote_routes(
        buy={"outAmount": "123456789", "priceImpactPct": "0.001"},
        sell={"outAmount": "9900000", "priceImpactPct": "0.001"},
    ))
    run(TokenSafetyChecker(session).check("MINT1", notional_usd=10.0))
    sell_call = [c for c in session.calls if c[1].get("inputMint") == "MINT1"]
    assert sell_call[0][1]["amount"] == "123456789"


# --------------------------------------------------------------------------
# cross-channel ranking
# --------------------------------------------------------------------------
def row(name, *, calls=10, with_address=0, checked=10, tradable=0, safe=0):
    return {"name": name, "calls": calls, "with_address": with_address,
            "checked": checked, "tradable": tradable, "safe": safe}


def rank(rows, capsys, notional=10.0):
    from pumpbot.cli import _print_solana_ranking

    _print_solana_ranking(rows, notional)
    return capsys.readouterr().out


def test_ranking_is_skipped_for_a_single_channel(capsys):
    """With one channel the per-token detail is the output."""
    assert rank([row("only")], capsys) == ""


def test_channels_are_ordered_by_what_survives_to_a_fill(capsys):
    out = rank([
        row("mediocre", tradable=5, safe=2),
        row("best", tradable=8, safe=7),
        row("dead", tradable=0, safe=0),
    ], capsys)
    positions = [out.index(n) for n in ("best", "mediocre", "dead")]
    assert positions == sorted(positions)
    assert "Start with best" in out


def test_a_channel_that_routes_nothing_says_execution_cannot_help(capsys):
    out = rank([row("dead", tradable=0, safe=0), row("other", tradable=1, safe=1)],
               capsys)
    assert "nothing routes — execution cannot help" in out


def test_routing_without_clearing_thresholds_is_distinguished(capsys):
    out = rank([row("thin", tradable=6, safe=0), row("ok", tradable=6, safe=6)],
               capsys)
    assert "routes, but none clears the thresholds" in out


def test_all_dead_says_so_plainly(capsys):
    out = rank([row("a", tradable=0, safe=0), row("b", tradable=2, safe=0)], capsys)
    assert "Nothing here is tradable" in out
    assert "no amount of execution" in out


def test_address_posting_channels_are_called_out(capsys):
    """The DEX-specific quality signal: a bare ticker can name a dozen mints,
    so a channel that posts addresses is tradable by construction."""
    out = rank([
        row("addresses", calls=10, with_address=9, tradable=9, safe=8),
        row("tickers", calls=10, with_address=0, tradable=1, safe=0),
    ], capsys)
    assert "1 channel(s) post contract addresses" in out
    assert "100%" not in out.split("RANKING")[0]        # no stray percentages


def test_address_share_is_reported_per_channel(capsys):
    out = rank([
        row("half", calls=10, with_address=5, tradable=5, safe=5),
        row("none", calls=10, with_address=0, tradable=1, safe=1),
    ], capsys)
    assert "50%" in out


def test_a_channel_with_no_calls_does_not_divide_by_zero(capsys):
    out = rank([row("silent", calls=0, checked=0), row("other", tradable=1, safe=1)],
               capsys)
    assert "no calls" in out
