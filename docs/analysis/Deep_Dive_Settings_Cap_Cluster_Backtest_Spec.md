<callout icon="🧠">
	Companion to the 10x simulation. Answers your five follow-ups: (1) is your read correct, (2) what you can change **right now in Telegram**, (3) can we layer changes **over** the bot without touching it, (4) does the **+300% exit** fight `peak_cluster`, (5) a **detailed per-strategy report** + the **extra backtest data** we need. Net: **5x is the honest, provable target**; the levers are mostly config you can flip tonight.
</callout>
## 1. Your read vs. the data
<table header-row="true">
<tr>
<td>Your call</td>
<td>Verdict</td>
<td>Evidence</td>
</tr>
<tr>
<td>Edge strategies are real, just need adjustment</td>
<td>✅ Correct</td>
<td>`late_observed_no` 74% WR +\$168; wins \$1,517 vs `lost` \$894 = +\$611 core</td>
</tr>
<tr>
<td>quick_flip is bad: much capital, many trades, tiny profit</td>
<td>✅ Correct</td>
<td>176 trades, \$1,301 capital, −\$7.5 net. Pure drag on capital</td>
</tr>
<tr>
<td>Cutting flip removes the −\$110 stop bleed</td>
<td>⚠️ Partly</td>
<td>Those \$110 are `flip_stop` doing its job; cutting flip removes the churn + frees capital, which is the real win</td>
</tr>
<tr>
<td>Exit should fire at −50%, not −85%</td>
<td>✅✅ Correct + already a knob</td>
<td>`THESIS_EXIT_MAX_ROI_PCT` default is **−85** → that's why losses average −88%. Setting = −50 is your single biggest lever</td>
</tr>
<tr>
<td>"Exit if it can exit; if no liquidity, exit quick"</td>
<td>⚠️ Half exposed</td>
<td>Loss threshold is a knob; the min-bid liquidity floor is a code default (not in Telegram)</td>
</tr>
<tr>
<td>peaker is bad, peak_cluster is salvageable</td>
<td>✅ but nuance → see §5</td>
<td>peaker −\$3.9; **`peaker_cool_basket`**** +\$57 comes from the SAME module** — don't blanket-disable</td>
</tr>
<tr>
<td>peak_cluster skips neighbours (buys 14,16,18 not 15,16,17)</td>
<td>✅✅ Confirmed bug</td>
<td>Basket fills by *probability*, not *adjacency* → breaks contiguity. See ¥4</td>
</tr>
<tr>
<td>late_observed_no \<0.50 = low win → reduce boost/bankroll, boost \>0.50</td>
<td>✅ Correct, needs overlay</td>
<td>\<0.50 band −\$96; 0.50–0.85 is the +\$271 engine. Band-based sizing isn't a current knob</td>
</tr>
<tr>
<td>Offline/adaptive ML boost by realized win/loss</td>
<td>⚠️ Partly exists</td>
<td>Sizer already blends win-rate into strength; per-strategy *auto* boost by performance = overlay</td>
</tr>
<tr>
<td>ML errors are free-tier limits; no data → skip → gate</td>
<td>✅ Correct</td>
<td>Logs full of `HTTP 503`  • `Open-Meteo cooling down`; entries then fire on stale forecasts</td>
</tr>
</table>
## 2. What you can change RIGHT NOW in Telegram
The bot ships a full runtime-settings panel: **`/settings`** (tabbed) + **`/set KEY VALUE`** + **`/toggle KEY`**. Overrides apply live (strategies re-read them each scan) and persist to `data/runtime_settings.json`, so they survive restarts. **No code or ****`.env`**** edit, no redeploy.**
### Do these tonight (exposed knobs)
<table header-row="true">
<tr>
<td>Goal</td>
<td>Setting</td>
<td>Now → Set</td>
<td>Tab</td>
</tr>
<tr>
<td>**Exit losers at −50%** (biggest lever)</td>
<td>`THESIS_EXIT_MAX_ROI_PCT`</td>
<td>−85 → **−50**</td>
<td>Exits</td>
</tr>
<tr>
<td>Keep the early exit on</td>
<td>`THESIS_EXIT_ENABLED`</td>
<td>on</td>
<td>Exits</td>
</tr>
<tr>
<td>**Kill quick_flip churn**</td>
<td>`QUICK_FLIP_ENABLED`</td>
<td>on → **off** (or trim below)</td>
<td>Strat</td>
</tr>
<tr>
<td>…or trim it instead</td>
<td>`QUICK_FLIP_MAX_SIZE_USD` / `QUICK_FLIP_MAX_CONCURRENT` / `QUICK_FLIP_MIN_EDGE`</td>
<td>shrink size, fewer, raise edge</td>
<td>Flip</td>
</tr>
<tr>
<td>**Keep good cool baskets, cut bad peaker**</td>
<td>`PEAKER_PREFER_COOL` on + `PEAKER_MIN_CONFIDENCE` / `PEAKER_MIN_EDGE` up + `PEAKER_WARM_SIZE_MULT` down</td>
<td>tighten</td>
<td>Peaker</td>
</tr>
<tr>
<td>Overweight the winning cool basket</td>
<td>`PEAKER_COOL_SIZE_MULT`</td>
<td>↑ (e.g. 1.3–1.6)</td>
<td>Peaker</td>
</tr>
<tr>
<td>More confident late-obs picks</td>
<td>`LATE_OBSERVED_MIN_LOCK` / `MIN_EDGE`</td>
<td>↑</td>
<td>LateObs</td>
</tr>
<tr>
<td>Concentrate size on winners</td>
<td>`KELLY_TIER_VGOOD_USD` / `KELLY_TIER_PERFECT_USD` up, `KELLY_TIER_BASE_USD` down</td>
<td>re-tier</td>
<td>Risk</td>
</tr>
<tr>
<td>Tighter overall selectivity</td>
<td>`GRADE_MIN_TO_TRADE` / `MIN_EDGE_TO_ENTER`</td>
<td>↑</td>
<td>Risk</td>
</tr>
<tr>
<td>Reduce premature basket booking</td>
<td>`PROFIT_CAP_ROI_PCT`</td>
<td>300 → **500** (blunt fix; real fix in ¥4)</td>
<td>Exits</td>
</tr>
<tr>
<td>Pick a reliable ML model</td>
<td>`ML_MODEL`</td>
<td>cycle</td>
<td>ML</td>
</tr>
</table>
<callout icon="⚠️">
	**Careful:** setting `PEAKER_ENABLED` = off ALSO kills `peaker_cool_basket` (+\$57), your 2nd-best strategy — they're one module. Tighten it instead of disabling.
</callout>
### What you CANNOT change from Telegram (needs the overlay — §3)
- **Entry-band sizing** for `late_observed_no` (down-boost \<0.50, up-boost 0.50–0.85). `LATE_OBSERVED_NO_MIN_PRICE` caps at 0.20, so you can't gate \<0.50 from settings.
- **Liquidity-aware "exit quick if no bid"** — the min-bid floor is a code default.
- **Peak-cluster contiguity fix** (the 14,16,18 bug).
- **Auto boost-by-performance** (adaptive per-strategy multiplier from realized win/loss).
- **Excluding baskets from the +300% cap.**
## 3. Can we add adjustments "over" the bot without changing it?
**Yes — two clean layers, both non-invasive:**
1. **Runtime-settings overlay (already built):** every knob in §2 is applied *on top of* the base config at call-time and is fully reversible. This is literally "over it" — nothing in the core logic changes.
2. **A thin overlay module for the rest:** the bot already runs its exits this way — `exit_policies.py` states it runs *"WITHOUT modifying PositionManager."* We mirror that pattern: a small add-on that (a) post-processes each signal's size by entry-band + adaptive win-rate, (b) adds a liquidity-aware −50% exit pass, (c) rewrites the cluster leg-selection to be contiguous, (d) exempts baskets from the profit cap. It's called from the main loop alongside the existing policies — the original strategy files stay untouched, so you can A/B or roll back instantly.
## 4. The +300% cap DOES fight peak_cluster (confirmed)
**Mechanism:** `check_profit_caps` books any position above `PROFIT_CAP_ROI_PCT` (300%) — and unlike the flip/thesis exits, **it does NOT skip hold-to-resolution legs**. But `peak_cluster` is an *any-one-wins, hold-to-\$1* basket. So when the single winning leg hits +300% (price \~\$0.48 from a \~\$0.12 entry) the cap can book it early — even though at settlement that leg pays **\$1.00 (≈+733%)**, and the basket *needs* that full payout to cover its other losing legs.
**Evidence — the +300% exit fires almost entirely on cheap basket/lottery legs:**
<table header-row="true">
<tr>
<td>Strategy</td>
<td>+300% exits</td>
<td>PnL</td>
<td>Entry range (avg)</td>
</tr>
<tr>
<td>late_observed_yes</td>
<td>19</td>
<td>+\$223</td>
<td>0.05–0.23 (0.11)</td>
</tr>
<tr>
<td>peak_cluster</td>
<td>8</td>
<td>+\$129</td>
<td>0.05–0.21 (0.12)</td>
</tr>
<tr>
<td>peaker_cool_basket</td>
<td>7</td>
<td>+\$83</td>
<td>0.09–0.24 (0.15)</td>
</tr>
<tr>
<td>quick_flip</td>
<td>1</td>
<td>+\$24</td>
<td>0.20</td>
</tr>
</table>
Those are exactly the cheap tails where riding to \$1 matters most. **Fix:** exempt `peak_cluster` / `*_basket` (hold-to-resolution) legs from the global cap, or raise the cap for baskets, so the winner rides to settlement. The cap is still great for single-leg directional wins.
## 5. Peak-cluster bugs (confirmed root cause)
**"Skips neighbours (14,16,18 instead of 15,16,17)":** the basket builds a window around the peak, then does `window_sorted = sorted(by -probability)` and greedily adds the **highest-probability** buckets under the cost budget. It picks by *probability*, not *adjacency* — so it can drop the true neighbour and grab a higher-prob non-adjacent bucket, breaking the contiguous cluster. That defeats the "cover the neighbourhood of the peak" thesis. **Fix:** fill outward contiguously from the centre (center, ±1, ±2…) and only skip a bucket for price/liquidity, never re-order by probability.
**"Sometimes buys a single leg":** there's already a fix (REQ-27) that floors share count so the cheapest leg clears the venue minimum, else it *skips the whole basket*. So single-leg baskets now mostly come from **live fills failing on thin legs**, not the sizing math — worth logging per-leg fill status to confirm.
## 6. Detailed per-strategy report
### The fat right tail (ROI \> 150%) lives in the cheap baskets/lottery
<table header-row="true">
<tr>
<td>Strategy</td>
<td>Big wins (\>150%)</td>
<td>PnL from them</td>
</tr>
<tr>
<td>late_observed_yes</td>
<td>34</td>
<td>+\$457</td>
</tr>
<tr>
<td>peak_cluster</td>
<td>14</td>
<td>+\$216</td>
</tr>
<tr>
<td>peaker_cool_basket</td>
<td>8</td>
<td>+\$96</td>
</tr>
<tr>
<td>quick_flip</td>
<td>2</td>
<td>+\$33</td>
</tr>
</table>
### late_observed_yes (the lottery) — only works CHEAP
<table header-row="true">
<tr>
<td>Entry band</td>
<td>Trades</td>
<td>WR</td>
<td>PnL</td>
</tr>
<tr>
<td>0.00–0.05</td>
<td>6</td>
<td>17%</td>
<td>+\$4</td>
</tr>
<tr>
<td>**0.05–0.10**</td>
<td>43</td>
<td>30%</td>
<td>**+\$109**</td>
</tr>
<tr>
<td>0.10–0.20</td>
<td>32</td>
<td>19%</td>
<td>−\$18</td>
</tr>
<tr>
<td>0.20–0.35</td>
<td>39</td>
<td>28%</td>
<td>+\$1</td>
</tr>
<tr>
<td>0.35+</td>
<td>43</td>
<td>33%</td>
<td>−\$46</td>
</tr>
</table>
**Read:** the YES lottery is profitable ONLY below \~\$0.10; above \$0.35 it bleeds. Gate `late_observed_yes` to cheap tails and keep size small/fixed.
**Where the lottery wins (roi\>50%, 45 wins, +\$519) — city concentration:**
<table header-row="true">
<tr>
<td>City</td>
<td>Wins</td>
<td>PnL</td>
</tr>
<tr>
<td>Shanghai</td>
<td>9</td>
<td>+\$171</td>
</tr>
<tr>
<td>Paris</td>
<td>9</td>
<td>+\$93</td>
</tr>
<tr>
<td>London</td>
<td>11</td>
<td>+\$91</td>
</tr>
<tr>
<td>Hong Kong</td>
<td>4</td>
<td>+\$53</td>
</tr>
<tr>
<td>Seoul</td>
<td>5</td>
<td>+\$50</td>
</tr>
<tr>
<td>Tokyo</td>
<td>4</td>
<td>+\$42</td>
</tr>
</table>
The upside clusters in **Asian (Shanghai/HK/Seoul/Tokyo) + European (Paris/London)** markets — i.e. their local afternoon/evening resolution windows. Winners concentrate at **\$0.05 (+\$143) and \$0.10 (+\$120)** entries. (A precise hour-of-day cut needs per-city timezone on the `ts` column — easy to add once we log it; see §7.)
### late_observed_no — the engine, by entry band (recap)
<table header-row="true">
<tr>
<td>Band</td>
<td>WR</td>
<td>PnL</td>
<td>Action</td>
</tr>
<tr>
<td>\<0.50</td>
<td>0–45%</td>
<td>−\$96</td>
<td>down-boost / gate</td>
</tr>
<tr>
<td>**0.50–0.85**</td>
<td>69–79%</td>
<td>**+\$271**</td>
<td>up-boost (the money)</td>
</tr>
<tr>
<td>0.85+</td>
<td>90%</td>
<td>−\$6</td>
<td>keep small (rare −100% erases wins)</td>
</tr>
</table>
### peaker family
<table header-row="true">
<tr>
<td>Variant</td>
<td>Trades</td>
<td>WR</td>
<td>PnL</td>
<td>Verdict</td>
</tr>
<tr>
<td>peaker_cool_basket</td>
<td>41</td>
<td>44%</td>
<td>+\$57</td>
<td>Keep + overweight</td>
</tr>
<tr>
<td>peaker_warm_basket</td>
<td>2</td>
<td>50%</td>
<td>+\$3</td>
<td>Neutral</td>
</tr>
<tr>
<td>peaker (solo)</td>
<td>4</td>
<td>50%</td>
<td>−\$4</td>
<td>Tighten/suppress</td>
</tr>
<tr>
<td>peak_cluster</td>
<td>78</td>
<td>23%</td>
<td>−\$27</td>
<td>Fix contiguity + cap exemption → tail is +\$216</td>
</tr>
</table>
## 7. Extra data we need for a TRUE backtest
You nailed the core limitation: the ledger logs only entry and exit, so a backtester **cannot see how far a position dipped or spiked** — it can't know if a −50% stop would have triggered, or whether a stopped trade would have recovered. We need the **intra-trade price path**. Add a lightweight per-scan snapshot (`positions_timeseries.jsonl`) and these ledger columns:
<table header-row="true">
<tr>
<td>New field</td>
<td>Meaning</td>
<td>Enables</td>
</tr>
<tr>
<td>`min_price` / `mae_pct`</td>
<td>worst price & ROI reached (Max Adverse Excursion)</td>
<td>Would a −50% stop have fired?</td>
</tr>
<tr>
<td>`max_price` / `mfe_pct`</td>
<td>best price & ROI reached (Max Favorable Excursion)</td>
<td>Did we book too early / leave upside?</td>
</tr>
<tr>
<td>`t_to_mae` / `t_to_mfe`</td>
<td>minutes from entry to those extremes</td>
<td>Timing of stops/targets</td>
</tr>
<tr>
<td>`crossed_-20/-30/-50`</td>
<td>timestamps it broke each level</td>
<td>Test any stop threshold exactly</td>
</tr>
<tr>
<td>`recovered_after_stop`</td>
<td>hit −50% then still settled a WIN</td>
<td>The false-negative cost of a stop</td>
</tr>
<tr>
<td>`final_settlement`</td>
<td>win/lose at resolution regardless of our exit</td>
<td>Score every exit rule counterfactually</td>
</tr>
<tr>
<td>`post_exit_path`</td>
<td>price track AFTER we sold</td>
<td>"Did we sell too early?"</td>
</tr>
<tr>
<td>`reversals`</td>
<td>count of ROI sign flips</td>
<td>Choppiness / whipsaw risk</td>
</tr>
<tr>
<td>`bid_size_at_level`</td>
<td>available liquidity when a stop would fire</td>
<td>"Could we ACTUALLY exit at −50%?"</td>
</tr>
</table>
Sampling: append `{ts, price, roi, bid, ask, bid_size}` for every open position each scan, and **keep tracking through resolution (even after our own exit)**. With that, I can replay every position and test −50% vs −40% vs hedge-switch *exactly* — turning the current bounded 7–9x estimate into a proven number. (`peak_price` is already tracked for trailing — we mainly need the trough + full path.)
## 8. Honest target
With the exposed knobs alone (−50% exit, kill/limit flip, tighten peaker, retier sizing, raise cap): **\~3–5x is realistic and provable now.** Pushing to \~7–10x needs the overlay (entry-band sizing, contiguity fix, basket cap-exemption, liquidity-aware exit) **and** the backtest data in §7 to prove it rather than estimate it. Your instinct is right: **5x is the credible headline, 10x is the stretch once the data + overlay are in.**
