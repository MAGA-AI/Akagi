# mjai_bot/majiang_ai_port.py
# -*- coding: utf-8 -*-
"""
Port of key decision-making logic inspired by kobalab/majiang-ai to Python,
structured to be easily plugged into Akagi's mjai_bot/bot.py.

What you get:
- SuanPai: counts unseen tiles and provides Paishu (normalized remaining tiles)
- Paishu: normalized weights for lookahead (val/pop/push)
- PlayerPolicy: evaluates discard/call/riichi/kan decisions via shallow lookahead
  using normalized remaining tiles and situation-aware thresholds (push/fold)

How to wire with Akagi (example):
---------------------------------
from mjai_bot.majiang_ai_port import PlayerPolicy, SuanPai

class Bot(...):
    def __init__(self, ...):
        self.policy = PlayerPolicy()

    def choose_action(self, state):
        # 'state' should provide:
        # - my_hand: list[str] tiles like 'm1', 'p5', 's9', 'z1' or red 'm5r' etc.
        # - melds: my melds
        # - discards: dict[player_seat -> list[str]] discards per player
        # - open_melds: list[list[str]] all open melds on table
        # - doras: list[str] dora indicators (e.g. 'm4' means dora 'm5')
        # - round, dealer, scores, honba, kyoutaku
        # - legal_actions: dict with candidates for 'discard','chi','pon','kan','riichi','tsumo','ron'
        # - risk_info: {opponent_id: {'riichi':bool, 'threat':float}, ...}  # optional
        # - wall_remain: int  # tiles left in the wall
        action = self.policy.decide(state)
        return action

Notes:
- This file is self-contained; no external ML deps.
- Tile notation helpers in this file accept strings 'm/p/s' + '1..9', honors 'z1..z7'.
- Red 5 tiles can be written as 'm5r','p5r','s5r'. Internally we normalize to base 'm5','p5','s5' for counts.
- Tuning knobs are exposed at the top (see TUNABLES).
"""

from __future__ import annotations
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple
import math
import collections
import copy
import random
import os
import os, logging
AI_LOG = logging.getLogger("majiang_ai")
if not AI_LOG.handlers:
    h = logging.StreamHandler()
    fmt = logging.Formatter("AI[% (levelname)s ] % (message)s")
    h.setFormatter(fmt)
    AI_LOG.addHandler(h)
AI_LOG.setLevel(logging.INFO if os.getenv("AI_DEBUG") else logging.WARNING)


__all__ = ["PlayerPolicy", "Decision", "SuanPai", "Paishu", "to_base_tile", "is_red5"]

# =========================
# TUNABLES (safe defaults)
# =========================
TUNABLES = {
    # Lookahead controls
    "MAX_SHANTEN_LOOKAHEAD": 2,      # deep eval up to 2-shanten (>=3 uses simple eval)
    "WIDTH_BY_SHANTEN": {0: 1.00, 1: 0.85, 2: 0.72},  # scale by shanten breadth

    # Weights for eval composition
    "W_SHAPE_BASE": 1.0,             # shape/ukeire weight
    "W_SCORE_BASE": 0.32,            # score potential weight
    "W_DEFENSE_BASE": 0.52,          # defense/safety weight penalty

    # Riichi thresholds
    "RIICHI_MIN_EXPECTED_GAIN": 0.42,   # expected gain threshold for riichi (normalized)
    "RIICHI_ALLOW_BAD_WAIT_EARLY": True,

    # Push/fold thresholds (scaled by threat & round)
    "PUSH_THRESHOLD": 0.28,
    "FOLD_THRESHOLD": -0.30,

    # Kan policy
    "KAN_URADORA_BONUS": 0.10,       # bonus to expected value for closed kan (adds risk)
    "KAN_RISK_PENALTY": 0.07,

    # Furiten handling: zero the eval for waits that are dead
    "FURITEN_ZERO": True,

    # Dora/aka multipliers in simple eval
    "DORA_MULT": 0.25,
    "AKA5_MULT": 0.20,

    # Danger model (very light; Akagi may provide richer risk)
    "RIICHI_DANGER_PENALTY": 0.20,   # per tile danger unit when someone riichi
    "THREAT_SCALE": 1.10,             # scales external risk_info['threat']

    # Placement / Last-avoidance mode
    "PLACEMENT_MODE": True,
    "PLACEMENT_DIFF_BRACKETS": [2000, 5000],  # thresholds vs last-place diff
    "PLACEMENT_DEF_MULTS": [1.5, 1.25, 1.0],   # <=2000, <=5000, else
    "PLACEMENT_PUSH_ADDS": [0.12, 0.06, 0.0], # add to PUSH_THRESHOLD by bracket
    "PLACEMENT_GAIN_ADDS": [0.18, 0.07, 0.0], # add to RIICHI_MIN_EXPECTED_GAIN by bracket
    "PLACEMENT_LAST_AGGR": 0.08,              # when you are last: reduce gain need by this
    "SOUTH_DEF_BONUS": 0.07,                  # add to push threshold in South rounds
}

TUNABLES.update({
    # 残り局による守備寄せ（数字が大きいほど守備が強まる）
    "ENDGAME_ROUNDS_BRACKETS": [2, 4],        # 残り局数 <=2, <=4, else
    "ENDGAME_DEF_MULTS":       [1.6, 1.25, 1.0],
    "ENDGAME_PUSH_ADDS":       [0.10, 0.05, 0.0],
    "ENDGAME_GAIN_ADDS":       [0.10, 0.04, 0.0],

    # 放銃期待失点スケール
    "DEALIN_BASE_CHILD": 3900,  # 子に打ったときの基準損失（概算の期待値）
    "DEALIN_BASE_PARENT": 5800, # 親に打ったときの基準損失
    "DEALIN_THREAT_MULT": 1.0,  # 相手 threat の掛け目
    "DEALIN_CAP_FOR_LASSSAFE": 0.6,  # ラス転落し得る場面では危険罰則上限をこれ以上に引き上げる（大きい=重く罰する）

    # トップ/2着守備の強化
    "LEAD_SAFE_MARGIN": 6000,   # これ以上離れていて残り少ならかなり守る
    "LEAD_DEF_BONUS": 0.10,     # push threshold に加算する量

    # ラス目攻め緩和
    "LAST_NEED_PUSH_BONUS": 0.08,  # プッシュしやすく
    "LAST_RIICHI_NEED_DROP": 0.10, # リーチ必要期待値を下げる
})

# Detect AI style
AI_STYLE = os.getenv("AI_STYLE", "attack").lower()

if AI_STYLE == "attack":
    TUNABLES.update({
        "W_SCORE_BASE": 0.42,
        "W_DEFENSE_BASE": 0.38,
        "RIICHI_MIN_EXPECTED_GAIN": 0.33,
        "PUSH_THRESHOLD": 0.23,
        "RIICHI_DANGER_PENALTY": 0.16,
        "THREAT_SCALE": 0.9,
        "SOUTH_DEF_BONUS": 0.04,
    })
elif AI_STYLE == "defense":
    TUNABLES.update({
        "W_SCORE_BASE": 0.28,
        "W_DEFENSE_BASE": 0.58,
        "RIICHI_MIN_EXPECTED_GAIN": 0.46,
        "PUSH_THRESHOLD": 0.30,
        "RIICHI_DANGER_PENALTY": 0.24,
        "THREAT_SCALE": 1.20,
        "SOUTH_DEF_BONUS": 0.09,
    })
else:
    # balance (default)
    TUNABLES.update({
        "W_SCORE_BASE": 0.32,
        "W_DEFENSE_BASE": 0.52,
        "RIICHI_MIN_EXPECTED_GAIN": 0.42,
        "PUSH_THRESHOLD": 0.28,
        "RIICHI_DANGER_PENALTY": 0.20,
        "THREAT_SCALE": 1.10,
        "SOUTH_DEF_BONUS": 0.07,
    })


# =========================
# Tile utilities
# =========================
SUITS = ('m', 'p', 's')
HONORS = ('z',)
ALL_TILES: List[str] = [f"{s}{n}" for s in SUITS for n in range(1,10)] + [f"z{n}" for n in range(1,8)]

def is_honor(t: str) -> bool:
    return t.startswith('z')

def parse_tile(t: str) -> Tuple[str,int]:
    s = t[0]; n = int(t[1])
    return s, n

def is_red5(t: str) -> bool:
    return len(t) == 3 and t.endswith('r') and t[0] in SUITS and t[1] == '5'

def to_base_tile(t: str) -> str:
    """Strip red marker if present (m5r->m5)."""
    return f"{t[0]}5" if is_red5(t) else t

def tile_nexts(t: str) -> List[str]:
    s,n = parse_tile(t)
    if s in HONORS: return []
    res = []
    for d in (-2,-1,1,2):
        k = n + d
        if 1 <= k <= 9:
            res.append(f"{s}{k}")
    return res

def tile_neighbors(t: str) -> List[str]:
    s,n = parse_tile(t)
    if s in HONORS: return []
    res = []
    for d in (-1,1):
        k = n + d
        if 1 <= k <= 9:
            res.append(f"{s}{k}")
    return res

# =========================
# SuanPai & Paishu (normalized)
# =========================
class Paishu:
    """
    Normalized remaining tiles view used for lookahead.
    Guarantees sum over tiles equals practical draw capacity left in wall.
    """
    def __init__(self, counts: Dict[str,int], wall_remain: int):
        # raw unseen count by tile (base-tile indexing: 'm5' holds pool for 'm5'+'m5r')
        self._counts = counts
        self.wall_remain = max(0, wall_remain)
        # cache normalized values
        self._norm_cache: Dict[str,float] = {}
        self._recompute_norm()

    def _recompute_norm(self):
        total = sum(max(c,0) for c in self._counts.values())
        self._norm_cache.clear()
        if total <= 0 or self.wall_remain <= 0:
            for t in ALL_TILES:
                self._norm_cache[t] = 0.0
            return
        scale = float(self.wall_remain) / float(total)
        for t in ALL_TILES:
            c = max(self._counts.get(to_base_tile(t), 0), 0)
            self._norm_cache[t] = c * scale

    def val(self, t: str) -> float:
        """Return normalized remaining amount for tile t (base-tile address)."""
        return self._norm_cache.get(to_base_tile(t), 0.0)

    def pop(self, t: str):
        bt = to_base_tile(t)
        if bt in self._counts and self._counts[bt] > 0:
            self._counts[bt] -= 1
        if self.wall_remain > 0:
            self.wall_remain -= 1
        self._recompute_norm()

    def push(self, t: str):
        bt = to_base_tile(t)
        self._counts[bt] = self._counts.get(bt,0) + 1
        self.wall_remain += 1
        self._recompute_norm()

class SuanPai:
    """
    Count unseen tiles (wall + opponents' hands) from visible info (hand, discards, open melds, dora flips).
    """
    def __init__(self):
        self.reset()

    def reset(self):
        self.unseen: Dict[str,int] = {t: 4 for t in ALL_TILES}  # base tiles only
        self.red5_seen = {'m':0,'p':0,'s':0}

    def observe_initial(self, my_hand: List[str], dora_indicators: List[str]):
        self.see_tiles(my_hand)
        # Dora indicator consumes the indicator tile itself; the dora tile remains unseen
        self.see_tiles([to_base_tile(x) for x in dora_indicators])

    def see_tiles(self, tiles: List[str]):
        for t in tiles:
            bt = to_base_tile(t)
            if bt in self.unseen and self.unseen[bt] > 0:
                self.unseen[bt] -= 1
            if is_red5(t):
                self.red5_seen[t[0]] += 1

    def see_meld(self, tiles: List[str]):
        self.see_tiles(tiles)

    def get_paishu(self, wall_remain: int) -> Paishu:
        return Paishu(copy.deepcopy(self.unseen), wall_remain)

# =========================
# Hand / shanten helpers (simplified)
# =========================
def count_shanten_like(hand: List[str]) -> int:
    """
    Lightweight shanten proxy:
    - honors = isolated -> penalize
    - sequences & pairs count
    Returns 0..3 typical range; >=3 treated as 'far' (simple eval only).
    (This is a heuristic proxy; you can swap to a true xiangting routine if available.)
    """
    suits = {'m':[], 'p':[], 's':[], 'z':[]}
    for t in hand:
        suits[t[0]].append(int(t[1]) if t[0] != 'z' else int(t[1]))
    seq_like = 0
    pair = 0
    for s in ('m','p','s'):
        arr = sorted(suits[s])
        i=0
        while i < len(arr)-1:
            if arr[i+1]-arr[i] == 0:
                pair += 1; i += 2
            elif arr[i+1]-arr[i] in (1,2):
                seq_like += 1; i += 2
            else:
                i += 1
    pair += max(0, collections.Counter(suits['z']).most_common(1)[0][1]//2) if suits['z'] else 0
    # Map to rough shanten
    score = seq_like + pair*0.8
    if score >= 6: return 0
    if score >= 4: return 1
    if score >= 2: return 2
    return 3

def ukeire_candidates_after_discard(hand: List[str]) -> Dict[str,int]:
    """
    Very light ukeire estimator: for each potential receive tile, count formations improved.
    """
    cand = collections.Counter()
    for t in hand:
        for n in tile_neighbors(t) + tile_nexts(t):
            cand[to_base_tile(n)] += 1
    return dict(cand)

# =========================
# Risk model (lightweight)
# =========================
def tile_danger_basic(t: str, risk_info: Optional[Dict]=None) -> float:
    danger = 0.0
    if not risk_info: return danger
    for opp, info in risk_info.items():
        th = float(info.get('threat', 0.0)) * TUNABLES["THREAT_SCALE"]
        if info.get('riichi', False):
            th += TUNABLES["RIICHI_DANGER_PENALTY"]
        danger += th
    return danger

# =========================
# Player policy
# =========================
@dataclass
class Decision:
    type: str                     # 'discard'|'chi'|'pon'|'kan'|'riichi'|'tsumo'|'ron'|'pass'
    tile: Optional[str]=None
    meld: Optional[List[str]]=None
    extra: Optional[Dict]=None

class PlayerPolicy:
    def __init__(self):
        self.random = random.Random()

    def _rounds_left(self, state) -> int:
            # ざっくり推定。state に rounds_left が来ていればそれを使う
            rl = getattr(state, 'rounds_left', None)
            if isinstance(rl, int):
                return max(0, rl)
            # フォールバック：南場なら残2～4局程度で概算。壁残も軽く使える。
            wall = getattr(state, 'wall_remain', 70)
            # 壁 70 → 4局, 壁 30 → 1局 くらいの線形近似
            approx = int(max(1, round((wall / 70.0) * 4)))
            return approx

    def _expected_deal_in_loss(self, state, opp_is_parent: bool, opp_threat: float) -> float:
        base = TUNABLES["DEALIN_BASE_PARENT"] if opp_is_parent else TUNABLES["DEALIN_BASE_CHILD"]
        return base * (1.0 + TUNABLES["DEALIN_THREAT_MULT"] * max(0.0, opp_threat))

    def _placement_adjust(self, state):
        """
        既存の diff_to_last/順位 に加え、残局数も織り込む。
        戻り値: (def_mult, push_thr, gain_need)
        """
        try:
            if not TUNABLES.get('PLACEMENT_MODE', True):
                raise Exception('off')

            diff_last = getattr(state, 'diff_to_last', None)
            my_rank   = getattr(state, 'my_rank', None)
            diff_above = getattr(state, 'diff_to_above', None)  # 3位との差(ラス目用)など任意で渡せる

            push_thr = TUNABLES['PUSH_THRESHOLD']
            gain_need = TUNABLES['RIICHI_MIN_EXPECTED_GAIN']
            def_mult = 1.0

            # 南場ボーナス（既存）
            if getattr(state, 'round_phase', 'E') == 'S':
                push_thr += TUNABLES.get('SOUTH_DEF_BONUS', 0.0)

            # --- 残局数効果 ---
            rl = self._rounds_left(state)
            b0, b1 = TUNABLES["ENDGAME_ROUNDS_BRACKETS"]
            ridx = 0 if rl <= b0 else (1 if rl <= b1 else 2)
            def_mult *= TUNABLES["ENDGAME_DEF_MULTS"][ridx]
            push_thr += TUNABLES["ENDGAME_PUSH_ADDS"][ridx]
            gain_need += TUNABLES["ENDGAME_GAIN_ADDS"][ridx]

            # --- ラス目の攻め緩和 ---
            if my_rank == 4:
                push_thr = max(0.0, push_thr - TUNABLES.get("LAST_NEED_PUSH_BONUS", 0.08))
                gain_need = max(0.0, gain_need - max(
                    TUNABLES.get('PLACEMENT_LAST_AGGR', 0.08),
                    TUNABLES.get('LAST_RIICHI_NEED_DROP', 0.10)
                ))
                # 3位との差が小さければさらに押しやすく
                if isinstance(diff_above, (int, float)) and diff_above <= 3000:
                    push_thr = max(0.0, push_thr - 0.03)

            # --- 非ラス時のラス回避強化 ---
            if diff_last is not None and my_rank != 4:
                b0,b1 = TUNABLES['PLACEMENT_DIFF_BRACKETS']
                idx = 0 if diff_last <= b0 else (1 if diff_last <= b1 else 2)
                def_mult *= TUNABLES['PLACEMENT_DEF_MULTS'][idx]
                push_thr += TUNABLES['PLACEMENT_PUSH_ADDS'][idx]
                gain_need += TUNABLES['PLACEMENT_GAIN_ADDS'][idx]

            # --- トップ/2着での“守り切り” ---（残局少 & 十分リード）
            lead = getattr(state, 'lead_over_next', None)  # 次着との差（トップ/2着時に有効）
            if my_rank in (1,2) and isinstance(lead, (int,float)):
                if rl <= 2 and lead >= TUNABLES["LEAD_SAFE_MARGIN"]:
                    push_thr += TUNABLES["LEAD_DEF_BONUS"]
                    def_mult *= 1.15
                    gain_need += 0.08
            
            AI_LOG.info(
                "PLACEMENT: rl=%s rank=%s diff_last=%s diff_above=%s lead_next=%s -> def_mult=%.2f push_thr=%.2f gain_need=%.2f",
                getattr(state, 'rounds_left', None), getattr(state, 'my_rank', None),
                getattr(state, 'diff_to_last', None), getattr(state, 'diff_to_above', None),
                getattr(state, 'lead_over_next', None), def_mult, push_thr, gain_need
            )

            return def_mult, push_thr, gain_need
        except Exception:
            return 1.0, TUNABLES['PUSH_THRESHOLD'], TUNABLES['RIICHI_MIN_EXPECTED_GAIN']
    
    def _placement_adjust(self, state):
        """Return (def_mult, push_thr, gain_need_adj) according to placement mode."""
        try:
            if not TUNABLES.get('PLACEMENT_MODE', True):
                raise Exception('off')
            diff_last = getattr(state, 'diff_to_last', None)
            my_rank = getattr(state, 'my_rank', None)
            push_thr = TUNABLES['PUSH_THRESHOLD']
            gain_need = TUNABLES['RIICHI_MIN_EXPECTED_GAIN']
            def_mult = 1.0
            if getattr(state, 'round_phase', 'E') == 'S':
                push_thr += TUNABLES.get('SOUTH_DEF_BONUS', 0.0)
            if my_rank == 4:
                push_thr = max(0.0, push_thr - 0.05)
                gain_need = max(0.0, gain_need - TUNABLES.get('PLACEMENT_LAST_AGGR', 0.10))
            if diff_last is not None and my_rank != 4:
                b0,b1 = TUNABLES['PLACEMENT_DIFF_BRACKETS']
                idx = 0 if diff_last <= b0 else (1 if diff_last <= b1 else 2)
                def_mult *= TUNABLES['PLACEMENT_DEF_MULTS'][idx]
                push_thr += TUNABLES['PLACEMENT_PUSH_ADDS'][idx]
                gain_need += TUNABLES['PLACEMENT_GAIN_ADDS'][idx]
            AI_LOG.info(
                "PLACEMENT: rl=%s rank=%s diff_last=%s diff_above=%s lead_next=%s -> def_mult=%.2f push_thr=%.2f gain_need=%.2f",
                getattr(state, 'rounds_left', None), getattr(state, 'my_rank', None),
                getattr(state, 'diff_to_last', None), getattr(state, 'diff_to_above', None),
                getattr(state, 'lead_over_next', None), def_mult, push_thr, gain_need
            )
            return def_mult, push_thr, gain_need
        except Exception:
            return 1.0, TUNABLES['PUSH_THRESHOLD'], TUNABLES['RIICHI_MIN_EXPECTED_GAIN']
# ----- public entry -----
    def decide(self, state) -> Decision:
        """
        Expect 'state' object/dict with keys described in the module docstring.
        """
        my = list(state.my_hand)
        legal = state.legal_actions
        sp = SuanPai()
        sp.observe_initial(my, state.doras)
        # observe everyone discards/open melds
        for pl, disc in getattr(state, "discards", {}).items():
            sp.see_tiles(disc)
        for m in getattr(state, "open_melds", []):
            sp.see_meld(m)
        paishu = sp.get_paishu(getattr(state, "wall_remain", 70))

        # Tsumo/Ron auto
        if legal.get('ron'):     return Decision('ron')
        if legal.get('tsumo'):   return Decision('tsumo')

        # Kan decision (closed/add)
        kan = self._maybe_kan(my, legal, paishu, state)
        if kan: return kan

        # Riichi or discard
        riichi, discard = self._choose_discard_or_riichi(my, legal, paishu, state)
        if riichi: return riichi
        if discard: return discard

        # Calls (chi/pon) – speed vs risk

        call = self._maybe_call(legal, paishu, state)
        if call: return call

        return Decision('pass')

    # ----- core evals -----
    def _choose_discard_or_riichi(self, hand: List[str], legal, paishu: Paishu, state):
        def_mult, push_thr_eff, gain_need_eff = self._placement_adjust(state)
        shanten = count_shanten_like(hand)
        # Evaluate each discard: expected value composed of shape/score/defense
        best = (-1e9, None, None)  # (score, tile, features)
        ukeire_base = ukeire_candidates_after_discard(hand)

        unique_tiles = sorted(set(hand))
        for t in unique_tiles:
            after = list(hand); after.remove(t)
            eval_val, feats = self._eval_hand(after, paishu, state, shanten, ukeire_base)
            # danger penalty for discarding t
            danger = tile_danger_basic(t, getattr(state, "risk_info", None))

            # 放銃期待失点スケールの概算（最大脅威の相手に対して）
            risk_info = getattr(state, "risk_info", {}) or {}
            max_threat = 0.0; opp_is_parent = False
            for opp, info in risk_info.items():
                th = float(info.get('threat', 0.0)) * TUNABLES["THREAT_SCALE"]
                if info.get('riichi', False):
                    th += TUNABLES["RIICHI_DANGER_PENALTY"]
                if th > max_threat:
                    max_threat = th
                    opp_is_parent = bool(info.get('is_parent', False))  # state 側で入れられるなら使う

            exp_loss = self._expected_deal_in_loss(state, opp_is_parent, max_threat)
            loss_scale = 1.0 + (exp_loss / 8000.0)  # 8,000 基準でスケール（概算）

            # ラス転落が見えるときはさらに重く
            diff_last = getattr(state, 'diff_to_last', None)
            my_rank   = getattr(state, 'my_rank', None)
            if my_rank != 4 and isinstance(diff_last, (int,float)) and exp_loss >= max(0, diff_last):
                loss_scale = max(loss_scale, 1.0 + TUNABLES["DEALIN_CAP_FOR_LASSSAFE"])

            eval_val -= TUNABLES["W_DEFENSE_BASE"] * danger * def_mult * loss_scale
            if eval_val > best[0]:
                best = (eval_val, t, feats)

        best_score, best_tile, best_feats = best

        # Riichi decision (if legal)
        if legal.get('riichi'):
            allow_bad = TUNABLES["RIICHI_ALLOW_BAD_WAIT_EARLY"] and getattr(state, "wall_remain", 70) >= 30
            gain_need = gain_need_eff
            if (best_feats and best_feats.get('is_tenpai', False)
                and (best_feats.get('good_wait', False) or allow_bad)
                and best_score is not None and best_score >= gain_need):
                AI_LOG.info(
                "RIICHI? tenpai=%s good_wait=%s best_score=%s need=%.3f allow_bad=%s",
                bool(best_feats and best_feats.get('is_tenpai')),
                bool(best_feats and best_feats.get('good_wait')),
                ("{:.3f}".format(best_score) if isinstance(best_score, (int,float)) else str(best_score)),
                gain_need, allow_bad
            )

            # トップ/2着で残り少 & 十分リード → リーチは少し厳しく
            rl = self._rounds_left(state)
            lead = getattr(state, 'lead_over_next', None)
            if best_feats and best_feats.get('is_tenpai', False) and (lead is not None):
                if getattr(state, 'my_rank', None) in (1,2) and rl <= 2 and lead >= TUNABLES["LEAD_SAFE_MARGIN"]:
                    gain_need += 0.10
                    allow_bad = False  # クソ待ちは切る

            # ラス目は緩める
            if getattr(state, 'my_rank', None) == 4:
                gain_need = max(0.0, gain_need - TUNABLES.get("LAST_RIICHI_NEED_DROP", 0.10))

            if (best_feats and best_feats.get('is_tenpai', False)
                and (best_feats.get('good_wait', False) or allow_bad)
                and best_score is not None and best_score >= gain_need):
                return Decision('riichi', tile=best_tile, extra={'feats':best_feats}), None

        return None, Decision('discard', tile=best_tile, extra={'feats':best_feats, 'score':best_score})

    def _eval_hand(self, hand: List[str], paishu: Paishu, state, shanten_now: int, ukeire_base: Dict[str,int]):
        """
        Combine:
          - shape/ukeire (lookahead with Paishu for <= MAX_SHANTEN_LOOKAHEAD)
          - score potential (dora/red)
          - defensive context
        """
        # shape/ukeire
        shape_val, feats = self._eval_shape_with_lookahead(hand, paishu, state, shanten_now, ukeire_base)

        # score potential
        dora_bonus = 0.0
        dora_tiles = set(self._dora_tiles_from_indicators(getattr(state, "doras", [])))
        for t in hand:
            if to_base_tile(t) in dora_tiles:
                dora_bonus += TUNABLES["DORA_MULT"]
            if is_red5(t):
                dora_bonus += TUNABLES["AKA5_MULT"]

        score_val = dora_bonus

        total = (TUNABLES["W_SHAPE_BASE"] * shape_val
                 + TUNABLES["W_SCORE_BASE"] * score_val)

        return total, feats

    def _eval_shape_with_lookahead(self, hand: List[str], paishu: Paishu, state, shanten_now: int, ukeire_base: Dict[str,int]):
        feats = {'is_tenpai': False, 'good_wait': False}
        sh = count_shanten_like(hand)
        if sh == 0:
            feats['is_tenpai'] = True
            # simple wait quality: number of ukeire*paishu
            wait_quality = 0.0
            for rcv, base in ukeire_base.items():
                wait_quality += base * paishu.val(rcv)
            feats['good_wait'] = wait_quality >= 2.2
            return TUNABLES["WIDTH_BY_SHANTEN"].get(0,1.0) * (1.0 + wait_quality*0.2), feats

        if sh > TUNABLES["MAX_SHANTEN_LOOKAHEAD"]:
            # simple eval: sum of potential receives weighted by paishu
            s = 0.0
            for rcv, base in ukeire_base.items():
                s += base * paishu.val(rcv)
            return TUNABLES["WIDTH_BY_SHANTEN"].get(min(sh,2),0.72) * math.tanh(0.4*s), feats

        # Lookahead: for each useful receive, go forward one ply
        expv = 0.0
        width = TUNABLES["WIDTH_BY_SHANTEN"].get(sh, 0.72)
        for rcv, base in ukeire_base.items():
            w = paishu.val(rcv)
            if w <= 0: continue
            paishu.pop(rcv)
            # naive improvement: reduce shanten if rcv helps pattern
            sh_next = max(0, sh-1)
            gain = base * (1.0 + (0.25 if sh_next == 0 else 0.0))
            expv += w * math.tanh(0.3*gain)
            paishu.push(rcv)

        return width * expv, feats

    def _maybe_kan(self, hand: List[str], legal, paishu: Paishu, state):
        kans = legal.get('kan') or []
        best_gain = -1e9; best=None
        for k in kans:
            # closed/add kan effect: +ura bonus - risk
            gain = TUNABLES["KAN_URADORA_BONUS"] - TUNABLES["KAN_RISK_PENALTY"] * self._threat_level(state)
            if gain > best_gain:
                best_gain = gain; best = k
        if best and best_gain > 0:
            return Decision('kan', tile=best)
        return None

    def _maybe_call(self, legal, paishu: Paishu, state):
        # Chi/Pon for speed if far from tenpai and push-worthy
        calls = []
        for k in ('chi','pon'):
            if legal.get(k):
                for m in legal[k]:
                    calls.append(('chi' if k=='chi' else 'pon', m))
        if not calls: return None

        def_mult, push_thr, _ = self._placement_adjust(state)
        need_speed = False
        # ラス目 & 残局少 & 3位との差が小さい → 速度で取りに行く
        if getattr(state, 'my_rank', None) == 4:
            rl = self._rounds_left(state)
            diff_above = getattr(state, 'diff_to_above', None)
            if rl <= 3 and isinstance(diff_above, (int,float)) and diff_above <= 4000:
                need_speed = True

        # 脅威が低い & 押し閾値を満たす or 速度が勝ち筋
        if (self._threat_level(state) < push_thr) or need_speed:
            # choose call that increases ukeire the most
            best = (-1e9, None, None)
            for typ, meld in calls:
                inc = len(meld)  # proxy improvement
                score = inc * 0.2
                if score > best[0]:
                    best = (score, typ, meld)
            if best[1]:
                return Decision(best[1], meld=best[2])
        return None

    def _threat_level(self, state) -> float:
        tl = 0.0
        risk_info = getattr(state, "risk_info", None)
        if not risk_info: return tl
        for opp, info in risk_info.items():
            th = float(info.get('threat',0.0))
            if info.get('riichi', False): th += 1.0
            tl += th
        return tl / max(1.0, len(risk_info))

    # ----- Dora helpers -----
    def _dora_tiles_from_indicators(self, indicators: List[str]) -> List[str]:
        res = []
        for ind in indicators:
            s,n = ind[0], int(ind[1])
            if s == 'z':
                order = {1:2,2:3,3:4,4:5,5:6,6:7,7:1}
                res.append(f"z{order[n]}")
            else:
                nxt = n+1 if n<9 else 1
                res.append(f"{s}{nxt}")
        return res
