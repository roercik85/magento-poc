import asyncio

import pytest

from pumpbot.marketdata.onchain import OnchainTokens, TokenPair, _pair_from
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
    r = run(OnchainTokens(session).resolve(contract="MINT1"))
    assert r.ok
    assert r.mint == "MINT1"
    assert r.source == "contract"


def test_a_contract_with_no_pair_still_resolves_but_says_so():
    session = FakeSession({"/tokens/MINT1": {"pairs": []}})
    r = run(OnchainTokens(session).resolve(contract="MINT1"))
    assert r.mint == "MINT1"
    assert "no DEX pair" in r.reason


def test_a_unique_active_ticker_resolves():
    session = FakeSession({"/search": {"pairs": [raw_pair("MINT1", "PEPE")]}})
    r = run(OnchainTokens(session).resolve(ticker="PEPE"))
    assert r.ok
    assert r.mint == "MINT1"


def test_two_active_mints_under_one_ticker_is_ambiguous():
    """Guessing between them is how you buy an impersonator."""
    session = FakeSession({"/search": {"pairs": [
        raw_pair("MINT1", "HEDGE", vol=60_000.0),
        raw_pair("MINT2", "HEDGE", vol=55_000.0),
    ]}})
    r = run(OnchainTokens(session).resolve(ticker="HEDGE"))
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
    r = run(OnchainTokens(session).resolve(ticker="HEDGE"))
    assert not r.ok
    assert "14 mint(s)" in r.reason


def test_claimed_liquidity_alone_does_not_resolve_a_ticker():
    """Depth is the number a scam inflates; transactions are the one it
    cannot."""
    session = FakeSession({"/search": {"pairs": [
        raw_pair("FAKE", "BONK", liq=249_000_000.0, vol=3.99, buys=1, sells=1),
    ]}})
    assert not run(OnchainTokens(session).resolve(ticker="BONK")).ok


def test_pairs_named_differently_are_ignored():
    session = FakeSession({"/search": {"pairs": [raw_pair("MINT1", "PEPECOIN")]}})
    r = run(OnchainTokens(session).resolve(ticker="PEPE"))
    assert not r.ok
    assert "no pair on any chain" in r.reason


def test_resolution_without_either_input():
    r = run(OnchainTokens(FakeSession({})).resolve())
    assert not r.ok
    assert "neither" in r.reason


# --- ohlcv -----------------------------------------------------------------
def test_ohlcv_is_normalised_and_sorted():
    session = FakeSession({"/ohlcv/": {"data": {"attributes": {"ohlcv_list": [
        [1_700_000_120, 3.0, 3.1, 2.9, 3.05, 100.0],
        [1_700_000_060, 2.9, 3.0, 2.8, 3.0, 50.0],
    ]}}}})
    rows = run(OnchainTokens(session).ohlcv("POOL"))
    assert [r[0] for r in rows] == [1_700_000_060_000.0, 1_700_000_120_000.0]
    assert rows[0][4] == 3.0


def test_malformed_candles_are_skipped():
    session = FakeSession({"/ohlcv/": {"data": {"attributes": {"ohlcv_list": [
        [1_700_000_060, 2.9, 3.0, 2.8, 3.0, 50.0],
        ["bad"],
    ]}}}})
    assert len(run(OnchainTokens(session).ohlcv("POOL"))) == 1


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
        mint="MINT1", chain="solana", symbol="X", pair_address="P", dex="raydium",
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
        mint="MINT1", chain="solana", symbol="X", pair_address="P", dex="raydium",
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
def row(name, *, calls=10, with_address=0, checked=10, resolved=None,
        tradable=0, safe=0):
    return {"name": name, "calls": calls, "with_address": with_address,
            "checked": checked,
            "resolved": tradable if resolved is None else resolved,
            "tradable": tradable, "safe": safe}


def rank(rows, capsys, notional=10.0):
    from pumpbot.cli import _print_onchain_ranking

    _print_onchain_ranking(rows, notional)
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
    assert "Best: best" in out


def test_calls_that_are_never_identified_are_distinguished(capsys):
    """A bare ticker naming a dozen mints fails at a different place than a
    token that is identified perfectly and has no market."""
    out = rank([
        row("tickers", calls=10, resolved=0, tradable=0, safe=0),
        row("ok", tradable=6, safe=6),
    ], capsys)
    assert "never identified" in out


def test_identified_but_unroutable_is_its_own_note(capsys):
    """The case the data showed: a channel posting addresses whose tokens are
    dead. Identification succeeded; there is simply no market."""
    out = rank([
        row("dead tokens", calls=10, with_address=9, resolved=9, tradable=0, safe=0),
        row("ok", tradable=6, safe=6),
    ], capsys)
    assert "identified, but nothing routes" in out
    assert "never identified" not in out


def test_routing_without_clearing_thresholds_is_distinguished(capsys):
    out = rank([row("thin", resolved=6, tradable=6, safe=0),
                row("ok", tradable=6, safe=6)], capsys)
    assert "routes, below every threshold" in out


def test_all_dead_says_so_plainly(capsys):
    out = rank([row("a", tradable=0, safe=0), row("b", tradable=2, safe=0)], capsys)
    assert "Nothing here is tradable" in out
    assert "no amount of execution" in out


def test_address_share_is_reported_per_channel(capsys):
    out = rank([
        row("half", calls=10, with_address=5, tradable=5, safe=5),
        row("none", calls=10, with_address=0, tradable=1, safe=1),
    ], capsys)
    assert "50%" in out


def test_posting_addresses_is_not_claimed_to_make_a_channel_tradable(capsys):
    """Refuted by real data: a channel posting addresses for 66% of its calls
    reached a fill on 3% of them. An address removes ambiguity; it says
    nothing about whether the token has a market."""
    out = rank([
        row("addresses", calls=35, with_address=23, resolved=23, tradable=4, safe=1),
        row("other", tradable=2, safe=2),
    ], capsys)
    assert "by construction" not in out
    assert "identified, but nothing routes" not in out    # 4 did route
    assert "3% reach a fill" in out


def test_a_channel_with_no_calls_does_not_divide_by_zero(capsys):
    out = rank([row("silent", calls=0, checked=0), row("other", tradable=1, safe=1)],
               capsys)
    assert "no calls" in out


# --- turning a call rate into time ----------------------------------------
def test_the_rate_is_converted_into_months_of_collecting(capsys):
    """A usable-call count reads as progress; the months it implies usually
    say something else."""
    out = rank([row("a", calls=26, tradable=10, safe=5),
                row("b", calls=6, tradable=2, safe=1)], capsys)
    assert "6 usable call(s) in 30 days" in out
    assert "0.20 a day" in out
    assert "month(s) at this rate" in out
    assert "Adding channels is the only lever" in out


def test_no_usable_calls_means_no_rate_projection(capsys):
    out = rank([row("a", tradable=1, safe=0), row("b", tradable=0, safe=0)], capsys)
    assert "month(s) at this rate" not in out


def test_a_higher_edge_needs_fewer_trades(capsys):
    import re

    out = rank([row("a", calls=100, tradable=60, safe=60),
                row("b", tradable=1, safe=1)], capsys)
    needed = [int(n.replace(",", ""))
              for n in re.findall(r"needs ([\d,]+) trades", out)]
    assert needed == sorted(needed)          # 3% edge first, then 2%, then 1%


# --------------------------------------------------------------------------
# multi-chain: the bug that reported tradable tokens as dead
# --------------------------------------------------------------------------
def test_pairs_on_every_chain_are_searched():
    """The first version filtered to Solana because the channel said "on sol".
    It also said "on Base" and "on Rh", and EVE's Solana pools are empty while
    it round trips at -1.2% on BSC."""
    session = FakeSession({"/search": {"pairs": [
        raw_pair("SOLMINT", "EVE", vol=0.0, buys=0, sells=0) | {"chainId": "solana"},
        raw_pair("0xBSC", "EVE", vol=13_652.0) | {"chainId": "bsc"},
    ]}})
    r = run(OnchainTokens(session).resolve(ticker="EVE"))
    assert r.ok
    assert r.mint == "0xBSC"
    assert r.chain == "bsc"


def test_the_chain_is_reported_for_a_contract_resolution():
    session = FakeSession({"/tokens/0xABC": {"pairs": [
        raw_pair("0xABC", "EVE") | {"chainId": "base"},
    ]}})
    r = run(OnchainTokens(session).resolve(contract="0xABC"))
    assert r.chain == "base"


def test_the_reason_counts_chains_not_just_mints():
    session = FakeSession({"/search": {"pairs": [
        raw_pair(f"M{i}", "HEDGE", vol=0.0, buys=1, sells=0) | {"chainId": c}
        for i, c in enumerate(["solana", "bsc", "base", "ethereum"])
    ]}})
    r = run(OnchainTokens(session).resolve(ticker="HEDGE"))
    assert "4 chain(s)" in r.reason


# --- chain registry --------------------------------------------------------
def test_known_chains_are_quotable():
    from pumpbot.marketdata.chains import get_chain, is_quotable

    for name in ("solana", "ethereum", "bsc", "base"):
        assert is_quotable(name), name
    assert get_chain("bsc").kind == "evm"
    assert get_chain("solana").kind == "solana"


def test_bnb_chain_usdc_has_eighteen_decimals():
    """Assuming six produces a quote for a millionth of the size, which routes
    perfectly and means nothing."""
    from pumpbot.marketdata.chains import get_chain

    assert get_chain("bsc").usdc_decimals == 18
    assert get_chain("base").usdc_decimals == 6


def test_chains_without_a_public_router_are_not_quotable():
    from pumpbot.marketdata.chains import describe_unquotable, is_quotable

    assert not is_quotable("robinhood")
    assert not is_quotable("arc")
    assert not is_quotable(None)
    why = describe_unquotable("robinhood")
    assert "unreachable" in why


def test_unquotable_is_not_reported_as_untradable():
    """HEDGE's biggest market is Robinhood Chain with $260k of daily volume.
    Calling that "no market" is how tradable tokens were reported as dead."""
    chk = TokenSafetyChecker(FakeSession({}))
    v = run(chk.check("0xABC", chain="robinhood", notional_usd=10.0))
    assert not v.quotable
    assert not v.safe
    assert "UNQUOTABLE" in v.verdict
    assert "UNTRADABLE" not in v.verdict


def test_the_right_quoter_is_chosen_per_chain():
    from pumpbot.marketdata.chains import get_chain
    from pumpbot.risk.token_safety import JupiterQuoter, LifiQuoter

    chk = TokenSafetyChecker(FakeSession({}))
    assert isinstance(chk.quoter_for(get_chain("solana")), JupiterQuoter)
    assert isinstance(chk.quoter_for(get_chain("base")), LifiQuoter)
    assert chk.quoter_for(get_chain("robinhood")) is None


# --- LI.FI quoting ---------------------------------------------------------
def lifi_session(*, usdc_decimals=6, token_decimals=18, buy=None, sell=None):
    def token(params):
        addr = (params.get("token") or "").lower()
        if addr.startswith("0x833589") or addr.startswith("0x8ac76a"):
            return {"decimals": usdc_decimals, "symbol": "USDC"}
        if token_decimals is None:
            return {"message": "Could not find token"}
        return {"decimals": token_decimals, "symbol": "TKN"}

    def quote(params):
        selling = (params.get("fromToken") or "").lower().startswith("0xdead")
        payload = sell if selling else buy
        return {"estimate": payload} if payload else {"message": "no route"}

    return FakeSession({"/token": token, "/quote": quote})


def test_lifi_round_trip_is_measured():
    from pumpbot.marketdata.chains import get_chain
    from pumpbot.risk.token_safety import LifiQuoter

    session = lifi_session(
        buy={"toAmount": "1000000000000000000"},
        sell={"toAmount": "9800000"},
    )
    rt = run(LifiQuoter(session).round_trip(get_chain("base"), "0xdeadbeef", 10.0, 300))
    assert rt.bought and rt.sold
    assert rt.round_trip_pct == pytest.approx(-2.0)


def test_lifi_reports_an_unknown_token_rather_than_guessing():
    from pumpbot.marketdata.chains import get_chain
    from pumpbot.risk.token_safety import LifiQuoter

    session = lifi_session(token_decimals=None)
    rt = run(LifiQuoter(session).round_trip(get_chain("base"), "0xdeadbeef", 10.0, 300))
    assert not rt.bought
    assert "unknown to the aggregator" in rt.error


def test_lifi_catches_a_honeypot():
    from pumpbot.marketdata.chains import get_chain
    from pumpbot.risk.token_safety import LifiQuoter

    session = lifi_session(buy={"toAmount": "1000000000000000000"}, sell=None)
    rt = run(LifiQuoter(session).round_trip(get_chain("bsc"), "0xdeadbeef", 10.0, 300))
    assert rt.bought and not rt.sold


def test_lifi_uses_the_chains_own_usdc_decimals():
    """BNB Chain's USDC has 18 decimals where most chains have 6."""
    from pumpbot.marketdata.chains import get_chain
    from pumpbot.risk.token_safety import LifiQuoter

    session = lifi_session(usdc_decimals=18,
                           buy={"toAmount": "5"}, sell={"toAmount": "5"})
    run(LifiQuoter(session).round_trip(get_chain("bsc"), "0xdeadbeef", 10.0, 300))
    buy_call = [c for c in session.calls
                if "quote" in c[0] and not
                (c[1].get("fromToken") or "").lower().startswith("0xdead")][0]
    assert buy_call[1]["fromAmount"] == str(10 * 10 ** 18)
