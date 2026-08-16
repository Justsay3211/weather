<callout icon="⚡">
	Paste-ready `/set` commands for the live bot. **The one-line 3x fix is at the top** — it's a pure setting value, so it changes NO code, NO loop speed, NO strategy mechanism, NO execution path. It only moves the number at which the *existing* exit already fires. Simulation: that single change ≈ **3.9x** (\$932 vs \$240).
</callout>
## ⭐ The 3x quick fix (do this first)
Your losers currently ride to ≈−88% before the exit triggers, because the exit threshold ships at **−85%**. Moving it to **−50%** cuts every big loser roughly in half — nothing else changes.
```javascript
/set THESIS_EXIT_ENABLED on
/set THESIS_EXIT_MAX_ROI_PCT -50
```
<callout icon="✅">
	Why it's safe: `THESIS_EXIT_MAX_ROI_PCT` is a runtime number read at call-time by the exit check that ALREADY runs every scan. No new work, no extra latency, no logic change — the loss just gets cut sooner. Fully reversible (`/set THESIS_EXIT_MAX_ROI_PCT -85` to undo).
</callout>
## 🔧 Full tune (paste as a block — pushes toward 5x)
### 1) Exit sooner (the core lever)
```javascript
/set THESIS_EXIT_ENABLED on
/set THESIS_EXIT_MAX_ROI_PCT -50
```
### 2) Kill the quick_flip churn
```javascript
/set QUICK_FLIP_ENABLED off
```
### 3) Keep the winning cool basket, suppress bad peaker/warm/solo
```javascript
/set PEAKER_ENABLED on
/set PEAKER_PREFER_COOL on
/set PEAKER_COOL_SIZE_MULT 1.4
/set PEAKER_WARM_SIZE_MULT 0.5
/set PEAKER_MIN_CONFIDENCE 0.7
/set PEAKER_SOLO_MIN_CONFIDENCE 0.86
/set PEAKER_MIN_EDGE 0.05
```
### 4) Late-observed: more confident, push NO off the junk floor, cut expensive YES
```javascript
/set LATE_OBSERVED_NO_SIDE on
/set LATE_OBSERVED_MIN_LOCK 0.65
/set LATE_OBSERVED_MIN_EDGE 0.12
/set LATE_OBSERVED_NO_MIN_PRICE 0.2
/set LATE_OBSERVED_YES_MIN_EDGE 0.16
```
### 5) Concentrate size on the winners, cap single-position risk
```javascript
/set KELLY_TIER_BASE_USD 2
/set KELLY_TIER_GOOD_USD 5
/set KELLY_TIER_VGOOD_USD 12
/set KELLY_TIER_PERFECT_USD 18
/set KELLY_MAX_FRACTION 0.1
/set MAX_BET_PCT 0.15
/set MAX_SINGLE_MARKET_PCT 0.2
```
### 6) Tighter overall selectivity
```javascript
/set GRADE_MIN_TO_TRADE 0.45
/set MIN_EDGE_TO_ENTER 0.12
```
### 7) Stop the +300% cap from booking basket winners too early
```javascript
/set PROFIT_CAP_ROI_PCT 500
```
### 8) Keep the safety guards on
```javascript
/set PORTFOLIO_GUARD_ENABLED on
/set DRAWDOWN_GATE_ENABLED on
```
<callout icon="💡">
	Tip: send a few at a time and watch `/status`. Everything persists to `data/runtime_settings.json` and survives restart. To reset a knob, `/set KEY <old value>`; to see current values open `/settings`.
</callout>
## ⚠️ What these commands CANNOT do (needs the overlay module)
These four levers have no Telegram knob and require the thin add-on (they don't touch core either, but they're code, not settings):
- **City throttle** — there's no per-city setting; Madrid/Ankara/Houston/Chicago/Tokyo drag can't be zeroed from Telegram.
- **Entry-band sizing** for `late_observed_no` (down-boost \<0.50, up-boost 0.50–0.85). `LATE_OBSERVED_NO_MIN_PRICE` maxes at 0.20, so commands can only nudge, not gate at 0.50.
- **Peak-cluster contiguity fix** (the 14,16,18 bug).
- **Basket exemption** from the profit cap (command #7 above is only a blunt raise).
## Expected effect
<table header-row="true">
<tr>
<td>Layer</td>
<td>Lever</td>
<td>Approx result</td>
</tr>
<tr>
<td>Command #1 alone</td>
<td>−50% exit</td>
<td>**\~3.9x**</td>
</tr>
<tr>
<td>All commands</td>
<td>  • gate flip, tune peaker, retier sizing, selectivity</td>
<td>**\~4–5x**</td>
</tr>
<tr>
<td>  • overlay (§ above)</td>
<td>city throttle, band sizing, cluster fix, cap exemption</td>
<td>**\~7–10x** (prove with the backtest data)</td>
</tr>
</table>
