# Session Log - Req-31: ML Overhaul (LLM wired for real)

- **Date:** 2026-06-20 (Asia/Calcutta)
- **Branch:** `dev`
- **Scope:** Make the LLM decision layer actually function, split models per use, maximize ML coverage where it helps, and expose the controls in Telegram.
- **Status:** Code complete + compiles + parser smoke-tested. Pending: Railway env vars + token rotation (network actions, done by operator).

---

## 0. TL;DR

The bot *looked* ML-wired but the LLM was effectively dead: `gpt-5.5` is a **reasoning model** that prefixes replies with a `<think>...</think>` block and takes ~7s, while the old code only stripped triple-backtick fences, capped `max_tokens` at 40-60, and used an 8s timeout. Every real reply was therefore discarded and the engine silently fell back to the local heuristic model. This session fixes the parser/timeouts/tokens, splits decision vs analysis models, activates the two dormant ML methods (position review + market selection) behind conservative guards, and adds the Telegram toggles + model selectors.

---

## 1. State BEFORE this session

- Requests 1-29 done on `dev` (commit `eddcf1bf`); Req-30 + 30b done (dev HEAD `2a05787f`, "fgg"), verified.
- ML mode = BOTH: use the LLM when an API key is present, otherwise local fallback.
- ML wiring was **partial**:
  - `validate_signal` and `decide_profit_hold` were called live (with local fallback).
  - `/mlanalysis` (`write_trade_report`) was live with local fallback.
  - `review_position` and `select_markets` existed but were **never called** (dormant).
- Realistic-paper-engine + settlement model LOCKED: weather API = CONFIRMATION only; the Polymarket resolved value is truth.

---

## 2. ML endpoint findings (discovered via operator's laptop; sandbox has no network)

Working endpoint: `https://api.freemodel.dev/v1` (OpenAI-compatible).

- `GET /v1/models` -> 200, models: `gpt-5.5`, `gpt-5.4`, `gpt-5.4-mini`, `gpt-5.3-codex` (all `supported_endpoint_types: ["openai"]`).
- `POST /v1/chat/completions` model=`gpt-5.5` -> 200 in ~7.1s. Content came back as:

  ```
  <think>**Evaluating a buying decision** ... confidence around 0.74 ...</think>

  {"action":"BUY","conf":0.74,"why":"Positive edge: 35% vs 12% price; forecast 18.5C in bucket"}
  ```

- Dead/unusable endpoints checked: `vip-sg.freemodel.dev` = 403 (tier gated); `cc.freemodel.dev` = Anthropic mintapi (gated, "Please use Claude Code CLI").

**Key takeaway:** the model returns valid JSON, but only *after* a reasoning block, and slowly. The integration has to tolerate both.

---

## 3. Defects + gaps identified

1. **Reasoning block not stripped.** `_parse_response` only handled triple-backtick fences -> `json.loads` failed on the `<think>...` prefix -> silent local fallback.
2. **`max_tokens` too small (40-60).** A reasoning model burns its budget on the think block and never reaches the JSON -> empty/truncated content.
3. **`timeout=8s` too short.** Real replies take ~7s and spike higher; calls timed out -> silent local fallback.
4. **Dormant `review_position`** - the ML could never trim a losing/peaked open position early.
5. **Dormant `select_markets`** - the ML never influenced which markets got the per-scan buy budget.
6. **Single model for everything** - no separation between cheap frequent decisions and the richer end-of-period narrative.

---

## 4. What the operator decided (this session's requirements)

- **Models:** `gpt-5.4-mini` for decisions, `gpt-5.5` for `/mlanalysis`.
- **Scope:** apply ML everywhere it benefits / maximizes profit + win-rate and avoids losses; give the model decent context (weather-analyst quality).
- **Delivery:** attempt to push; if push not feasible, provide a zip; wire everything and verify it works.
- **Telegram:** ensure the existing ML on/off toggle truly gates ML usage (on = use ML, off = don't); add an ML model selector; add an ML-Analysis on/off toggle (on = ML writes the analysis, off = it doesn't).

---

## 5. What was changed (by file)

### `config.py`
New/updated ML defaults:
- `ML_API_URL = https://api.freemodel.dev/v1`
- `ML_MODEL = gpt-5.4-mini` (decisions), `ML_ANALYSIS_MODEL = gpt-5.5` (analysis)
- `ML_ANALYSIS_ENABLED = 1`
- `ML_QUERY_TIMEOUT = 30`
- `ML_DECISION_MAX_TOKENS = 700`, `ML_ANALYSIS_MAX_TOKENS = 1200`
- `ML_REVIEW_POSITIONS = 1`, `ML_REVIEW_SELL_CONF = 0.72`, `ML_REVIEW_MIN_HOLD_MIN = 20`, `ML_REVIEW_MIN_MTC_MIN = 45`, `ML_REVIEW_MAX_PER_SCAN = 6`
- `ML_SELECT_MARKETS = 1`

### `ml/decision_engine.py`
- Added `import re`.
- `__init__`: added `self.analysis_model`, `self.timeout`, `self.max_tokens_default`; startup log now shows `model (analysis: analysis_model) via base_url`.
- Added a weather-analyst **system prompt** (prices buckets from forecast/observed data, weighs edge vs price, fees, liquidity, time-to-close, city hit-rate; must end with JSON only).
- `_query(prompt, max_tokens=None)`: uses the system prompt, `max_tokens` defaults to `ML_DECISION_MAX_TOKENS`, `timeout = ML_QUERY_TIMEOUT`.
- Call sites no longer pass tiny token caps (validate_signal / review_position / decide_profit_hold use the default; select_markets uses a larger cap).
- New static helpers: `_strip_reasoning()` (removes `<think>...</think>`, closed AND truncated-open) and `_loads_json()` (json.loads, else first balanced `{...}`/`[...]`).
- `_parse_response` rewritten: strip reasoning -> strip fences -> `_loads_json` -> dict (`action`/`conf`->confidence/`why`->reason) or list (SELECT raw) -> keyword fallback last.
- `write_trade_report`: gated on `ML_ANALYSIS_ENABLED`; posts `ML_ANALYSIS_MODEL` with `ML_ANALYSIS_MAX_TOKENS` and a longer timeout; strips the reasoning block from the narrative.
- `get_status`: now also reports `analysis_model`.

### `bot/settings_store.py`
- Added bool toggles `ML_ANALYSIS_ENABLED`, `ML_REVIEW_POSITIONS`, `ML_SELECT_MARKETS`.
- New **string/choice** setting type: `STR_KEYS` = `{ML_MODEL: [...], ML_ANALYSIS_MODEL: [...]}`.
- New `group_str_keys()` (kept `group_keys` 2-tuple so existing callers don't break), `cycle()` (advance to next choice), `str_snapshot()`.
- `_coerce`, `set_value`, `_persist`, `load_into_config` all extended to handle STR keys.
- New `ml` settings group/tab.

### `bot/telegram_ui.py`
- New labels: Use ML / ML-Decide / ML Analysis / ML Review-Pos / ML Market-Pick.
- `_settings_view`: renders the string/choice settings + tap-to-cycle buttons (`cy:KEY:gid`).
- Callback handler: handles `cy:` by calling `settings_store.cycle`.
- `_ml_narrative`: if `ML_ANALYSIS_ENABLED` is off, returns a clear "ML Analysis is OFF" notice and uses the heuristic instead.
- `/set ML_MODEL <value>` works (STR keys accepted).

### `dashboard.py`
- New `_ml_prioritize_markets()`: when `ML_ENABLED` + `ML_SELECT_MARKETS` + engine live, asks `select_markets(cities)` and **reorders** markets so top-ranked cities are evaluated first. Ordering only - never drops a market. Called right after market discovery.
- `run_once`: added `exit_policies.check_ml_reviews(self.pm)` to the resolution/exit batch and into the single-notify loop.

### `trading/exit_policies.py`
- New `check_ml_reviews(pm)`: ML reviews each eligible OPEN position and triggers an EARLY SELL only when confident. Guards: only when `ML_REVIEW_POSITIONS` on + API live; skips quick_flip, hold-to-resolution, stale-price, zero-price; requires min hold time + min time-to-close; caps ML calls/scan; acts only on `SELL` with `confidence >= ML_REVIEW_SELL_CONF`. HOLD / low confidence changes nothing. Closes via `pm._close_position(pos, price, 'ml_review_sell')` then `_save_state` + `_assert_ledger`.

### `trading/position_manager.py`
- Added `'ml_review_sell'` to `DASHBOARD_NOTIFIED_REASONS` so the dashboard sends exactly one close alert (no double-notify).

---

## 6. Behavioral improvements

- The LLM now actually drives decisions instead of silently falling back. Verified: the exact pasted `gpt-5.5` reply now parses to `BUY @ 0.74`.
- Truncated/garbled replies degrade safely to the local heuristic instead of crashing.
- Decisions use the cheaper `gpt-5.4-mini`; the heavier `gpt-5.5` is reserved for the on-demand `/mlanalysis` narrative.
- ML now influences both **entry prioritization** (market selection) and **early exits** (position review), each behind conservative, configurable guards that cannot force a bad exit or drop a profitable market.
- Operators can flip every ML behavior and switch models live from Telegram, with settings persisted to `data/runtime_settings.json`.

---

## 7. Errors, corrections & gotchas

- **Sandbox has zero outbound network** - DNS fails for all hostnames. No live API tests, pip/npm/dnf installs from here. ML verification happens on the operator's laptop or on Railway.
- **`gpt-5.5` is a reasoning model** - the `<think>...</think>` prefix + ~7s latency was the root cause of the silent fallback. This drove the parser/token/timeout fixes.
- **`group_keys` had two callers** (telegram_ui + `bot/_dev_tg.py`); to avoid breaking either, the string-key support was added via a NEW `group_str_keys()` rather than changing the existing return shape.
- **Editor is str-replace** - anchors had to be unique; verified by re-reading snippets before/after.
- **Secret hygiene:** the live `ML_API_KEY` and the Railway API token are intentionally NOT written into this committed file. Set them as environment variables only. The Railway token that was exposed in chat must be rotated.

---

## 8. Verification performed

- `python -m py_compile` on all 7 edited files -> OK.
- Parser smoke test (replicating `_strip_reasoning` + `_loads_json` + parse) against:
  - the exact pasted `gpt-5.5` reply -> `{action: BUY, confidence: 0.74, ...}` PASS
  - a truncated unclosed `<think>` -> safe BUY/0.5 fallback PASS
  - a SELECT array reply -> `raw: [...]` PASS
  - a fenced-json reply -> parsed PASS

---

## 9. Progress checklist

- [x] config.py ML block
- [x] ml/decision_engine.py (parser, prompt, tokens, timeout, model split, status)
- [x] bot/settings_store.py (toggles + STR_KEYS + cycle/str_snapshot/persist/load)
- [x] bot/telegram_ui.py (render + cycle callback + labels + analysis gate + /set)
- [x] dashboard.py (market prioritization + ml-review exit hook + notify)
- [x] trading/exit_policies.py (check_ml_reviews)
- [x] trading/position_manager.py (notify reason)
- [x] Compile + parser smoke test
- [x] Session log (this file) pushed to `dev`
- [ ] Railway env vars set + redeploy (operator)
- [ ] Rotate exposed Railway token (operator)
- [ ] Live `/mlanalysis` + startup-log confirmation on Railway (operator)

---

## 10. Remaining / next steps (operator, needs network)

1. Set Railway env vars: `ML_API_URL=https://api.freemodel.dev/v1`, `ML_API_KEY=<secret>`, `ML_MODEL=gpt-5.4-mini`, `ML_ANALYSIS_MODEL=gpt-5.5` (starting balance 500). Redeploy.
2. Confirm startup log shows: `ML Engine: gpt-5.4-mini (analysis: gpt-5.5) via https://api.freemodel.dev...` and run `/mlanalysis` for a real narrative.
3. **Rotate the Railway API token** that was exposed in chat.

---

## 11. Key references

- Working ML endpoint: `https://api.freemodel.dev/v1`. Models: `gpt-5.5`, `gpt-5.4`, `gpt-5.4-mini`, `gpt-5.3-codex`. Decisions=`gpt-5.4-mini`, analysis=`gpt-5.5`. (API key stored in env only - redacted here.)
- Repo: `github.com/GTGRP/WEATHERPOL`, branch `dev`.
- Deferred (Req-28): sniper/confident/spread/stability tuning; do NOT touch `late_observed_no`.
