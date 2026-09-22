import asyncio
import json

from pumpbot.marketdata.binance import BinanceSymbols, _fetch_range, fetch_klines


def run(coro):
    return asyncio.run(coro)


def info(*symbols):
    return {"symbols": [
        {"symbol": s, "status": "TRADING", "baseAsset": s[:-4], "quoteAsset": "USDT",
         "filters": [{"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.01"},
                     {"filterType": "NOTIONAL", "minNotional": "5.0"}]}
        for s in symbols
    ]}


# --- symbols ---------------------------------------------------------------
def test_symbols_resolve_in_compact_form():
    s = BinanceSymbols.from_payload(info("HEMIUSDT", "SAGAUSDT"))
    assert s.resolve("HEMIUSDT") == "HEMIUSDT"
    assert s.resolve("HEMI-USDT") == "HEMIUSDT"
    assert s.resolve("hemi/usdt".upper()) == "HEMIUSDT"
    assert s.resolve("NOPEUSDT") is None


def test_halted_symbols_are_excluded():
    payload = info("AUSDT")
    payload["symbols"].append({"symbol": "BUSDT", "status": "HALT",
                               "baseAsset": "B", "quoteAsset": "USDT",
                               "filters": []})
    s = BinanceSymbols.from_payload(payload)
    assert s.resolve("BUSDT") is None
    assert len(s) == 1


def test_min_notional_is_exposed():
    s = BinanceSymbols.from_payload(info("HEMIUSDT"))
    assert s.min_notional("HEMIUSDT") == 5.0
    assert s.min_notional("NOPEUSDT") == 0.0


# --- paging ----------------------------------------------------------------
class Resp:
    def __init__(self, payload, status=200):
        self._p, self.status = payload, status
        self.headers = {}

    async def json(self):
        return self._p

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class PagingSession:
    """Serves at most 1000 klines per call, oldest first, like the venue."""

    def __init__(self, step_ms=1000, available=2500):
        self.step, self.available = step_ms, available
        self.calls = []

    def get(self, url):
        import urllib.parse as up

        q = dict(up.parse_qsl(url.split("?", 1)[1]))
        start, end = int(q["startTime"]), int(q["endTime"])
        self.calls.append((start, end))
        first = start
        rows = []
        while len(rows) < 1000 and first <= end and len(rows) < self.available:
            rows.append([first, "1", "1.1", "0.9", "1.05", "10", first + self.step,
                         "100"])
            first += self.step
        return Resp(rows)


def test_a_range_pages_past_the_thousand_cap():
    """Without paging, a three-day minute window returns its first sixteen
    hours and the rest looks like a symbol that did not exist yet."""
    session = PagingSession(step_ms=60_000, available=10_000)
    rows = run(_fetch_range(session, "XUSDT", "1m", 0, 60_000 * 4_320))
    assert len(session.calls) > 1
    assert len(rows) > 1000
    starts = [s for s, _e in session.calls]
    assert starts == sorted(starts)
    assert len(set(starts)) == len(starts)


def test_paging_stops_on_a_short_page():
    session = PagingSession(step_ms=1000, available=10)
    rows = run(_fetch_range(session, "XUSDT", "1s", 0, 1_000_000))
    assert len(session.calls) == 1
    assert len(rows) == 10


def test_a_non_200_ends_the_range():
    class Failing:
        def __init__(self):
            self.calls = 0

        def get(self, url):
            self.calls += 1
            return Resp({"code": -1121}, status=400)

    session = Failing()
    assert run(_fetch_range(session, "XUSDT", "1s", 0, 100_000)) == []
    assert session.calls == 1


# --- the two windows -------------------------------------------------------
class RecordingSession(PagingSession):
    def __init__(self):
        super().__init__(step_ms=1000, available=5)
        self.intervals = []

    def get(self, url):
        import urllib.parse as up

        q = dict(up.parse_qsl(url.split("?", 1)[1]))
        self.intervals.append((q["interval"], int(q["startTime"]), int(q["endTime"])))
        return super().get(url)


def test_seconds_around_the_call_and_minutes_before_it(tmp_path):
    """Three days of seconds would be a quarter of a million bars to answer a
    question minutes answer perfectly well."""
    session = RecordingSession()
    post = 1_700_000_000_000
    run(fetch_klines(session, [("XUSDT", post)], tmp_path / "p.jsonl",
                     spike_before_s=600, spike_after_s=1_800, lookback_days=3))

    grains = {i for i, _s, _e in session.intervals}
    assert grains == {"1s", "1m"}

    second_window = [x for x in session.intervals if x[0] == "1s"][0]
    assert second_window[1] == post - 600_000
    assert second_window[2] == post + 1_800_000

    minute_window = [x for x in session.intervals if x[0] == "1m"][0]
    # A margin past the requested lookback, so the outermost measurement is not
    # sitting on the very first candle where the feed refuses to extrapolate.
    assert minute_window[1] < post - 3 * 86_400_000


def test_repeated_calls_on_one_symbol_merge_into_one_window(tmp_path):
    session = RecordingSession()
    post = 1_700_000_000_000
    run(fetch_klines(session, [("XUSDT", post), ("XUSDT", post + 60_000)],
                     tmp_path / "p.jsonl", spike_before_s=600, spike_after_s=1_800,
                     lookback_days=0))
    assert len([x for x in session.intervals if x[0] == "1s"]) == 1


def test_distant_calls_are_fetched_separately(tmp_path):
    session = RecordingSession()
    post = 1_700_000_000_000
    run(fetch_klines(session, [("XUSDT", post), ("XUSDT", post + 10 * 86_400_000)],
                     tmp_path / "p.jsonl", spike_before_s=600, spike_after_s=1_800,
                     lookback_days=0))
    assert len([x for x in session.intervals if x[0] == "1s"]) == 2


def test_rows_land_in_the_scoring_feed_format(tmp_path):
    from pumpbot.marketdata.historical import HistoricalFeed

    session = RecordingSession()
    path = tmp_path / "p.jsonl"
    run(fetch_klines(session, [("XUSDT", 1_700_000_000_000)], path,
                     spike_before_s=10, spike_after_s=10, lookback_days=0))
    feed = HistoricalFeed.from_file(path)
    assert feed.symbols == ["XUSDT"]

    row = json.loads(path.read_text().splitlines()[0])
    # Binance orders klines open/high/low/close, unlike KuCoin.
    assert row["high"] == 1.1
    assert row["low"] == 0.9
    assert row["close"] == 1.05


def test_unlisted_symbols_are_not_fetched(tmp_path):
    session = RecordingSession()
    run(fetch_klines(session, [("GHOSTUSDT", 1_700_000_000_000)], tmp_path / "p.jsonl",
                     symbols=BinanceSymbols.from_payload(info("XUSDT")),
                     lookback_days=0))
    assert session.intervals == []


# --- choosing the market-data host ----------------------------------------
def test_the_full_host_is_preferred_when_reachable():
    from pumpbot.marketdata.binance import pick_data_base

    class Session:
        def get(self, url):
            return Resp({}, status=200)

    base, note = run(pick_data_base(Session()))
    assert "api.binance.com" in base
    assert note == ""


def test_a_geo_block_falls_back_and_says_what_it_costs():
    """The mirror's prices are current but its symbol list is smaller, and a
    symbol it lacks is absent from klines too — so a call naming one is
    dropped as unlisted rather than priced."""
    from pumpbot.marketdata.binance import pick_data_base

    class Session:
        def get(self, url):
            return Resp({}, status=451)

    base, note = run(pick_data_base(Session()))
    assert "binance.vision" in base
    assert "451" in note
    assert "smaller" in note


def test_a_network_failure_also_falls_back():
    from pumpbot.marketdata.binance import pick_data_base

    class Session:
        def get(self, url):
            raise OSError("no route to host")

    base, note = run(pick_data_base(Session()))
    assert "binance.vision" in base
    assert "OSError" in note
