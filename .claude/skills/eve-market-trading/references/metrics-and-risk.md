# Metrics, Scoring, and Risk

Use these formulas consistently. Keep raw metrics visible beside any composite score.

## Contents

1. Price and fee math
2. Liquidity and capacity
3. Depth and competition
4. Volatility and catalysts
5. Position sizing
6. Candidate scoring
7. Common traps

## 1. Price and fee math

Let:

- `B` = executable buy price per unit, normally the bid used for a new buy order
- `S` = executable sell price per unit, normally the ask used for a new sell order
- `f_b` = buy-order broker rate
- `f_s` = sell-order broker rate
- `t` = sales-tax rate
- `r` = conservative relist reserve as a fraction of sell price
- `h` = hauling, courier, collateral-risk allowance, and other cost per unit

Gross spread:

`S - B`

Displayed spread percentage, matching the common ask-denominator convention:

`(S - B) / S`

Capital consumed per unit:

`C = B * (1 + f_b)`

Net proceeds per unit:

`P = S * (1 - f_s - t - r) - h`

Net profit per unit:

`N = P - C`

Net return on deployed capital:

`ROI = N / C`

Breakeven sell price:

`S_break_even = (B * (1 + f_b) + h) / (1 - f_s - t - r)`

For immediate buy/sell actions, remove only fees that genuinely do not apply. For relists, the official fee is path-dependent; use the CCP formula when exact old/new prices and Advanced Broker Relations are known. A flat reserve is a screening approximation, not accounting truth.

Run at least three cases when the position is meaningful:

- Optimistic: current top prices, expected fees, no extra slippage
- Base: expected fill price, actual fees, reasonable relist reserve
- Stress: one or more book levels of slippage, slower exit, adverse price move, extra relist/logistics cost

## 2. Liquidity and capacity

Use multiple windows, normally 7, 30, and 90 days.

- Calendar-day average volume: `sum(volume) / calendar_days`, including zero-volume days
- ISK turnover: `average price * average daily volume`
- Plausible capture: apply a conservative fraction, often 5-20%, based on competition, update cadence, hub, and evidence
- Expected daily units: `average daily volume * capture fraction`
- Days to acquire: `units / expected buy-side fills per day`
- Days to exit: `units / expected sell-side fills per day`
- Capital velocity: `realized net profit / average deployed capital / holding days`

ESI history shows completed daily market activity, not which side a trader can capture. Do not treat total volume as guaranteed buy-order fills and again as guaranteed sell-order fills.

An item can have excellent percentage margin but poor ISK/day. Rank both ROI and absolute capacity.

## 3. Depth and competition

Inspect:

- Units and ISK within 1%, 2%, and 5% of best bid/ask
- Number of distinct orders and concentration of the top three
- Age and remaining volume of orders
- Gap from top order to the next meaningful level
- Number/frequency of price changes if available
- Whether proposed size would consume a visible share of depth or reveal the strategy

Depth is not the same as fill flow. Use it to test quote robustness, slippage, and market impact.

Competition cost includes time. A lower-margin item requiring one update per day may outperform a high-margin item requiring constant relists.

## 4. Volatility and catalysts

Useful measures:

- 30d/90d VWAP and median price
- Daily-price standard deviation and coefficient of variation
- Current price z-score versus a stable historical window
- Distance from 52-week high/low
- Volume anomaly versus 30d baseline
- Bid/ask versus recent traded range

Do not blindly mean-revert event-driven items. First classify the catalyst:

- Temporary sale or pack injection
- Patch-driven demand or bill-of-material change
- Doctrine/war/destruction demand
- Event loot supply
- Mining, scarcity, or resource-distribution change
- NPC-seeded supply or store price anchor
- PLEX/extractor/injector/accelerator relationship

Write a causal chain: event -> affected activity -> input/output item -> inventory lag -> expected price/volume behavior -> invalidation.

## 5. Position sizing

Size by the smallest applicable cap:

1. Maximum portfolio percentage
2. Maximum loss if the stress exit occurs
3. Units plausibly acquired and sold inside the target holding period
4. A conservative share of near-touch depth
5. Logistics capacity and collateral exposure
6. Order-slot and attention budget

Suggested starting guardrails, to be replaced by user constraints:

- Keep 20-40% liquid reserve for opportunities, fees, and adverse fills
- Limit an unproven item to 2-5% of total capital
- Limit a validated liquid item to 5-10%
- Limit one correlated catalyst/theme to 20-30%
- Pilot with one-third of target size when data confidence is medium or lower
- Do not size so the base exit requires capturing more than 10-20% of recent daily volume without strong evidence

These are guardrails, not promises or universal optimal values.

## 6. Candidate scoring

Use scores only to organize review. A recommended 0-100 structure:

- 25 points: net margin and breakeven cushion
- 20 points: absolute net ISK/day relative to capital
- 15 points: consistent volume and time to exit
- 15 points: depth resilience and low slippage
- 10 points: competition compatible with user's update cadence
- 10 points: price stability or a well-supported catalyst
- 5 points: data freshness and source confidence

Apply hard gates before scoring:

- Positive base-case profit after all costs
- Positive stress-case or explicitly bounded downside
- Minimum liquidity and order count
- Position fits capital and exit-time constraints
- Data fresh enough for the strategy
- No known mechanics mismatch such as treating global PLEX as regional

Never recommend a candidate only because it ranks first. Explain the edge and failure modes.

## 7. Common traps

- **Top-order illusion:** one tiny order creates the entire spread.
- **Historical-price illusion:** average price is not currently executable.
- **Volume double count:** assuming the trader can capture total market volume on both entry and exit.
- **Fee blindness:** gross margin disappears after two broker fees, sales tax, and relists.
- **Attention blindness:** frequent updates make ISK/hour poor.
- **Capacity blindness:** percentage return is high but deployable capital is tiny.
- **Inventory accounting error:** wallet growth ignores escrow, stock at cost, future sales tax, or unsold losses.
- **Selection leakage:** a public screener sends many traders into the same visible spread.
- **Catalyst leakage:** the news is already priced in.
- **Correlation blindness:** several item names are all the same mineral, doctrine, event, or store-sale bet.
- **Tool-scope mismatch:** structures, contracts, remote buy ranges, or regions are excluded.
- **Manipulation/outlier contamination:** extreme trade prints or fake-looking orders distort statistics.
- **Sunk-cost holding:** refusing to exit because the original target has not returned.
