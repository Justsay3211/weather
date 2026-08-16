"""
BASKET position identification.

A "basket" here is an any-one-wins group of adjacent buckets held to resolution:
peak_cluster and the peaker cool/warm baskets. The WHOLE point of a basket is
that the single winning leg pays $1 and covers the cost of the losing legs, so
every leg MUST ride to settlement. Cutting a losing leg early (thesis exit) turns
a designed-to-win structure into a guaranteed partial loss.

The analysis of paper_trades (10).csv confirmed this is real and large: the
thesis exit closed 33 basket legs as `thesis_invalidated`, and thesis exits were
the single biggest realized leak overall (-$819 across 130 exits). Lowering
THESIS_EXIT_MAX_ROI_PCT from -85 to -50 made it worse (it started cutting basket
legs at -50%..-68%).

`is_basket_position(pos)` lets the exit policy skip these legs. Detection is
deliberately broad (name + basket_leg flag + cluster_box tag) so no basket leg
slips through.
"""

# Canonical basket strategy tags.
BASKET_STRATEGIES = {
    "peak_cluster",
    "peaker_cool_basket",
    "peaker_warm_basket",
    "peak_basket",
}


def is_basket_position(pos) -> bool:
    """True when `pos` is a leg of an any-one-wins basket held to resolution."""
    try:
        strat = (getattr(pos, "strategy", "") or "").strip().lower()
    except Exception:
        return False
    if strat in BASKET_STRATEGIES:
        return True
    if "basket" in strat or "cluster" in strat:
        return True
    if getattr(pos, "basket_leg", False):
        return True
    if getattr(pos, "cluster_box", ""):
        return True
    return False
