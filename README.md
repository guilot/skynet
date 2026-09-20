# Bitget Momentum Scanner

A real-time scanner for Bitget USDT-perpetual futures that detects
statistically abnormal expansions of price and volume, plus a paper-trading
bot that acts on those signals through the same rule engine that the
backtester uses.

**The strategy does not have an edge.** It was measured
against a pre-registered criterion over 1,636 trades and 90 days of
reconstructed history, and it failed all three tests. The numbers, and how
they were obtained, are in [Results](#results) below. The code is published
because the machinery around the conclusion — the shared rule engine, the
reproducible backtest, the safety design of the live bot — is worth more than
the strategy it disproved.

> Code, comments and docstrings are in Spanish. Only this README is in English.

---

## Table of contents

- [What it does](#what-it-does)
- [The strategy](#the-strategy)
- [Results](#results)
- [Why it loses](#why-it-loses)
- [How the conclusion was reached](#how-the-conclusion-was-reached)
- [Architecture](#architecture)
- [Running it](#running-it)
- [Configuration](#configuration)
- [Tests](#tests)
- [Safety design of the live bot](#safety-design-of-the-live-bot)
- [Repository layout](#repository-layout)
- [License](#license)

---

## What it does

1. **Selects a universe.** Every 15 minutes it ranks Bitget's USDT-perp pairs
   and keeps those whose *typical minute volume* clears a floor. The gate is
   deliberately not 24h volume: measured on live data, 24h volume cannot tell
   a sustainable book from two bursts and silence — even a \$50M threshold
   still admitted a symbol with a median minute volume of \$0.

2. **Builds a volume profile.** For each symbol it downloads 14 days of 1m
   candles and derives an expected volume per minute-of-day, which is what
   makes "abnormal" mean anything. The profile is persisted, so restarts only
   fill the gap.

3. **Scores every symbol, every second.** Thirteen inputs, weighted to sum to
   exactly 100 (a test enforces it): returns over 1m/3m/5m/15m/1h/24h,
   relative volume over 1m/5m/session, a demand burst ratio, a return z-score,
   market cap, and distance from VWAP. The VWAP curve is the only one that
   goes negative — it penalises chasing something already extended.

4. **Emits state transitions.** Score maps to a state:
   `NORMAL < 50 ≤ WATCH < 65 ≤ HOT < 80 ≤ SIGNAL < 90 ≤ EXTREME`. Crossings
   are what the strategy trades, and every one is persisted with the
   configuration fingerprint and code revision that produced it, so the
   history stays segmentable after a recalibration.

5. **Trades them on paper.** The bot opens and manages positions through the
   same `PositionRules` engine the backtester drives, and a web dashboard
   shows the live scanner on top and the trade history underneath.

---

## The strategy

**Entry.** A transition that crosses *upward* into `WATCH` or higher from
below, with a score of at least 70 — which in practice means the symbol
landed in `HOT` or above in a single step. Direction is taken from the
transition. Position sizing: 2% of equity as margin at 20x, at most 5
concurrent positions.

**Exits**, in the order the engine evaluates them:

| Rule | Trigger | Action |
|---|---|---|
| `STOP` | Price moves 2.5% against the entry | Close everything remaining |
| `SCALE_HOT` | First upward crossing into `HOT` after entry | Take 33% off |
| `SCALE_SIGNAL` | First upward crossing into `SIGNAL` | Take another 33% off |
| `EXTREME` | Crossing into `EXTREME` | Hold the rest 3 more minutes, then close at market |
| `STALE_BE` | 10 minutes with no state change | Close at break-even |

The tranche for the level you *entered* at is never paid — entering directly
at `HOT` does not immediately sell 33%. After the first partial fill the stop
moves to break-even, using **the price actually executed**, not the
theoretical one. A symbol that closes 3 losing trades within an hour is frozen
for 3 hours.

**The key structural decision:** `PositionRules` is a single event-driven
engine shared by the backtester and the live bot, and it *proposes* exits
(`ExitIntent`) rather than executing them. The driver executes — the
backtester at the reference price, the bot against the broker — and reports
back the real `Fill`. Rules that depend on the obtained price are decided
there. The live bot feeds it a synthetic candle per tick
(`open = high = low = close = observed price`), so the bot only ever reacts to
prices it actually saw, while the backtester feeds real 1m OHLC. Same decision
code, two clocks.

---

## Results

Measured over **90 days** of reconstructed history (16 Jun – 15 Sep 2026),
91 symbols, 262,052 transitions, **1,636 trades**:

```
1,636 trades    46.7% winners    net −445.42 USDT    mean −0.27

[1] net excluding the 3 best trades (+176.13):  −621.55    FAIL
[2] thirds: −104.68 / −183.46 / −157.28 (0 of 3)           FAIL
[3] net after fees:                             −445.42    FAIL
```

**Verdict: the strategy has not demonstrated an edge.** All three
pre-registered tests fail.

Test 2 is the most conclusive. All three thirds are negative and of similar
magnitude — there is no good period carrying the rest, and no isolated bad
streak. An earlier `+33.76%` figure came from a two-week window and rested on
a **single trade** (+211.32 USDT); across 90 days it does not recur.

---

## Why it loses

```
GROSS (before fees):   +142.04 USDT
fees:                  −587.47 USDT
NET:                   −445.42 USDT

gross per trade:              +0.087 USDT
needed to cover fees:          0.359 USDT
```

**The signal is not garbage — it makes money gross.** It makes 8.7 cents per
trade while the toll is 36. It would have to be four times better just to
break even.

At 20x with staggered exits, commission is **2.40% of the margin on every
trade**: the price has to move 0.12% in your favour before you earn anything
at all. With 1,636 trades in 90 days, no small edge survives that.

This also answers the question that prompted the investigation — *"how do I
cut the losing trades?"* — the wrong way round: it was never a problem of
losers. **The cost of trading is four times the edge.** Any variant has to
quadruple gross per trade, or cut the number of trades or the cost per trade
by the same factor.

Deliberately **not** done: searching for the parameter set that fixes the
result. With 1,636 trades and enough sweeps one will always turn up, and that
is precisely the error the criterion was written to prevent.

---

## How the conclusion was reached

The full criterion is in [`docs/criterio-de-decision.md`](docs/criterio-de-decision.md)
(Spanish). What matters about it is the order of events: **it was written
before the extended backtest ran, and before anyone saw a single new number.**

That mattered because the week before, three hypotheses had been formed from
striking individual cases — an exit rule after `EXTREME`, a 1m thrust filter,
and sizing by signal quality. All three looked solid. All three collapsed
against a split sample. With 40–99 trades and a distribution dominated by one
outlier, you will always find a pattern.

A follow-up search for a subgroup with a better *directional* edge (measured
by MFE/MAE, max favourable vs max adverse excursion) was run on a train/holdout
split: 1 of 20 candidates survived into the holdout, non-monotone, with a
ratio of 0.992 — consistent with chance. Note that the metric has to be
computed as a **ratio of means**, not a mean of per-trade ratios: the latter
explodes whenever MAE ≈ 0 and reported a baseline of 5.96 with *20 of 20*
groups "surviving". That result was too good, which is how the bug was caught.

Four known biases are documented alongside the result, rather than left
implicit:

1. **Survivorship.** The universe is rebuilt from pairs that exist *today*.
   Pairs listed and delisted within the period are missing, and this strategy
   trades exactly the kind of alt-coin where that happens. **Biases upward.**
2. **`market_cap` is approximated** with today's circulating supply — the only
   one of the 13 score inputs that cannot be reconstructed from candles.
3. **The backtest is pessimistic about stops.** It reads each minute's high
   and low; the live bot only sees the prices it samples. Over the 39 trades
   both took, the live bot came out **+32.22 USDT better** on stop exits.
4. **The reconstruction produces ~76% of the transitions** the live scanner
   generates on the same data. The scanner evaluates every second against the
   in-flight candle; Bitget's history only goes down to 1m. The missing ones
   are intra-minute spikes. **Direction of bias unknown** — the historical
   backtest is a *coarser* version of the strategy, not the same one.

---

## Architecture

The project was built in three phases, and the layout still reflects them.

**Phase 1 — the scanner.** `universe/` selects pairs, `engine/` keeps the
candle buffers and REST/WS ingestion, `scoring/` turns metrics into points
and points into states, `storage/` persists candles, profiles, transitions and
signal outcomes to SQLite, `api/` serves the dashboard.

**Phase 2 — the strategy and the backtest.** `strategy/` holds the rule
engine and nothing else: no I/O, no portfolio loop, and it may not import
from `backtest/`. `backtest/` drives it over stored history, collapses
consecutive signals from the same symbol into episodes, segments the history
by provenance fingerprint, and refuses to emit a recommendation below 30
episodes. Its report is frozen by a **golden master** test, character for
character.

**Phase 3 — live execution.** `bot/` contains the runner, the brokers
(`PaperBroker` and `BitgetBroker`) and the brakes; `bitget/private.py` is the
authenticated V2 client. Three effective modes: `paper`, `real_lectura`
(authenticated reads, paper execution) and `real`.

**`herramientas/`** holds the three offline tools written for the final
investigation: a resumable parallel downloader for historical candles, a
reconstructor that replays the scoring engine minute by minute over them, and
the evaluator that applies the three criterion points and exits 0 or 1.

---

## Running it

Requires Python 3.12+ (developed on 3.14).

```bash
pip install -e ".[dev]"
python -m scanner_volumen
```

Dashboard at `http://127.0.0.1:8000`.

The first start downloads 14 days of 1m candles per symbol (~25 minutes with
the default universe). The scanner is usable from minute one: symbols without
a complete profile are marked ⚠ and fall back to a less reliable volume
reference. History is persisted, so later restarts only fill the gap.

To inspect an existing database without running the scanner:

```bash
python ver_panel.py path/to/scanner.db
```

**Credentials.** The bot reads them from the environment, never from a
committed file. See `deploy/ENTORNO.md`. They are never logged, never appear
in a `repr`, and never appear in an exception message.

---

## Configuration

Everything lives in `config.toml`, heavily commented with the measurement
that justifies each value. The ones that matter most:

| Key | Effect |
|---|---|
| `universe.min_profile_median_volume` | The real gate: minimum typical minute volume to stay in the active universe |
| `universe.min_volume_24h` | Cheap prefilter: only bounds how many symbols get a history download |
| `universe.max_symbols` | Cap on symbols watched at once |
| `states.watch/hot/signal/extreme` | Score thresholds for each state |
| `score.curves.*` | How each metric translates into points |
| `bot.enabled` / `bot.modo` | Whether the bot runs, and in which mode |
| `bot.perdida_diaria_max` | Daily loss fraction that brakes new entries |

Score weights sum to exactly 100 and a test verifies it: recalibrating one
curve means compensating in another.

---

## Tests

```bash
pytest
```

836 passing, 4 skipped, no network access — the suite runs against real
Bitget responses captured in `tests/fixtures/`.

An integration bench against Bitget's **demo** account exists separately and
is skipped unless demo keys are present:

```bash
pytest -m integracion
```

That bench is the only code in the project that sends real orders to an
exchange, so it is built to be *structurally* incapable of touching a live
account, with two independent guards. It reads credentials only from three
demo-specific environment variable names — there is no code path that reads
any other name, so no accidental fallback exists — and it aborts before
constructing the authenticated client unless the `productType` and the symbol
are unambiguously simulated.

The symbol check is worth a note, because the obvious version of it is wrong.
Demo symbols start with `S`, so `startswith("S")` looks sufficient — but
`SOLUSDT`, `SUIUSDT`, `SHIBUSDT`, `SEIUSDT`, `SANDUSDT` and `SXPUSDT` are all
real pairs this scanner trades, and all six passed it. The discriminant is the
**suffix**: demo symbols quote against `SUSDT`. All six are pinned as negative
test cases.

Running that bench against the demo account is also what turned a pile of
API assumptions into facts, and corrected six of them — among others:
`marginCoin` derives from the productType; `place-order` requires
`marginMode`; `holdSide` uses *order* vocabulary, so `"buy"` means a LONG
position and passing the wrong side silently targets the other one; and in
hedge mode `reduceOnly` is rejected outright, which is why one-way mode is
mandatory — the entire safety model of live execution rests on reduce-only
exits.

---

## Safety design of the live bot

Even though the conclusion is not to trade this, the guards are the part of
the repo most worth reading:

- **Two independent keys for real mode.** `modo = "real"` in `config.toml`
  *and* the environment variable `SCANNER_BOT_REAL`. Either one alone runs
  paper.
- **Emergency stop file.** If `data/parar_bot` exists, entries stop. It uses
  `os.stat`, not `Path.exists()` — on Python 3.13+ `exists()` swallows
  `EACCES` and returns `False`, so an unreadable stop file failed *open*. The
  three cases are distinguished: absent → no brake, unreadable → brake,
  present → brake. "I can't tell" is never treated as "nothing there".
- **Daily loss brake**, with the day's reference balance validated
  (`isfinite` and `> 0`) at both entry points. A `nan` balance used to be
  persisted as the reference and disabled the brake for the whole UTC day,
  across restarts.
- **Reduce-only stop orders placed on the exchange**, so a stop survives the
  bot crashing.
- **Reserve reconciliation on restart.** An orphan reserve is adopted from
  order history, but marked degraded and kept under veto rather than trusted.
- **The bot does not reconfigure the account**, with one narrowly scoped
  exception: it may set isolated margin and leverage per symbol, and refuses
  even that if a position is open on that symbol.

---

## Repository layout

```
scanner_volumen/
  universe/    pair selection
  engine/      candle buffers, REST + WebSocket ingestion
  scoring/     score curves and state thresholds
  strategy/    shared rule engine (no I/O, no dependency on backtest/)
  backtest/    offline driver, episodes, segmentation, report
  bot/         live runner, brokers, brakes
  bitget/      public and authenticated V2 clients
  storage/     SQLite persistence
  api/         FastAPI server and dashboard
herramientas/  offline tools: downloader, reconstructor, criterion evaluator
deploy/        deployment script and environment notes
docs/          the pre-registered decision criterion
tests/         836 unit tests + the demo integration bench
```

---

## License

MIT — see [LICENSE](LICENSE).

This is published as a record of an investigation, not as trading advice and
not as something to run with money. Its measured result is a loss.
