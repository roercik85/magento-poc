"""Command line interface.

    pumpbot simulate            # paper run, synthetic or recorded corpus
    pumpbot record              # listen and log, place nothing
    pumpbot score               # rank channels from a recorded corpus
    pumpbot discover            # enumerate candidate channels
    pumpbot gate                # how close simulation is to unlocking live
    pumpbot live                # real orders — gated three ways
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .config import Config, ConfigError, load_config
from .engine import RunResult
from .gate import PromotionGate, config_fingerprint
from .logbook import Logbook
from .reporting.report import ReportWriter
from .runner import (
    DEFAULT_SYNTHETIC_CHANNELS,
    LiveRunner,
    SimulationRunner,
    recorded_messages,
    synthetic_messages,
)


def _install_uvloop() -> str:
    try:
        import uvloop

        uvloop.install()
        return "uvloop"
    except ImportError:
        return "asyncio"


# ---------------------------------------------------------------------------
def cmd_simulate(args: argparse.Namespace) -> int:
    cfg = _load(args)
    cfg.mode = "simulate"
    if args.label:
        cfg.run_label = args.label
    if args.min_channel_score is not None:
        cfg.risk.min_channel_score = args.min_channel_score

    ground_truth: dict = {}
    feed = None
    if args.corpus:
        messages = recorded_messages(args.corpus)
        if not messages:
            print(f"error: no messages found in {args.corpus}", file=sys.stderr)
            return 2
        source_desc = f"{len(messages)} recorded messages from {args.corpus}"
        proceed, feed = _load_prices(cfg, args, messages)
        if not proceed:
            return 2
    else:
        messages, ground_truth = synthetic_messages(
            count=args.messages, seed=args.seed, call_rate=args.call_rate
        )
        source_desc = (
            f"{len(messages)} synthetic messages "
            f"({len(DEFAULT_SYNTHETIC_CHANNELS)} channels, seed {args.seed})"
        )

    channel_scores = _load_channel_scores(args.use_scores) if args.use_scores else None
    if channel_scores:
        print(f"▸ channel filter: {len(channel_scores)} scored channel(s) from "
              f"{args.use_scores}, minimum {cfg.risk.min_channel_score}")

    print(f"▸ simulating: {source_desc}")
    runner = SimulationRunner(
        cfg,
        messages,
        seed=args.seed,
        symbol_quality=ground_truth,
        channel_scores=channel_scores,
        feed=feed,
    )
    result = asyncio.run(runner.run())
    return _finish(cfg, result, args)


def cmd_record(args: argparse.Namespace) -> int:
    cfg = _load(args)
    _require_telegram(cfg, args.config)
    cfg.mode = "record"
    loop_impl = _install_uvloop()

    from .execution.simulator import SimulatedExecutor
    from .marketdata.feed import SyntheticPumpFeed

    corpus = Path(args.out or "data/recorded/corpus.jsonl")
    log = Logbook(corpus)
    # Record mode never sends an order, so the executor and feed are inert
    # stand-ins that exist only to satisfy the engine's wiring.
    feed = SyntheticPumpFeed(seed=0)
    executor = SimulatedExecutor(cfg.execution.simulation, feed)

    market = None
    prices = Path(args.prices_out or "data/recorded/prices.jsonl")
    if cfg.marketdata.venue == "kucoin" and not args.no_market_data:
        from .marketdata.kucoin import KucoinRecordingSession

        market = KucoinRecordingSession(
            prices,
            window_s=cfg.marketdata.record_window_s,
            rest_base=cfg.marketdata.rest_base,
        )

    print(f"▸ recording to {corpus} on {loop_impl}. Ctrl-C to stop. No orders will be placed.")
    if market is not None:
        print(f"▸ market data → {prices} (+ raw ticks alongside), "
              f"{cfg.marketdata.record_window_s}s per called symbol")
    elif not args.no_market_data:
        print("▸ no tick recorder for this venue; prices will need backfilling "
              "with `pumpbot fetch-prices`")
    else:
        print("⚠ --no-market-data: messages only. On KuCoin there is no historical "
              "sub-minute data, so these calls will never be scoreable.")

    runner = LiveRunner(
        cfg, executor=executor, feed=feed, logbook=log, record_only=True, market=market
    )
    result = asyncio.run(_run_with_sigint(runner))
    print(f"▸ recorded {result.messages_seen} messages, "
          f"{result.signals_parsed} parsed as signals")
    if market is not None:
        print(f"▸ {market.bars_written:,} 1s bars across "
              f"{len(market.recorder.symbols_recorded)} symbol(s), "
              f"{market.recorder.ticks_seen:,} ticks, {market.reconnects} reconnect(s)")
    return _finish(cfg, result, args)


def cmd_live(args: argparse.Namespace) -> int:
    cfg = _load(args)
    _require_telegram(cfg, args.config)
    cfg.mode = "live"

    gate = PromotionGate(cfg.gate, config_fingerprint(cfg))
    try:
        gate.assert_live_allowed(args.i_accept_live_trading_risk)
    except PermissionError as exc:
        print(f"⛔ {exc}", file=sys.stderr)
        return 3

    try:
        cfg.validate()
    except ConfigError as exc:
        print(f"⛔ {exc}", file=sys.stderr)
        return 2

    loop_impl = _install_uvloop()
    cap = cfg.execution.live.max_notional_quote_hard_cap
    market = None

    if cfg.execution.venue == "kucoin":
        from .execution.live_kucoin import KucoinLiveExecutor
        from .marketdata.kucoin import KucoinRecordingSession

        # The recorder doubles as the live price feed: the same tick stream
        # that marks open positions is the one written to disk, so a live run
        # produces a scoreable corpus as a side effect.
        market = KucoinRecordingSession(
            Path("data/recorded/live_prices.jsonl"),
            window_s=cfg.marketdata.record_window_s,
            rest_base=cfg.marketdata.rest_base,
        )
        feed = market.feed
        executor = KucoinLiveExecutor(
            cfg.execution.live,
            cfg.marketdata.rest_base,
            hard_cap_quote=cap,
            dry_run=args.dry_run,
            passphrase_env=cfg.execution.live.passphrase_env,
        )
    else:
        from .execution.live_binance import BinanceLiveExecutor
        from .marketdata.feed import BinanceFeed

        feed = BinanceFeed(cfg.marketdata.rest_base)
        executor = BinanceLiveExecutor(
            cfg.execution.live,
            cfg.marketdata.rest_base,
            hard_cap_quote=cap,
            dry_run=args.dry_run,
        )

    print(f"▸ LIVE on {cfg.execution.venue} ({loop_impl}). Per-trade notional "
          f"{cfg.risk.position_notional_quote} {cfg.risk.quote_asset}, hard cap {cap}. "
          f"Ctrl-C to stop.")
    log = Logbook(Path("data/recorded/live.jsonl"))
    runner = LiveRunner(cfg, executor=executor, feed=feed, logbook=log, market=market)
    result = asyncio.run(_run_with_sigint(runner))
    return _finish(cfg, result, args)


def cmd_score(args: argparse.Namespace) -> int:
    """Re-score channels from a recorded corpus without trading."""
    cfg = _load(args)
    cfg.mode = "simulate"
    cfg.risk.max_trades_per_run = 0          # observe only; scoring still runs

    messages = recorded_messages(args.corpus)
    if not messages:
        print(f"error: no messages in {args.corpus}", file=sys.stderr)
        return 2

    proceed, feed = _load_prices(cfg, args, messages)
    if not proceed:
        return 2

    runner = SimulationRunner(cfg, messages, seed=args.seed, feed=feed)
    result = asyncio.run(runner.run())

    if not result.channel_scores:
        print(
            f"Not enough data: no channel reached "
            f"{cfg.scoring.min_signals_for_score} scored calls. Record for longer.",
            file=sys.stderr,
        )
        return 1

    print(f"\n{'#':>2}  {'channel':<28} {'calls':>6} {'hit':>7} {'median':>9} "
          f"{'pre-run':>8} {'orig':>6} {'score':>7}")
    print("─" * 82)
    for i, c in enumerate(result.channel_scores, 1):
        print(f"{i:>2}  {c.name[:28]:<28} {c.signals:>6} {100*c.hit_rate:>6.1f}% "
              f"{c.median_return_pct:>+8.2f}% {c.pre_pump_pct:>7.1f}% "
              f"{c.originator_score:>6.2f} {c.composite:>7.3f}")
    print()
    for c in result.channel_scores:
        print(f"  {c.name}: {c.verdict}")
    print()

    if args.write_config:
        keep = [c for c in result.channel_scores if c.composite >= args.min_score]
        payload = {
            "telegram": {
                "channels": [
                    {"id": c.chat_id, "name": c.name, "tier": "trusted"} for c in keep
                ]
            }
        }
        Path(args.write_config).write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"▸ wrote {len(keep)} channel(s) scoring ≥ {args.min_score} to {args.write_config}")
    return 0


def cmd_discover(args: argparse.Namespace) -> int:
    cfg = _load(args)
    _require_telegram(cfg, args.config)

    async def _go() -> int:
        from telethon import TelegramClient

        from .ingest.telegram import prepare_session_path
        from .scoring.discovery import discover

        client = TelegramClient(
            prepare_session_path(cfg.telegram.session_name),
            cfg.telegram.api_id,
            cfg.telegram.api_hash,
        )
        await client.start()
        try:
            candidates = await discover(
                client,
                include_search=not args.joined_only,
                min_participants=args.min_participants,
            )
        finally:
            await client.disconnect()

        print(f"\n{'chat_id':>16}  {'members':>9}  source            title")
        print("─" * 88)
        for c in candidates:
            print(f"{c.chat_id:>16}  {c.participants or 0:>9}  "
                  f"{c.source[:16]:<16}  {c.title[:40]}")
        print(f"\n{len(candidates)} candidate(s).")
        print(
            "\nNone of these are vetted. Add them to config.yaml as tier: candidate, "
            "run `pumpbot record` for at least a week, then `pumpbot score`.\n"
            "Subscriber counts and pinned track records are marketing, not evidence."
        )

        if args.write_config:
            payload = {"telegram": {"channels": [c.as_config_entry() for c in candidates]}}
            Path(args.write_config).write_text(json.dumps(payload, indent=2), encoding="utf-8")
            print(f"▸ wrote {len(candidates)} entries to {args.write_config}")
        return 0

    return asyncio.run(_go())


# Telegram picks how it delivers a login code and does not make the choice
# obvious. "I never got a code" is almost always "the code went somewhere I was
# not looking", so name the destination in plain language.
_CODE_DESTINATIONS = {
    "SentCodeTypeApp": (
        "the Telegram APP itself — open Telegram on a device where you are "
        "already logged in and read the chat named \"Telegram\" (the official "
        "one with the blue check). It is NOT an SMS."
    ),
    "SentCodeTypeSms": "an SMS to this number.",
    "SentCodeTypeCall": "a phone CALL that reads the digits out.",
    "SentCodeTypeMissedCall": (
        "a MISSED CALL — the code is the last digits of the calling number."
    ),
    "SentCodeTypeFlashCall": "a flash call — the code is in the calling number.",
    "SentCodeTypeFragmentSms": "Fragment (fragment.com), not a normal SMS.",
    "SentCodeTypeFirebaseSms": "an SMS via Firebase.",
    "SentCodeTypeEmailCode": "EMAIL, to the address on the account.",
    "SentCodeTypeSmsWord": (
        "an SMS containing a single WORD, not digits. Enter the word."
    ),
    "SentCodeTypeSmsPhrase": (
        "an SMS containing a PHRASE of several words, not digits. Enter the phrase."
    ),
}


def cmd_login(args: argparse.Namespace) -> int:
    """Log in to Telegram and nothing else.

    Separated from the commands that use the session because a failed login
    inside `triage` looks like a triage problem. This reports what Telegram
    actually said at each step — above all *where* it claims the code went,
    which is the one fact that resolves almost every "no code arrived".
    """
    import asyncio

    cfg = _load(args)
    _require_telegram(cfg, args.config)

    async def _go() -> int:
        from telethon import TelegramClient
        from telethon.errors import (
            ApiIdInvalidError,
            FloodWaitError,
            PhoneCodeExpiredError,
            PhoneCodeInvalidError,
            PhoneNumberBannedError,
            PhoneNumberInvalidError,
            SessionPasswordNeededError,
        )

        from .ingest.telegram import prepare_session_path

        session = prepare_session_path(cfg.telegram.session_name)
        print(f"▸ api_id {cfg.telegram.api_id}, session {session}.session")

        client = TelegramClient(session, cfg.telegram.api_id, cfg.telegram.api_hash)
        try:
            await client.connect()
        except Exception as exc:                     # noqa: BLE001
            print(f"⛔ could not reach Telegram: {type(exc).__name__}: {exc}",
                  file=sys.stderr)
            return 1
        print("▸ connected")

        try:
            if await client.is_user_authorized():
                me = await client.get_me()
                print(f"✅ already logged in as {me.first_name or ''} "
                      f"(@{me.username or 'no username'}, id {me.id})")
                print("   Delete the session file to log in as someone else.")
                return 0

            phone = args.phone or input("phone number (international, e.g. +48...): ").strip()
            if not phone.startswith("+"):
                print(f"▸ adding the missing + -> +{phone}")
                phone = "+" + phone

            try:
                sent = await client.send_code_request(phone, force_sms=args.force_sms)
            except PhoneNumberInvalidError:
                print(f"⛔ Telegram rejected {phone} as a phone number.", file=sys.stderr)
                return 1
            except PhoneNumberBannedError:
                print(f"⛔ {phone} is banned from Telegram.", file=sys.stderr)
                return 1
            except ApiIdInvalidError:
                print("⛔ api_id/api_hash rejected. They must be from the SAME "
                      "application at my.telegram.org, copied without stray spaces.",
                      file=sys.stderr)
                return 1
            except FloodWaitError as exc:
                mins = exc.seconds / 60
                print(f"⛔ rate limited: Telegram wants {exc.seconds}s "
                      f"(~{mins:.0f} min) before the next attempt.\n"
                      f"   Repeated tries make this longer, so wait it out.",
                      file=sys.stderr)
                return 1

            kind = type(sent.type).__name__
            where = _CODE_DESTINATIONS.get(kind, f"delivery type {kind}")
            print(f"\n▸ Telegram says the code was sent to {where}")
            if getattr(sent, "next_type", None) is not None:
                nxt = type(sent.next_type).__name__
                print(f"  If it does not arrive, a resend would use: {nxt}"
                      + ("  (run again with --force-sms)"
                         if "Sms" in nxt and not args.force_sms else ""))
            if getattr(sent, "timeout", None):
                print(f"  Valid for about {sent.timeout}s.")
            print("\n  Type the code here — do not paste it into any chat. Telegram "
                  "invalidates\n  codes it sees forwarded in messages.\n")

            code = (args.code or input("code: ")).strip().replace(" ", "")
            try:
                await client.sign_in(phone, code, phone_code_hash=sent.phone_code_hash)
            except PhoneCodeInvalidError:
                print("⛔ wrong code.", file=sys.stderr)
                return 1
            except PhoneCodeExpiredError:
                print("⛔ that code expired. Run this again for a fresh one.",
                      file=sys.stderr)
                return 1
            except SessionPasswordNeededError:
                import getpass

                print("▸ two-factor authentication is on.")
                await client.sign_in(password=getpass.getpass("2FA password: "))

            me = await client.get_me()
            print(f"\n✅ logged in as {me.first_name or ''} "
                  f"(@{me.username or 'no username'}, id {me.id})")
            print(f"   Session saved to {session}.session — treat that file as a "
                  f"credential; anyone holding it is logged in as you.")
            print("\n   Next: join some channels in Telegram, then run "
                  "`pumpbot triage --all-joined`.")
            return 0
        finally:
            await client.disconnect()

    return asyncio.run(_go())


def cmd_triage(args: argparse.Namespace) -> int:
    """Judge channels on their past, today, instead of recording for weeks.

    Two passes. The structural screen rejects channels on shape alone — selling
    access, forwarding, pre-announcing, firehosing — before a single price is
    fetched. Survivors get scored against one-minute candles around their
    historical calls.

    One-minute resolution cannot tell you what a fast bot would have been
    filled at on a 30-second spike. It answers the two questions that do not
    need sub-minute precision, and that kill most channels anyway: how far the
    price had already run before the call, and whether the call is worth
    anything five to fifteen minutes later. Survivors of *this* still need live
    tick recording before you trade them.
    """
    import asyncio

    cfg = _load(args)
    if not args.from_export:
        _require_telegram(cfg, args.config)
    cfg.mode = "simulate"
    cfg.risk.max_trades_per_run = 0              # observe only

    # At one-minute resolution a 30s horizon is noise. Force something the data
    # can actually support, and say so rather than quietly scoring garbage.
    if cfg.scoring.primary_horizon_s < 300:
        print(f"▸ scoring horizon raised {cfg.scoring.primary_horizon_s}s -> 300s "
              f"for triage: 1-minute candles cannot resolve anything shorter")
        cfg.scoring.primary_horizon_s = 300
    cfg.marketdata.return_horizons_s = [60, 300, 900, 1800]
    cfg.scoring.min_signals_for_score = args.min_calls

    async def _go() -> int:
        import aiohttp

        from .ingest.history import screen, summarise
        from .marketdata.historical import HistoricalFeed
        from .marketdata.kucoin import KucoinSymbols, fetch_klines
        from .parsing.extractor import SignalExtractor

        if not args.from_export:
            from telethon import TelegramClient

            from .ingest.history import read_history
            from .ingest.telegram import prepare_session_path

        extractor = SignalExtractor(
            ignore_symbols=cfg.parsing.ignore_symbols,
            quote_assets=cfg.parsing.quote_assets,
            accept_contracts=cfg.parsing.accept_contracts,
            min_confidence=cfg.parsing.min_confidence,
        )

        if args.from_export:
            from .ingest.telegram_export import (
                ExportError,
                ExportStats,
                explain_empty,
                read_export,
            )

            stats = ExportStats()
            try:
                histories = read_export(args.from_export, days=args.days, stats=stats)
            except ExportError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 1
            print(f"▸ reading {args.days}d from export {args.from_export} "
                  f"({stats.files} file(s), {stats.channels_seen} channel(s))\n")
            for h in histories:
                print(f"  · {h.name[:34]:<34} {len(h.messages):>5} msgs "
                      f"over {h.span_days:>5.1f}d")
            if not histories:
                print(f"\nerror: {explain_empty(stats, args.days)}", file=sys.stderr)
                return 1
        else:
            client = TelegramClient(
                prepare_session_path(cfg.telegram.session_name),
                cfg.telegram.api_id,
                cfg.telegram.api_hash,
            )
            await client.start()

            try:
                targets = [(c.id, c.name) for c in cfg.telegram.channels
                           if c.tier != "blocked"]
                if not targets or args.all_joined:
                    from telethon.tl.types import Channel

                    targets = []
                    async for dialog in client.iter_dialogs():
                        entity = dialog.entity
                        if isinstance(entity, Channel):
                            targets.append((int(f"-100{entity.id}"), entity.title or ""))
                    print(f"▸ no channels configured; using {len(targets)} "
                          f"joined channel(s)")

                if not targets:
                    print("error: no channels to triage. Join some, or list them in "
                          f"telegram.channels in {args.config or 'config.yaml'}.",
                          file=sys.stderr)
                    return 1

                print(f"▸ reading {args.days}d of history from "
                      f"{len(targets)} channel(s)\n")
                histories = []
                for chat_id, name in targets:
                    try:
                        h = await read_history(
                            client, chat_id, name=name,
                            days=args.days, limit=args.limit,
                        )
                    except Exception as exc:             # noqa: BLE001
                        print(f"  ✗ {name or chat_id}: {type(exc).__name__}: {exc}")
                        continue
                    histories.append(h)
                    print(f"  · {h.name[:34]:<34} {len(h.messages):>5} msgs "
                          f"over {h.span_days:>5.1f}d")
            finally:
                await client.disconnect()

        # Load the venue's listings before screening, so a channel's cadence
        # counts calls that could actually be traded here rather than every
        # regex hit. Prose false positives would otherwise inflate a channel's
        # call rate and can reject it for volume it never had.
        async with aiohttp.ClientSession() as session:
            symbols = await KucoinSymbols.load(session, cfg.marketdata.rest_base)
        print(f"\n▸ {len(symbols)} symbols listed on {cfg.marketdata.venue}")

        def is_tradable(symbol: str) -> bool:
            return symbols.resolve(symbol) is not None

        screens = [
            screen(h, extractor, min_signals_for_score=args.min_calls,
                   is_tradable=is_tradable)
            for h in histories
        ]
        screens.sort(key=lambda s: (s.rejected, -s.calls_per_day))

        print(f"\n{'channel':<30} {'msgs':>6} {'calls':>6} {'/day':>6} "
              f"{'fwd':>5} {'paid':>5} {'pre':>5}  verdict")
        print("─" * 104)
        for sc in screens:
            per_day = f"{sc.calls_per_day:>6.1f}" if sc.calls_per_day else "     —"
            print(f"{sc.name[:30]:<30} {sc.messages:>6} {sc.calls:>6} "
                  f"{per_day} {100*sc.forward_ratio:>4.0f}% "
                  f"{100*sc.paid_pitch_ratio:>4.0f}% {100*sc.preannounce_ratio:>4.0f}%  "
                  f"{sc.verdict[:46]}")

        flagged = [sc for sc in screens if sc.flags]
        if flagged:
            print("\nflags:")
            for sc in flagged:
                for f in sc.flags:
                    print(f"  {sc.name[:28]:<28} {f}")

        stats = summarise(screens)
        print(f"\n▸ structural screen: {stats['rejected']}/{stats['channels']} rejected, "
              f"{stats['kept']} worth pricing")

        survivors = {sc.chat_id for sc in screens if not sc.rejected}
        if not survivors:
            print("\nNothing survived the structural screen. That is a result: none of "
                  "these channels is worth weeks of recording.")
            return 0

        # ---- price the survivors ---------------------------------------
        messages = []
        for h in histories:
            if h.chat_id in survivors:
                messages.extend(h.messages)
        messages.sort(key=lambda m: m.received_wall_ms)

        pairs = []
        for m in messages:
            sig = extractor.extract(m)
            if sig is not None and sig.symbol:
                pairs.append((sig.symbol, m.posted_wall_ms or m.received_wall_ms))

        prices_path = Path(args.prices_out)
        async with aiohttp.ClientSession() as session:
            listed = {s for s, _ in pairs if symbols.resolve(s)}
            unlisted = {s for s, _ in pairs} - listed
            print(f"\n▸ {len(pairs)} calls across {len(listed)} symbol(s) listed on "
                  f"{cfg.marketdata.venue}")
            if unlisted:
                shown = ", ".join(sorted(unlisted)[:10])
                print(f"  {len(unlisted)} symbol(s) not listed here — dropped, not "
                      f"counted as flat: {shown}"
                      + (" …" if len(unlisted) > 10 else ""))
            if not listed:
                print("\nNone of these calls name a symbol tradable on this venue.",
                      file=sys.stderr)
                return 1

            print("▸ fetching 1m candles (this paces itself to stay inside the "
                  "rate limit)…")
            done = [0]

            def progress(venue: str, rows: int) -> None:
                done[0] += 1
                if done[0] % 10 == 0 or rows == 0:
                    mark = "✗" if rows == 0 else "·"
                    print(f"  {mark} {done[0]}/{len(listed)} {venue} {rows} bars")

            await fetch_klines(
                session, pairs, prices_path,
                rest_base=cfg.marketdata.rest_base,
                window_before_s=args.before, window_after_s=args.after,
                symbols=symbols, progress=progress,
            )

        feed = HistoricalFeed.from_file(prices_path)
        covered, total, missing = feed.coverage([s for s, _ in pairs])
        print(f"▸ priced {covered}/{total} symbols")

        runner = SimulationRunner(cfg, messages, seed=0, feed=feed)
        result = await runner.run()

        if not result.channel_scores:
            print(f"\nNo channel reached {args.min_calls} priced calls. Either the "
                  f"window is too short, or these channels mostly name tokens this "
                  f"venue does not list.", file=sys.stderr)
            return 1

        print(f"\n{'#':>2}  {'channel':<28} {'calls':>6} {'hit':>7} {'median':>9} "
              f"{'pre-run':>8} {'orig':>6} {'best hold':>11} {'score':>7}")
        print("─" * 96)
        for i, c in enumerate(result.channel_scores, 1):
            print(f"{i:>2}  {c.name[:28]:<28} {c.signals:>6} {100*c.hit_rate:>6.1f}% "
                  f"{c.median_return_pct:>+8.2f}% {c.pre_pump_pct:>7.1f}% "
                  f"{c.originator_score:>6.2f} {c.best_horizon_s:>8}s "
                  f"{c.composite:>7.3f}")
        print()
        for c in result.channel_scores:
            print(f"  {c.name}: {c.verdict}")

        keep = [c for c in result.channel_scores if c.composite >= args.min_score]
        print(f"\n▸ {len(keep)} channel(s) scored >= {args.min_score}")
        if keep and args.write_config:
            payload = {"telegram": {"channels": [
                {"id": c.chat_id, "name": c.name, "tier": "trusted"} for c in keep
            ]}}
            Path(args.write_config).write_text(json.dumps(payload, indent=2),
                                               encoding="utf-8")
            print(f"▸ wrote them to {args.write_config}")

        writer = ReportWriter(cfg.reporting.output_dir, cfg.reporting.formats)
        paths = writer.write(result, None)
        for fmt, path in paths.items():
            print(f"  report ({fmt}): {path}")

        print("\nNext: record the survivors live. One-minute candles cannot price a "
              "30-second spike, so these scores rank channels — they do not tell you "
              "what a fast bot would have been filled at.")
        return 0

    return asyncio.run(_go())


def cmd_inspect(args: argparse.Namespace) -> int:
    """Show what the parser makes of real messages, one by one.

    "0 calls found" has two very different causes — a quiet channel, or a
    parser that is too strict — and the summary line cannot tell them apart.
    This prints the messages alongside the verdict on each, so the difference
    is visible rather than guessed at.
    """
    import asyncio

    cfg = _load(args)

    async def _go() -> int:
        from .ingest.telegram_export import (
            ExportError,
            ExportStats,
            explain_empty,
            read_export,
        )
        from .parsing.extractor import SignalExtractor

        extractor = SignalExtractor(
            ignore_symbols=cfg.parsing.ignore_symbols,
            quote_assets=cfg.parsing.quote_assets,
            accept_contracts=cfg.parsing.accept_contracts,
            min_confidence=args.min_confidence
            if args.min_confidence is not None else cfg.parsing.min_confidence,
        )

        stats = ExportStats()
        try:
            histories = read_export(args.from_export, days=args.days, stats=stats)
        except ExportError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        if not histories:
            print(f"error: {explain_empty(stats, args.days)}", file=sys.stderr)
            return 1

        if args.channel:
            needle = args.channel.lower()
            histories = [h for h in histories if needle in h.name.lower()]
            if not histories:
                print(f"error: no channel matching {args.channel!r}", file=sys.stderr)
                return 1

        listed = None
        if not args.no_venue_check:
            import aiohttp

            from .marketdata.kucoin import KucoinSymbols

            async with aiohttp.ClientSession() as session:
                symbols = await KucoinSymbols.load(session, cfg.marketdata.rest_base)
            listed = symbols
            print(f"▸ {len(symbols)} symbols listed on {cfg.marketdata.venue}\n")

        for h in histories:
            print("=" * 100)
            print(f"{h.name}  —  {len(h.messages)} messages over {h.span_days:.1f}d")
            print("=" * 100)

            if args.summary:
                _print_parse_summary(h, extractor, listed, args.limit)
                continue

            hits = misses = unlisted = 0
            shown = 0
            for m in h.messages:
                sig = extractor.extract(m)
                text = " ".join(m.text.split())[:88]

                if sig is None or not sig.symbol:
                    misses += 1
                    if args.show_misses and shown < args.limit:
                        shown += 1
                        print(f"  ·  {'—':<14} {text}")
                    continue

                tradable = listed is None or listed.resolve(sig.symbol) is not None
                if tradable:
                    hits += 1
                    mark = "✓"
                else:
                    unlisted += 1
                    mark = "✗"
                if shown < args.limit:
                    shown += 1
                    print(f"  {mark}  {sig.symbol:<14} {text}")
                    print(f"     {'':<14} via {sig.matched_by}, confidence "
                          f"{sig.confidence:.2f}"
                          + ("" if tradable else "  ← NOT listed on this venue"))

            total = len(h.messages)
            print(f"\n  {hits} tradable call(s), {unlisted} call(s) on unlisted "
                  f"symbols, {misses} non-calls, out of {total} messages")
            if misses and not args.show_misses:
                print("  Re-run with --show-misses to see what did not parse — "
                      "that is where a real call the parser missed would show up.")
            print()

        return 0

    return asyncio.run(_go())


def _print_parse_summary(history, extractor, listed, limit: int) -> None:  # noqa: ANN001
    """Aggregate a channel's parses by symbol and by pattern.

    A channel producing seventy apparent calls needs the distribution, not
    seventy lines: if sixty of them are one prose word arriving through the
    imperative pattern, that is visible instantly here and invisible in a
    message-by-message dump.
    """
    from collections import Counter

    by_symbol: Counter = Counter()
    by_pattern: Counter = Counter()
    examples: Dict[str, str] = {}
    tradable = 0

    for m in history.messages:
        sig = extractor.extract(m)
        if sig is None or not sig.symbol:
            continue
        by_symbol[sig.symbol] += 1
        by_pattern[sig.matched_by] += 1
        examples.setdefault(sig.symbol, " ".join(m.text.split())[:60])
        if listed is None or listed.resolve(sig.symbol) is not None:
            tradable += 1

    if not by_symbol:
        print("  no calls parsed at all\n")
        return

    print(f"\n  {'symbol':<16} {'n':>4}  {'venue':<9} example")
    print("  " + "-" * 92)
    for symbol, count in by_symbol.most_common(limit):
        ok = listed is None or listed.resolve(symbol) is not None
        mark = "listed" if ok else "NOT listed"
        print(f"  {symbol:<16} {count:>4}  {mark:<9} {examples[symbol]}")
    if len(by_symbol) > limit:
        print(f"  … {len(by_symbol) - limit} more symbol(s)")

    print("\n  by pattern: " + ", ".join(
        f"{name}={n}" for name, n in by_pattern.most_common()
    ))
    total = sum(by_symbol.values())
    print(f"  {total} parse(s) over {len(by_symbol)} distinct symbol(s); "
          f"{tradable} tradable here\n")


def cmd_solana_check(args: argparse.Namespace) -> int:
    """Ask whether a channel's calls are tradable on Solana at all.

    This runs before any execution is built, because the answer decides
    whether building it is worth doing. For each call it resolves the token —
    preferring a contract address in the message over the ticker, since the
    ticker is a claim and the address is the instrument — then quotes buying
    the position and selling it straight back.
    """
    import asyncio

    cfg = _load(args)

    async def _go() -> int:
        import aiohttp

        from .ingest.telegram_export import (
            ExportError,
            ExportStats,
            explain_empty,
            read_export,
        )
        from .marketdata.solana import SolanaTokens
        from .parsing.extractor import SignalExtractor
        from .risk.token_safety import SafetyLimits, TokenSafetyChecker

        extractor = SignalExtractor(
            ignore_symbols=cfg.parsing.ignore_symbols,
            quote_assets=cfg.parsing.quote_assets,
            accept_contracts=True,               # the whole point here
            min_confidence=cfg.parsing.min_confidence,
        )

        stats = ExportStats()
        try:
            histories = read_export(args.from_export, days=args.days, stats=stats)
        except ExportError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        if not histories:
            print(f"error: {explain_empty(stats, args.days)}", file=sys.stderr)
            return 1
        if args.channel:
            needle = args.channel.lower()
            histories = [h for h in histories if needle in h.name.lower()]
            if not histories:
                print(f"error: no channel matching {args.channel!r}", file=sys.stderr)
                return 1

        limits = SafetyLimits(
            min_liquidity_usd=args.min_liquidity,
            min_volume_h24=args.min_volume,
            min_txns_h24=args.min_txns,
        )

        async with aiohttp.ClientSession() as session:
            tokens = SolanaTokens(session)
            checker = TokenSafetyChecker(session)

            for history in histories:
                # One entry per distinct token, newest call wins. A channel
                # mentioning the same token nine times is one decision.
                calls: Dict[str, Tuple[str, Optional[str], str]] = {}
                for m in history.messages:
                    sig = extractor.extract(m)
                    if sig is None:
                        contract, chain = extractor.find_contracts(m.text)
                        if not contract or chain != "solana":
                            continue
                        key, base = contract, ""
                    else:
                        key = sig.contract or sig.symbol or ""
                        base = (sig.base or "").upper()
                    if not key:
                        continue
                    calls[key] = (base, sig.contract if sig else key,
                                  " ".join(m.text.split())[:52])

                print("=" * 104)
                print(f"{history.name}  —  {len(calls)} distinct token(s) called")
                print("=" * 104)
                if not calls:
                    print("  nothing to check\n")
                    continue

                print(f"  {'token':<14} {'how':<9} {'liq':>11} {'vol24':>11} "
                      f"{'tx24':>6} {'round-trip':>11}  verdict")
                print("  " + "-" * 100)

                tradable = safe = 0
                for key, (base, contract, example) in list(calls.items())[:args.limit]:
                    resolution = await tokens.resolve(
                        contract=contract if contract else None,
                        ticker=base or None,
                    )
                    label = (base or (contract or "")[:12]) or "?"

                    if not resolution.ok:
                        print(f"  {label:<14} {'—':<9} {'':>11} {'':>11} {'':>6} "
                              f"{'':>11}  {resolution.reason[:44]}")
                        continue

                    pair = resolution.best
                    verdict = await checker.check(
                        resolution.mint, symbol=label,
                        notional_usd=args.notional, limits=limits, pair=pair,
                    )
                    if verdict.tradable:
                        tradable += 1
                    if verdict.safe:
                        safe += 1

                    rt = (f"{verdict.round_trip_pct:+.1f}%"
                          if verdict.round_trip_pct is not None else "—")
                    print(f"  {label:<14} {resolution.source:<9} "
                          f"${pair.liquidity_usd:>10,.0f} ${pair.volume_h24:>10,.0f} "
                          f"{pair.txns_h24:>6} {rt:>11}  {verdict.verdict[:44]}"
                          if pair else
                          f"  {label:<14} {resolution.source:<9} {'':>11} {'':>11} "
                          f"{'':>6} {rt:>11}  {verdict.verdict[:44]}")
                    await asyncio.sleep(args.pace)

                checked = min(len(calls), args.limit)
                print(f"\n  {tradable}/{checked} tradable, {safe}/{checked} pass every "
                      f"safety threshold at ${args.notional:.0f}\n")

                if tradable == 0:
                    print("  Every call this channel made is unroutable. Execution "
                          "cannot fix that: there is nothing on the other side.\n")
                elif safe == 0:
                    print("  Some route, none clears the thresholds. Trading these "
                          "means accepting the cost the round trip just measured.\n")

        return 0

    return asyncio.run(_go())


def cmd_gate(args: argparse.Namespace) -> int:
    cfg = _load(args)
    status = PromotionGate(cfg.gate, config_fingerprint(cfg)).status()

    print(f"\n  {'🟢 UNLOCKED' if status.unlocked else '🔒 LOCKED'}")
    print(f"  {status.qualifying_runs}/{status.required} qualifying runs "
          f"(streak {status.current_streak}) on config {status.fingerprint}")
    print(f"\n  {status.reason}\n")
    if status.recent:
        print(f"  {'run':<12} {'finished':<22} {'mode':<9} {'trades':>7} {'net':>9}  counts")
        print("  " + "─" * 76)
        for r in status.recent:
            mark = "yes" if r.counts_toward_gate else f"no — {r.excluded_because}"
            print(f"  {r.run_id:<12} {r.finished_at:<22} {r.mode:<9} "
                  f"{r.trades:>7} {r.net_return_pct:>+8.2f}%  {mark}")
        print()
    return 0


# ---------------------------------------------------------------------------
def _require_telegram(cfg: Config, config_path: str) -> None:
    """Fail clean, before any connection, when credentials are missing.

    This is the first wall every new user hits, and a traceback out of the
    event loop is a bad answer to "I have not set this up yet".
    """
    tg = cfg.telegram
    if tg.api_id and tg.api_hash:
        return
    missing = []
    if not tg.api_id:
        missing.append("api_id")
    if not tg.api_hash:
        missing.append("api_hash")
    print(
        f"error: Telegram {' and '.join(missing)} not set.\n"
        f"\n"
        f"  1. Go to https://my.telegram.org -> API development tools\n"
        f"     and create an application (title/short name: anything, e.g.\n"
        f"     \"pumpbot\"; leave URL empty; platform: Other).\n"
        f"     If it shows ERROR, reload the page — the app is usually\n"
        f"     created anyway and the values appear at the top.\n"
        f"\n"
        f"  2. Export them, so no file has to hold the secret:\n"
        f"       export {tg.api_id_env}=<the number>\n"
        f"       export {tg.api_hash_env}=<the 32-char hash>\n"
        f"\n"
        f"  (Or set telegram.api_id / telegram.api_hash in "
        f"{config_path or 'config.yaml'},\n"
        f"   which is gitignored — but the environment is safer.)",
        file=sys.stderr,
    )
    raise SystemExit(2)


def _load_channel_scores(report_json: str) -> dict:
    """Read ``{chat_id: composite}`` out of a previous run's report."""
    path = Path(report_json)
    if not path.exists():
        print(f"error: {report_json} not found", file=sys.stderr)
        raise SystemExit(2)
    data = json.loads(path.read_text(encoding="utf-8"))
    return {int(c["chat_id"]): float(c["composite"]) for c in data.get("channels", [])}


def _load_prices(cfg: Config, args: argparse.Namespace, messages) -> Tuple[bool, object]:
    """Resolve the price feed for a recorded corpus.

    Returns ``(proceed, feed)``. A ``feed`` of ``None`` with ``proceed`` true
    means "fall back to the synthetic feed", which the caller has explicitly
    asked for. Replaying real messages against fabricated prices produces
    confident, meaningless numbers, so it is never the default.
    """
    from .marketdata.historical import HistoricalFeed

    if not getattr(args, "prices", None):
        if getattr(args, "synthetic_prices", False):
            print(
                "⚠ replaying a recorded corpus against SYNTHETIC prices. The P&L and "
                "channel scores below describe a made-up market, not these symbols. "
                "Use this to exercise the pipeline, never to judge a channel."
            )
            return True, None
        print(
            "error: replaying a corpus needs real prices.\n"
            "  Fetch them:   pumpbot fetch-prices <corpus> --out data/recorded/prices.jsonl\n"
            "  Then:         ... --prices data/recorded/prices.jsonl\n"
            "  To exercise the pipeline against a fabricated market instead, pass "
            "--synthetic-prices (results are meaningless).",
            file=sys.stderr,
        )
        return False, None

    feed = HistoricalFeed.from_file(args.prices)
    wanted = _corpus_symbols(cfg, messages)
    covered, total, missing = feed.coverage(wanted)
    print(f"▸ prices: {len(feed.symbols)} symbol(s) loaded, "
          f"{covered}/{total} corpus symbols covered")
    if missing:
        shown = ", ".join(missing[:8]) + (" …" if len(missing) > 8 else "")
        print(f"  ⚠ no price data for {len(missing)} symbol(s): {shown}")
        print("    These calls are dropped, not scored. Delisted and never-listed "
              "tokens are common in this corpus type, and silently treating them as "
              "flat would bias every channel's score upward.")
    if total and covered / total < 0.5:
        print("  ⚠ under half the corpus is priceable; channel scores will be thin.")
    return True, feed


def _corpus_symbols(cfg: Config, messages) -> list:
    """Symbols the parser would actually trade in this corpus."""
    from .parsing.extractor import SignalExtractor

    ex = SignalExtractor(
        ignore_symbols=cfg.parsing.ignore_symbols,
        quote_assets=cfg.parsing.quote_assets,
        accept_contracts=cfg.parsing.accept_contracts,
        min_confidence=cfg.parsing.min_confidence,
    )
    out = []
    for m in messages:
        sig = ex.extract(m)
        if sig is not None and sig.symbol:
            out.append(sig.symbol)
    return out


def cmd_fetch_prices(args: argparse.Namespace) -> int:
    """Pull 1-second klines around every signal in a corpus."""
    import asyncio

    from .marketdata.historical import fetch_prices

    cfg = _load(args)
    messages = recorded_messages(args.corpus)
    if not messages:
        print(f"error: no messages in {args.corpus}", file=sys.stderr)
        return 2

    from .parsing.extractor import SignalExtractor

    ex = SignalExtractor(
        ignore_symbols=cfg.parsing.ignore_symbols,
        quote_assets=cfg.parsing.quote_assets,
        accept_contracts=cfg.parsing.accept_contracts,
        min_confidence=cfg.parsing.min_confidence,
    )
    pairs = []
    for m in messages:
        sig = ex.extract(m)
        if sig is not None and sig.symbol:
            pairs.append((sig.symbol, m.posted_wall_ms or m.received_wall_ms))

    if not pairs:
        print("error: the corpus contains no parseable symbol signals", file=sys.stderr)
        return 1

    unique = len({s for s, _ in pairs})
    print(f"▸ fetching 1s klines for {unique} symbol(s) across {len(pairs)} signal(s)")
    print(f"  window: -{args.before}s to +{args.after}s around each call")

    def progress(symbol: str, rows: int) -> None:
        marker = "·" if rows else "✗"
        print(f"  {marker} {symbol:<16} {rows:>6} rows")

    written = asyncio.run(fetch_prices(
        pairs, args.out, rest_base=cfg.marketdata.rest_base,
        window_before_s=args.before, window_after_s=args.after,
        progress=progress,
    ))

    empty = [s for s, n in written.items() if n == 0]
    total_rows = sum(written.values())
    print(f"\n▸ wrote {total_rows:,} rows to {args.out}")
    if empty:
        print(f"  {len(empty)} symbol(s) returned nothing — delisted, never listed, "
              f"or outside the venue's 1s kline retention: {', '.join(empty[:10])}"
              + (" …" if len(empty) > 10 else ""))
    return 0


# Tried in order when -c was not given. Examples are never picked up: they are
# templates, and silently running one would hide that no real config exists.
_CONFIG_SEARCH = ("config.yaml", "config.10usd.yaml", "config.local.yaml")


def find_config() -> Optional[Path]:
    """The config this directory implies, or None to use built-in defaults."""
    for name in _CONFIG_SEARCH:
        path = Path(name)
        if path.exists():
            return path
    others = sorted(
        p for p in Path(".").glob("config*.yaml") if ".example." not in p.name
    )
    return others[0] if others else None


def _load(args: argparse.Namespace) -> Config:
    """Resolve the config, in this order:

    1. ``-c`` if it was given — an explicit request, so a missing file is an
       error rather than something to paper over.
    2. A config this directory implies (config.yaml, config.10usd.yaml, …).
    3. Built-in defaults.

    No command requires a config file. Secrets come from the environment, and
    demanding a file for everything else just puts a wall in front of the first
    thing anybody runs. Whatever is in force is printed, because a run that
    silently picked up settings from somewhere unseen is its own kind of bug.
    """
    explicit = getattr(args, "config", None)
    if explicit is not None:
        path = Path(explicit)
        if not path.exists():
            print(f"config error: {explicit} not found.\n"
                  f"  Omit -c to use built-in defaults, or copy a template:\n"
                  f"    cp config.example.yaml {explicit}",
                  file=sys.stderr)
            raise SystemExit(2)
    else:
        path = find_config()
        if path is None:
            print("▸ config: built-in defaults "
                  "(cp config.example.yaml config.yaml to change anything)")
            cfg = Config()
            cfg.apply_env_overrides()
            cfg.validate()
            return _announce_credentials(cfg, "built-in defaults")
        print(f"▸ config: {path}")

    try:
        cfg = load_config(path)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc

    return _announce_credentials(cfg, str(path))


def _announce_credentials(cfg: Config, source: str) -> Config:
    """Say where credentials came from, and nudge secrets out of files."""
    from_env = [
        name for name, value in (
            (cfg.telegram.api_id_env, os.environ.get(cfg.telegram.api_id_env)),
            (cfg.telegram.api_hash_env, os.environ.get(cfg.telegram.api_hash_env)),
        ) if value
    ]
    if from_env:
        print(f"▸ telegram credentials from environment: {', '.join(from_env)}")
    elif cfg.telegram.api_hash:
        print(f"⚠ telegram api_hash is stored in {source}. Prefer "
              f"{cfg.telegram.api_hash_env} in the environment — a config file "
              f"holding a secret is one commit away from being published.")
    return cfg


async def _run_with_sigint(runner: LiveRunner) -> RunResult:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, runner.stop)
        except NotImplementedError:              # pragma: no cover - Windows
            pass
    return await runner.run()


def _finish(cfg: Config, result: RunResult, args: argparse.Namespace) -> int:
    """Record the run against the gate and write the report."""
    gate = PromotionGate(cfg.gate, config_fingerprint(cfg))
    gate.record_run(
        run_id=result.run_id,
        label=result.label,
        finished_at=result.finished_at,
        mode=result.mode,
        trades=result.trades,
        net_return_pct=result.net_return_pct,
        net_pnl_quote=result.net_pnl_quote,
    )
    status = gate.status()

    writer = ReportWriter(cfg.reporting.output_dir, cfg.reporting.formats)
    paths = writer.write(result, status)

    print()
    print(f"  net {result.net_return_pct:+.2f}%  ({result.net_pnl_quote:+.2f} "
          f"{cfg.risk.quote_asset})  over {result.trades} trades  "
          f"[{result.wins}W/{result.losses}L]")
    if result.halted_reason:
        print(f"  ⛔ {result.halted_reason}")
    print(f"  gate: {status.qualifying_runs}/{status.required} "
          f"{'🟢 unlocked' if status.unlocked else '🔒 locked'}")
    print()
    for fmt, path in paths.items():
        print(f"  report ({fmt}): {path}")
    print()

    if args.print_report and "markdown" in paths:
        print(paths["markdown"].read_text(encoding="utf-8"))

    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="pumpbot",
        description="Telegram pump-channel signal research and trading harness.",
    )
    p.add_argument("-c", "--config", default=None,
                   help="config file (default: config.yaml or config.10usd.yaml "
                        "in the working directory, else built-in defaults)")
    sub = p.add_subparsers(dest="command", required=True)

    def _common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--print-report", action="store_true",
                        help="dump the Markdown report to stdout when the run ends")

    sim = sub.add_parser("simulate", help="paper run — no real orders")
    sim.add_argument("--corpus", help="replay a recorded JSONL corpus")
    sim.add_argument("--prices", metavar="PRICES_JSONL",
                     help="historical prices for --corpus (see: pumpbot fetch-prices)")
    sim.add_argument("--synthetic-prices", action="store_true",
                     help="replay a corpus against fabricated prices — exercises the "
                          "pipeline, produces meaningless P&L")
    sim.add_argument("--messages", type=int, default=600,
                     help="synthetic message count (ignored with --corpus)")
    sim.add_argument("--call-rate", type=float, default=0.08,
                     help="fraction of synthetic messages carrying a real call")
    sim.add_argument("--seed", type=int, default=0)
    sim.add_argument("--label", help="override run_label")
    sim.add_argument("--use-scores", metavar="REPORT_JSON",
                     help="load channel scores from a previous run's report.json and "
                          "enforce risk.min_channel_score against them")
    sim.add_argument("--min-channel-score", type=float,
                     help="override risk.min_channel_score for this run")
    _common(sim)
    sim.set_defaults(func=cmd_simulate)

    rec = sub.add_parser("record", help="listen and log; never trades")
    rec.add_argument("--out", help="corpus path (default data/recorded/corpus.jsonl)")
    rec.add_argument("--prices-out",
                     help="tick/bar output (default data/recorded/prices.jsonl)")
    rec.add_argument("--no-market-data", action="store_true",
                     help="record messages only — on KuCoin this makes the corpus "
                          "permanently unscoreable")
    _common(rec)
    rec.set_defaults(func=cmd_record)

    sc = sub.add_parser("score", help="rank channels from a recorded corpus")
    sc.add_argument("corpus")
    sc.add_argument("--prices", metavar="PRICES_JSONL",
                    help="historical prices (see: pumpbot fetch-prices)")
    sc.add_argument("--synthetic-prices", action="store_true",
                    help="score against fabricated prices — meaningless, for testing only")
    sc.add_argument("--seed", type=int, default=0)
    sc.add_argument("--min-score", type=float, default=0.55)
    sc.add_argument("--write-config", help="write qualifying channels to this JSON file")
    sc.set_defaults(func=cmd_score)

    di = sub.add_parser("discover", help="enumerate candidate channels")
    di.add_argument("--joined-only", action="store_true",
                    help="skip global search; only channels this account has joined")
    di.add_argument("--min-participants", type=int, default=500)
    di.add_argument("--write-config", help="write candidates to this JSON file")
    di.set_defaults(func=cmd_discover)

    fp = sub.add_parser("fetch-prices", help="download historical prices for a corpus")
    fp.add_argument("corpus")
    fp.add_argument("--out", default="data/recorded/prices.jsonl")
    fp.add_argument("--before", type=int, default=600,
                    help="seconds of history before each call (default 600)")
    fp.add_argument("--after", type=int, default=3600,
                    help="seconds after each call (default 3600)")
    fp.set_defaults(func=cmd_fetch_prices)

    lg = sub.add_parser("login",
                        help="log in to Telegram and report exactly what happened")
    lg.add_argument("--phone", help="international format, e.g. +48123456789")
    lg.add_argument("--code", help="skip the prompt (the code, not your password)")
    lg.add_argument("--force-sms", action="store_true",
                    help="ask for a real SMS instead of an in-app code")
    lg.set_defaults(func=cmd_login)

    tr = sub.add_parser("triage",
                        help="judge channels on their history, today — no weeks of recording")
    tr.add_argument("--from-export", metavar="PATH",
                    help="read history from a Telegram Desktop JSON export "
                         "(folder or result.json) instead of the API — needs no "
                         "login, no api_id, no code")
    tr.add_argument("--days", type=int, default=30, help="how far back to read")
    tr.add_argument("--limit", type=int, default=3000,
                    help="max messages per channel")
    tr.add_argument("--all-joined", action="store_true",
                    help="triage every joined channel, ignoring telegram.channels")
    tr.add_argument("--min-calls", type=int, default=8,
                    help="calls a channel needs before it gets a score")
    tr.add_argument("--min-score", type=float, default=0.50)
    tr.add_argument("--before", type=int, default=600,
                    help="seconds of candles before each call")
    tr.add_argument("--after", type=int, default=1800,
                    help="seconds after each call")
    tr.add_argument("--prices-out", default="data/recorded/triage_prices.jsonl")
    tr.add_argument("--write-config", help="write surviving channels to this JSON")
    tr.set_defaults(func=cmd_triage)

    ins = sub.add_parser("inspect",
                         help="show what the parser makes of each message")
    ins.add_argument("--from-export", required=True, metavar="PATH",
                     help="Telegram Desktop JSON export (folder or result.json)")
    ins.add_argument("--channel", help="only channels whose name contains this")
    ins.add_argument("--days", type=int, default=30)
    ins.add_argument("--limit", type=int, default=60,
                     help="max lines printed per channel")
    ins.add_argument("--summary", action="store_true",
                     help="aggregate by symbol and pattern instead of printing "
                          "every message — the compact view for a busy channel")
    ins.add_argument("--show-misses", action="store_true",
                     help="also print messages that produced no call")
    ins.add_argument("--min-confidence", type=float,
                     help="override parsing.min_confidence for this run")
    ins.add_argument("--no-venue-check", action="store_true",
                     help="skip checking symbols against the venue's listings")
    ins.set_defaults(func=cmd_inspect)

    sc = sub.add_parser("solana-check",
                        help="are a channel's calls tradable on Solana at all?")
    sc.add_argument("--from-export", required=True, metavar="PATH")
    sc.add_argument("--channel", help="only channels whose name contains this")
    sc.add_argument("--days", type=int, default=30)
    sc.add_argument("--limit", type=int, default=40,
                    help="max tokens checked per channel")
    sc.add_argument("--notional", type=float, default=10.0,
                    help="position size the quotes are taken at")
    sc.add_argument("--min-liquidity", type=float, default=15_000.0)
    sc.add_argument("--min-volume", type=float, default=10_000.0)
    sc.add_argument("--min-txns", type=int, default=100)
    sc.add_argument("--pace", type=float, default=0.3,
                    help="seconds between tokens, to stay inside rate limits")
    sc.set_defaults(func=cmd_solana_check)

    g = sub.add_parser("gate", help="show promotion-gate status")
    g.set_defaults(func=cmd_gate)

    lv = sub.add_parser("live", help="REAL ORDERS — gated")
    lv.add_argument("--i-accept-live-trading-risk", action="store_true",
                    help="required acknowledgement; read LEGAL.md first")
    lv.add_argument("--dry-run", action="store_true",
                    help="build and sign orders but never send them")
    _common(lv)
    lv.set_defaults(func=cmd_live)

    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
