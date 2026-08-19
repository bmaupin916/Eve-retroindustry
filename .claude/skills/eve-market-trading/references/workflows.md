# Trading Workflows

Choose the narrowest workflow that answers the request. Combine workflows only when dependencies require it.

## Contents

1. Station trading
2. Inter-hub and regional arbitrage
3. Swing and catalyst trading
4. Industry, reactions, PI, and reprocessing
5. Portfolio and journal review
6. Daily and weekly operating rhythm

## 1. Station trading

1. Select the exact hub/station and fee profile.
2. Screen for positive net spread, sufficient daily ISK turnover, robust near-touch depth, and manageable competition.
3. Inspect the in-game history graph or ESI history. Confirm trades occur near both sides rather than only at one extreme.
4. Estimate buy-side and sell-side fill rates conservatively.
5. Place a small pilot position. Do not immediately fund every candidate from the same screener.
6. Update on a schedule compatible with the user's time. Include relist costs and avoid reflexive micro-updates.
7. Exit when margin, fill rate, time limit, catalyst, or capital-opportunity rule invalidates the position.

Prefer recurring-use items with stable turnover and enough spread after fees. Wider spreads in smaller hubs may compensate for slower exits; Jita offers depth but stronger competition.

## 2. Inter-hub and regional arbitrage

1. Compare executable source asks with destination bids/asks and depth.
2. Include purchase fees, destination selling fees, tax, hauling/courier reward, collateral opportunity cost, packaging constraints, and expected losses.
3. Confirm route length, security status, cargo volume, ship value, gank exposure, and destination access.
4. Size to destination absorption, not source availability.
5. Use courier contracts when the all-in cost and collateral risk beat self-hauling. Never omit time-to-accept and delivery risk.
6. Stage destination listings to avoid collapsing the book.

Compute profit per m3 and profit per trip in addition to ROI and ISK/day.

## 3. Swing and catalyst trading

1. Identify the primary source: CCP patch note, expansion, event, offer, doctrine shift, destruction event, MER trend, or store promotion.
2. Map first-order and second-order effects across inputs, outputs, substitutes, stockpiles, and production lag.
3. Determine whether the catalyst changes equilibrium or only timing.
4. Compare current price/volume with pre-event baselines and prior analogous events.
5. Define entry range, maximum chase price, inventory cap, expected lag, time stop, and event invalidation.
6. Scale out into strength. Do not wait for a perfect top.

For PLEX, extractors, injectors, MCT, accelerators, and packs, model the entire conversion/value chain and distinguish cash-store supply from NES demand.

## 4. Industry, reactions, PI, and reprocessing

1. Use executable input prices and realistic output prices.
2. Include blueprint ME/TE, invention probabilities and datacores, job cost index, system/facility taxes, structure/rig bonuses, fuel or opportunity cost, hauling, broker fees, sales tax, and failed-attempt costs.
3. Compare build versus buy and sell-to-buy-order versus list-on-sell-order cases.
4. Cap runs by demand and time to exit. A positive per-unit margin does not justify a month of market supply.
5. Test input-price and output-price sensitivity.
6. Consider whether selling intermediate materials has better capital velocity than building the final item.

Use EVE Cookbook or EVE Guru to generate the cost tree, then independently verify the largest inputs and the output market.

For PI, include POCO taxes, extraction variability, factory throughput, hauling time, and local-risk premium.

## 5. Portfolio and journal review

Use a consistent equity equation:

`fund value = cash + escrow/buy orders + conservative inventory value - future sales tax - known liabilities`

Maintain inventory at average purchase cost and also mark it to a conservative executable exit. Show both cost-basis and liquidation views.

For each item track:

- Total purchases and quantity; average purchase price
- Total sales and quantity; average sale price
- All fees and logistics costs
- Current inventory quantity, cost, mark, and projected net profit
- Realized net profit and ROI
- First/last trade dates, median hold time, and stale days
- Thesis, catalyst, invalidation, and postmortem category

Review forecast versus realized fills. Promote thresholds that work across new periods, not only the sample used to invent them.

## 6. Daily and weekly operating rhythm

### Daily, 10-20 minutes

1. Check stale or invalidated positions before discovering new ones.
2. Review fills, wallet, and material market news.
3. Update only orders whose expected benefit exceeds the relist and attention cost.
4. Recycle capital from low-velocity positions according to predefined time stops.
5. Record actions and assumptions.

### Weekly

1. Reconcile fund value, realized profit, inventory, escrow, and future taxes.
2. Rank holdings by capital velocity, profit, time spent, and forecast error.
3. Review correlated exposure and reserve cash.
4. Read the latest Oz Report, CCP news/patch notes, and relevant market data.
5. Retire weak screen criteria; add hypotheses to a watchlist before trading them.

### Monthly

1. Review the latest CCP MER and raw data for regime changes.
2. Compare strategy-level returns and attention cost.
3. Recalculate actual fee rates and update skill/standings assumptions.
4. Run stress cases on the largest positions.
5. Preserve an auditable snapshot so later evaluation is not distorted by hindsight.
