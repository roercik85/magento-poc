import json

import pytest

from pumpbot.ingest.telegram_export import (
    ExportError,
    flatten_text,
    read_export,
)


def write_export(tmp_path, chats, *, as_dir=True):
    payload = {"about": "export", "chats": {"about": "x", "list": chats}}
    if as_dir:
        folder = tmp_path / "ChatExport"
        folder.mkdir()
        (folder / "result.json").write_text(json.dumps(payload), encoding="utf-8")
        return folder
    path = tmp_path / "result.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def msg(i, unix, text, **kw):
    out = {"id": i, "type": "message", "date_unixtime": str(int(unix)), "text": text}
    out.update(kw)
    return out


def chat(name, chat_id, messages, kind="public_channel"):
    return {"name": name, "type": kind, "id": chat_id, "messages": messages}


@pytest.fixture
def now():
    import time

    return time.time()


# --- text flattening -------------------------------------------------------
def test_plain_string_text():
    assert flatten_text("BUY $PEPE NOW") == "BUY $PEPE NOW"


def test_formatted_text_is_flattened():
    """A ticker posted in bold arrives as a list. Treating it as a string
    silently drops the one part that matters."""
    value = ["BUY ", {"type": "bold", "text": "$PEPE"}, " NOW\nTP1: 10%"]
    assert flatten_text(value) == "BUY $PEPE NOW\nTP1: 10%"


def test_link_entities_contribute_their_text():
    value = [{"type": "text_link", "text": "chart", "href": "http://x"}, " $PEPE"]
    assert flatten_text(value) == "chart $PEPE"


@pytest.mark.parametrize("value", [None, "", []])
def test_empty_text_variants(value):
    assert flatten_text(value) == ""


# --- reading ---------------------------------------------------------------
def test_reads_channels_and_normalises_ids(tmp_path, now):
    path = write_export(tmp_path, [
        chat("alpha", 1234567890, [msg(1, now - 3600, "BUY $PEPE NOW")]),
    ])
    (h,) = read_export(path, days=7)
    assert h.name == "alpha"
    # The rest of the system uses the -100-prefixed form.
    assert h.chat_id == -1001234567890
    assert h.messages[0].text == "BUY $PEPE NOW"


def test_already_negative_ids_are_left_alone(tmp_path, now):
    path = write_export(tmp_path, [
        chat("alpha", -1001234567890, [msg(1, now - 3600, "BUY $PEPE NOW")]),
    ])
    assert read_export(path, days=7)[0].chat_id == -1001234567890


def test_private_chats_are_skipped(tmp_path, now):
    path = write_export(tmp_path, [
        chat("Mum", 444, [msg(1, now - 3600, "call me")], kind="personal_chat"),
        chat("alpha", 999, [msg(1, now - 3600, "BUY $PEPE NOW")]),
    ])
    assert [h.name for h in read_export(path, days=7)] == ["alpha"]


def test_service_entries_are_ignored(tmp_path, now):
    path = write_export(tmp_path, [
        chat("alpha", 999, [
            msg(1, now - 3600, "BUY $PEPE NOW"),
            {"id": 2, "type": "service", "date_unixtime": str(int(now - 1800)),
             "action": "pin_message"},
        ]),
    ])
    assert len(read_export(path, days=7)[0].messages) == 1


def test_forwards_and_edits_are_counted(tmp_path, now):
    path = write_export(tmp_path, [
        chat("relay", 999, [
            msg(1, now - 3600, "BUY $A NOW", forwarded_from="alpha"),
            msg(2, now - 3500, "BUY $B NOW"),
            msg(3, now - 3400, "BUY $C NOW", edited_unixtime=str(int(now - 3300))),
        ]),
    ])
    h = read_export(path, days=7)[0]
    assert h.forwards == 1
    assert h.edits == 1


def test_messages_outside_the_window_are_dropped(tmp_path, now):
    path = write_export(tmp_path, [
        chat("alpha", 999, [
            msg(1, now - 60 * 86400, "BUY $OLD NOW"),
            msg(2, now - 3600, "BUY $NEW NOW"),
        ]),
    ])
    h = read_export(path, days=7)
    assert len(h[0].messages) == 1
    assert "NEW" in h[0].messages[0].text


def test_channels_with_no_messages_in_window_are_omitted(tmp_path, now):
    path = write_export(tmp_path, [
        chat("stale", 999, [msg(1, now - 90 * 86400, "BUY $OLD NOW")]),
    ])
    assert read_export(path, days=7) == []


def test_span_days_is_measured(tmp_path, now):
    path = write_export(tmp_path, [
        chat("alpha", 999, [
            msg(1, now - 5 * 86400, "BUY $A NOW"),
            msg(2, now - 1 * 86400, "BUY $B NOW"),
        ]),
    ])
    assert read_export(path, days=30)[0].span_days == pytest.approx(4.0, abs=0.1)


def test_post_and_receive_times_match(tmp_path, now):
    """An export has no arrival time. Inventing one would fabricate latency."""
    path = write_export(tmp_path, [
        chat("alpha", 999, [msg(1, now - 3600, "BUY $PEPE NOW")]),
    ])
    m = read_export(path, days=7)[0].messages[0]
    assert m.received_wall_ms == m.posted_wall_ms
    assert m.source_session == "export"


def test_iso_date_without_unixtime(tmp_path):
    from datetime import datetime, timedelta, timezone

    recent = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S")
    payload = {"chats": {"list": [
        {"name": "alpha", "type": "public_channel", "id": 999, "messages": [
            {"id": 1, "type": "message", "date": recent, "text": "BUY $PEPE NOW"},
        ]},
    ]}}
    path = tmp_path / "result.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert len(read_export(path, days=7)[0].messages) == 1


# --- input shapes and errors ----------------------------------------------
def test_accepts_a_folder_or_the_json_file(tmp_path, now):
    chats = [chat("alpha", 999, [msg(1, now - 3600, "BUY $PEPE NOW")])]
    assert read_export(write_export(tmp_path, chats), days=7)
    other = tmp_path / "direct"
    other.mkdir()
    assert read_export(write_export(other, chats, as_dir=False), days=7)


def test_single_chat_export_shape(tmp_path, now):
    path = tmp_path / "result.json"
    path.write_text(json.dumps({
        "name": "alpha", "type": "public_channel", "id": 999,
        "messages": [msg(1, now - 3600, "BUY $PEPE NOW")],
    }), encoding="utf-8")
    assert read_export(path, days=7)[0].name == "alpha"


def test_missing_export_is_explained(tmp_path):
    with pytest.raises(ExportError, match="not found"):
        read_export(tmp_path / "nope", days=7)


def test_folder_without_result_json_is_explained(tmp_path):
    folder = tmp_path / "ChatExport"
    folder.mkdir()
    with pytest.raises(ExportError, match="result.json"):
        read_export(folder, days=7)


def test_html_export_is_explained_not_crashed(tmp_path):
    """Telegram Desktop defaults to HTML; the JSON option has to be chosen."""
    path = tmp_path / "result.json"
    path.write_text("<html><body>not json</body></html>", encoding="utf-8")
    with pytest.raises(ExportError, match="not valid JSON"):
        read_export(path, days=7)


def test_unrecognised_json_names_the_export_setting(tmp_path):
    path = tmp_path / "result.json"
    path.write_text(json.dumps({"something": "else"}), encoding="utf-8")
    with pytest.raises(ExportError, match="Machine-readable JSON"):
        read_export(path, days=7)


# --- it feeds the screen ---------------------------------------------------
def test_export_histories_screen_correctly(tmp_path, now):
    from pumpbot.ingest.history import screen
    from pumpbot.parsing.extractor import SignalExtractor

    path = write_export(tmp_path, [
        chat("relay", 111, [
            msg(i, now - (30 - i) * 3600, f"BUY $AA{i}X NOW", forwarded_from="alpha")
            for i in range(25)
        ]),
    ])
    h = read_export(path, days=7)[0]
    s = screen(h, SignalExtractor(quote_assets=["USDT"], min_confidence=0.55))
    assert s.forward_ratio == 1.0
    assert s.rejected


# --- folders of per-chat exports -------------------------------------------
def single_chat_export(folder, name, chat_id, messages):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "result.json").write_text(json.dumps({
        "name": name, "type": "public_channel", "id": chat_id,
        "messages": messages,
    }), encoding="utf-8")


def test_folder_of_per_chat_exports_is_read(tmp_path, now):
    """The macOS App Store app has no global export, only per-chat, so twenty
    channels means twenty folders."""
    single_chat_export(tmp_path / "ChatExport_alpha", "alpha", 111,
                       [msg(1, now - 3600, "BUY $AAA NOW")])
    single_chat_export(tmp_path / "ChatExport_beta", "beta", 222,
                       [msg(1, now - 3600, "BUY $BBB NOW")])
    names = sorted(h.name for h in read_export(tmp_path, days=7))
    assert names == ["alpha", "beta"]


def test_nested_export_folders_are_found(tmp_path, now):
    single_chat_export(tmp_path / "Telegram" / "ChatExport_alpha", "alpha", 111,
                       [msg(1, now - 3600, "BUY $AAA NOW")])
    assert [h.name for h in read_export(tmp_path, days=7)] == ["alpha"]


def test_the_same_channel_across_exports_is_merged(tmp_path, now):
    """Per-chat exports taken on different days overlap. Emitting the channel
    twice would split its sample so neither half reaches a score."""
    single_chat_export(tmp_path / "export_monday", "alpha", 111, [
        msg(1, now - 5 * 86400, "BUY $AAA NOW"),
        msg(2, now - 4 * 86400, "BUY $BBB NOW"),
    ])
    single_chat_export(tmp_path / "export_friday", "alpha", 111, [
        msg(2, now - 4 * 86400, "BUY $BBB NOW"),        # overlap
        msg(3, now - 1 * 86400, "BUY $CCC NOW"),
    ])
    histories = read_export(tmp_path, days=30)
    assert len(histories) == 1
    h = histories[0]
    assert [m.message_id for m in h.messages] == [1, 2, 3]
    assert h.span_days == pytest.approx(4.0, abs=0.1)


def test_merged_messages_are_in_time_order(tmp_path, now):
    single_chat_export(tmp_path / "b", "alpha", 111,
                       [msg(9, now - 1 * 86400, "BUY $LATE NOW")])
    single_chat_export(tmp_path / "a", "alpha", 111,
                       [msg(1, now - 5 * 86400, "BUY $EARLY NOW")])
    stamps = [m.posted_wall_ms for m in read_export(tmp_path, days=30)[0].messages]
    assert stamps == sorted(stamps)


def test_a_folder_with_no_exports_anywhere_is_explained(tmp_path):
    (tmp_path / "random").mkdir()
    with pytest.raises(ExportError, match="neither do the folders inside"):
        read_export(tmp_path, days=7)


def test_html_export_names_the_json_setting(tmp_path):
    folder = tmp_path / "ChatExport"
    folder.mkdir()
    (folder / "result.json").write_text("<html></html>", encoding="utf-8")
    with pytest.raises(ExportError, match="Machine-readable JSON"):
        read_export(folder, days=7)


# --- diagnosing an empty export --------------------------------------------
def test_only_my_messages_restriction_is_named(tmp_path):
    """The bulk export lists public channels but exports none of their text,
    which is indistinguishable from a bad date range unless you check whether
    any channel had text at all."""
    from pumpbot.ingest.telegram_export import ExportStats, explain_empty

    path = write_export(tmp_path, [
        chat("pump one", 111, []),
        chat("pump two", 222, []),
    ])
    stats = ExportStats()
    assert read_export(path, days=30, stats=stats) == []
    assert stats.channels_seen == 2
    assert stats.channels_with_text == 0
    assert stats.looks_like_only_my_messages

    why = explain_empty(stats, 30)
    assert "only my messages" in why
    assert "Export chat history" in why


def test_a_stale_export_is_diagnosed_as_a_date_range(tmp_path, now):
    from pumpbot.ingest.telegram_export import ExportStats, explain_empty

    path = write_export(tmp_path, [
        chat("alpha", 111, [msg(1, now - 90 * 86400, "BUY $OLD NOW")]),
    ])
    stats = ExportStats()
    assert read_export(path, days=7, stats=stats) == []
    assert not stats.looks_like_only_my_messages
    assert stats.messages_outside_window == 1

    why = explain_empty(stats, 7)
    assert "outside the last 7 days" in why
    assert "only my messages" not in why


def test_an_export_with_content_is_not_flagged(tmp_path, now):
    from pumpbot.ingest.telegram_export import ExportStats

    path = write_export(tmp_path, [
        chat("alpha", 111, [msg(1, now - 3600, "BUY $PEPE NOW")]),
    ])
    stats = ExportStats()
    assert read_export(path, days=7, stats=stats)
    assert stats.channels_with_text == 1
    assert stats.messages_in_window == 1
    assert not stats.looks_like_only_my_messages


def test_stats_count_files_and_chats(tmp_path, now):
    from pumpbot.ingest.telegram_export import ExportStats

    single_chat_export(tmp_path / "a", "alpha", 111,
                       [msg(1, now - 3600, "BUY $AAA NOW")])
    single_chat_export(tmp_path / "b", "beta", 222,
                       [msg(1, now - 3600, "BUY $BBB NOW")])
    stats = ExportStats()
    read_export(tmp_path, days=7, stats=stats)
    assert stats.files == 2
    assert stats.channels_seen == 2


# --- inspect ---------------------------------------------------------------
def run_inspect(tmp_path, capsys, **kw):
    import argparse

    from pumpbot.cli import cmd_inspect

    args = argparse.Namespace(
        config=None, from_export=str(tmp_path), channel=None, days=30,
        limit=100, show_misses=False, min_confidence=None, no_venue_check=True,
        summary=False,
    )
    for k, v in kw.items():
        setattr(args, k, v)
    code = cmd_inspect(args)
    return code, capsys.readouterr().out


def test_inspect_separates_calls_from_prose(tmp_path, capsys, now):
    """"0 calls" has two causes — a quiet channel or an over-strict parser —
    and the summary line cannot tell them apart."""
    write_export(tmp_path, [
        chat("alpha", 111, [
            msg(1, now - 3600, "BUY $PEPE NOW 🚀"),
            msg(2, now - 3500, "LONG SETUP incoming"),
            msg(3, now - 3400, "gm everyone"),
        ]),
    ])
    code, out = run_inspect(tmp_path, capsys)
    assert code == 0
    assert "PEPEUSDT" in out
    assert "1 tradable call(s)" in out
    assert "2 non-calls" in out


def test_inspect_shows_the_matching_pattern(tmp_path, capsys, now):
    write_export(tmp_path, [
        chat("alpha", 111, [msg(1, now - 3600, "BUY $PEPE NOW 🚀")]),
    ])
    _, out = run_inspect(tmp_path, capsys)
    assert "via imperative" in out or "via cashtag" in out
    assert "confidence" in out


def test_inspect_can_show_what_did_not_parse(tmp_path, capsys, now):
    """Where a real call the parser missed would show up."""
    write_export(tmp_path, [
        chat("alpha", 111, [msg(1, now - 3600, "some unparseable prose")]),
    ])
    _, quiet = run_inspect(tmp_path, capsys)
    assert "some unparseable prose" not in quiet
    assert "--show-misses" in quiet

    _, loud = run_inspect(tmp_path, capsys, show_misses=True)
    assert "some unparseable prose" in loud


def test_inspect_filters_by_channel(tmp_path, capsys, now):
    write_export(tmp_path, [
        chat("alpha calls", 111, [msg(1, now - 3600, "BUY $PEPE NOW")]),
        chat("beta signals", 222, [msg(1, now - 3600, "BUY $BONK NOW")]),
    ])
    _, out = run_inspect(tmp_path, capsys, channel="beta")
    assert "beta signals" in out
    assert "alpha calls" not in out


def test_inspect_confidence_override_changes_what_parses(tmp_path, capsys, now):
    write_export(tmp_path, [
        chat("alpha", 111, [msg(1, now - 3600, "Coin: PEPE")]),
    ])
    _, strict = run_inspect(tmp_path, capsys, min_confidence=0.99)
    assert "0 tradable call(s)" in strict

    _, loose = run_inspect(tmp_path, capsys, min_confidence=0.1)
    assert "1 tradable call(s)" in loose


def test_inspect_reports_an_unreadable_export(tmp_path, capsys):
    code, _ = run_inspect(tmp_path, capsys, from_export=str(tmp_path / "nope"))
    assert code == 1


def test_per_chat_and_account_exports_side_by_side(tmp_path, now):
    """The real layout: several per-chat exports and an account export in one
    downloads folder. The account export repeats a channel the per-chat one
    already covered."""
    single_chat_export(tmp_path / "ChatExport_2026-09-20", "alpha", 111, [
        msg(i, now - (10 - i) * 86400, f"BUY $AAA{i}X NOW") for i in range(5)
    ])
    single_chat_export(tmp_path / "ChatExport_2026-09-20 (1)", "beta", 222, [
        msg(i, now - (10 - i) * 86400, f"BUY $BBB{i}X NOW") for i in range(5)
    ])
    (tmp_path / "DataExport_2026-09-20").mkdir()
    (tmp_path / "DataExport_2026-09-20" / "result.json").write_text(json.dumps({
        "chats": {"list": [
            # Overlaps the per-chat export, plus messages it did not contain.
            chat("alpha", 111, [
                msg(i, now - (10 - i) * 86400, f"BUY $AAA{i}X NOW")
                for i in range(5)
            ] + [msg(90, now - 86400, "BUY $LATERX NOW")]),
            chat("empty public", 999, []),      # the export restriction
        ]},
    }), encoding="utf-8")

    histories = {h.name: h for h in read_export(tmp_path, days=30)}
    assert set(histories) == {"alpha", "beta"}

    ids = [m.message_id for m in histories["alpha"].messages]
    assert len(ids) == len(set(ids)), "overlapping messages were duplicated"
    assert 90 in ids, "messages only in the account export were lost"
    assert len(ids) == 6


def test_inspect_summary_aggregates_by_symbol_and_pattern(tmp_path, capsys, now):
    """A channel producing seventy apparent calls needs the distribution, not
    seventy lines: one prose word repeating sixty times is visible here and
    invisible in a message-by-message dump."""
    write_export(tmp_path, [
        chat("alpha", 111, [
            *[msg(i, now - 3600 - i, "BUY $PEPE NOW") for i in range(5)],
            *[msg(100 + i, now - 3600 - i, "BUY $BONK NOW") for i in range(2)],
        ]),
    ])
    _, out = run_inspect(tmp_path, capsys, summary=True)
    assert "PEPEUSDT" in out
    assert "BONKUSDT" in out
    assert "by pattern:" in out
    assert "7 parse(s) over 2 distinct symbol(s)" in out


def test_inspect_summary_marks_unlisted_symbols(tmp_path, capsys, now):
    write_export(tmp_path, [
        chat("alpha", 111, [msg(1, now - 3600, "BUY $PEPE NOW")]),
    ])

    class OnlyBonk:
        @staticmethod
        def resolve(symbol):
            return "BONK-USDT" if symbol == "BONKUSDT" else None

    import argparse

    from pumpbot.cli import _print_parse_summary
    from pumpbot.ingest.telegram_export import read_export as _read
    from pumpbot.parsing.extractor import SignalExtractor

    history = _read(tmp_path, days=30)[0]
    _print_parse_summary(
        history,
        SignalExtractor(quote_assets=["USDT"], min_confidence=0.55),
        OnlyBonk(), 20,
    )
    assert "NOT listed" in capsys.readouterr().out


def test_inspect_summary_on_a_channel_with_no_calls(tmp_path, capsys, now):
    write_export(tmp_path, [
        chat("alpha", 111, [msg(1, now - 3600, "gm everyone")]),
    ])
    _, out = run_inspect(tmp_path, capsys, summary=True)
    assert "no calls parsed at all" in out
