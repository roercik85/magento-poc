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


# ---------------------------------------------------------------------------
# Regressions from a real channel: one month of posts produced 35 "calls" and
# not one named a symbol any venue lists.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "text,expected",
    [
        # The pair pattern extracts BTC, which is an ignored major.
        ("#BTCUSDT long setup 🚀", None),
        ("$ETHUSDT target hit", None),
        ("BTC/USDT breakout incoming", None),
        # Prose that lands in the ticker slot after a verb.
        ("LONG SETUP incoming, get ready", None),
        ("Quick SCALP now", None),
        ("Trade CLOSED in profit 🚀", None),
        ("UPDATE: we are in profit 🔥", None),
        ("Thanks for the FEEDBACK everyone!", None),
        ("BITCOIN buy now", None),
        ("buy the SUPPORT zone", None),
        ("watch this RESISTANCE level", None),
        # Real calls must survive all of it.
        ("BUY $PEPE NOW 🚀", "PEPEUSDT"),
        ("MYRO USDT go go go", "MYROUSDT"),
    ],
)
def test_real_channel_prose_is_not_a_call(ex, text, expected):
    sig = ex.extract(msg(text))
    assert (sig.symbol if sig else None) == expected


def test_a_base_carrying_the_quote_is_not_doubled():
    """"#BTCUSDT" parsed to a base of BTCUSDT, and appending the quote made
    BTCUSDTUSDT — a symbol no venue lists, so the call was dropped as
    untradable instead of recognised as a major to ignore."""
    ex = SignalExtractor(quote_assets=["USDT"], min_confidence=0.0)
    sig = ex.extract(msg("$WIFUSDT pumping now"))
    assert sig is not None
    assert sig.symbol == "WIFUSDT"
    assert not sig.symbol.endswith("USDTUSDT")


def test_ignored_majors_are_caught_through_a_full_pair():
    ex = SignalExtractor(ignore_symbols=["BTC"], quote_assets=["USDT"],
                         min_confidence=0.0)
    assert ex.extract(msg("$BTCUSDT to the moon")) is None


def test_default_config_ignores_the_majors():
    """The shipped YAML listed them; the built-in defaults did not, so any
    run without a config treated every BTC mention as a call."""
    from pumpbot.config import Config

    cfg = Config()
    assert "BTC" in cfg.parsing.ignore_symbols
    assert "ETH" in cfg.parsing.ignore_symbols

    ex = SignalExtractor(
        ignore_symbols=cfg.parsing.ignore_symbols,
        quote_assets=cfg.parsing.quote_assets,
        min_confidence=cfg.parsing.min_confidence,
    )
    assert ex.extract(msg("#BTCUSDT long setup 🚀")) is None
    assert ex.extract(msg("BUY $PEPE NOW")) is not None


def test_default_ignore_list_is_not_shared_between_configs():
    from pumpbot.config import Config

    a, b = Config(), Config()
    a.parsing.ignore_symbols.clear()
    assert b.parsing.ignore_symbols


# ---------------------------------------------------------------------------
# More regressions from real channels.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "text,expected",
    [
        # "#" marks topics at least as often as tickers, and case separates
        # them: a ticker hashtag is written in caps.
        ("With #base hype heating up I've added another play", None),
        ("#Gaming narrative on RH is starting to heat up", None),
        ("#defi season incoming", None),
        ("#W0X just at $78k mc", "W0XUSDT"),
        ("#PEPE breaking out", "PEPEUSDT"),
        # A hyphen strands a fragment that is not itself a stopword.
        ("$ETH 💰 LONG SET-UP 📈", None),
        ("clean BREAK-OUT here", None),
        ("nice PULL-BACK to support", None),
        # "$" is the ticker convention, so any case is a call.
        ("Aped small $rwacash here", "RWACASHUSDT"),
        ("Just aped $HEDGE now", "HEDGEUSDT"),
    ],
)
def test_topic_hashtags_and_split_words(ex, text, expected):
    sig = ex.extract(msg(text))
    assert (sig.symbol if sig else None) == expected


def test_hashtag_pattern_is_reported_separately():
    """Worth distinguishing from a cashtag: hashtags are the lower-precision
    source, and the report shows which pattern produced each call."""
    ex = SignalExtractor(quote_assets=["USDT"], min_confidence=0.0)
    sig = ex.extract(msg("#WIFHAT breaking out"))
    assert sig is not None
    assert sig.matched_by == "hashtag"
    assert sig.confidence < 0.72       # below a cashtag's


def test_lowercase_hashtag_does_not_leak_through_uppercasing(ex):
    """The text is upper-cased for most patterns, which would turn #base into
    #BASE and admit it. The hashtag pattern has to read the original."""
    assert ex.extract(msg("#base is heating up now 🚀")) is None
