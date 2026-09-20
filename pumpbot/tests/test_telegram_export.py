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
