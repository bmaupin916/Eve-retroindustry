# Data Sources and Oz Curriculum

Use this file to choose evidence and understand source limitations. Recheck tool behavior when a high-value decision depends on it.

## Contents

1. Official sources
2. Oz resources
3. Tools from Oz's tools page
4. Source-selection rules
5. Data lineage checklist

## 1. Official sources

### EVE Swagger Interface (ESI)

- Overview: https://developers.eveonline.com/docs/services/esi/overview/
- API base: `https://esi.evetech.net/latest/`
- Public regional orders: `GET /markets/{region_id}/orders/`
- Public regional history: `GET /markets/{region_id}/history/`
- Type metadata: `GET /universe/types/{type_id}/`
- Character orders, wallet transactions, journal, and assets require EVE SSO and narrow scopes.

Always paginate from the `X-Pages` header. Respect `Expires`, `ETag`, `X-ESI-Error-Limit-Remain`, `X-ESI-Error-Limit-Reset`, `Retry-After`, and HTTP 420/429. Record whether structure orders or remote-range buy orders are missing from the analysis.

Known market identifiers used by the bundled script:

| Market | Region ID | NPC station ID |
|---|---:|---:|
| Jita | 10000002 | 60003760 |
| Amarr | 10000043 | 60008494 |
| Dodixie | 10000032 | 60011866 |
| Rens | 10000030 | 60004588 |
| Hek | 10000042 | 60005686 |
| Global PLEX | 19000001 | N/A |

PLEX type ID is `44992`. CCP launched a unified global PLEX market on 2025-07-07. Purchases deliver to the PLEX Vault and do not require hauling. Source: https://www.eveonline.com/news/view/global-plex-market-now-live

### Fees and matching

- Broker fee and sales tax: https://support.eveonline.com/hc/en-us/articles/203218962-Broker-Fee-and-Sales-Tax
- Buy/sell matching and relist formula: https://support.eveonline.com/hc/en-us/articles/203218932-Buy-and-Sell-Orders

As reviewed on 2026-08-17, the official support article states a 3% base NPC-station broker fee, reducible by Broker Relations and standings to a 1% minimum, and a 7.5% base sales tax reducible to about 3.37% through Accounting. Upwell fees follow different rules. Recheck before using these numbers.

### Economy and catalysts

- EVE news, patch notes, expansions, events, and offers: https://www.eveonline.com/news
- Monthly Economic Reports: search the official news archive for the latest report and download its raw-data bundle.
- June 2026 MER example: https://www.eveonline.com/news/view/monthly-economic-report-june-2026

Use MER for macro supply, production, destruction, mining, ISK faucets/sinks, and regional context. It is not a live item-picking feed. Combine it with item mechanics, bill of materials, destruction/use, and current order books.

### Automation policy

- Botting policy: https://support.eveonline.com/hc/en-us/articles/7370802950172-Botting
- June 2026 ban report: https://www.eveonline.com/news/view/monthly-ban-report-june-2026

Keep analysis outside the client. Do not automate inputs or account actions.

## 2. Oz resources

### Tools page

https://www.theoz.space/tools

This is the routing page requested by the user. It links the community sheet, Mokaam, MER Power BI, Adam4EVE, EVE Cookbook, EVE Webtools PI, Quantum Anomaly, and EVE Guru.

### Community trading spreadsheet

https://docs.google.com/spreadsheets/d/1NV1jRo9glgkI6FawJA1WvzJOwizxjIskfMgAhRdN5YM/edit?usp=sharing

Version observed: v4.2.0, 2026-06-20. The sheet imports jEveAssets transactions and tracks investments, profit, and opportunities per item rather than serving as a raw transaction ledger. Its model includes:

- Cash, escrow/buy orders, inventory value, future sales tax, and total fund value
- Inception and current value, change, monthly return, largest position, winners/losers
- Item-level purchases, sales, average costs, inventory margin, projected profit, recent activity
- Hub comparisons including Jita, Amarr, and Dodixie price/percentile fields
- Action and buy-opportunity flags based on user-configured thresholds

Observed starter defaults in v4.2.0: 8% sales tax, 3% buy/sell broker fees, buy until an item reaches 10M ISK of inventory while projected profit exceeds 30%, and sell only when projected profit is at least 1M ISK and inventory margin exceeds 20%. These are workbook defaults, not universal recommendations. Replace them with the actual trader profile.

### YouTube channel

Channel: https://www.youtube.com/ozeve

Durable curriculum to consult when the task calls for it:

| Topic | Video |
|---|---|
| Station trading foundation | https://www.youtube.com/watch?v=0Y2v_DFEQ5g |
| Detailed item identification | https://www.youtube.com/watch?v=rMIbv6EDMDQ |
| Adam4EVE guide | https://www.youtube.com/watch?v=muvp9XhaU5c |
| Quick item-selection guide | https://www.youtube.com/watch?v=bEMyvBBmKEE |
| Update cadence | https://www.youtube.com/watch?v=28cclpzJPfs |
| Hub selection | https://www.youtube.com/watch?v=xARXrkpZ9A4 |
| Manipulation basics and risk | https://www.youtube.com/watch?v=soTyBeEgJ4I |
| Five trading lifehacks | https://www.youtube.com/watch?v=GsigXZoqo4U |
| Spreadsheet guide | https://www.youtube.com/watch?v=ZgJQ5klmuUM |
| 2026 Zero-to-Omega episode 1 | https://www.youtube.com/watch?v=ix_lxPkwbE8 |
| 2026 Zero-to-Omega episode 2 | https://www.youtube.com/watch?v=SHB2dCPPd7U |
| 2026 Zero-to-Omega episode 3 | https://www.youtube.com/watch?v=y6omCbKLkVw |
| EVE Guru regional module | https://www.youtube.com/watch?v=V-is2aag0_E |

Use weekly Oz Reports and MER analyses for hypotheses and catalyst awareness. Verify any mentioned item against current data before recommending it.

## 3. Tools from Oz's tools page

### Mokaam historic market data

https://mokaam.dk/

Designed to batch ESI-derived historical statistics for sheets. Important endpoints:

- `/API/market/items?regionid=10000002&typeid=34,35`
- `/API/market/all?regionid=10000002`
- `/API/market/type_ids`
- `/API/market/query?type=items&regionid=10000002&query=...`

Batch comma-separated type IDs; do not issue one request per item. Mokaam warns it may block abusive clients. Its former orders endpoint is disabled because the underlying estimates were unreliable. Historical outputs include median volume, price, order count, market size, highs/lows, spread, VWAP, 52-week range, and standard deviation. Some missing data is carried forward or interpolated, so use it for screening and then verify execution with live ESI/in-game orders.

### Adam4EVE

https://www.adam4eve.eu/

Oz highlights Margin Finder and Material Influence. Also use order depth, market trends, hub history, regional volume, orderbook age, price comparison, manufacturing profitability, and MER views. Margin Finder exposes spread, prices, average trades, ISK traded, flows, and data age. A large displayed spread can still be false if depth, age, fills, or fees are poor.

### MER in Power BI

Linked from https://www.theoz.space/tools. Use it to explore CCP MER data visually. Cite the underlying CCP MER when making factual claims.

### EVE Cookbook

https://evecookbook.com/

Industry build calculator with blueprint, quantity, BPC runs, ME/TE, system, facility tax, reactions, build steps, and structure/rig inputs. Compare build economics against executable material costs and realistic sell prices, not optimistic regional averages.

### EVE Webtools PI

https://www.eve-webtools.com/Planetary/

Use for PI chain planning. Validate POCO taxes, hauling, extraction assumptions, factory throughput, and actual market absorption.

### Quantum Anomaly

https://www.qsna.eu/

Use pack/NES analysis and the Oz Report dashboard where available. Treat store promotions as event catalysts: model PLEX, extractors, injectors, accelerators, and pack contents as a connected system.

### EVE Guru

https://eveguru.online/

Manufacturing and regional trading planner. Use its outputs as a candidate generator. Verify its fee profile, skills, structures, input prices, demand filters, logistics, and data timestamps before acting.

## 4. Source-selection rules

- Use ESI live orders for executable market prices.
- Use ESI history or Mokaam for actual historical trading and volatility.
- Use Adam4EVE for discovery, comparisons, depth, and convenient visualization.
- Use Oz Reports for hypotheses, education, and current narrative context.
- Use CCP news/patch notes for mechanics and event dates.
- Use MER for macro regime and supply/destruction context.
- Use build/PI tools for transformation economics.
- Use the player's wallet/order data for realized performance.

Never average conflicting sources silently. Explain scope and pick the source closest to the decision being made.

## 5. Data lineage checklist

For every named opportunity record:

- Observed UTC and freshness/expiry
- Tranquility or another datasource
- Type ID and exact item name
- Region ID and location ID
- Public ESI, in-game, authenticated ESI, or third-party source
- Included/excluded structures, contracts, remote buy ranges, and global markets
- Fee, skill, standings, structure, relist, logistics, and slippage assumptions
- History window and treatment of zero-volume days/outliers
- Any inferred catalyst and the source supporting it
