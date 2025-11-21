# -*- coding: utf-8 -*-
from __future__ import annotations
from dataclasses import dataclass
from typing import List, Optional, Dict
import os
import logging
from .safety import SafetyContext, aggregate_danger, bucketize, Tile

log = logging.getLogger("akagi.last_avoid")

@dataclass
class TableState:
    round_wind: str
    honba: int
    kyotaku: int
    dealer: int              # 0..3
    turn: int                # 未使用（将来拡張）
    remaining_tiles: int
    scores: List[int]        # 0..3
    me: int
    riichi_flags: List[bool]
    rivers: Dict[int, List]  # [(tile,tsumogiri), ...]
    my_tiles: Optional[List[Tile]] = None
    dora_indicators: Optional[List[Tile]] = None
    riichi_early_turns: Optional[Dict[int, int]] = None  # actor->宣言順目

@dataclass
class MoveCandidate:
    tile: Tile
    kind: str                # 'discard', 'chi', 'pon', ...
    ev_point: float = 0.0
    danger_score: float = 0.0

@dataclass
class LastAvoidConfig:
    enabled: bool = True
    danger_threshold_high: float = float(os.getenv("AKAGI_LAST_AVOID_DANGER_HIGH", "0.5"))
    danger_threshold_low: float  = float(os.getenv("AKAGI_LAST_AVOID_DANGER_LOW", "0.8"))
    must_fold_point_diff: int    = int(os.getenv("AKAGI_LAST_AVOID_MUST_FOLD", "8000"))
    can_escape_point_diff: int   = int(os.getenv("AKAGI_LAST_AVOID_CAN_ESCAPE", "2000"))

def rank_order(scores: List[int]) -> List[int]:
    return sorted(range(4), key=lambda i: (-scores[i], i))

def placement(me: int, scores: List[int]) -> int:
    return rank_order(scores).index(me) + 1

def diff_to_above(me: int, scores: List[int]) -> int:
    order = rank_order(scores)
    me_pos = order.index(me)
    if me_pos == 0:
        return 0
    above = order[me_pos - 1]
    return max(0, scores[above] - scores[me])

def compute_global_risk(ts: TableState) -> float:
    risk = 0.0
    south = (ts.round_wind in ("S", "W", "N"))
    if south: risk += 0.8
    if ts.remaining_tiles <= 18: risk += 0.7
    riichi_n = sum(1 for f in ts.riichi_flags if f)
    if riichi_n >= 2: risk += 1.2
    elif riichi_n == 1: risk += 0.6
    if ts.dealer != ts.me and ts.riichi_flags[ts.dealer]:
        risk += 0.4
    return risk

def choose_with_last_avoid(
    mortal_candidates: List[MoveCandidate],
    ts: TableState,
    cfg: Optional[LastAvoidConfig] = None
) -> MoveCandidate:
    cfg = cfg or LastAvoidConfig()
    if not cfg.enabled:
        return max(mortal_candidates, key=lambda c: c.ev_point)

    plc = placement(ts.me, ts.scores)
    diff_up = diff_to_above(ts.me, ts.scores)
    global_risk = compute_global_risk(ts)

    can_escape = (plc == 4 and diff_up <= cfg.can_escape_point_diff)
    must_fold = (plc == 4 and diff_up >= cfg.must_fold_point_diff and global_risk >= 1.5)

    ctx = SafetyContext(
        riichi_flags=ts.riichi_flags,
        rivers=ts.rivers,
        my_index=ts.me,
        remaining_tiles=ts.remaining_tiles,
        dealer=ts.dealer,
        my_tiles=ts.my_tiles,
        dora_indicators=ts.dora_indicators,
        riichi_early_turns=ts.riichi_early_turns,
    )

    # 危険度を付与
    for c in mortal_candidates:
        if c.kind == "discard":
            c.danger_score = aggregate_danger(c.tile, ctx)
        else:
            c.danger_score = 0.0

    if must_fold:
        discards = [c for c in mortal_candidates if c.kind == "discard"]
        discards.sort(key=lambda c: (c.danger_score, -c.ev_point))
        best = discards[0]
        # log.info("[LAST-AVOID] FOLD plc=%d diff_up=%d risk=%.2f -> %s danger=%.2f(%s)",
        #          plc, diff_up, global_risk, best.tile, best.danger_score, bucketize(best.danger_score))
        return best

    danger_th = cfg.danger_threshold_high if (plc == 4 or global_risk >= 1.2) else cfg.danger_threshold_low
    discards = [c for c in mortal_candidates if c.kind == "discard"]
    safeish = [c for c in discards if c.danger_score <= danger_th]
    if safeish:
        best = max(safeish, key=lambda c: c.ev_point)
        # log.info("[LAST-AVOID] CTRL plc=%d diff_up=%d risk=%.2f th=%.2f -> %s danger=%.2f(%s) EV=%.3f",
        #          plc, diff_up, global_risk, danger_th, best.tile, best.danger_score, bucketize(best.danger_score), best.ev_point)
        return best

    discards.sort(key=lambda c: (c.danger_score, -c.ev_point))
    best = discards[0] if discards else max(mortal_candidates, key=lambda c: c.ev_point)
    # log.info("[LAST-AVOID] FORCE plc=%d diff_up=%d risk=%.2f -> %s danger=%.2f(%s) EV=%.3f",
    #          plc, diff_up, global_risk, best.tile, getattr(best, "danger_score", 0.0), bucketize(getattr(best, "danger_score", 0.0)), best.ev_point)
    return best
