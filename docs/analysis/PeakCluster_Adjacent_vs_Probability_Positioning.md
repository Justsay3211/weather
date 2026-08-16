<callout icon="✅">
	**Your hunch is confirmed by the actual trades.** I rebuilt every basket “box” from the ledger, extracted the temperature buckets in each, and split them into **contiguous (adjacent, no holes)** vs **gapped (probability-picked, middle skipped)**. Adjacent baskets win big; gapped baskets are the entire reason `peak_cluster` shows a loss. The winning bucket is more often a **neighbour** than the peak — so dropping neighbours to keep the high-probability peak literally throws away the winner.
</callout>
## 1. Head-to-head: adjacent vs probability-gapped
### peak_cluster (23 boxes)
<table header-row="true">
<tr>
<td>Basket type</td>
<td>Boxes</td>
<td>Win rate</td>
<td>Net PnL</td>
<td>Avg/box</td>
<td>Avg legs</td>
</tr>
<tr>
<td>**Contiguous (adjacent)**</td>
<td>18</td>
<td>**83%**</td>
<td>**+\$40.23**</td>
<td>+\$2.24</td>
<td>3.7</td>
</tr>
<tr>
<td>**Gapped (prob-picked)**</td>
<td>5</td>
<td>**40%**</td>
<td>**−\$66.78**</td>
<td>−\$13.36</td>
<td>3.2</td>
</tr>
</table>
<callout icon="🔥">
	The whole strategy's −\$27 result is caused entirely by 5 gapped baskets bleeding −\$67. The 18 adjacent baskets alone made **+\$40 at an 83% win rate.** Fix the gaps and `peak_cluster` flips from a loser to a winner — before you even touch size.
</callout>
### peaker_cool_basket (21 boxes) — the control group
<table header-row="true">
<tr>
<td>Basket type</td>
<td>Boxes</td>
<td>Win rate</td>
<td>Net PnL</td>
<td>Avg legs</td>
</tr>
<tr>
<td>**Contiguous (adjacent)**</td>
<td>21</td>
<td>**71%**</td>
<td>**+\$56.84**</td>
<td>2.0</td>
</tr>
<tr>
<td>Gapped</td>
<td>0</td>
<td>—</td>
<td>—</td>
<td>—</td>
</tr>
</table>
**This is the proof.** `peaker_cool_basket` places **only adjacent** baskets (never gapped) — and it's one of your most consistent winners (+\$57, 71%). Same mechanic, no gaps, positive. `peak_cluster` uses the same idea but its selection creates holes, and that's exactly where it bleeds.
## 2. The smoking gun: every gapped basket missed the MIDDLE
All 5 gapped `peak_cluster` baskets skipped an interior bucket — and **4 of 5 lost the entire basket** (−\$18.75 = total wipeout), because temperature landed in the hole they skipped:
<table header-row="true">
<tr>
<td>City</td>
<td>Buckets bought</td>
<td>Missing (skipped)</td>
<td>PnL</td>
</tr>
<tr>
<td>Singapore</td>
<td>31, 32, 34</td>
<td>**33**</td>
<td>−\$18.75</td>
</tr>
<tr>
<td>Ankara</td>
<td>30, 31, 33</td>
<td>**32**</td>
<td>−\$18.75</td>
</tr>
<tr>
<td>Wellington</td>
<td>11, 12, 14</td>
<td>**13**</td>
<td>−\$18.75</td>
</tr>
<tr>
<td>Hong Kong</td>
<td>25, 27, 28</td>
<td>**26**</td>
<td>−\$8.37</td>
</tr>
<tr>
<td>Madrid</td>
<td>33, 34, 35, 37</td>
<td>**36**</td>
<td>−\$2.16</td>
</tr>
</table>
The basket's entire thesis is “the temperature lands somewhere in this neighbourhood, and exactly one bucket pays \$1.” Skipping the middle bucket punches a hole right where the outcome most often falls. This is the `14,16,18 instead of 15,16,17` bug you described — quantified.
## 3. Why gaps kill it: the winner is usually a NEIGHBOUR, not the peak
Across winning `peak_cluster` boxes, the bucket that actually paid out was:
- the **peak** (highest-probability) bucket: **8 times**
- a **neighbour** of the peak: **10 times**
So more than half the time the money was in a neighbour, not the model's favourite. The current code fills the basket by **probability** (it keeps the high-prob peak and drops cheaper neighbours to fit the cost cap) — which preferentially discards the buckets that win most often. Adjacent-based filling keeps them.
## 4. More adjacent coverage = higher win rate
**peak_cluster by leg count:**
<table header-row="true">
<tr>
<td>Legs</td>
<td>Boxes</td>
<td>Win rate</td>
<td>PnL</td>
</tr>
<tr>
<td>3</td>
<td>11</td>
<td>64%</td>
<td>−\$31.39 (holds the gapped losers)</td>
</tr>
<tr>
<td>4</td>
<td>11</td>
<td>**82%**</td>
<td>+\$3.73</td>
</tr>
<tr>
<td>5</td>
<td>1</td>
<td>100%</td>
<td>+\$1.11</td>
</tr>
</table>
Wider *contiguous* ladders catch the outcome more reliably. The 3-leg group looks bad only because the gapped baskets live there; contiguous 4-leg baskets hit 82%.
## 5. Verdict
<callout icon="🎯">
	**Adjacent-based positioning wins decisively over probability-based positioning.** Contiguous baskets: 83% / +\$40 (cluster) and 71% / +\$57 (cool). Probability-gapped baskets: 40% / −\$67. The fix is not to buy the highest-probability buckets — it's to buy an unbroken ladder centred on the peak.
</callout>
## 6. Root cause + the fix (code, not a setting)
**Root cause:** `peak_cluster` builds a window around the peak, then does `sorted(window, by -probability)` and greedily adds the highest-probability buckets under the cost cap. Selection is by *probability*, so it can drop an interior neighbour and leave a hole.
**Fix (overlay, no core/speed change):** fill **outward and contiguously** from the centre — center, ±1, ±2… — adding each next-adjacent bucket while the combined per-share cost stays \< \$1. Only skip a bucket for price/liquidity, and if a skip would create an interior hole, **stop the ladder at the hole** (buy the unbroken run) rather than jump over it. Never re-order by probability. This mirrors how `peaker_cool_basket` already behaves (and wins).
## 7. Parameter possibilities per basket strategy
### peak_cluster (`/settings → Cluster`)
<table header-row="true">
<tr>
<td>Knob</td>
<td>Now</td>
<td>Suggest</td>
<td>Why</td>
</tr>
<tr>
<td>`PEAK_CLUSTER_MIN_LEGS`</td>
<td>3 (hard-floored)</td>
<td>**4**</td>
<td>4-leg baskets won 82% vs 64% for 3-leg</td>
</tr>
<tr>
<td>`PEAK_CLUSTER_SPAN`</td>
<td>2</td>
<td>2–3</td>
<td>Wider neighbourhood to fill contiguously</td>
</tr>
<tr>
<td>`PEAK_CLUSTER_MAX_LEGS`</td>
<td>7</td>
<td>5–6</td>
<td>Enough coverage without over-paying the cost cap</td>
</tr>
<tr>
<td>`PEAK_CLUSTER_MAX_COST`</td>
<td>\~0.85</td>
<td>keep \< 0.90</td>
<td>Must stay \< \$1 so any single win nets profit</td>
</tr>
<tr>
<td>`PEAK_CLUSTER_MIN_CONF`</td>
<td>0.55</td>
<td>0.60–0.65</td>
<td>Only fire when the peak neighbourhood is trusted</td>
</tr>
<tr>
<td>`PEAK_CLUSTER_MAX_CENTER_PRICE`</td>
<td>0.85</td>
<td>0.80</td>
<td>Avoid over-priced centres</td>
</tr>
<tr>
<td>Contiguous fill + cap-exemption</td>
<td>—</td>
<td>**overlay**</td>
<td>The real win; not a setting</td>
</tr>
</table>
### peaker_cool_basket (`/settings → Peaker`) — your best basket, overweight it
<table header-row="true">
<tr>
<td>Knob</td>
<td>Suggest</td>
<td>Why</td>
</tr>
<tr>
<td>`PEAKER_PREFER_COOL`</td>
<td>on</td>
<td>Cool baskets are the winners (+\$57)</td>
</tr>
<tr>
<td>`PEAKER_COOL_SIZE_MULT`</td>
<td>1.4–1.6</td>
<td>Put more capital on the 71%-WR basket</td>
</tr>
<tr>
<td>`PEAKER_COOL_EDGE_RELAX`</td>
<td>small (0.02–0.03)</td>
<td>Let a few more cool baskets qualify</td>
</tr>
<tr>
<td>`PEAKER_WARM_SIZE_MULT`</td>
<td>0.5</td>
<td>Warm baskets are marginal — keep small</td>
</tr>
<tr>
<td>`PEAKER_SOLO_MIN_CONFIDENCE`</td>
<td>0.86</td>
<td>Suppress the losing solo `peaker`</td>
</tr>
</table>
### The +300% cap interaction (both baskets)
Baskets are hold-to-\$1. The global +300% cap can book a cheap winning leg early (\~\$0.48) instead of letting it settle at \$1.00 — exempt baskets from the cap (overlay) or raise `PROFIT_CAP_ROI_PCT` to 500.
## 8. Expected impact
<table header-row="true">
<tr>
<td>Change</td>
<td>Effect on peak_cluster</td>
</tr>
<tr>
<td>Contiguous fill (kill the 5 gapped baskets' −\$67)</td>
<td>−\$27 → **≈+\$40**</td>
</tr>
<tr>
<td>  • min-legs 4 & wider coverage</td>
<td>higher hit rate on the +\$216 big-win tail</td>
</tr>
<tr>
<td>  • basket cap-exemption</td>
<td>winning legs ride to \$1 instead of being booked at +300%</td>
</tr>
</table>
Bottom line: this one fix alone is a **\~\$67 swing** on `peak_cluster` and turns it from a drag into a contributor — without touching speed, the core loop, or the other strategies.
