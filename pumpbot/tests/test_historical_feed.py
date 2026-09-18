import json

import pytest

from pumpbot.marketdata.historical import HistoricalFeed


def write_prices(tmp_path, rows):
    p = tmp_path / "prices.jsonl"
    with p.open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    return p


def bar(symbol, t_ms, close, high=None, low=None, qv=1000.0):
    return {"symbol": symbol, "t_ms": t_ms, "close": close,
            "high": high if high is not None else close,
            "low": low if low is not None else close,
            "quote_volume": qv}


def test_as_of_lookup_returns_the_last_bar_at_or_before(tmp_path):
    feed = HistoricalFeed.from_file(write_prices(tmp_path, [
        bar("XUSDT", 1000, 10.0), bar("XUSDT", 2000, 11.0), bar("XUSDT", 3000, 12.0),
    ]))
    assert feed.price("XUSDT", 1000) == 10.0
    assert feed.price("XUSDT", 1999) == 10.0
    assert feed.price("XUSDT", 2000) == 11.0
    assert feed.price("XUSDT", 2500) == 11.0


def test_before_the_first_bar_is_unknown(tmp_path):
    feed = HistoricalFeed.from_file(write_prices(tmp_path, [bar("XUSDT", 5000, 10.0)]))
    assert feed.price("XUSDT", 4999) is None


def test_stale_quotes_are_refused_not_extrapolated(tmp_path):
    """A stale price in a pump is confidently wrong, which is the worst kind."""
    feed = HistoricalFeed.from_file(
        write_prices(tmp_path, [bar("XUSDT", 1000, 10.0)]), max_staleness_ms=5_000
    )
    assert feed.price("XUSDT", 5_500) == 10.0
    assert feed.price("XUSDT", 6_001) is None


def test_unknown_symbol_is_recorded_as_a_miss(tmp_path):
    feed = HistoricalFeed.from_file(write_prices(tmp_path, [bar("XUSDT", 1000, 10.0)]))
    assert feed.price("NOPEUSDT", 1000) is None
    assert "NOPEUSDT" in feed.misses


def test_rows_are_sorted_even_if_the_file_is_not(tmp_path):
    feed = HistoricalFeed.from_file(write_prices(tmp_path, [
        bar("XUSDT", 3000, 12.0), bar("XUSDT", 1000, 10.0), bar("XUSDT", 2000, 11.0),
    ]))
    assert feed.price("XUSDT", 2500) == 11.0


def test_depth_uses_median_quote_volume(tmp_path):
    feed = HistoricalFeed.from_file(write_prices(tmp_path, [
        bar("XUSDT", 1000, 10.0, qv=100.0),
        bar("XUSDT", 2000, 10.0, qv=500.0),
        bar("XUSDT", 3000, 10.0, qv=900.0),
    ]))
    assert feed.depth_quote("XUSDT") == 500.0
    assert feed.depth_quote("OTHERUSDT") == 0.0


def test_bar_spread_flags_unreliable_resolution(tmp_path):
    feed = HistoricalFeed.from_file(write_prices(tmp_path, [
        bar("TIGHTUSDT", 1000, 100.0, high=100.1, low=99.9),
        bar("WIDEUSDT", 1000, 100.0, high=150.0, low=80.0),
    ]))
    assert feed.bar_spread_bps("TIGHTUSDT", 1500) == pytest.approx(20.0)
    assert feed.bar_spread_bps("WIDEUSDT", 1500) == pytest.approx(7000.0)


def test_coverage_reports_what_is_missing(tmp_path):
    feed = HistoricalFeed.from_file(write_prices(tmp_path, [
        bar("AUSDT", 1000, 1.0), bar("BUSDT", 1000, 1.0),
    ]))
    covered, total, missing = feed.coverage(["AUSDT", "BUSDT", "CUSDT", "AUSDT"])
    assert (covered, total) == (2, 3)
    assert missing == ["CUSDT"]


def test_empty_file_loads_without_exploding(tmp_path):
    feed = HistoricalFeed.from_file(write_prices(tmp_path, []))
    assert feed.symbols == []
    assert feed.price("XUSDT", 0) is None
