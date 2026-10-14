# Kalshi public market and event API facts

Verified on 2026-07-21 against official Kalshi documentation only.

- Public market and event data may be requested without an API key or authentication header.
  Apex uses only the production REST base `https://external-api.kalshi.com/trade-api/v2` and
  bounded `GET /markets`, one exact `GET /markets/{ticker}`, ordinary `GET /events`, one exact
  `GET /events/{event_ticker}?with_nested_markets=true`, per-market `GET /markets/{ticker}/orderbook`, `GET /series/{series_ticker}`,
  and the fallback `GET /series/fee_changes`. [Kalshi market-data quick start](https://docs.kalshi.com/getting_started/quick_start_market_data)
  and [API environments](https://docs.kalshi.com/getting_started/api_environments)
  document that boundary.
- `GET /markets` returns a top-level `markets` array and a cursor. A caller starts without a
  cursor, sends the returned cursor for the next page, and terminates at a null cursor.
  Apex bounds both page size and number of pages, and rejects cursor repetition. [Get Markets](https://docs.kalshi.com/api-reference/market/get-markets)
  and [pagination](https://docs.kalshi.com/getting_started/pagination) describe this behavior.
- `GET /markets` and `GET /markets/{ticker}` provide exact market and event
  tickers but do not document a market `series_ticker`. For bounded capture,
  Apex retains structurally eligible list markets with series identity marked
  `PENDING_EVENT_ENRICHMENT`, then fetches that exact event with
  `with_nested_markets=true`. The event's exact `series_ticker` and nested
  market membership are the authority for series lineage; Apex never infers it
  from ticker, title, or subtitle text. It then obtains series fee evidence and
  refreshes the exact market once before any orderbook observation. [Get market](https://docs.kalshi.com/api-reference/market/get-market)
  and [Get Events](https://docs.kalshi.com/api-reference/events/get-events)
  document those responses.
- `GET /markets?status=` accepts the filter vocabulary `unopened`, `open`, `paused`,
  `closed`, and `settled`, but returned market records use lifecycle values
  `initialized`, `active`, `inactive`, `closed`, `determined`, `disputed`, `amended`, and
  `finalized`. In particular, `status=open` returns `active` trading markets. Apex maps those
  documented response states to its canonical filter-state vocabulary before structural selection;
  it requests `status=open` and explicitly sends `mve_filter=exclude`.
  [Market lifecycle](https://docs.kalshi.com/getting_started/market_lifecycle) documents both
  vocabularies.
- Current authoritative price fields are fixed-point dollar strings such as
  `yes_bid_dollars`, `yes_ask_dollars`, `no_bid_dollars`, and `no_ask_dollars`. Quantities use
  `_fp` fixed-point strings. Apex parses those strings straight to `Decimal`; it does not fall
  back to deprecated cent fields. [Kalshi fixed-point migration](https://docs.kalshi.com/getting_started/fixed_point_migration)
  and the [changelog](https://docs.kalshi.com/changelog) record the current representation.
- The observed response shape includes market and event tickers, lifecycle timestamps,
  primary and secondary rules, quote sizes, volume, open interest, and liquidity. Apex retains
  the exact raw bytes, SHA-256, receive time, response cursor, and normalized lineage. Receive
  time—not `updated_time`—is its REST snapshot observation time.
- `GET /events` returns a top-level `events` array plus cursor, accepts `limit`, `cursor`,
  `status`, and `with_nested_markets`, and documents `unopened`, `open`, `closed`, and
  `settled` event states. Apex requests `status=open` with nested markets, bounds pages and
  events, and rejects cursor cycles. [Get Events](https://docs.kalshi.com/api-reference/events/get-events)
  and [pagination](https://docs.kalshi.com/getting_started/pagination) document the contract.
- Event records include `event_ticker`, authoritative `series_ticker`, title/category fields,
  `mutually_exclusive`, `settlement_sources`, and nested market observations when requested.
  Capture enriches at most ten deterministically ordered candidates and stops after the first
  exact event, series, and refreshed-market lineage chain validates. Ordinary `GET /events`
  excludes multivariate events; the separately documented multivariate endpoint is intentionally
  outside this boundary. [Get Events](https://docs.kalshi.com/api-reference/events/get-events)
- Exact market records document structured `market_type`, `strike_type`, `floor_strike`, and
  `cap_strike` fields. Apex's threshold-ladder scan supports only ordinary binary `greater` and
  `less` threshold records: a lower `floor_strike` is broader for `greater`, while a higher
  `cap_strike` is broader for `less`. It requires exact event membership, series identity,
  settlement-source evidence, and structured rule-family compatibility. It never derives a
  relationship from a title, subtitle, ticker, response order, or natural-language similarity.
  Ranges, custom strikes, MVE records, and unsupported structured forms remain unsupported.
  [Get Event](https://docs.kalshi.com/api-reference/events/get-event) and
  [Get Market](https://docs.kalshi.com/api-reference/market/get-market) document these fields.
- `mutually_exclusive` does not establish exhaustive outcome coverage, and this documented
  public response does not provide an explicit exhaustiveness assertion. Apex persists
  `UNKNOWN` for live scans and refuses complete-cover consistency classification in that state.
  Fixture-only, separately hash-bound evidence may be `VERIFIED` for offline tests; it is not
  attributed to Kalshi.
- The orderbook response contains only bid arrays: `orderbook_fp.yes_dollars` and
  `orderbook_fp.no_dollars`. Each current fixed-point level supplies a dollar-price string and
  fixed-point contract-count string; the provider orders bids from low to high. The public
  orderbook guide states that a YES bid at `x` is a NO ask at `1.00-x`, and conversely a NO bid at
  `y` is a YES ask at `1.00-y`. Apex derives, rather than receives, those asks and retains the
  original bid evidence. [Orderbook responses](https://docs.kalshi.com/getting_started/orderbook_responses)
  documents the unauthenticated single-market endpoint and its response. The separately documented
  [multiple-orderbook endpoint](https://docs.kalshi.com/api-reference/market/get-multiple-market-orderbooks)
  lists required Kalshi access headers as of this verification, so it is intentionally not used by
  this unauthenticated public-evidence boundary.
- `GET /series/{series_ticker}` returns current series fee metadata including `fee_type` and
  `fee_multiplier`. `GET /series/fee_changes` accepts a series ticker and `show_historical`; Apex
  requests it only if current series metadata cannot represent an exact fee. It persists the
  received raw bytes and effective-time field when supplied. [Get series](https://docs.kalshi.com/api-reference/market/get-series)
  and [Get series fee changes](https://docs.kalshi.com/api-reference/exchange/get-series-fee-changes)
  document those responses.
- A capture makes at most twenty sequential orderbook requests, with a locally
  enforced five-to-three-hundred-second interval. Its optional selection discovery is bounded to
  three pages and 300 markets; no orderbook is requested until exactly one structurally eligible
  ticker has been selected. A trading-compatible lifecycle, exact ticker/rule/event/series lineage, and
  supported binary structure are required, but list-market quotes, visible liquidity, a shadow
  candidate, and a positive complement gap are not. Empty and one-sided structurally valid books
  remain retained observations. There is no automatic retry loop. This remains far below the
  documented read-budget model; Apex still treats 429 and transient 5xx responses as
  evidence-bearing bounded failures, not a reason to increase request rate. [Rate limits and
  tiers](https://docs.kalshi.com/getting_started/rate_limits) describes separate read budgets and
  token costs.
- The current official fee schedule, effective July 7, 2026, gives immediate taker fees as
  `round up(M × 0.07 × C × P × (1-P))`, where `M` is the series multiplier, `C` quantity, and `P`
  dollar price. Its maker coefficient is `0.0175`; Apex records it but does not model maker
  behavior. Kalshi rounds the trade fee itself up to a centicent, then applies balance-alignment
  rounding with possible per-order accumulator rebates. This phase uses the named conservative
  assumption `per-displayed-slice-centicent-trade-fee-then-cent-balance-rounding-no-rebate-v1`:
  each displayed depth slice is an immediate separate taker fill, applies the documented centicent
  trade-fee and cent-balance rounding, and takes no rebate credit. [July 2026 fee schedule](https://kalshi.com/docs/kalshi-fee-schedule.pdf)
  and [fee rounding](https://docs.kalshi.com/getting_started/fee_rounding) are the only fee sources
  used here.
- A threshold-ladder round makes sequential single-market public orderbook requests and retains
  both receive times. It evaluates a verified pair only within its configuration-bound skew,
  derives the broader YES ask from the broader NO bid and the narrower NO ask from the narrower
  YES bid, matches equal displayed quantities with `Decimal`, and applies fees plus conservative
  rounding independently to both legs. The retained settlement floor is `1.00` per pair; any
  possible two-dollar middle-state value is not treated as guaranteed. A repeated positive lower
  bound is only repeated point-in-time research evidence, not simultaneous availability,
  fillability, profitability, arbitrage, or execution authority.

This is REST-snapshot evidence for structural data-quality and logical-consistency research only.
It is not a fair probability, price forecast, recommendation, trading signal, or arbitrage claim.
There is no WebSocket support, credentials, authenticated account access, order submission,
financial-state mutation, or execution capability. A positive fee-aware complement or threshold
bundle is only a
displayed-liquidity shadow priority: it does not prove availability, fillability, profitability,
or authority to trade. Reverify these facts against official Kalshi documentation before extending
this provider boundary.

## Resolution/settlement fields (added 2026-09-01)

Genuinely reverified against the live production API during Kalshi Settlement &
Resolution Evidence Maturation v1 -- not carried over from the fixture cache or
assumed. Two real, retained-corpus tickers (`KXTEMPLAXH-26JUL2108-T73.99`,
`KXGOLDH-26JUL2108-T4059.99`) were fetched live via `GET /markets/{ticker}` from
an isolated worktree; a representative response for a resolved market includes,
alongside the fields the original section above already documents:

```json
{
  "ticker": "KXTEMPLAXH-26JUL2108-T73.99",
  "status": "finalized",
  "result": "no",
  "settlement_value_dollars": "0.0000",
  "settlement_ts": "2026-07-21T13:30:59.802886Z",
  "close_time": "2026-07-21T12:00:00Z",
  "settlement_timer_seconds": 3600
}
```

- `result` is a lowercase `"yes"`/`"no"` string (Apex normalizes to uppercase).
  Its own field-level documentation does not itself assert *when* in the
  lifecycle it first becomes populated -- observed only alongside
  `status="finalized"` in every real, retained-corpus example fetched during
  this milestone. Apex does not assume it is exclusively populated at
  `finalized` (no genuine `determined`/`disputed`/`amended` example was
  available in the retained corpus to confirm or refute this either way);
  see Kalshi Settlement & Resolution Evidence Maturation
  v1 for why Apex treats `result` as
  authoritative if and only if `status == "finalized"`, regardless.
- `settlement_value_dollars` is a fixed-point dollar string (`"0.0000"`/`"1.0000"`
  observed), parsed straight to `Decimal`, matching every other dollar field
  this document already describes.
- `settlement_ts` and `close_time` are both RFC 3339 UTC timestamp strings,
  parsed with the same `parse_public_market_timestamp` this document's
  discovery paths already use. They are genuinely distinct real-world
  instants (`close_time` when trading ended; `settlement_ts` when the
  settlement was actually computed) -- observed roughly `settlement_timer_
  seconds` apart in the examples above (close 12:00:00Z, settlement
  ~13:00-13:31Z, against a documented 3600-second timer), consistent with
  Kalshi's own settlement-timer field.
- The full documented lifecycle beyond `open`/`closed` -- `determined`,
  `disputed`, `amended`, `finalized` (see the main section above) -- was
  genuinely exercised as a *classification* target by this milestone, but
  only `finalized` was actually observed in retained-corpus data fetched
  live during this pass; no genuine `determined`/`disputed`/`amended`
  example currently exists in Apex's retained corpus to inspect. Apex's
  own handling is therefore deliberately conservative for the unobserved
  states rather than assumed from the state names alone -- see Kalshi
  Settlement & Resolution Evidence Maturation v1.
- `event_ticker` is present directly on a market object returned by
  `GET /markets/{ticker}`, confirmed in the same live responses.

These fields are read only by the narrow, single-market/single-nested-market
resolution-evidence path (`adapters.counterfactual_followup_sources.
KalshiFollowUpPublicMarketSource`); the bulk discovery clients (`GET /markets`,
`GET /events` with `status=open`) remain exactly as scoped above and never see
these fields, since a discovery request is hardcoded to `status=open` and an
open market never carries them.
