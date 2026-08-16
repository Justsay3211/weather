<callout icon="⚡">
	Analysis of `paper_trades (9).csv` (692 closed trades, 23 Jun → 9 Jul 2026) + the `Sport_Poly` chat/ML export. **Question: what could have turned the +\$240 net result on a \$500 bankroll into \~10x?** Answer: the edge is real and concentrated in `late_observed_no`; the money is lost to uncapped −100%/−88% losses and mis-sizing. Fixing exits + sizing models to a realistic **7–9x**.
</callout>
## 1. Baseline (what actually happened)
<table header-row="true">
<tr>
<td>Metric</td>
<td>Value</td>
</tr>
<tr>
<td>Bankroll</td>
<td>\$500</td>
</tr>
<tr>
<td>Net realized PnL</td>
<td>**+\$239.68** (+48% in 2 weeks)</td>
</tr>
<tr>
<td>Closed trades</td>
<td>692</td>
</tr>
<tr>
<td>Total capital deployed (recycled)</td>
<td>\~\$5,600</td>
</tr>
<tr>
<td>Win rate</td>
<td>\~43%</td>
</tr>
</table>
> Note: the \~\$228 you quoted is the mark-to-market figure (open positions included). Net *realized* PnL from the ledger is \$239.68 — I use that as the baseline.
### Where the money comes from and goes
<table header-row="true">
<tr>
<td>Exit type</td>
<td>Count</td>
<td>PnL</td>
<td>Nature</td>
</tr>
<tr>
<td>`won` (held to resolution)</td>
<td>244</td>
<td>**+\$1,517**</td>
<td>The engine</td>
</tr>
<tr>
<td>`profit_cap_book`</td>
<td>35</td>
<td>**+\$459**</td>
<td>Best mechanism (\~+300% avg) — do not touch</td>
</tr>
<tr>
<td>`flip_stop`</td>
<td>129</td>
<td>−\$110</td>
<td>Working as designed</td>
</tr>
<tr>
<td>`thesis_invalidated`</td>
<td>104</td>
<td>**−\$747**</td>
<td>A “stop” that fires at −88% AFTER the price gaps</td>
</tr>
<tr>
<td>`lost` (settled at \$0)</td>
<td>162</td>
<td>**−\$894**</td>
<td>Never exited — total loss</td>
</tr>
</table>
**The entire problem in one line:** gross wins ≈ \$1,976, gross losses ≈ \$1,751. The edge is real but almost fully eaten by two loss buckets (`lost` + `thesis_invalidated` = −\$1,641).
## 2. The edge is concentrated (you're right about late_observed)
<table header-row="true">
<tr>
<td>Strategy</td>
<td>Trades</td>
<td>WR</td>
<td>PnL</td>
<td>Verdict</td>
</tr>
<tr>
<td>**late_observed_no**</td>
<td>228</td>
<td>**74%**</td>
<td>**+\$168.5**</td>
<td>Core edge — but under-sized</td>
</tr>
<tr>
<td>peaker_cool_basket</td>
<td>41</td>
<td>44%</td>
<td>+\$56.8</td>
<td>Best $`/return (roi/`$ +0.19)</td>
</tr>
<tr>
<td>late_observed_yes</td>
<td>163</td>
<td>28%</td>
<td>+\$49.5</td>
<td>Lottery: cheap tails, +100–450% winners</td>
</tr>
<tr>
<td>quick_flip</td>
<td>176</td>
<td>22%</td>
<td>−\$7.5</td>
<td>Churn, ties up capital → cut</td>
</tr>
<tr>
<td>peak_cluster</td>
<td>78</td>
<td>23%</td>
<td>−\$26.6</td>
<td>Biggest drag → cut</td>
</tr>
<tr>
<td>peaker</td>
<td>4</td>
<td>50%</td>
<td>−\$3.9</td>
<td>Oversized, too few → cut</td>
</tr>
</table>
**Key sizing insight — ****`late_observed_no`**** by entry price:**
<table header-row="true">
<tr>
<td>Entry band</td>
<td>Trades</td>
<td>WR</td>
<td>PnL</td>
<td>Deployed</td>
</tr>
<tr>
<td>0.00–0.30</td>
<td>8</td>
<td>0%</td>
<td>**−\$82**</td>
<td>\$87</td>
</tr>
<tr>
<td>0.30–0.50</td>
<td>11</td>
<td>45%</td>
<td>−\$14</td>
<td>\$136</td>
</tr>
<tr>
<td>**0.50–0.70**</td>
<td>85</td>
<td>69%</td>
<td>**+\$208**</td>
<td>\$1,188</td>
</tr>
<tr>
<td>**0.70–0.85**</td>
<td>72</td>
<td>79%</td>
<td>**+\$63**</td>
<td>\$941</td>
</tr>
<tr>
<td>0.85–1.01</td>
<td>52</td>
<td>90%</td>
<td>−\$6</td>
<td>\$493</td>
</tr>
</table>
The sweet spot is **0.50–0.85** (69–79% WR). The \<0.50 band is broken (−\$96), and the 0.85+ band has 90% WR but the rare −100% losses erase all the small wins. **The sizer is doing the opposite of what it should**: it deploys similar capital everywhere instead of concentrating on the 0.50–0.85 conviction zone.
### City dispersion
Winners: **Paris +\$131, Seoul +\$98, Shanghai +\$47, Moscow +\$37, Wellington +\$25**. Leakers: **Madrid −\$33, Ankara −\$21, Houston −\$20, Chicago −\$18, Tokyo −\$17, Lucknow −\$18**.
## 3. Operational finding (from the chat/ML export)
The `Sport_Poly` log is dominated by two recurring failures that directly sabotage the core strategy:
- **ML API down constantly** — hundreds of `ML API FAIL: HTTP 503` / `TIMEOUT — using local model`. The ML veto/sizing layer was effectively offline much of the time.
- **Observed weather unavailable** — repeated `All Open-Meteo endpoints are cooling down` / `observed fetch returned no data`. The late-observed lock depends on this feed; when it's down, entries fire on stale forecasts.
**Implication:** a chunk of the −\$1,641 loss mass is infrastructure, not strategy. A reliable observed feed (METAR/ASOS, exact airport, free) + resilient ML is itself a multiplier.
## 4. Simulated possibilities (every lever)
<callout icon="🔬">
	Method: for exact levers (gating, city throttle, band filter, reallocation) PnL is recomputed as `cost × ROI` per trade — ROI is fixed by the outcome, so rescaling capital is exact. For exit levers (stops / hedge-switch) the ledger has no intra-trade price path, so those are parametrized with conservative/realistic/optimistic loss floors. Freed capital is redeployed at the winners' historical roi/\$ (+0.135), and single-position size is capped at a % of bankroll to stay realistic.
</callout>
### Standalone levers (each applied alone)
<table header-row="true">
<tr>
<td>#</td>
<td>Lever</td>
<td>Result</td>
<td>Multiple</td>
</tr>
<tr>
<td>L1</td>
<td>Cut losing strategies (quick_flip, peak_cluster, peaker)</td>
<td>\$278</td>
<td>1.2x</td>
</tr>
<tr>
<td>L1b</td>
<td>…and redeploy that freed capital into winners</td>
<td>\$516</td>
<td>2.2x</td>
</tr>
<tr>
<td>L2</td>
<td>City throttle (drop negative-PnL cities)</td>
<td>\$408</td>
<td>1.7x</td>
</tr>
<tr>
<td>L3</td>
<td>Cut `late_observed_no` entries below 0.50</td>
<td>\$336</td>
<td>1.4x</td>
</tr>
<tr>
<td>L4</td>
<td>**Observation-invalidation exit** — cut invalidated legs at bid (floor −50%)</td>
<td>**\$932**</td>
<td>**3.9x**</td>
</tr>
<tr>
<td>L4a</td>
<td>…optimistic floor −40%</td>
<td>\$1,091</td>
<td>4.6x</td>
</tr>
<tr>
<td>L4b</td>
<td>…conservative floor −60%</td>
<td>\$773</td>
<td>3.2x</td>
</tr>
<tr>
<td>L5</td>
<td>**Hedge-switch** on invalidation (sell dead leg, buy new-truth bucket) — net −20%</td>
<td>**\$1,397**</td>
<td>**5.8x**</td>
</tr>
<tr>
<td>L5a</td>
<td>…optimistic net +30% recovery</td>
<td>\$2,180</td>
<td>9.1x</td>
</tr>
<tr>
<td>L5b</td>
<td>…conservative net −40%</td>
<td>\$1,084</td>
<td>4.5x</td>
</tr>
</table>
**Biggest single lever by far: fixing the invalidation exit.** The −88%/−100% loss mass is the whole game. A price stop can't help (weather binaries teleport), but an *observation-triggered* exit (P(win)\<5–10% → sell now) or a hedge-switch fires at the gap instead of after it.
### Stacked scenarios (levers combined)
<table header-row="true">
<tr>
<td>Scenario</td>
<td>Levers</td>
<td>PnL</td>
<td>Multiple</td>
</tr>
<tr>
<td>**Conservative**</td>
<td>gate losers + cut \<0.50 band + city throttle + invalidation floor −60% + strength-sizing (cap 6%) + redeploy</td>
<td>**\$1,037**</td>
<td>**4.3x**</td>
</tr>
<tr>
<td>**Realistic**</td>
<td>above + floor −45% + hedge-switch net −10% + strength-sizing (cap 8%) + redeploy at winner roi/\$</td>
<td>**\$1,766**</td>
<td>**7.4x**</td>
</tr>
<tr>
<td>**Aggressive**</td>
<td>above + hedge net +20% + strength-sizing (cap 12%)</td>
<td>**\$2,178**</td>
<td>**9.1x**</td>
</tr>
</table>
## 5. The 10x recipe (ranked by impact)
1. **Observation-invalidation exit + hedge-switch** (biggest lever, \~4–9x on its own). Replace the −88% `thesis_invalidated` coroner with a P(win)-triggered exit at the gap, and where a neighbour bucket becomes near-certain, roll into it instead of eating the loss.
2. **Concentrate size on the ****`late_observed_no`**** 0.50–0.85 conviction band** (69–79% WR). Signal-strength sizing (edge × grade), % -of-bankroll tiers, hard per-position cap.
3. **Cut ****`quick_flip`**** + ****`peak_cluster`**** + ****`peaker`****; reallocate that \~\$1,770 of recycled capital** into the winners (+\$275 alone).
4. **City throttles** — zero/halve Madrid, Ankara, Houston, Chicago, Tokyo, Lucknow; overweight Paris/Seoul/Shanghai.
5. **Filter out ****`late_observed_no`**** \< 0.50 entries** (broken band, −\$96).
6. **Fix the infrastructure** — reliable observed feed (METAR) + resilient ML; a share of the loss mass was stale-data/offline-ML entries.
7. **Keep untouched:** `profit_cap_book`, the +10% ML ladder, `flip_stop` level, and the `late_observed_yes` lottery (small fixed size, no stop).
<callout icon="⚠️">
	Honest caveats: (1) the exit/hedge levers depend on assumed fill quality because the ledger has no intra-trade price path — that's why they're shown as ranges. (2) Rescaling assumes the same fills at larger size (thin weather books may slip). (3) 2 weeks / 692 trades is a modest sample. The exact levers (gating, city, band, reallocation) are solid; the \~7–9x headline leans on the exit assumptions. Logging mark-to-market snapshots per open position would let us backtest the exit levers exactly instead of bounding them.
</callout>
<page url="https://app.notion.com/p/88dfa5e3b63b454f8fcf1472dec776a1">Deep Dive — Live Settings, +300% Cap, Cluster Bug & Backtest Spec</page>
<page url="https://app.notion.com/p/d4e57c658d294f74a297858c1c4a9830">Telegram Command Sheet + the 3x Quick Fix</page>
<page url="https://app.notion.com/p/37ee259fbc3b4df7900c348c1f55769f">Peak-Cluster & Baskets — Adjacent vs Probability Positioning (Proven)</page>
