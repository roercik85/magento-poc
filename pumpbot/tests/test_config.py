import pytest
import yaml

from pumpbot.config import ConfigError, load_config


def write(tmp_path, data):
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(data), encoding="utf-8")
    return p


def test_missing_file_explains_itself(tmp_path):
    with pytest.raises(ConfigError, match="config.example.yaml"):
        load_config(tmp_path / "nope.yaml")


def test_defaults_load(tmp_path):
    cfg = load_config(write(tmp_path, {"mode": "simulate"}))
    assert cfg.mode == "simulate"
    assert cfg.risk.position_notional_quote == 50.0


def test_nested_sections_are_built_not_left_as_dicts(tmp_path):
    cfg = load_config(write(tmp_path, {"risk": {"position_notional_quote": 25.0}}))
    assert cfg.risk.position_notional_quote == 25.0
    assert not isinstance(cfg.risk, dict)


def test_unknown_key_is_an_error(tmp_path):
    """A silently ignored typo in a risk limit is found out about from the
    account balance."""
    with pytest.raises(ConfigError, match="positon_notional_quote"):
        load_config(write(tmp_path, {"risk": {"positon_notional_quote": 25.0}}))


def test_unknown_top_level_key_is_an_error(tmp_path):
    with pytest.raises(ConfigError, match="riskk"):
        load_config(write(tmp_path, {"riskk": {}}))


def test_ladder_is_built_into_dataclasses(tmp_path):
    cfg = load_config(write(tmp_path, {
        "strategy": {"take_profit_ladder": [
            {"gain_pct": 5.0, "fraction": 0.5},
            {"gain_pct": 10.0, "fraction": 0.5},
        ]}
    }))
    assert cfg.strategy.take_profit_ladder[0].gain_pct == 5.0


def test_ladder_over_one_hundred_percent_is_rejected(tmp_path):
    with pytest.raises(ConfigError, match="take_profit_ladder"):
        load_config(write(tmp_path, {
            "strategy": {"take_profit_ladder": [
                {"gain_pct": 5.0, "fraction": 0.7},
                {"gain_pct": 10.0, "fraction": 0.7},
            ]}
        }))


def test_scoring_weights_must_sum_to_one(tmp_path):
    with pytest.raises(ConfigError, match="sum to 1.0"):
        load_config(write(tmp_path, {
            "scoring": {"weights": {"hit_rate": 0.9, "median_return": 0.9,
                                    "originator": 0.1, "consistency": 0.1}}
        }))


def test_unknown_mode_rejected(tmp_path):
    with pytest.raises(ConfigError, match="unknown mode"):
        load_config(write(tmp_path, {"mode": "yolo"}))


def test_channels_are_built(tmp_path):
    cfg = load_config(write(tmp_path, {
        "telegram": {"channels": [{"id": -1001, "name": "x", "tier": "trusted"}]}
    }))
    assert cfg.telegram.channels[0].tier == "trusted"


# --- live guards ----------------------------------------------------------
def test_live_requires_credentials(tmp_path, monkeypatch):
    for var in ("PUMPBOT_API_KEY", "PUMPBOT_API_SECRET", "PUMPBOT_API_PASSPHRASE"):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(ConfigError, match="PUMPBOT_API_KEY"):
        load_config(write(tmp_path, {"mode": "live"}))


def test_kucoin_live_requires_the_passphrase(tmp_path, monkeypatch):
    """KuCoin signs with a third factor. Finding that out at the first order
    rather than at startup wastes a live session."""
    monkeypatch.setenv("PUMPBOT_API_KEY", "k")
    monkeypatch.setenv("PUMPBOT_API_SECRET", "s")
    monkeypatch.delenv("PUMPBOT_API_PASSPHRASE", raising=False)
    with pytest.raises(ConfigError, match="PUMPBOT_API_PASSPHRASE"):
        load_config(write(tmp_path, {
            "mode": "live",
            "execution": {"venue": "kucoin"},
            "risk": {"position_notional_quote": 5.0},
        }))


def test_binance_live_does_not_require_a_passphrase(tmp_path, monkeypatch):
    monkeypatch.setenv("PUMPBOT_API_KEY", "k")
    monkeypatch.setenv("PUMPBOT_API_SECRET", "s")
    monkeypatch.delenv("PUMPBOT_API_PASSPHRASE", raising=False)
    cfg = load_config(write(tmp_path, {
        "mode": "live",
        "execution": {"venue": "binance"},
        "marketdata": {"venue": "binance", "rest_base": "https://api.binance.com"},
        "risk": {"position_notional_quote": 5.0},
    }))
    assert cfg.execution.venue == "binance"


def test_unsupported_venue_is_rejected(tmp_path):
    with pytest.raises(ConfigError, match="unsupported venue"):
        load_config(write(tmp_path, {"execution": {"venue": "mtgox"}}))


def test_live_enforces_the_notional_hard_cap(tmp_path, monkeypatch):
    monkeypatch.setenv("PUMPBOT_API_KEY", "k")
    monkeypatch.setenv("PUMPBOT_API_SECRET", "s")
    monkeypatch.setenv("PUMPBOT_API_PASSPHRASE", "p")
    with pytest.raises(ConfigError, match="hard cap"):
        load_config(write(tmp_path, {
            "mode": "live",
            "risk": {"position_notional_quote": 10_000.0},
            "execution": {"live": {"max_notional_quote_hard_cap": 100.0}},
        }))


def test_live_passes_with_credentials_and_a_small_size(tmp_path, monkeypatch):
    monkeypatch.setenv("PUMPBOT_API_KEY", "k")
    monkeypatch.setenv("PUMPBOT_API_SECRET", "s")
    monkeypatch.setenv("PUMPBOT_API_PASSPHRASE", "p")
    cfg = load_config(write(tmp_path, {
        "mode": "live", "risk": {"position_notional_quote": 20.0},
    }))
    assert cfg.is_live


def test_example_config_is_valid():
    """The shipped example must actually load, or the first-run experience is
    a stack trace."""
    from pathlib import Path

    example = Path(__file__).resolve().parents[1] / "config.example.yaml"
    cfg = load_config(example)
    assert cfg.mode == "simulate"
    assert len(cfg.strategy.take_profit_ladder) == 3


def test_ten_usd_profile_is_valid_and_guarded():
    """The shipped live-test profile must load and keep exposure bounded."""
    from pathlib import Path

    cfg = load_config(Path(__file__).resolve().parents[1] / "config.10usd.example.yaml")
    assert cfg.execution.venue == "kucoin"
    assert cfg.risk.starting_equity == 10.0
    # Worst case exposure cannot exceed the account.
    exposure = cfg.risk.position_notional_quote * cfg.risk.max_concurrent_positions
    assert exposure < cfg.risk.starting_equity
    # A config mistake cannot put more than the hard cap into one order.
    assert cfg.risk.position_notional_quote <= cfg.execution.live.max_notional_quote_hard_cap
    assert cfg.execution.live.max_notional_quote_hard_cap < cfg.risk.starting_equity


# --- telegram credentials from the environment -----------------------------
def test_env_supplies_telegram_credentials(tmp_path, monkeypatch):
    """Secrets should never need to live in a file at all."""
    monkeypatch.setenv("PUMPBOT_TG_API_ID", "987654")
    monkeypatch.setenv("PUMPBOT_TG_API_HASH", "deadbeef" * 4)
    cfg = load_config(write(tmp_path, {"mode": "simulate"}))
    assert cfg.telegram.api_id == 987654
    assert cfg.telegram.api_hash == "deadbeef" * 4


def test_env_overrides_the_file(tmp_path, monkeypatch):
    monkeypatch.setenv("PUMPBOT_TG_API_ID", "111")
    monkeypatch.setenv("PUMPBOT_TG_API_HASH", "fromenv")
    cfg = load_config(write(tmp_path, {
        "telegram": {"api_id": 222, "api_hash": "fromfile"},
    }))
    assert cfg.telegram.api_id == 111
    assert cfg.telegram.api_hash == "fromenv"


def test_file_is_used_when_env_is_absent(tmp_path, monkeypatch):
    monkeypatch.delenv("PUMPBOT_TG_API_ID", raising=False)
    monkeypatch.delenv("PUMPBOT_TG_API_HASH", raising=False)
    cfg = load_config(write(tmp_path, {
        "telegram": {"api_id": 222, "api_hash": "fromfile"},
    }))
    assert cfg.telegram.api_id == 222


def test_non_numeric_api_id_env_is_a_clear_error(tmp_path, monkeypatch):
    monkeypatch.setenv("PUMPBOT_TG_API_ID", "not-a-number")
    with pytest.raises(ConfigError, match="must be an integer"):
        load_config(write(tmp_path, {"mode": "simulate"}))


def test_shipped_templates_contain_no_credentials():
    """Regression: the tracked templates must never carry a real secret."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    for name in ("config.example.yaml", "config.10usd.example.yaml"):
        cfg = load_config(root / name)
        assert cfg.telegram.api_id == 0, f"{name} ships a real api_id"
        assert cfg.telegram.api_hash == "", f"{name} ships a real api_hash"


# --- first-run guard -------------------------------------------------------
def test_missing_telegram_credentials_exits_cleanly(capsys, monkeypatch):
    """The first wall a new user hits must not be a traceback."""
    from pumpbot.cli import _require_telegram
    from pumpbot.config import Config

    cfg = Config()
    with pytest.raises(SystemExit) as exc:
        _require_telegram(cfg, "config.yaml")
    assert exc.value.code == 2

    err = capsys.readouterr().err
    assert "my.telegram.org" in err
    assert "PUMPBOT_TG_API_ID" in err
    assert "PUMPBOT_TG_API_HASH" in err
    assert "Traceback" not in err


def test_guard_passes_once_credentials_are_present():
    from pumpbot.cli import _require_telegram
    from pumpbot.config import Config

    cfg = Config()
    cfg.telegram.api_id = 123
    cfg.telegram.api_hash = "x" * 32
    _require_telegram(cfg, "config.yaml")        # must not raise


def test_guard_names_only_what_is_missing(capsys):
    from pumpbot.cli import _require_telegram
    from pumpbot.config import Config

    cfg = Config()
    cfg.telegram.api_id = 123
    with pytest.raises(SystemExit):
        _require_telegram(cfg, "config.yaml")
    err = capsys.readouterr().err
    assert "api_hash not set" in err
    assert "api_id and" not in err


# --- defaults must stand on their own --------------------------------------
def test_default_config_is_a_working_strategy():
    """An empty ladder would silently disable the trailing stop, which only
    arms on the first rung — so a default run would measure something other
    than what the docs describe."""
    from pumpbot.config import Config

    cfg = Config()
    cfg.validate()
    assert cfg.strategy.take_profit_ladder, "defaults ship no take-profit ladder"
    fractions = sum(r.fraction for r in cfg.strategy.take_profit_ladder)
    assert fractions == pytest.approx(1.0)
    gains = [r.gain_pct for r in cfg.strategy.take_profit_ladder]
    assert gains == sorted(gains)


def test_default_ladder_is_not_shared_between_instances():
    """A mutable default leaking across configs would make one run's tuning
    silently affect the next."""
    from pumpbot.config import Config

    a, b = Config(), Config()
    a.strategy.take_profit_ladder.clear()
    assert b.strategy.take_profit_ladder


def test_simulate_runs_without_a_config_file(tmp_path, capsys):
    """README promises `pumpbot simulate` works with no setup at all."""
    import argparse

    from pumpbot.cli import _load

    args = argparse.Namespace(config=str(tmp_path / "absent.yaml"), defaults_ok=True)
    cfg = _load(args)
    assert cfg.mode == "simulate"
    assert cfg.strategy.take_profit_ladder
    assert "built-in defaults" in capsys.readouterr().out


def test_commands_that_need_a_config_still_demand_one(tmp_path):
    import argparse

    from pumpbot.cli import _load

    args = argparse.Namespace(config=str(tmp_path / "absent.yaml"), defaults_ok=False)
    with pytest.raises(SystemExit) as exc:
        _load(args)
    assert exc.value.code == 2


def test_env_credentials_still_apply_to_the_default_config(tmp_path, monkeypatch):
    import argparse

    from pumpbot.cli import _load

    monkeypatch.setenv("PUMPBOT_TG_API_ID", "4242")
    monkeypatch.setenv("PUMPBOT_TG_API_HASH", "h" * 32)
    args = argparse.Namespace(config=str(tmp_path / "absent.yaml"), defaults_ok=True)
    cfg = _load(args)
    assert cfg.telegram.api_id == 4242


def test_session_directory_is_created(tmp_path, monkeypatch):
    """state/ is gitignored, so it is absent from every fresh clone. Telethon
    opens the session in SQLite inside its constructor, and a missing
    directory fails with 'unable to open database file' — which reads like a
    Telegram problem and is not one."""
    from pumpbot.ingest.telegram import prepare_session_path

    monkeypatch.chdir(tmp_path)
    assert not (tmp_path / "state").exists()
    returned = prepare_session_path("state/pumpbot")
    assert returned == "state/pumpbot"
    assert (tmp_path / "state").is_dir()


def test_session_path_without_a_directory_is_left_alone(tmp_path, monkeypatch):
    from pumpbot.ingest.telegram import prepare_session_path

    monkeypatch.chdir(tmp_path)
    assert prepare_session_path("pumpbot") == "pumpbot"


def test_preparing_an_existing_session_directory_is_idempotent(tmp_path, monkeypatch):
    from pumpbot.ingest.telegram import prepare_session_path

    monkeypatch.chdir(tmp_path)
    (tmp_path / "state").mkdir()
    prepare_session_path("state/pumpbot")
    prepare_session_path("state/pumpbot")
    assert (tmp_path / "state").is_dir()


# --- login diagnostics -----------------------------------------------------
def test_every_telethon_code_type_has_a_plain_explanation():
    """"I never got a code" is almost always "it went somewhere I was not
    looking", so every delivery type Telegram can pick must be explained.

    Skipped where telethon is absent: it is an optional dependency, and
    simulation must keep working without it.
    """
    auth = pytest.importorskip("telethon.tl.types").auth

    from pumpbot.cli import _CODE_DESTINATIONS

    telethon_types = {
        n for n in dir(auth)
        if n.startswith("SentCodeType") and n != "SentCodeTypeSetUpEmailRequired"
    }
    missing = telethon_types - set(_CODE_DESTINATIONS)
    assert not missing, f"no explanation for: {sorted(missing)}"


def test_app_delivery_says_it_is_not_an_sms():
    from pumpbot.cli import _CODE_DESTINATIONS

    text = _CODE_DESTINATIONS["SentCodeTypeApp"]
    assert "NOT an SMS" in text
    assert "Telegram" in text


def test_word_and_phrase_codes_are_called_out():
    """Newer Telegram sends a word or a phrase instead of digits; somebody
    scanning for a number will not recognise it."""
    from pumpbot.cli import _CODE_DESTINATIONS

    assert "WORD" in _CODE_DESTINATIONS["SentCodeTypeSmsWord"]
    assert "PHRASE" in _CODE_DESTINATIONS["SentCodeTypeSmsPhrase"]
