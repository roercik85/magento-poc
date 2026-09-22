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

This project lives in the `pumpbot/` subdirectory of the repository, so that
is where the install runs from — `pip install -e .` at the repository root
fails with "neither 'setup.py' nor 'pyproject.toml' found".

```bash
git clone https://github.com/roercik85/magento-poc.git
cd magento-poc/pumpbot          # <- the project root, not the repo root

python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[live,dev]"    # drop [live] for simulation only
```

Verify before going near Telegram — this needs no config file, no
credentials and no network:

```bash
pumpbot simulate --messages 2000
```

That should print a report and `gate: 1/10 🔒 locked`. Needs Python 3.11+.

**No command needs a config file.** Secrets come from the environment, and
everything else has a working default. Copy a template only to change
something:

```bash
cp config.example.yaml config.yaml            # or config.10usd.example.yaml
```

A config in the working directory is picked up automatically — `config.yaml`
first, then `config.10usd.yaml`, then any other `config*.yaml`. Templates
(`*.example.yaml`) are never selected, since silently running one would hide
that no real config exists. Every command prints which config is in force.
Passing `-c` names a file explicitly, and a missing one is then an error
rather than a silent fallback.

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
discover → triage → record → score → simulate → gate → live
   ▲          │        │
   │          │        └─ weeks, but only on channels that survived triage
   └──────────┘  hours, on history — kills most candidates same day
```

### 1. `discover` — enumerate candidates

```bash
pumpbot discover --write-config candidates.json
```

Lists channels the account has joined, plus public search results. **None of
these are vetted.** Subscriber counts are bought and pinned track records are
screenshots.

### 2b. `record` — gather evidence, trade nothing

```bash
pumpbot record --out data/recorded/corpus.jsonl
```

Listens and logs. Places no orders. This is the step people skip and it is the
only one that produces information. Run it for **weeks**, not hours.

### 3. `triage` — judge channels on their past, today

```bash
pumpbot triage --days 30 --write-config shortlist.json

# or with no API login at all, from a Telegram Desktop export:
pumpbot triage --from-export ~/Downloads/ChatExport --days 30
```

**This is what stops you spending weeks on junk.** Telegram serves a channel's
history and KuCoin serves one-minute candles at least a year back, so a
channel's last month of calls can be scored this afternoon.

Two passes. The **structural screen** rejects channels on shape alone, before
fetching a single price:

| Pattern | What it means |
|---|---|
| Pitches a VIP tier | The product is the subscription, not the trading |
| Forwards most content | A relay — find what it forwards from |
| "Next call in 10 minutes" | An accumulation window for whoever knows the ticker |
| 40+ calls a day | Volume, not selection: something it named always went up |
| Too quiet to ever reach a sample | Cannot be evaluated at all |

Survivors get **priced against one-minute candles**. That resolution cannot
tell you what a fast bot would have been filled at on a 30-second spike — but
it answers the two questions that do not need sub-minute precision, and that
kill most channels anyway: how far the price had already run *before* the call,
and whether the call is worth anything 5-15 minutes later.

> Triage ranks channels. It does not tell you what you would have earned.
> Survivors still need live tick recording before you trade them.

**If the API login will not work**, `--from-export` skips Telegram entirely.
Login codes for freshly created applications are sometimes accepted by the
server and then silently never delivered, which leaves nothing to debug.

There are two macOS apps called Telegram and they export differently:

**Export one channel at a time**, from the chat list: right-click the channel
→ *Export chat history* → format **JSON**, media **off**.

The account-wide export under Settings → Advanced does *not* work for this.
It fixes public channels at **"only my messages"** and the checkbox cannot be
cleared — the content belongs to the channel owner — so a broadcast channel you
merely read exports with zero messages. `triage` detects that case and says so
rather than reporting an empty result.

Per-chat export has no such limit. You end up with one folder per channel;
put them in one directory and point `--from-export` at it. They are found
recursively, merged and de-duplicated, since exports taken on different days
overlap. Media off matters: it is gigabytes, and triage reads only text.

Recording still needs a working login later. The pass that decides whether any
of this is worth doing does not.

### 3b. `inspect` — see what the parser made of each message

```bash
pumpbot inspect --from-export PATH --channel "safe calls" --show-misses
```

When triage reports few calls or none, that has two very different causes — a
quiet channel, or a parser that is too strict — and the summary line cannot
tell them apart. This prints each message with the verdict on it: the symbol,
the pattern that fired, the confidence, and whether the venue lists it.
`--show-misses` prints what produced nothing, which is where a real call the
parser dropped would show up.

`--summary` aggregates by symbol and pattern instead of printing every
message — the view you want when a channel produces seventy apparent calls and
you need to know whether sixty of them are one prose word arriving through the
same pattern.

`--min-confidence` overrides the threshold for one run, so you can see what a
looser setting would admit before changing anything.

### 3c. `onchain-check` — are the calls tradable on any chain?

```bash
pumpbot onchain-check --from-export PATH --channel "gem signals" --notional 10
```

On a centralised venue the ticker *is* the instrument. On a DEX it is a claim:
anyone can mint a token with any name, and measured against the live chain
while this was built, fourteen distinct mints call themselves HEDGE, seven call
themselves EVE, and DexScreener's top result for "BONK" is a mint with $249M of
claimed liquidity, $3.99 of daily volume and two trades in a day.

**Every chain is searched, not one.** An earlier version checked Solana alone,
because the channel said "on sol" — it also said "on Base", "on Rh" and "on
Arc". Tokens it reported as having no market trade normally elsewhere: EVE's
Solana pools are empty while it round trips at -1.2% on BSC and -2.9% on Base.
Quoting is Jupiter for Solana and LI.FI for EVM chains, both keyless.

A chain with no public router is reported **unquotable, never untradable**.
HEDGE's largest market is Robinhood Chain with $260k of daily volume and
nothing here can price it — that means look elsewhere for a router, not that
there is no market.

So resolution is contract-first. An address in the message names the token; a
bare ticker resolves only when exactly one candidate shows real trading
activity, and two plausible candidates is reported as ambiguous rather than
guessed.

Each resolved token is then **bought and sold back in quotes**. That one
measurement catches, at the position's real size:

| What it catches | How |
|---|---|
| Not tradable | No buy route exists |
| **Honeypot** | Buys route, the sell does not — invisible in liquidity and holder counts |
| Transfer tax | The round trip loses far more than fees |
| Dead depth | Your own order is the market |

Run this before building anything on top of a channel. If nothing routes, no
amount of execution work helps: there is nothing on the other side.

With several channels, `--brief` drops the per-token detail and prints a
ranking by how much of each channel's output survives to a fill:

```
  channel                    calls  w/addr  named  routes  usable  where they die
  Fortune AI Official           26      0%     10      10       5  19% reach a fill
  Crypto Gem Signals            35     66%     23       4       1  3% reach a fill
  Wall Street Gems               6      0%      0       0       0  never identified
```

A call dies in one of two quite different places, and a single count hides
which. `named` is how many were identified at all — a bare ticker can name a
dozen mints and this refuses to guess between them. `routes` is how many of
those had a market. The first failure is fixable by choosing channels that
post contract addresses; the second is not fixable at all.

> Measured over fifteen real channels: posting addresses is **necessary but
> not sufficient**. The channel above posts an address with 66% of its calls,
> and reaches a fill on 3% of them — the addresses resolve perfectly and name
> tokens with no market.

The footer converts the observed rate into the months of collecting needed
before a result is distinguishable from luck. A usable-call count reads as
progress; that conversion usually says something else.

### 4. `fetch-prices` — get the prices to judge against *(Binance only)*

```bash
pumpbot fetch-prices data/recorded/corpus.jsonl --out data/recorded/prices.jsonl
```

Pulls 1-second klines around every call in the corpus. **`score` and
`simulate --corpus` refuse to run without prices**, because replaying real
messages against the built-in synthetic feed would produce confident numbers
about a market that does not exist. `--synthetic-prices` overrides the refusal
for pipeline testing and says loudly that the output is meaningless.

> **On KuCoin there is no backfill — skip this step, it cannot work.** KuCoin
> serves klines no finer than 1 minute and only the last ~100 trades (about
> half a minute on a liquid pair). A 30-second pump is invisible in a 1-minute
> bar. So `pumpbot record` captures ticks **live**, via the trade websocket,
> subscribing to each symbol the moment a channel names it and writing 1-second
> bars in the same format this command produces. Run `record` with market data
> enabled from day one: messages recorded without matching ticks can never be
> scored, at any price, and you will not find that out until you try.

Expect a meaningful fraction of symbols to come back empty — delisted, never
listed on your venue, or outside its 1-second kline retention. Those calls are
dropped rather than treated as flat, which would bias every channel's score
upward.

### 5. `score` — rank what you recorded

```bash
pumpbot score data/recorded/corpus.jsonl --prices data/recorded/prices.jsonl \
    --write-config trusted.json --min-score 0.6
```

Ranks channels and prints a verdict for each. Copy the qualifying ones into
`config.yaml` as `tier: trusted` and set `risk.min_channel_score`.

### 6. `simulate` — paper trade

```bash
pumpbot simulate --corpus data/recorded/corpus.jsonl --prices data/recorded/prices.jsonl
pumpbot simulate --corpus data/recorded/corpus.jsonl --prices data/recorded/prices.jsonl \
    --use-scores runs/<earlier>/report.json
```

`--use-scores` enforces `risk.min_channel_score` against a previous run's
scores. **Score on one period and trade on another** — scoring and trading the
same data is overfitting, and it will flatter you.

### 7. `gate` — see how close you are

```bash
pumpbot gate
```

### 8. `live` — real orders

```bash
export PUMPBOT_API_KEY=... PUMPBOT_API_SECRET=...
export PUMPBOT_API_PASSPHRASE=...            # KuCoin only; checked at startup
pumpbot -c config.10usd.yaml live --dry-run  # builds and signs, sends nothing
pumpbot -c config.10usd.yaml live --i-accept-live-trading-risk
```

## The 10 USDT test

`config.10usd.yaml` is a ready profile: 2 USDT per trade, 3 concurrent slots,
6 USDT maximum exposure, 3 USDT hard cap per order, 25% drawdown stop. KuCoin's
0.1 USDT minimum means nothing about this size is artificial.

**It answers real questions:** do orders reach the venue and fill; is real
latency near the simulated 220 ms; does the exit ladder fire on a real book or
does the sell round to zero; is slippage during an actual pump anywhere near
35 bps; does the bot survive a websocket drop and a delisted symbol.

**It cannot tell you whether the strategy is profitable.** Per-trade dispersion
for these trades is around 7%, so distinguishing an edge from zero needs
roughly `(2 × 7 / edge)²` trades:

| True edge per trade | Trades needed | Profit at 2 USDT/trade |
|---|---|---|
| 0.5% | 740 | 7.40 USDT |
| 1.0% | 185 | 3.70 USDT |
| 2.0% | 47 | 1.88 USDT |
| 3.0% | 21 | 1.26 USDT |

Measured round-trip cost at this size is ~0.42%, so anything under ~0.4% gross
per trade is negative before it starts. The money is irrelevant by design —
47 trades to earn 1.88 USDT is a good trade if what you bought was finding out
whether the edge is real.

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

## Venues

| | KuCoin | Binance |
|---|---|---|
| Live execution | ✅ | ✅ |
| Tradable USDT pairs | 833 | 1370 |
| Minimum order | **0.1 USDT**, every pair | 5 USDT on most pairs |
| Taker fee | 0.1% | 0.1% |
| Historical sub-minute prices | ❌ none — must record live | ✅ **1-second klines, 30+ days back** |
| Market data without a key or geo-block | ✅ | ✅ via `data-api.binance.vision` |
| Measured slippage, 10 USDT order | ~12 bps one-way | — |

**Pick the venue the channels actually name.** Most "pump" channels target
Binance, and measuring them elsewhere throws away the calls that matter: a run
against KuCoin dropped twelve of thirty-six symbols as "not listed", including
tokens with over half a million dollars of daily volume, then reported a
verdict on the leftovers.

Binance's one-second klines also remove the need to record live before
learning anything. A pump that peaks forty seconds after a call is measurable
retrospectively — `triage` fetches seconds around each call and minutes for the
three days before it, which is the window where accumulation ahead of a call
shows up. Use `config.binance.example.yaml`.

KuCoin is the default. It is where low-cap pump targets more often live, its
0.1 USDT minimum makes small live tests possible, and its books at 5-10 USDT
cost about 12 bps one-way — measured across twelve pairs in the 5k-400k USDT
daily volume band, against a median half-spread of 11 bps.

The trade-off is the data: **Binance lets you backfill prices after the fact,
KuCoin does not.** On KuCoin the market data has to be captured while the call
is live or it is gone. That is why `record` runs a tick recorder rather than
leaving prices to a later step.

> Measured on calm books. During the pump you are trying to trade, everyone
> sweeps at once and realised slippage is a multiple of this. The simulator's
> 35 bps default is deliberately left about 3x above the calm measurement.

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
ingest/history.py     past messages + structural screen (no prices needed)
ingest/telegram_export.py   Telegram Desktop JSON — the no-API path
   ↓
parsing/extractor.py  precompiled patterns, ~25 µs, precision over recall
   ↓
risk/manager.py       cooldowns, concurrency, drawdown halt, channel filter
   ↓
strategy/pump.py      entry chase guard; stop / trailing / ladder / time exits
   ↓
execution/            simulator.py | live_binance.py | live_kucoin.py
marketdata/           feed.py | historical.py | binance.py | kucoin.py | onchain.py
risk/token_safety.py  round-trip quote: honeypot, tax and depth in one check
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
pytest -q        # 337 tests
```

Covering parser precision (including the false positives that would fire market
orders on prose), exit priority, risk limits, gate arithmetic, slippage
direction, P&L accounting with partial exits, scorer discrimination, config
validation, determinism, and end-to-end report generation.

## Configuration

Everything lives in `config.yaml` (gitignored; copy from `config.example.yaml`,
which is commented throughout) — or nowhere at all, since every setting has a
working default and the file is optional. Unknown keys are a hard error, not a
shrug: a silently ignored typo in a risk limit is the kind of thing you find
out about from your account balance.

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
- **Finding the channels is not automated, and cannot be.** `discover`
  enumerates what your account can see; which of them are worth trading is
  decided by `record` + `score` on observed outcomes. A list of channel names
  from a web search is the marketing material this whole scoring module exists
  to distrust.
- **Image and OCR signals are ignored.** Some channels post the ticker as an
  image specifically to slow bots down.
- **The significance test assumes independent, roughly normal returns.** Pump
  trades are neither. Treat it as a floor on the evidence you need, not a
  ceiling.
