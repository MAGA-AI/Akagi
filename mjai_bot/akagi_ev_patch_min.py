
# akagi_ev_patch_min.py
# Minimal, drop-in helpers to upgrade EV with goal-driven rank-up logic,
# honba/kyotaku effects, ukeire-speed, nouten penalty, and push-thresholds.
# Import this file and call the functions at the points noted in README.

from dataclasses import dataclass
import math

def _get(ctx, key, default=None):
    # ctx can be dict-like or object with attributes
    if isinstance(ctx, dict):
        return ctx.get(key, default)
    return getattr(ctx, key, default)

def kyotaku_honba_ev(ctx, win_rate: float) -> float:
    """Expected extra gain from kyotaku (riichi sticks) and honba.
    Add this to the EV *in the action where you can actually collect it*.
    Typically:
      - Riichi: you may not always collect kyotaku by ron-only lines,
                but winning collects all. Use win_rate multiplier.
      - Dama: if you can ron without consuming kyotaku (depends on your logic),
              also add here.
    """
    kyotaku = float(_get(ctx, "kyotaku_riichi_sticks", _get(ctx, "kyotaku", 0.0)))
    honba   = float(_get(ctx, "honba", 0.0))
    # 1000 per stick, 300 per honba tick expected (simplified).
    return win_rate * (kyotaku * 1000.0 + honba * 300.0)

def speed_gain(ctx, ukeire_tiles: int = None) -> float:
    """Simple speed proxy = (ukeire / 20) * sqrt(remaining_draws).
    Use as a multiplicative factor on raw win_rate pre- or post-shape adjustment.
    """
    if ukeire_tiles is None:
        ukeire_tiles = int(_get(ctx, "ukeire_tiles", 0))
    draws = int(max(0, _get(ctx, "turns_left", 0)))
    return (max(0, ukeire_tiles) / 20.0) * math.sqrt(max(1, draws))

def push_threshold(ctx) -> float:
    """Return minimum acceptable win/lose ratio to keep pushing.
    Tune these constants by log-based calibration later.
    """
    turns_left = int(_get(ctx, "turns_left", 18))
    is_parent  = bool(_get(ctx, "is_dealer", False))
    rank       = int(_get(ctx, "my_rank", 4))
    # Late stage?
    late = turns_left <= 5
    # Very late endgame adds pressure to get tenpai (noten penalty), lower threshold
    very_late = turns_left <= 2

    # baseline
    thr = 1.1
    if is_parent:
        thr = 1.0
    if late:
        thr -= 0.15
    if very_late:
        thr -= 0.2
    if rank == 4:
        thr -= 0.2  # last place can push a bit more
    return max(0.4, thr)

def nouten_future_risk(ctx, safe_tiles_next: int = None) -> float:
    """Rough future liability if you will run out of safe tiles shortly.
    Add to the 'lose' component in EV.
    """
    if safe_tiles_next is None:
        safe_tiles_next = int(_get(ctx, "safe_tiles_next", 0))
    turns_left = int(_get(ctx, "turns_left", 18))
    base = 0.0
    if turns_left <= 6 and safe_tiles_next <= 1:
        base += 800.0  # approximate noten or forced push cost
    if turns_left <= 3 and safe_tiles_next == 0:
        base += 1200.0
    return base

def goal_driven_override(ev: float, action: str, ctx, basepoint: float,
                         win_rate: float, ukeire_tiles: int = None) -> float:
    """Boost or damp EV based on required points to rank up and how this action helps.
    action: one of {'reach','dama','call','fold'} (free-form ok)
    basepoint: current hand basic points estimate (pre/reach)
    win_rate: current win probability for this action
    ukeire_tiles: optional override for ukeire
    """
    need = _get(ctx, "required_points_for_rank_up", None)
    if need is None:
        return ev
    # Reach tends to open routes for higher bp via ura/han
    reach_mult = 1.2 if action.lower().startswith("reach") else 1.0
    bp = max(1000.0, float(basepoint)) * reach_mult

    if ukeire_tiles is None:
        ukeire_tiles = int(_get(ctx, "ukeire_tiles", 0))
    draws = int(max(0, _get(ctx, "turns_left", 0)))

    # Crude probability to hit or exceed target this hand:
    # - proportional to win_rate
    # - better with more ukeire
    # - better with more time (draws)
    p_hit = min(1.0, win_rate * (0.3 + ukeire_tiles/20.0) * (1.0 + 0.05*draws))

    boost = 1.0
    if bp >= need:
        boost += 0.25
    else:
        # If under the required points, still boost a little if chance to improve exists
        shortfall = max(0.0, need - bp)
        # Larger boost if chance to overtake with ura/tsumo/honba exists
        boost += 0.15 * p_hit * (1.0 if shortfall <= 2000 else 0.6)

    # If action is 'fold' and we still need points, dampen
    if action.lower().startswith("fold"):
        boost *= 0.85

    return ev * boost

def calibrated_probability(p: float, a: float = 1.0, b: float = 0.0) -> float:
    """Logit calibration: p' = sigmoid(a*logit(p) + b).
    Keep a~1.1, b~0.0 as defaults, tune from logs later.
    """
    p = min(max(p, 1e-6), 1.0 - 1e-6)
    logit = math.log(p/(1.0-p))
    adj = a*logit + b
    return 1.0/(1.0 + math.exp(-adj))

def apply_safety_and_future_losses(lose_ev: float, ctx, safe_tiles_next: int = None) -> float:
    """Add future liabilities (noten/safe-tile starvation) into lose component."""
    return lose_ev + nouten_future_risk(ctx, safe_tiles_next)

def should_push(win_rate: float, lose_rate: float, ctx) -> bool:
    thr = push_threshold(ctx)
    ratio = win_rate / max(1e-6, lose_rate)
    return ratio >= thr

def ev_with_kyotaku_honba(base_ev: float, win_rate: float, ctx) -> float:
    return base_ev + kyotaku_honba_ev(ctx, win_rate)

def speed_adjusted_winrate(win_rate: float, ctx, ukeire_tiles: int = None) -> float:
    return min(1.0, max(0.0, win_rate * (1.0 + speed_gain(ctx, ukeire_tiles))))
