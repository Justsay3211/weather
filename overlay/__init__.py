"""
Weather Sniper Bot — OVERLAY package.

Everything in here is ADD-ON behaviour that sits ON TOP of the bot core without
changing the hot path, the strategies' math, or the ledger. The core calls into
these modules through a handful of tiny, guarded hooks (each wrapped in
try/except so a missing/broken overlay can NEVER stop the bot from trading).

Modules:
  strategy_gate    — decoupled per-(sub)strategy trade toggles (late_observed
                     yes/no, peaker solo/cool/warm, peak_cluster). Enforced at
                     the single placement choke point: a signal may still be
                     generated, but NO order is placed when its toggle is OFF.
  basket_guard     — identifies any-one-wins basket positions so the thesis
                     exit stops cutting basket legs early (the -$819 leak).
  reserve_takeout  — adjustable cash RESERVE + a TAKEOUT pool that fences a
                     share of winning profit as untouchable, withdrawable cash.
  sizing_overlay   — bounded, opt-in size multiplier for the proven
                     late_observed_no entry-price band.
  advanced_settings— registers every extra tunable (with info text + ranges)
                     into the Telegram /settings panel.

Design rules:
  * Fail-open: unknown strategy => allowed; overlay error => core proceeds.
  * No import of core modules at module top (avoids import cycles); Config and
    logger only.
  * State is held in-memory and persisted lazily (no per-call disk reads).
"""

__all__ = [
    "strategy_gate",
    "basket_guard",
    "reserve_takeout",
    "sizing_overlay",
    "advanced_settings",
]

__version__ = "1.0.0"
