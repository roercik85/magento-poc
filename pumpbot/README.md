# pumpbot

A research and trading harness for Telegram pump-channel signals.

Simulation first. Live trading is locked behind a record of profitable
simulated runs, an explicit acknowledgement flag, and a hard notional cap.
**Read [`LEGAL.md`](LEGAL.md) before you unlock it** — MiCA applies to you if
you are in the EU, and the economics of these channels are not what they look
like.

---

## What this is actually for

The stated goal is "trade pump signals faster than everyone else". Speed is
addressed seriously (see [Latency](#latency)), but it is the second-order
problem. The first-order problem is that **most pump channels are designed so
that the organisers profit and the followers fund it.** They accumulate before
they post; the post creates the liquidity they sell into.

So the centre of gravity here is not the order router — it is
[`scoring/channels.py`](src/pumpbot/scoring/channels.py), which judges channels
on observed outcomes and answers four separate questions:

| Failure mode | What it looks like | Metric that catches it |
|---|---|---|
| Calls simply don't work | Price doesn't move | **Hit rate** |
| Works, but not after costs | +0.4% move, 0.7% round trip | **Median return, net** |
| Reposts someone else's call | Late by minutes | **Originator score** |
| You are the exit liquidity | Already ran before the post | **Pre-post run** |

Every metric is reported as a **median**, never a mean alone. A channel whose
mean is positive and median negative is one where a few outliers carry the
average while the typical call loses — and the report says so in those words.

## Install

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[live,dev]"      # drop [live] for simulation only
cp config.example.yaml config.yaml
```

Simulation needs nothing but `PyYAML`. Telethon, aiohttp, orjson and uvloop are
only needed once you point it at real Telegram and a real venue.

## Try it immediately

No Telegram account, no network, no credentials:

```bash
pumpbot simulate --messages 2000 --seed 1
```

That builds a synthetic corpus of five channels with deliberately different
quality, runs the full pipeline, and writes a report to `runs/`. The scorer is
never told which channel is which; recovering the planted ranking is one of the
tests.

## The workflow

```
discover → record → fetch-prices → score → simulate → gate → live
   ▲                                  │
   └──────────────────────────────────┘  iterate until channels earn their place
```

### 1. `discover` — enumerate candidates

```bash
pumpbot discover --write-config candidates.json
```

Lists channels the account has joined, plus public search results. **None of
these are vetted.** Subscriber counts are bought and pinned track records are
screenshots.

### 2. `record` — gather evidence, trade nothing

```bash
pumpbot record --out data/recorded/corpus.jsonl
```

Listens and logs. Places no orders. This is the step people skip and it is the
only one that produces information. Run it for **weeks**, not hours.

### 3. `fetch-prices` — get the prices to judge against

```bash
pumpbot fetch-prices data/recorded/corpus.jsonl --out data/recorded/prices.jsonl
```

Pulls 1-second klines around every call in the corpus. **`score` and
`simulate --corpus` refuse to run without this**, because replaying real
messages against the built-in synthetic feed would produce confident numbers
about a market that does not exist. `--synthetic-prices` overrides the refusal
for pipeline testing and says loudly that the output is meaningless.

Expect a meaningful fraction of symbols to come back empty — delisted, never
listed on your venue, or outside its 1-second kline retention. Those calls are
dropped rather than treated as flat, which would bias every channel's score
upward.

### 4. `score` — rank what you recorded

```bash
pumpbot score data/recorded/corpus.jsonl --prices data/recorded/prices.jsonl \
    --write-config trusted.json --min-score 0.6
```

Ranks channels and prints a verdict for each. Copy the qualifying ones into
`config.yaml` as `tier: trusted` and set `risk.min_channel_score`.

### 5. `simulate` — paper trade

```bash
pumpbot simulate --corpus data/recorded/corpus.jsonl --prices data/recorded/prices.jsonl
pumpbot simulate --corpus data/recorded/corpus.jsonl --prices data/recorded/prices.jsonl \
    --use-scores runs/<earlier>/report.json
```

`--use-scores` enforces `risk.min_channel_score` against a previous run's
scores. **Score on one period and trade on another** — scoring and trading the
same data is overfitting, and it will flatter you.

### 6. `gate` — see how close you are

```bash
pumpbot gate
```

### 7. `live` — real orders

```bash
export PUMPBOT_API_KEY=... PUMPBOT_API_SECRET=...
pumpbot live --dry-run                       # builds and signs orders, sends nothing
pumpbot live --i-accept-live-trading-risk
```

## The promotion gate

You asked for "ten profitable runs, then real money". That is implemented
literally, plus three guards that make it mean what you intended:

- **Thin runs don't count.** `min_trades_per_run` (default 5) stops one lucky
  fill from counting as a profitable run. Ten one-trade runs is a coin flipped
  ten times.
- **Live runs never count.** Otherwise the gate could be unlocked by the thing
  it exists to authorise.
- **Config fingerprint.** Change the stop loss and the streak resets. A streak
  earned with a 3% stop tells you nothing about a 12% stop.

The gate opening is *permission*, not *recommendation*. Live still needs
`--i-accept-live-trading-risk`, credentials in the environment, and stays under
`execution.live.max_notional_quote_hard_cap` (default 100).

Every report also carries a **significance test** on per-trade returns. A
thirty-trade run almost never clears |t| = 2, and the report says so plainly —
because ten such runs in a row is exactly how noise passes a ten-run gate.

## Latency

Speed is engineered where it pays, in order of effect:

1. **Multiple Telegram sessions.** Different accounts land on different Telegram
   data centres and the spread between them is routinely 30–120 ms. Set
   `telegram.extra_sessions`; the engine dedupes and keeps whichever copy
   arrives first. This is the single largest available win.
2. **Raw MTProto update handler.** Telethon's friendly `events.NewMessage`
   resolves entities before calling you, which can issue an RPC — an entire
   round trip to a Telegram DC. We read the channel id and text straight off the
   wire and resolve names later, off the hot path.
3. **Edited messages.** Many channels post a placeholder and *edit* in the
   ticker. `watch_edits: true` catches the edit, often seconds ahead of anyone
   reading by eye.
4. **Warm connections.** A cold TCP+TLS handshake to the venue is two round
   trips of pure waste. The pool is held open and pinged.
5. **Pre-computed everything.** Symbol filters loaded at startup, HMAC key
   pre-expanded and copied per order, `quoteOrderQty` so the venue does the
   sizing and we never hit a `LOT_SIZE` reject under time pressure.
6. **Synced clock.** Drift produces `recvWindow` rejects that look exactly like
   latency problems.

In-process decision time measures around **0.3 ms p50**. Every report prints the
percentiles and the per-stage breakdown. Once you are here, the remaining wins
are physical: run in the same region as the matching engine. Rewriting the
Python buys nothing.

## Reports

Written after **every** run to `runs/<timestamp>_<run_id>/` as Markdown, JSON
and HTML:

- headline verdict and net return after costs
- trade distribution — median vs mean, best/worst, top-winner concentration
- **"Is this result real?"** — t-statistic and how many trades you'd need
- signal funnel and a breakdown of *why* signals were rejected
- latency percentiles and per-stage timings
- how positions closed (stop / trailing / ladder / time)
- channel scoring table with verdicts and each channel's **best holding period**
- every trade, with entry latency and slippage
- promotion gate status

## Architecture

```
ingest/telegram.py    raw MTProto handler, multi-session, dedupe
   ↓
parsing/extractor.py  precompiled patterns, ~25 µs, precision over recall
   ↓
risk/manager.py       cooldowns, concurrency, drawdown halt, channel filter
   ↓
strategy/pump.py      entry chase guard; stop / trailing / ladder / time exits
   ↓
execution/            simulator.py (pessimistic) | live_binance.py (warm path)
marketdata/           feed.py (live + synthetic) | historical.py (klines)
   ↓
scoring/channels.py   hit rate, net median, originator, pre-post run
   ↓
reporting/report.py   markdown + json + html
```

One code path serves simulation and live. The moment the backtest and the live
bot are different programs, the backtest stops predicting anything.

### The simulation model is deliberately unkind

`marketdata/feed.py` models a pump as: a hidden accumulation ramp, a fast public
leg after the post, then exponential decay back toward — and often below — the
starting price. Fills are priced **at fill time, not signal time**, so latency
costs real money in the simulation. Slippage scales with size against resting
depth, fees are charged both ways, and orders are rejected 4% of the time.

A simulator that starts the price at 1.0 the instant the message arrives will
tell you this strategy prints money. It does not.

## Tests

```bash
pytest -q        # 113 tests
```

Covering parser precision (including the false positives that would fire market
orders on prose), exit priority, risk limits, gate arithmetic, slippage
direction, P&L accounting with partial exits, scorer discrimination, config
validation, determinism, and end-to-end report generation.

## Configuration

Everything lives in `config.yaml` (gitignored; copy from `config.example.yaml`,
which is commented throughout). Unknown keys are a hard error, not a shrug — a
silently ignored typo in a risk limit is the kind of thing you find out about
from your account balance.

## Limitations, stated up front

- **Synthetic mode validates the machinery, not the strategy.** It cannot tell
  you whether real channels are profitable. Only `record` + `fetch-prices` +
  `score` can.
- **1-second klines bound the fill, they do not pin it.** Historical sub-second
  data is not available at this scale, so scoring measures from the close of the
  bar containing the post. Where that bar is wide, the observation is noisy —
  the feed exposes `bar_spread_bps` so you can see where.
- **Solana/EVM contract signals are parsed but not routed.** DEX execution
  (Jupiter/Raydium, Jito bundles, honeypot and liquidity checks) is not
  implemented. Contract-address calls are scored, not traded.
- **Image and OCR signals are ignored.** Some channels post the ticker as an
  image specifically to slow bots down.
- **The significance test assumes independent, roughly normal returns.** Pump
  trades are neither. Treat it as a floor on the evidence you need, not a
  ceiling.
