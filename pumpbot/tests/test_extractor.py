import pytest

from pumpbot.clock import now_ns, wall_ms
from pumpbot.models import RawMessage, SignalKind
from pumpbot.parsing.extractor import SignalExtractor


def msg(text: str) -> RawMessage:
    return RawMessage(
        chat_id=-1001, message_id=1, text=text,
        received_ns=now_ns(), received_wall_ms=wall_ms(),
    )


@pytest.fixture
def ex():
    return SignalExtractor(
        ignore_symbols=["BTC", "ETH", "USDT"],
        quote_assets=["USDT"],
        min_confidence=0.0,
    )


@pytest.mark.parametrize(
    "text,base",
    [
        ("🚀 BUY $PEPE NOW\nTarget: 10% 25%\nSL: 6%", "PEPE"),
        ("Coin: WIFHAT\nEntry: market", "WIFHAT"),
        ("LONG BONK here. Tight stop at 5%.", "BONK"),
        ("#FLOKI /USDT — entry now ⚡", "FLOKI"),
        ("Next call is LIVE: MYRO USDT — go go go", "MYRO"),
        ("APE INTO $TURBO 🔥 pump starting", "TURBO"),
    ],
)
def test_extracts_real_calls(ex, text, base):
    sig = ex.extract(msg(text))
    assert sig is not None, f"failed to extract from: {text!r}"
    assert sig.base == base
    assert sig.symbol == f"{base}USDT"
    assert sig.kind is SignalKind.SYMBOL


@pytest.mark.parametrize(
    "text",
    [
        "gm everyone, markets looking choppy today",
        "Remember to DYOR. NFA.",
        "Our VIP group has 3 spots left, DM for details",
        "What are you all watching this week?",
        "Congrats to everyone who caught the last one 🔥",
        "",
        "ok",
    ],
)
def test_ignores_noise(ex, text):
    assert ex.extract(msg(text)) is None


def test_ignores_majors(ex):
    assert ex.extract(msg("BUY $BTC NOW 🚀")) is None
    assert ex.extract(msg("$ETH looking strong, load up")) is None


def test_stopwords_are_not_tickers(ex):
    # "NOW" follows BUY and matches the imperative pattern's shape.
    assert ex.extract(msg("BUY NOW")) is None
    assert ex.extract(msg("Entry: TARGET")) is None


def test_urgency_raises_confidence(ex):
    calm = ex.extract(msg("Coin: PEPE"))
    urgent = ex.extract(msg("Coin: PEPE 🚀 BUY NOW FAST"))
    assert calm is not None and urgent is not None
    assert urgent.confidence > calm.confidence


def test_targets_and_stop_are_parsed(ex):
    sig = ex.extract(msg("BUY $PEPE\nTP1: 10%\nTP2: 25%\nSL: 6%"))
    assert sig is not None
    assert sig.target_pcts == [10.0, 25.0]
    assert sig.stop_pct == 6.0


def test_min_confidence_filters(ex):
    strict = SignalExtractor(quote_assets=["USDT"], min_confidence=0.95)
    assert strict.extract(msg("Coin: PEPE")) is None


def test_evm_contract(ex):
    sig = ex.extract(msg("new gem 0x" + "a" * 40 + " ape in now"))
    assert sig is not None
    assert sig.kind is SignalKind.CONTRACT
    assert sig.chain == "evm"


def test_contracts_can_be_disabled():
    ex = SignalExtractor(quote_assets=["USDT"], accept_contracts=False)
    assert ex.extract(msg("0x" + "b" * 40)) is None


def test_pair_pattern_beats_cashtag(ex):
    # Both patterns can match; the pair is the more specific evidence.
    sig = ex.extract(msg("$SOMETHING chatter ... PEPE/USDT entry"))
    assert sig is not None
    assert sig.matched_by == "pair"


@pytest.mark.parametrize(
    "text",
    [
        "Next call in 10 minutes. Get your USDT ready.",
        "send me 50 USDT please",
        "swap your USDC for the next one",
    ],
)
def test_quote_asset_mentions_are_not_pairs(ex, text):
    """Regression: allowing a space in the pair pattern must not match prose.

    The pair pattern accepts "MYRO USDT", so it has to stay case-sensitive on
    the original text — otherwise every sentence containing "USDT" produces a
    market order.
    """
    assert ex.extract(msg(text)) is None


def test_spaced_pair_is_matched(ex):
    sig = ex.extract(msg("Next call is LIVE: MYRO USDT — go go go"))
    assert sig is not None
    assert sig.base == "MYRO"
