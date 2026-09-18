# Read this before using live mode

This is not legal advice. It is the set of facts you should have in hand before
pointing this at a real account.

## 1. Market manipulation is regulated for crypto in the EU

Since MiCA (Regulation (EU) 2023/1114) came into application, market abuse
provisions apply to crypto-assets:

- **Article 91** prohibits market manipulation in crypto-assets.
- **Article 92** obliges trading platforms to detect and report it.
- National competent authorities (in Poland, **KNF**) supervise and enforce.

"Pump and dump" is a named example of market manipulation. The organisers of
such a scheme are squarely in scope. Whether a **follower** who knowingly trades
a coordinated pump is also in scope is fact-dependent — but "I only followed the
signal" is an argument you would be making after the fact, not a safe harbour.

Trading on a venue outside the EU does not remove EU law from you if you are
resident here.

## 2. Tax

In Poland, gains on crypto disposals are reported on **PIT-38** at a flat 19%.
Costs of acquisition are deductible; losses carry forward against future crypto
income. A bot generating hundreds of transactions creates a real record-keeping
obligation. The JSONL logs this tool writes are designed to be usable as that
record — keep them.

If trading becomes organised and continuous enough, tax authorities may
characterise it as business activity, which changes the treatment. Ask an
accountant before scale, not after.

## 3. Exchange terms of service

Most venues prohibit participation in manipulative schemes in their terms, and
several actively detect pump activity on low-cap pairs. The realistic
consequence is not a fine — it is a frozen account with your balance in it,
and an appeals process with no deadline.

Also check the venue's API rules: rate limits, market-order restrictions on
volatile pairs, and any requirement to register algorithmic trading.

## 4. What this software does and does not do

It **does**: read public channels you have joined, parse messages, score channel
track records, simulate trades, and — if you explicitly unlock it — place
ordinary market orders on your own account with your own credentials.

It **does not**, and will not be extended to: post to channels, create or
amplify signals, coordinate with other participants, wash trade, spoof, layer,
operate multiple accounts to disguise a single actor, or evade venue detection.
Those are the things that turn "trading badly" into "market manipulation", and
they are out of scope permanently.

## 5. The economics, stated plainly

Most Telegram pump channels are structured so that the organisers profit and the
followers fund it. They accumulate before the post; the post exists to create
the liquidity they sell into. The `pre_pump_pct` column in every report measures
exactly this. Where it is large, being faster than other followers does not help
you — it only gets you to the top of the move sooner.

Run `record` and `score` for weeks before you believe any channel is worth
trading. The scoring module exists to talk you out of this, and if it does, it
has paid for itself.

## 6. Money

`execution.live.max_notional_quote_hard_cap` is enforced in code and defaults to
100 units of the quote asset. Raise it only after live results — not simulated
ones — justify it. Start with an amount whose total loss you would find
uninteresting.
