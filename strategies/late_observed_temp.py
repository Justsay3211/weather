"""
Late Observed-Temperature strategy — the overhauled PRIMARY edge.

Thesis
------
For a daily-high (or daily-low) market, once the local day is far enough along
that the peak heating (or overnight cooling) is essentially done, the day's
extreme is *physically locked*: the final settled high can only be ≥ the max
already observed. The order book, however, often still prices stale forecast
uncertainty. We exploit the gap two ways:

* **YES** the bucket that the observed data says will win, when its price still
  leaves a positive, fee-adjusted edge.
* **NO** the buckets the observed data has made *impossible* (e.g. a low bucket
  after the high is already locked above it) when the book still prices them
  rich enough to clear fees — the audit's NO-side edge.

Gating is fee-aware (Polymarket weather taker fee = 5% × p × (1−p)) and
timing-aware (only trades once the day is sufficiently "locked").

The pure decision core lives in :func:`decide_legs` (only depends on
``data.fees`` + stdlib) so it is fully unit-testable offline. The
:class:`LateObservedTempStrategy` wraps it with project ``Config`` and the
observed-temperature probability model.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Dict, List, Optional, Sequence, Tuple

from data import fees

try:  # keep importable offline (Config imports dotenv)
    from config import Config  # type: ignore
except Exception:  # pragma: no cover
    Config = None  # type: ignore

try:
    from logger import log  # type: ignore
except Exception:  # pragma: no cover
    import logging
    log = logging.getLogger("late_observed_temp")


@dataclass
class LateObservedLeg:
    bucket_label: str
    side: str               # 'YES' or 'NO'
    token_id: str
    price: float            # price of the token we BUY (yes price for YES, no price for NO)
    our_probability: float  # our P(this token wins)
    edge: float             # our_probability - fee-adjusted breakeven
    ev_per_contract: float
    size_usd: float
    reason: str = ""


@dataclass
class LateObservedSignal:
    market_title: str
    city: str
    market_type: str
    observed_extreme_c: float
    remaining_extreme_c: Optional[float]
    hours_remaining: int
    lock_confidence: float
    legs: List[LateObservedLeg] = field(default_factory=list)
    reason: str = ""


@dataclass
class DecideParams:
    """Plain thresholds for the pure decision core (no Config dependency)."""
    min_edge: float = 0.10           # post-fee probability cushion required
    min_entry_price: float = 0.02    # cheap-tail floor: this is a HOLD-to-resolution
                                     # strategy, so EV+ sub-5c tails are allowed
    max_entry_price: float = 0.95    # don't pay through the roof for YES
    no_min_price: float = 0.04       # only NO a dead bucket if book still prices it richly
    no_max_price: float = 0.97       # NO token price ceiling (avoid ~$1 no-edge fills)
    taker: bool = True
    base_fraction: float = 0.06      # (legacy) base stake as fraction of balance
    max_fraction: float = 0.25       # per-trade balance-fraction SAFETY cap
    kelly_cap: float = 0.25          # (legacy, unused by the tiered sizer)
    max_legs: int = 4
    min_order_usd: float = 1.0
    # --- Signal-strength → absolute-USD allocation ladder ---------------
    # Replaces the old flat %-of-bankroll Kelly stake (which hit a 25%-of-
    # balance ceiling = $25 on $100 with NO dollar cap, draining the bankroll).
    # The stake now scales with a composite signal-strength score in [0, 1]
    # built from the post-fee edge and the weather-stability grade, laddered
    # between size_floor_usd (a barely-passing signal) and size_max_usd (a very
    # strong edge on a stable day). A very good signal therefore deploys MORE
    # capital instead of being capped at a flat amount; weak-but-valid signals
    # stay small. max_fraction above is still a per-trade balance safety.
    size_floor_usd: float = 3.0      # weakest valid signal stake (~user's "good $3-4")
    size_max_usd: float = 20.0       # strongest signal stake (~user's "very good $20")
    edge_full: float = 0.25          # post-fee edge that counts as max strength
    w_edge: float = 0.6              # weight of edge in the strength score
    w_grade: float = 0.4             # weight of grade in the strength score
    # --- Req-27 YES-side gating (late_observed_yes was a net loser) -------
    # The YES leg needs a HIGHER post-fee edge than the NO side, and is only
    # CONSIDERED when the strategy says the day is strongly locked (allow_yes,
    # set per-call from the lock confidence vs LATE_OBSERVED_YES_MIN_LOCK). The
    # NO side keeps the looser min_edge / lock so the winning NO edge is intact.
    yes_min_edge: float = 0.14       # YES-only post-fee edge floor (vs min_edge for NO)
    allow_yes: bool = True           # gate: only emit YES legs on a strongly-locked day


def _stake_usd(prob_win: float, price: float, balance: float, grade: float,
               edge: float, params: DecideParams) -> float:
    """Signal-strength → absolute-USD allocation.

    A composite strength score (post-fee ``edge`` + weather ``grade``) in [0, 1]
    is laddered linearly between ``size_floor_usd`` and ``size_max_usd`` so a
    stronger signal deploys MORE capital (up to the max) and a barely-passing
    one stays near the floor. The result is clamped to a per-trade balance
    fraction and the balance itself, so a single leg can never over-commit a
    small bankroll.
    """
    edge_norm = max(0.0, min(1.0, edge / params.edge_full)) if params.edge_full > 0 else 0.0
    grade_norm = max(0.0, min(1.0, grade)) if grade else 0.0
    strength = params.w_edge * edge_norm + params.w_grade * grade_norm
    strength = max(0.0, min(1.0, strength))
    usd = params.size_floor_usd + (params.size_max_usd - params.size_floor_usd) * strength
    # Safety: never exceed a fraction of the bankroll or the bankroll itself.
    usd = min(usd, balance * params.max_fraction, balance)
    if usd < params.min_order_usd:
        usd = params.min_order_usd
    return round(min(usd, balance), 2)


def decide_legs(
    observed_probs: Dict[str, float],
    yes_prices: Dict[str, float],
    yes_token_ids: Dict[str, str],
    balance: float,
    grade: float = 0.6,
    no_prices: Optional[Dict[str, float]] = None,
    no_token_ids: Optional[Dict[str, str]] = None,
    params: Optional[DecideParams] = None,
) -> List[LateObservedLeg]:
    """Pure decision core: turn observed bucket probabilities + book prices into
    fee-cleared YES/NO legs. Depends only on ``data.fees`` + stdlib.
    """
    params = params or DecideParams()
    no_prices = no_prices or {}
    no_token_ids = no_token_ids or {}
    legs: List[LateObservedLeg] = []

    for label, p_win in observed_probs.items():
        # --- YES side: bucket observed data expects to win ----------------
        # Req-27: only when allow_yes (day strongly locked) AND clearing the
        # higher YES-only edge floor (yes_min_edge). late_observed_yes lost net
        # at the looser NO-side gate, so the YES leg is now strict.
        yp = yes_prices.get(label)
        ytid = yes_token_ids.get(label)
        if params.allow_yes and yp is not None and ytid and params.min_entry_price <= yp <= params.max_entry_price:
            if fees.passes_fee_gate(p_win, yp, params.yes_min_edge, params.taker):
                edge = p_win - fees.breakeven_prob(yp, params.taker)
                legs.append(LateObservedLeg(
                    bucket_label=label, side="YES", token_id=ytid, price=yp,
                    our_probability=p_win, edge=edge,
                    ev_per_contract=fees.ev_per_contract(p_win, yp, params.taker),
                    size_usd=_stake_usd(p_win, yp, balance, grade, edge, params),
                    reason=f"observed P(win)={p_win:.0%} vs YES px {yp:.0%}, edge {edge:+.0%}",
                ))
                continue  # don't also NO a bucket we're going YES on

        # --- NO side: bucket observed data says is (near) impossible -------
        prob_no = 1.0 - p_win
        np_ = no_prices.get(label)
        ntid = no_token_ids.get(label)
        if np_ is None or not ntid:
            continue
        if not (params.no_min_price <= np_ <= params.no_max_price):
            continue
        if fees.passes_fee_gate(prob_no, np_, params.min_edge, params.taker):
            edge = prob_no - fees.breakeven_prob(np_, params.taker)
            legs.append(LateObservedLeg(
                bucket_label=label, side="NO", token_id=ntid, price=np_,
                our_probability=prob_no, edge=edge,
                ev_per_contract=fees.ev_per_contract(prob_no, np_, params.taker),
                size_usd=_stake_usd(prob_no, np_, balance, grade, edge, params),
                reason=f"observed P(no)={prob_no:.0%} vs NO px {np_:.0%}, edge {edge:+.0%}",
            ))

    # Keep the strongest few legs by edge to respect bankroll / position caps.
    legs.sort(key=lambda l: l.edge, reverse=True)
    return legs[: params.max_legs]


class LateObservedTempStrategy:
    """Observation-driven primary strategy (wraps :func:`decide_legs`)."""

    name = "late_observed_temp"

    def __init__(self):
        c = Config
        def g(attr, default):
            return getattr(c, attr, default) if c is not None else default
        self.enabled = bool(g("LATE_OBSERVED_ENABLED", 1))
        self.no_side_enabled = bool(g("LATE_OBSERVED_NO_SIDE", 1))
        self.min_lock_conf = float(g("LATE_OBSERVED_MIN_LOCK", 0.70))
        # Req-27: the YES leg requires a STRONGER lock than the NO side.
        self.yes_min_lock = float(g("LATE_OBSERVED_YES_MIN_LOCK", 0.80))
        self.params = DecideParams(
            min_edge=float(g("LATE_OBSERVED_MIN_EDGE", 0.10)),
            min_entry_price=float(g("LATE_OBSERVED_MIN_ENTRY_PRICE", 0.02)),
            max_entry_price=float(g("LATE_OBSERVED_MAX_YES_PRICE", 0.95)),
            no_min_price=float(g("LATE_OBSERVED_NO_MIN_PRICE", 0.04)),
            no_max_price=float(g("LATE_OBSERVED_NO_MAX_PRICE", 0.97)),
            taker=bool(g("ASSUME_TAKER_FILLS", 1)),
            base_fraction=float(g("LATE_OBSERVED_BASE_FRACTION", 0.06)),
            max_fraction=float(g("LATE_OBSERVED_MAX_FRACTION", 0.25)),
            kelly_cap=float(g("KELLY_FRACTION", 0.15)) + 0.10,
            max_legs=int(g("LATE_OBSERVED_MAX_LEGS", 4)),
            min_order_usd=float(g("MIN_ORDER_SIZE", 1.0)),
            size_floor_usd=float(g("LATE_OBSERVED_SIZE_FLOOR_USD", 3.0)),
            size_max_usd=float(g("LATE_OBSERVED_SIZE_MAX_USD", 20.0)),
            edge_full=float(g("LATE_OBSERVED_EDGE_FULL", 0.25)),
            w_edge=float(g("LATE_OBSERVED_W_EDGE", 0.6)),
            w_grade=float(g("LATE_OBSERVED_W_GRADE", 0.4)),
            yes_min_edge=float(g("LATE_OBSERVED_YES_MIN_EDGE", 0.14)),
        )

    def evaluate(
        self,
        market_title: str,
        buckets: Sequence[Tuple[str, float, float]],
        yes_prices: Dict[str, float],
        yes_token_ids: Dict[str, str],
        balance: float,
        city: str,
        observed_state,
        *,
        no_prices: Optional[Dict[str, float]] = None,
        no_token_ids: Optional[Dict[str, str]] = None,
        grade: float = 0.6,
        market_type: str = "highest_temperature",
    ) -> List[LateObservedSignal]:
        """Build at most one signal (a set of legs) for this market."""
        from data import observed_math as om  # local import: pure, always available

        if not self.enabled or observed_state is None:
            return []

        mode = "low" if "low" in (market_type or "").lower() else "high"
        lock = om.lock_confidence(
            observed_state.observed_extreme_c,
            observed_state.remaining_extreme_c,
            observed_state.remaining_spread_c,
            mode=mode,
        )
        if lock < self.min_lock_conf:
            # DIAGNOSTIC: the day isn't locked enough yet for the primary edge.
            # This is the #2 reason the primary stays silent — surface the exact
            # numbers (lock vs threshold + the observed state) so it's debuggable.
            log.info(
                f"   🌡️  PRIMARY skip {city} {mode} — lock {lock:.0%} < {self.min_lock_conf:.0%} "
                f"(obs {observed_state.observed_extreme_c:.1f}°C, "
                f"{observed_state.hours_remaining}h left, "
                f"±{observed_state.remaining_spread_c:.1f}°C across {observed_state.n_models} models)"
            )
            return []

        probs = om.observed_bucket_probabilities(
            observed_state.observed_extreme_c,
            observed_state.remaining_extreme_c,
            observed_state.remaining_spread_c,
            list(buckets),
            mode=mode,
        )

        # Req-27: gate the YES side on a STRONG lock (late_observed_yes lost net
        # on weakly-locked days). The NO side keeps the looser self.min_lock_conf
        # above; here we only decide whether YES legs are eligible this scan.
        allow_yes = lock >= self.yes_min_lock
        # BALANCE-AWARE MAX CAP (added 2026-07-27): late_observed_no is the only
        # proven winner, so let its hard $ cap grow WITH the balance as it
        # compounds. Effective cap = max(fixed LATE_OBSERVED_SIZE_MAX_USD,
        # LATE_OBSERVED_SIZE_MAX_PCT * balance). pct = 0 keeps the flat cap.
        # This only lifts the CEILING; the sizing math itself is unchanged.
        eff_size_max = self.params.size_max_usd
        try:
            _c = self.config if hasattr(self, "config") else None
        except Exception:
            _c = None
        try:
            from config import Config as _Cfg
            _pct = float(getattr(_Cfg, "LATE_OBSERVED_SIZE_MAX_PCT", 0.0) or 0.0)
            if _pct > 0 and balance and balance > 0:
                eff_size_max = max(eff_size_max, _pct * float(balance))
        except Exception:
            eff_size_max = self.params.size_max_usd
        params = replace(self.params, allow_yes=allow_yes, size_max_usd=eff_size_max)
        if not self.no_side_enabled:
            no_prices = None
            no_token_ids = None

        legs = decide_legs(
            observed_probs=probs,
            yes_prices=yes_prices,
            yes_token_ids=yes_token_ids,
            balance=balance,
            grade=grade,
            no_prices=no_prices,
            no_token_ids=no_token_ids,
            params=params,
        )
        if not legs:
            # DIAGNOSTIC: day IS locked, but no bucket cleared the fee gate at
            # current book prices — the #3 reason for silence. Show the gate so
            # we know whether to relax min_edge / entry band or it's just rich.
            log.info(
                f"   🌡️  PRIMARY no-edge {city} {mode} — lock {lock:.0%}, "
                f"obs {observed_state.observed_extreme_c:.1f}°C but no bucket cleared the "
                f"fee gate (YES {'on' if allow_yes else 'OFF (lock<%.0f%%)' % (self.yes_min_lock*100)} "
                f"need edge ≥ {params.yes_min_edge:.0%} @ {params.min_entry_price:.0%}-{params.max_entry_price:.0%}; "
                f"NO need edge ≥ {params.min_edge:.0%} @ {params.no_min_price:.0%}-{params.no_max_price:.0%})"
            )
            return []

        return [LateObservedSignal(
            market_title=market_title,
            city=city,
            market_type=market_type,
            observed_extreme_c=observed_state.observed_extreme_c,
            remaining_extreme_c=observed_state.remaining_extreme_c,
            hours_remaining=observed_state.hours_remaining,
            lock_confidence=lock,
            legs=legs,
            reason=(f"LATE-OBSERVED {mode.upper()} | observed={observed_state.observed_extreme_c:.1f}°C "
                    f"| lock={lock:.0%} | {len(legs)} leg(s)"),
        )]
