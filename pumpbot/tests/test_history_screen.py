import pytest

from pumpbot.ingest.history import ChannelHistory, screen, summarise
from pumpbot.models import RawMessage
from pumpbot.parsing.extractor import SignalExtractor


@pytest.fixture
def ex():
    return SignalExtractor(
        ignore_symbols=["BTC", "ETH", "USDT"], quote_assets=["USDT"],
        min_confidence=0.55,
    )


def history(texts, *, span_days=30.0, forwards=0, edits=0, name="chan", chat_id=-1001):
    msgs = [
        RawMessage(chat_id=chat_id, message_id=i, text=t, received_ns=0,
                   received_wall_ms=i * 1000.0, channel_name=name,
                   posted_wall_ms=i * 1000.0)
        for i, t in enumerate(texts)
    ]
    return ChannelHistory(chat_id=chat_id, name=name, messages=msgs,
                          forwards=forwards, edits=edits, span_days=span_days)


_A = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def _sym(i):
    """A distinct ticker per index, so repetition in a test is deliberate."""
    return f"{_A[i // 676 % 26]}{_A[i // 26 % 26]}{_A[i % 26]}X"


def calls(n, start=0):
    return [f"BUY ${_sym(i)} NOW 🚀" for i in range(start, start + n)]


NOISE = ["gm everyone", "chart looks bullish", "DYOR NFA", "market update today"]


# --- paid-access detection -------------------------------------------------
@pytest.mark.parametrize("text", [
    "Join our VIP group for premium signals",
    "Upgrade to VIP today",
    "Our private channel has better calls",
    "DM me for access",
    "Only $99/month for the paid group",
    "3 spots left in the VIP channel",
])
def test_paid_pitches_are_detected(ex, text):
    s = screen(history([text] * 10 + calls(20)), ex)
    assert s.paid_pitch_ratio > 0


@pytest.mark.parametrize("text", [
    "VIP treatment for this chart",           # VIP without a transactional verb
    "this coin is premium quality",
    "I sent you a DM about the chart",
    "gm everyone",
])
def test_innocent_mentions_are_not_paid_pitches(ex, text):
    s = screen(history([text] * 10 + calls(20)), ex)
    assert s.paid_pitch_ratio == 0


def test_heavy_subscription_pitching_is_rejected(ex):
    """The product is the subscription; its incentive is a record that looks
    good, not one that trades well."""
    s = screen(history(["Join our VIP group now, 2 spots left"] * 10 + calls(20)), ex)
    assert s.rejected
    assert "subscription" in s.verdict


# --- relay detection -------------------------------------------------------
def test_mostly_forwarded_content_is_rejected(ex):
    s = screen(history(calls(30), forwards=25), ex)
    assert s.rejected
    assert "relay" in s.verdict.lower()


def test_moderate_forwarding_is_only_flagged(ex):
    s = screen(history(calls(30), forwards=15), ex)
    assert not s.rejected
    assert any("forwards" in f for f in s.flags)


# --- pre-announcement ------------------------------------------------------
@pytest.mark.parametrize("text", [
    "Next call in 10 minutes",
    "New pump at 20:00",
    "Get your USDT ready",
    "Prepare your funds, next signal in 5 min",
])
def test_preannouncement_is_detected(ex, text):
    s = screen(history([text] * 3 + calls(30)), ex)
    assert s.preannounce_ratio > 0
    assert any("accumulation window" in f for f in s.flags)


def test_a_plain_call_is_not_a_preannouncement(ex):
    s = screen(history(calls(30)), ex)
    assert s.preannounce_ratio == 0


# --- cadence ---------------------------------------------------------------
def test_firehose_is_rejected(ex):
    """At this rate something it named always went up, which is the point."""
    s = screen(history(calls(2000), span_days=30.0), ex)
    assert s.calls_per_day > 40
    assert s.rejected
    assert "firehose" in s.verdict.lower()


def test_too_quiet_to_ever_score_is_rejected(ex):
    s = screen(history(calls(2) + NOISE * 20, span_days=30.0), ex,
               min_signals_for_score=12)
    assert s.rejected
    assert "too quiet" in s.verdict


def test_days_to_scoreable_is_reported(ex):
    s = screen(history(calls(30) + NOISE * 10, span_days=30.0), ex,
               min_signals_for_score=12)
    assert s.days_to_scoreable == pytest.approx(12.0, rel=0.2)


def test_a_channel_with_no_calls_is_rejected(ex):
    s = screen(history(NOISE * 25), ex)
    assert s.calls == 0
    assert s.rejected
    assert "not a signal channel" in s.verdict


def test_empty_history_is_rejected(ex):
    s = screen(history([]), ex)
    assert s.rejected
    assert s.messages == 0


# --- repetition / edits ----------------------------------------------------
def test_repeated_symbols_are_flagged(ex):
    s = screen(history(["BUY $SAMECOIN NOW 🚀"] * 30, span_days=30.0), ex)
    assert s.repeat_symbol_ratio == pytest.approx(1.0)
    assert any("repeat" in f for f in s.flags)


def test_heavy_editing_is_flagged(ex):
    s = screen(history(calls(30), span_days=30.0, edits=20), ex)
    assert any("edits" in f for f in s.flags)


# --- clean channel ---------------------------------------------------------
def test_a_clean_channel_is_recommended(ex):
    s = screen(history(calls(60) + NOISE * 40, span_days=30.0), ex,
               min_signals_for_score=12)
    assert not s.rejected
    assert s.verdict.startswith("RECORD")
    assert s.flags == []


def test_summarise_counts_survivors(ex):
    good = screen(history(calls(60) + NOISE * 40, span_days=30.0, name="good"), ex)
    bad = screen(history(NOISE * 25, name="bad"), ex)
    stats = summarise([good, bad])
    assert stats == {"channels": 2, "rejected": 1, "kept": 1,
                     "median_calls_per_day": pytest.approx(good.calls_per_day)}


# --- thin samples and venue validation -------------------------------------
def test_a_single_message_is_not_a_firehose(ex):
    """One post over zero elapsed days divided out to a million calls a day
    and was rejected for posting too much."""
    s = screen(history(["BUY $NEX NOW"], span_days=0.0), ex)
    assert s.calls_per_day == 0.0
    assert "INSUFFICIENT" in s.verdict
    assert "firehose" not in s.verdict.lower()


def test_a_short_window_is_reported_as_insufficient(ex):
    s = screen(history(calls(4), span_days=0.01), ex)
    assert "INSUFFICIENT" in s.verdict


def test_cadence_is_computed_once_there_is_enough_span(ex):
    s = screen(history(calls(30), span_days=10.0), ex)
    assert s.calls_per_day == pytest.approx(3.0)
    assert "INSUFFICIENT" not in s.verdict


def test_calls_are_counted_only_when_the_venue_lists_them(ex):
    listed = {"AAAXUSDT"}
    s = screen(history(calls(30), span_days=10.0), ex,
               is_tradable=lambda sym: sym in listed)
    assert s.calls == 1
    assert s.calls_per_day == pytest.approx(0.1)


def test_a_channel_whose_calls_are_all_unlisted_is_rejected(ex):
    """Exactly the observed case: 35 apparent calls, zero tradable."""
    s = screen(history(calls(30), span_days=10.0), ex,
               is_tradable=lambda sym: False)
    assert s.calls == 0
    assert s.rejected
    assert "none naming a symbol listed" in s.verdict


def test_a_low_listed_ratio_is_flagged(ex):
    listed = {f"{_sym(i)}USDT" for i in range(5)}
    s = screen(history(calls(30), span_days=10.0), ex,
               is_tradable=lambda sym: sym in listed)
    assert any("name a listed symbol" in f for f in s.flags)


def test_no_validator_means_every_parse_counts(ex):
    s = screen(history(calls(30), span_days=10.0), ex)
    assert s.calls == 30
