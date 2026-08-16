# Backtest Results Summary

## Test Parameters
- Period: 60 days (Apr 28 - May 28, 2026)
- Cities: Tokyo, Seoul, London, Taipei, Beijing, Ankara
- Starting Balance: $10.00
- Max Entry Price: $0.15
- Min Edge to Enter: 10%
- Bet Sizing: 8% of balance or $0.50 max per trade

## Results
| Metric | Value |
|--------|-------|
| Ending Balance | $1,120.69 |
| Total PnL | +$1,110.69 |
| ROI | +11,107% |
| Total Trades | 364 |
| Win Rate | 25.8% (94W / 270L) |
| Avg Entry Price | ~$0.05-0.15 |

## By City
| City | Trades | Win Rate | PnL |
|------|--------|----------|-----|
| Ankara | 62 | 35% | +$249.72 |
| Tokyo | 49 | 29% | +$99.20 |
| Seoul | 54 | 24% | +$144.88 |
| Taipei | 66 | 23% | +$265.02 |
| London | 62 | 23% | +$267.71 |
| Beijing | 71 | 23% | +$84.15 |

## Key Insight
Even with only 25% win rate, buying buckets at $0.05-0.15 that pay $1.00 on win
creates massive positive EV. The edge comes from having a BETTER forecast 
(multi-model ensemble) than what the market has priced in (lagging forecast).

## Strategy Validation
- Our multi-model approach (ECMWF+GFS+ICON+JMA+GEM) gives ~0.9°C MAE
- Market pricing appears to lag by ~1.8°C equivalent uncertainty  
- Edge of 10%+ on cheap buckets = highly profitable over time
- Ankara and Tokyo perform best (matches reference wallet preferences)
