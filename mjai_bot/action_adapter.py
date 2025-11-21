# mjai_bot/action_adapter.py
# -*- coding: utf-8 -*-
"""
Decision -> Bridge (Akagi) action dictionary adapter.
Conforms to the Bridge's expected tile notation and action keys.
"""
from typing import Dict, Any, Optional, List
from .majiang_ai_port import is_red5, to_base_tile

# Bridge準拠の牌表記（赤5は 5mr/5pr/5sr、字牌は E/S/W/N/P/F/C）
Z_TO_LETTER = {'z1':'E','z2':'S','z3':'W','z4':'N','z5':'P','z6':'F','z7':'C'}

def to_bridge_tile(t: str) -> str:
    # すでに Bridge 形式ならそのまま
    if t in ('E','S','W','N','P','F','C') or len(t)==3 and t in ('5mr','5pr','5sr'):
        return t
    # 赤5（m5r/p5r/s5r）→ 5mr/5pr/5sr
    if is_red5(t):
        return f"5{t[0]}r"
    # 字牌 z1..z7 → E/S/W/N/P/F/C
    if t.startswith('z'):
        return Z_TO_LETTER.get(t, t)
    # 数牌 m/p/s + 1..9 → 1m/.. の並び（Bridgeは数字→スート順）
    if t[0] in ('m','p','s') and t[1].isdigit():
        return f"{t[1]}{t[0]}"
    return t

def to_bridge_tiles(tiles: List[str]) -> List[str]:
    return [to_bridge_tile(x) for x in tiles]

def to_akagi_action(decision, me_seat: int, last_discard_seat: Optional[int] = None) -> Dict[str, Any]:
    """
    decision: PlayerPolicy.Decision
    me_seat: 0..3
    last_discard_seat: 直近の出牌者（チー/ポン/大明槓/ロンの target 用）
    """
    t = decision.type

    if t == "discard":
        return {"type": "dahai", "actor": me_seat, "pai": to_bridge_tile(decision.tile)}

    if t == "riichi":
        # reach -> (続けて) dahai の2段で送る運用を推奨
        return {"type": "reach", "actor": me_seat}

    if t == "chi":
        taken = to_bridge_tile(decision.extra.get("taken")) if decision.extra and decision.extra.get("taken") else None
        consumed = to_bridge_tiles(decision.meld or [])
        return {"type": "chi", "actor": me_seat, "pai": taken, "consumed": consumed, "target": last_discard_seat}

    if t == "pon":
        taken = to_bridge_tile(decision.extra.get("taken")) if decision.extra and decision.extra.get("taken") else None
        consumed = to_bridge_tiles(decision.meld or [])
        return {"type": "pon", "actor": me_seat, "pai": taken, "consumed": consumed, "target": last_discard_seat}

    if t == "kan":
        kind = (decision.extra or {}).get("kind", "ankan")  # "ankan"|"kakan"|"daiminkan"
        if kind == "ankan":
            return {"type": "ankan", "actor": me_seat, "consumed": to_bridge_tiles(decision.meld or [])}
        if kind == "kakan":
            return {"type": "kakan", "actor": me_seat, "pai": to_bridge_tile(decision.tile)}
        if kind == "daiminkan":
            return {"type": "daiminkan", "actor": me_seat, "pai": to_bridge_tile(decision.tile), "target": last_discard_seat}
        return {"type": "pass", "actor": me_seat}

    if t == "tsumo":
        return {"type": "tsumo", "actor": me_seat}

    if t == "ron":
        return {"type": "ron", "actor": me_seat, "target": last_discard_seat}

    return {"type": "pass", "actor": me_seat}
