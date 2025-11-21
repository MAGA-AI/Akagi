# mjai_bot/akagi_policy.py — Final Layer Enhanced (cleanup + fixes)
# Adds: (A) Opponent riichi timing, (B) discard tempo threat, (C) re-tenpai EV,
# (D) draw-rate handling, (E) psychological tilt, (F) hidden dora expect,
# (G) dynamic self-adjustment hooks — on top of previous 15 features.
# This version applies: (1) safe normalization at EV entry points,
# (2) speed-tag fallback boost for win-rate when deps are absent,
# (3) a first-pass KAN EV and decision wiring,
# (4) minor cleanup of unused imports/helpers,
# (5) **Hanchan end-conditions aware EV** (Mahjong Soul style): agariyame/tenpaiyame,
#     nishi-iri (west-in) target, sudden-death after west, and tobi handling (light).

from dataclasses import dataclass
import os
from typing import List, Optional, Dict

# ===== Optional deps (safe fallbacks) =====
try:
    from .akagi_ev_patch_min import (
        goal_driven_override,
        speed_adjusted_winrate,
        calibrated_probability, should_push
    )
except Exception:
    def goal_driven_override(ev, action, ctx, bp, win, ukeire): return ev
    def ev_with_kyotaku_honba(ev, win, ctx): return ev
    def speed_adjusted_winrate(win, ctx): return win
    def apply_safety_and_future_losses(lose_ev, ctx, safe_tiles_next=None): return lose_ev
    def calibrated_probability(p, a=1.0, b=0.0):
        try: return max(0.0, min(1.0, float(p)))
        except Exception: return 0.0
    def should_push(win_rate, deal_in_rate, ctx): return True

# ===== Env helpers =====
def _getf(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        return float(v) if v is not None else default
    except Exception:
        return default

def _geti(name: str, default: int) -> int:
    try:
        v = os.getenv(name)
        return int(v) if v is not None else default
    except Exception:
        return default

REACH_STICK_COST = 1000
TABLE_SPEED_MUL = _getf("AKAGI_TABLE_SPEED_MUL", 1.0)
SHAPE_RYANMEN_BONUS = 1.04
SHAPE_DEAD_SHANTEN_PENAL = 0.92
DORA_VIS_BONUS_PER = 0.01
RED_COUNT_BONUS_PER = 0.01
SAFETY_GOOD_BONUS = 0.96
TURN_LATE_DEF_PENAL = 1.06
EV_REACH_TOP_LEAD_MARGIN = 6000
EV_REACH_LEAD_UPWEIGHT = 1.03
INFO_LEAK_BETA = 1.0
DEFEND_VALUE_SCALAR = _getf("AKAGI_DEFEND_VALUE_SCALAR", 1.05)
TENPAI_VALUE_SCALE = 0.012
EV_LAST_ESCAPE_BONUS = _getf("AKAGI_LAST_ESCAPE_BONUS", 1.10)
EV_REACH_RISK_AVERSION = 0.95
EV_FORBID_KAN_TOP_LEAD = True
EV_MODE = "final_layer"

# ===== Placement EV (順位EV) settings =====
PLACEMENT_WEIGHT = _getf("AKAGI_PLACEMENT_WEIGHT", 0.35)
UMA_TOP   = _getf("AKAGI_UMA_TOP", 15.0)
UMA_SECOND= _getf("AKAGI_UMA_SECOND", 5.0)
UMA_THIRD = _getf("AKAGI_UMA_THIRD", -5.0)
UMA_LAST  = _getf("AKAGI_UMA_LAST", -15.0)
UMA_POINT_UNIT = _getf("AKAGI_UMA_POINT_UNIT", 1000.0)  # converts UMA to points-equivalent
# Parent (OYA) specific placement tuning
OYA_PLACEMENT_WIN_MUL  = _getf("AKAGI_OYA_PLACEMENT_WIN_MUL", 1.08)
OYA_PLACEMENT_LOSS_MUL = _getf("AKAGI_OYA_PLACEMENT_LOSS_MUL", 1.02)
OYA_RENCHAN_PLACEMENT_K = _getf("AKAGI_OYA_RENCHAN_PLACEMENT_K", 800.0)

# ===== Hanchan end rules (configurable; Mahjong Soul-like defaults) =====
WEST_IN_TARGET_DEFAULT = _geti("AKAGI_WEST_IN_TARGET", 30000)  # set 40000 if using 35k start/40k target rooms
ALLOW_AGARIYAME_DEFAULT = bool(int(os.getenv("AKAGI_ALLOW_AGARIYAME", "1")))
ALLOW_TENPAIYAME_DEFAULT = bool(int(os.getenv("AKAGI_ALLOW_TENPAIYAME", "1")))
SUDDEN_DEATH_AFTER_WEST_DEFAULT = bool(int(os.getenv("AKAGI_SUDDEN_DEATH_AFTER_WEST", "1")))

# ===== Yaku feasibility (wind/dragon awareness) defaults =====
# Call lines without any certain yaku should be heavily discouraged.
# These thresholds softly gate EV instead of hard constraints, to stay robust vs upstream estimates.
CALL_NO_YAKU_WIN_MUL = _getf("AKAGI_CALL_NO_YAKU_WIN_MUL", 0.2)
CALL_NO_YAKU_BP_MUL  = _getf("AKAGI_CALL_NO_YAKU_BP_MUL", 0.85)
DAMA_NO_YAKU_WIN_MUL = _getf("AKAGI_DAMA_NO_YAKU_WIN_MUL", 0.4)
DAMA_NO_YAKU_BP_MUL  = _getf("AKAGI_DAMA_NO_YAKU_BP_MUL", 0.9)

# ===== Utility small funcs =====

def _lead_margin(my: int, others: List[int]) -> int:
    if not others: return 0
    best_other = max(others)
    return my - best_other

def _is_last(my: int, others: List[int]) -> bool:
    if not others: return False
    return my == min([my] + others)

def _table_threat(ctx) -> bool:
    return bool(ctx.riichi_declared_count >= 1 or ctx.opponent_threat)

def _clamp01(x: float) -> float:
    try:
        if x != x: return 0.0
        if x < 0.0: return 0.0
        if x > 1.0: return 1.0
        return x
    except Exception:
        return 0.0

def _remain_ratio(ctx) -> float:
    tl = max(0, int(ctx.turns_left))
    return max(0.0, min(1.0, tl / 18.0))

def _risk_budget(ctx) -> float:
    """点差・局進行・脅威・降り切り余裕に応じて放銃率側をスケーリングする係数。
    1.0 より大きい => よりビビり（ lose 側が重くなる）
    1.0 より小さい => より押し気味（ lose 側が軽くなる）
    """
    base = 1.0
    remain = _remain_ratio(ctx)  # 1.0=東1付近, 0.0=山の終盤
    lead = _lead_margin(ctx.my_score, ctx.other_scores)
    is_last = _is_last(ctx.my_score, ctx.other_scores)

    # --- 局の価値による大枠（既存ロジック） ---
    rn = max(1, int(getattr(ctx, "round_number", 1)))

    # 東1〜東2はわずかに押し寄り
    if rn <= 2:
        base *= 0.98

    # 南場以降＆ラス目ならややビビり寄りに
    if rn >= 5 and is_last:
        base *= 1.05

    # 大きくトップ目なら守備寄り（点差に応じて）
    if lead >= 8000:
        base *= 1.06
    elif lead >= 4000:
        base *= 1.03

    # ……（中略：安全牌枚数やリーチ人数など既存の補正はそのまま残す）……

    # --- ここから追加：目的関数との連動 ---
    objective = _auto_objective(ctx)
    if objective == "avoid_last":
        # ラス回避モードでは放銃側をもう一段階重く見る
        base *= 1.08
    elif objective == "maintain":
        # トップ維持モードでもやや守備寄り
        base *= 1.03
    # go_top のときは base をそのまま（押し引きは既存ロジックどおり）

    # 過度な暴走・過度なチキンを防ぐためクリップ
    return max(0.90, min(1.35, base))




def _expected_ura_coef(ctx) -> float:
    return 1.0 + 0.02 * max(0.0, min(1.0, getattr(ctx, "ura_luck", 0.0)))

def _renchan_value(ctx) -> float:
    if not ctx.is_dealer:
        return 0.0
    cont = max(0.0, min(1.0, ctx.renchan_cont_prob))
    base = 0.00012 * ctx.oya_future_gain + 0.06 * cont

    # 点差・局進行による微調整
    lead = _lead_margin(ctx.my_score, ctx.other_scores)
    phase = 1.0 - _remain_ratio(ctx)  # 0=序盤, 1=終盤

    # ビハインド親番は連荘価値を少し上振れさせて押し寄り
    if lead < 0:
        base *= 1.15

    # 大トップかつ終盤の親番は「無理連荘」をやや抑制
    if lead > 12000 and phase > 0.6:
        base *= 0.75

    return base

def _capital_cost(ctx) -> int:
    frag = 1.0
    if ctx.is_oras and ctx.riichi_sticks_on_table >= 2:
        frag *= 1.25
    return int(REACH_STICK_COST * frag)

# === Unified probability helpers and soft switches ===
def _prob_affine(p, mul=1.0, add=0.0, lo=0.0, hi=1.0):
    try:
        p = (0.0 if p != p else float(p)) * mul + add
    except Exception:
        return lo
    if p < lo: return lo
    if p > hi: return hi
    return p

# small: normalization at EV entry

def _normalize_core(ctx):
    win  = _clamp01(float(getattr(ctx, "win_rate", 0.0) or 0.0))
    lose = _clamp01(float(getattr(ctx, "deal_in_rate", 0.0) or 0.0))
    bp   = max(1000.0, float(getattr(ctx, "basepoint", 0.0) or 0.0))
    return win, lose, bp


def _soft_defend_scale(defend_value: float, keep_value: float, s: float = 0.15) -> float:
    x = max(0.0, float(defend_value) - float(keep_value))
    denom = max(1e-6, keep_value + 1e-6)
    try:
        import math
        return 1.0 - s * (1.0 - math.exp(-x / denom))
    except Exception:
        return 1.0 if x <= 0 else (1.0 - s)


def _apply_table_bonus_to_bp(bp: float, ctx) -> float:
    try:
        bonus = 0.0
        rs = max(0, int(getattr(ctx, "riichi_sticks_on_table", 0)))
        hb = max(0, int(getattr(ctx, "honba_count", 0)))
        wr = max(0.0, min(1.0, float(getattr(ctx, "win_rate", 0.0))))
        if rs: bonus += 1000.0 * wr
        if hb: bonus += 300.0 * wr * hb
        return float(bp) + bonus
    except Exception:
        return bp


def _step_gain(effective_bp: float, need_bp: float, turns_left: int) -> float:
    try:
        gap = max(0.0, float(need_bp) - float(effective_bp))
        k = 0.0025 * (1.0 + max(0, 5 - min(5, int(turns_left))))
        return 1.0 + k * gap
    except Exception:
        return 1.0


def _goal_pressure(ctx, effective_bp: float) -> float:
    tbl = getattr(ctx, "required_bp_table", None) or {}
    need_top = tbl.get("top", getattr(ctx, "required_points_for_top", 0) or 0)
    need_second = tbl.get("second", getattr(ctx, "required_points_for_next_rank", 0) or 0)
    turns = max(0, int(getattr(ctx, "turns_left", 0)))
    g = 1.0
    if need_top:
        g *= _step_gain(effective_bp, need_top, turns)
    if need_second:
        g *= (0.5 * _step_gain(effective_bp, need_second, turns) + 0.5)
    return g

# ===== Context dataclass =====
from dataclasses import dataclass
from typing import List, Optional, Dict

@dataclass
class PolicyContext:
    # Scores
    my_score: int
    other_scores: List[int]
    player_id: int = 0
    is_oras: bool = False
    is_dealer: bool = False
    remaining_rounds: int = 4
    dealer_index: int = 0
    my_index: int = 0

    # ★ 局番号 (東1=1, 東2=2, 東3=3, 東4=4, 南1=5, 南2=6, 南3=7, 南4=8 くらいの想定)
    round_number: int = 1

    # Table status
    riichi_declared_count: int = 0
    opponent_threat: bool = False
    last_discard_is_yakuhai: bool = False
    turns_left: int = 12
    table_tsumogiri_streak: int = 0

    # ★ New: リーチ巡目情報（例: [6, 10]）
    riichi_turn_numbers: Optional[List[int]] = None

    # ★ New: 直近の捨て牌情報（危険読み用）
    # 例: [{"player": 1, "tile": "5m", "is_tsumogiri": True, "is_yakuhai": False, "is_terminal": False}, ...]
    last_discards: Optional[List[Dict[str, object]]] = None

    # Win model
    win_rate: float = 0.18
    deal_in_rate: float = 0.07
    tempai_rate: float = 0.45
    basepoint: float = 2600.0
    tsumo_rate: Optional[float] = None
    ron_rate: Optional[float] = None

    # Shape / safety
    is_ryanmen: bool = True
    shanten: int = 1
    safety_score: float = 0.5
    genbutsu_count: int = 3
    suji_count: int = 6
    wall_info: float = 0.0

    # Dora / red / waits
    dora_visible_count: int = 0
    red_count: int = 0
    good_wait_quality: float = 0.0
    wait_tile_count: float = 2.0
    ukeire_tiles: int = 8
    ukeire_risk_gradient: float = 0.0

    # ★ New: シャンテンの「質」 (0〜1, 良形・変化の多さ)
    shanten_quality: float = 0.0

    # ★ New: 受け入れ改善枚数・良形化ポテンシャル
    improve_tiles: int = 0              # 良形への変化枚数
    ryanmen_potential: float = 0.0      # リャンメン化の確率 (0〜1)
    max_hand_bp: int = 0                # 最大打点見込み（満貫なら8000など）

    # Chitoi-related
    is_chitoi: bool = False
    chitoi_tanki_class: Optional[str] = None
    chitoi_tanki_improve: float = 0.0
    chitoi_tanki_visible: int = 0
    chitoi_tanki_dora_touch: float = 0.0

    # Calling hints
    call_speed_gain: float = 0.0
    call_role_hint: Optional[Dict[str, float]] = None
    info_leak_penalty: float = 0.0
    opponent_reader_skill: float = 0.0

    # ★ New: 相手のタイプ（雀魂の長期スタatsを元に入れてもらう想定）
    opponent_aggressiveness: float = 0.0  # 0=超守備 1=超攻撃
    opponent_defense: float = 0.0         # 0=ザル 1=鉄壁

    # Oras/goal
    required_points_for_top: int = 0
    required_points_for_next_rank: int = 0
    required_bp_table: Optional[Dict[str, int]] = None
    oras_target_class: Optional[str] = None

    # Future/keep
    renchan_cont_prob: float = 0.0
    oya_future_gain: float = 0.0
    stasis_index: float = 0.0

    # ★ New: 引き分け（流局）率の粗い推定 (0〜1)
    draw_rate: float = 0.0

    # Defense coverage
    safe_tiles_next: float = 1.0
    safe_tiles_next2: float = 0.5

    # ★ New: 現在見えている安全牌情報
    safe_suji_count: int = 0       # 有効なスジ本数
    no_suji_tiles: int = 0         # 手牌の「無スジ」枚数
    shared_safe_tiles: int = 0     # 複数家に通る安全牌の枚数
    total_safe_tiles: int = 0      # 残り巡目分、降り切るのに十分な安全牌枚数

    # Hidden dora expect / luck
    hidden_dora_expect: float = 0.0
    ura_luck: float = 0.0

    # Sticks / honba
    riichi_sticks_on_table: int = 0
    honba_count: int = 0

    # Misc upgrade
    next_turn_upgrade_if_dama: float = 0.0
    upgrade_prob_next2: float = 0.0

    # --- Endgame rules ---
    west_in_target: int = WEST_IN_TARGET_DEFAULT
    allow_agariyame: bool = ALLOW_AGARIYAME_DEFAULT
    allow_tenpaiyame: bool = ALLOW_TENPAIYAME_DEFAULT
    sudden_death_after_west: bool = SUDDEN_DEATH_AFTER_WEST_DEFAULT

    # --- Yaku feasibility hints (set by upstream hand parser) ---
    # seat_wind / round_wind: 0:E / 1:S / 2:W / 3:N
    seat_wind: Optional[int] = None
    round_wind: Optional[int] = None
    # Potential yaku availability signals (coarse, 0..1). Upstream should fill when plausible.
    yakuhai_seat_potential: float = 0.0   # can form seat wind triplet (or already have)
    yakuhai_round_potential: float = 0.0  # can form round wind triplet (or already have)
    yakuhai_dragon_potential: float = 0.0 # can form any dragon triplet
    tanyao_potential: float = 0.0
    honitsu_potential: float = 0.0
    toitoi_potential: float = 0.0
    # Calling the tile is likely an otakaze (value-less wind) — set by upstream when candidate call is on a non-value wind
    calling_otakaze: bool = False



# ===== Core effects =====

def _sigmoid(x: float) -> float:
    try:
        import math
        return 1.0 / (1.0 + math.exp(-x))
    except Exception:
        return 0.5


def _placement_prob_from_margin(margin: float, turns_left: int) -> float:
    """Coarse probability from point margin to cross by hand.
    Scale widens with more turns left. Margin in points.
    """
    scale = 2000.0 + 300.0 * max(0, min(12, int(turns_left)))
    return _clamp01(_sigmoid(margin / max(1.0, scale)))


def _approx_action_win(ctx: PolicyContext, action: str) -> float:
    """Very light win-prob proxy per action, for placement-side bonuses.
    Mirrors _estimate_delta_points adjustments without shape details.
    """
    win = _clamp01(float(getattr(ctx, "win_rate", 0.0) or 0.0))
    if action == "reach":
        return _clamp01(win)
    if action == "dama":
        return _clamp01(win * 0.8)
    if action == "call":
        return _clamp01(min(0.95, win * (0.9 + 0.25 * ctx.call_speed_gain)))
    if action == "kan":
        return _clamp01(win)
    return win

def _parent_value_boost(ctx: PolicyContext, win: float, bp: float):
    """親番の価値をEVに反映（連荘・壁テク・南4親の押し強化）"""
    if not ctx.is_dealer:
        return win, bp

    # 親番は基本押し強化（南3・南4はさらに強く）
    phase = 1.0 - _remain_ratio(ctx)  # 0=序盤 1=終盤
    boost = 1.06 + 0.08 * phase       # 終盤ほど押し強め

    # 南4親は世界が違う
    if ctx.is_oras:
        boost *= 1.10  # リーチ押し強化
        bp    *= 1.05  # 打点価値アップ（裏ドラも重く）

    # 連荘すれば順位が大幅改善する見込みなら強め
    cont = max(0.0, min(1.0, ctx.renchan_cont_prob))
    boost *= (1.0 + 0.10 * cont)

    return win * boost, bp


def _estimate_delta_points(ctx: PolicyContext, action: str) -> float:
    """Lightweight delta estimation reusing front multipliers.
    Not shape-accurate but consistent across actions for placement layer.
    """
    win = _clamp01(float(getattr(ctx, "win_rate", 0.0) or 0.0))
    lose = _clamp01(float(getattr(ctx, "deal_in_rate", 0.0) or 0.0))
    bp  = max(1000.0, float(getattr(ctx, "basepoint", 0.0) or 0.0))

    if action == "reach":
        reach_bonus = (1.3 if ctx.is_dealer else 1.2)
        win_term  = win * bp * reach_bonus
        lose_term = lose * bp * EV_REACH_RISK_AVERSION
        if ctx.is_dealer:
            win_term  *= OYA_PLACEMENT_WIN_MUL
            lose_term *= OYA_PLACEMENT_LOSS_MUL
        return win_term - lose_term

    if action == "dama":
        win_term  = (win * 0.8) * bp
        lose_term = (lose * 0.8) * bp * (EV_REACH_RISK_AVERSION - 0.1)
        if ctx.is_dealer:
            win_term  *= OYA_PLACEMENT_WIN_MUL
            lose_term *= OYA_PLACEMENT_LOSS_MUL
        return win_term - lose_term

    if action == "call":
        win_adj  = min(0.95, win * (0.9 + 0.25 * ctx.call_speed_gain))
        lose_adj = lose * (1.05 + 0.15 * (1.0 if _table_threat(ctx) else 0.0))
        win_term  = win_adj * bp * (0.75 + 0.1 * ctx.call_speed_gain)
        lose_term = lose_adj * bp * (EV_REACH_RISK_AVERSION - 0.05)
        if ctx.is_dealer:
            win_term  *= OYA_PLACEMENT_WIN_MUL
            lose_term *= OYA_PLACEMENT_LOSS_MUL
        return win_term - lose_term

    if action == "kan":
        win_term  = win * bp * 1.10
        lose_term = lose * bp * (EV_REACH_RISK_AVERSION + 0.02)
        if ctx.is_dealer:
            win_term  *= OYA_PLACEMENT_WIN_MUL
            lose_term *= OYA_PLACEMENT_LOSS_MUL
        return win_term - lose_term

    return 0.0



def _placement_ev_for_action(ctx: PolicyContext, action: str) -> float:
    """Approximate placement EV (UMA-based) from expected point delta of action.
    PLACEMENT_WEIGHT を局進行と点差・ラス目・流局率に応じて動的に調整する。
    """
    delta = _estimate_delta_points(ctx, action)
    my_future = ctx.my_score + int(delta)
    others = list(ctx.other_scores) if ctx.other_scores else []
    if not others:
        return 0.0
    best_other = max(others)
    worst_other = min(others)

    # --- UMA 期待値 ---
    p_top  = _placement_prob_from_margin(my_future - best_other, ctx.turns_left)
    p_last = 1.0 - _placement_prob_from_margin(my_future - worst_other, ctx.turns_left)
    p_last = _clamp01(p_last)

    mid_mass = _clamp01(1.0 - p_top - p_last)

    # 現在の想定順位に応じて「2着/3着」の比率を少し傾ける
    # my_future を基準に、上にいる人数/下にいる人数からざっくり位置を推定
    above = sum(1 for s in others if s > my_future)
    below = sum(1 for s in others if s < my_future)

    if above == 0:
        # ほぼトップ目: 中間は 2着寄り
        mid_uma = UMA_SECOND * 0.7 + UMA_THIRD * 0.3
    elif below == 0:
        # ほぼラス目: 中間は 3着寄り（ラス回避優先）
        mid_uma = UMA_SECOND * 0.3 + UMA_THIRD * 0.7
    else:
        # 中間ポジション: 2着/3着をほぼ等重み
        mid_uma = (UMA_SECOND + UMA_THIRD) * 0.5

    expected_uma = (
        p_top * UMA_TOP + mid_mass * mid_uma + p_last * UMA_LAST
    )

    # --- 動的 PLACEMENT_WEIGHT ---
    phase = 1.0 - _remain_ratio(ctx)  # 0=序盤, 1=終盤
    lead = _lead_margin(ctx.my_score, others)
    is_last = _is_last(ctx.my_score, others)

    # ベースは環境変数の PLACEMENT_WEIGHT を基準に、終盤ほど重くする
    dyn_w = PLACEMENT_WEIGHT * (0.9 + 0.6 * phase)

    # オーラスは順位EVをさらに重視
    if ctx.is_oras:
        dyn_w *= 1.1

    # 大トップはそこまで順位EVを重くしない（守備側で本体EVが効く）
    if lead > 15000:
        dyn_w *= 0.85
    # 大ラス目は順位EVをさらに重くしてラス回避を強調
    elif lead < -8000:
        dyn_w *= 1.1

    # 流局率が高い場では順位EV寄り（テンパイ料・着順意識）
    dyn_w *= (1.0 + 0.3 * max(0.0, min(1.0, ctx.draw_rate)))

    # 安全域にクリップ
    dyn_w = max(0.10, min(0.90, dyn_w))

    rn = max(1, int(getattr(ctx, "round_number", 1)))

    # 南3〜南4は順位EVをもう少し重く
    if rn >= 7:
        dyn_w *= 1.1


    placement_points = expected_uma * UMA_POINT_UNIT * dyn_w

    # Dealer (OYA) bonus for renchan prospects
    if ctx.is_dealer:
        approx_win = _approx_action_win(ctx, action)
        renchan_bonus = (
            OYA_RENCHAN_PLACEMENT_K
            * max(0.0, getattr(ctx, "renchan_cont_prob", 0.0))
            * approx_win
        )
        placement_points += renchan_bonus * dyn_w

    return placement_points


def _has_any_call_yaku(ctx: PolicyContext) -> bool:
    role = ctx.call_role_hint or {}
    # merge explicit potentials with role hints
    tanyao = max(ctx.tanyao_potential, role.get("tanyao", 0.0))
    honitsu = max(ctx.honitsu_potential, role.get("honitsu", 0.0))
    toitoi = max(ctx.toitoi_potential, role.get("toitoi", 0.0))
    yakuhai_any = max(ctx.yakuhai_seat_potential, ctx.yakuhai_round_potential, ctx.yakuhai_dragon_potential)
    return (tanyao >= 0.5) or (honitsu >= 0.5) or (toitoi >= 0.5) or (yakuhai_any >= 0.5)


def _is_first(ctx: PolicyContext) -> bool:
    return ctx.my_score > max(ctx.other_scores) if ctx.other_scores else True


def _rank_after_gain_is_first(ctx: PolicyContext, gain: float) -> bool:
    future = ctx.my_score + int(max(0.0, gain))
    best_other = max(ctx.other_scores) if ctx.other_scores else -10**9
    return future > best_other


def _endgame_adjust(ev: float, action: str, ctx: PolicyContext, effective_bp: float, win: float) -> float:
    """Mahjong Soul-like end-condition aware scaling.
    - Oras (S4) agariyame/tenpaiyame
    - West-in target pressure
    - Sudden-death after West-in (light)
    """
    mul = 1.0
    add = 0.0

    # Common quicks
    is_first = _is_first(ctx)
    first_after_gain = _rank_after_gain_is_first(ctx, effective_bp)
    can_cross_target_on_win = (ctx.my_score + int(max(0.0, effective_bp))) >= ctx.west_in_target

    # Oras logic
    if ctx.is_oras and ctx.is_dealer:
        # --- 南4親のトップ確定上がり ---
        target = ctx.west_in_target
        if target > 0:
            # この和了で確定トップならご褒美EV
            if (ctx.my_score + int(max(0.0, effective_bp))) >= target:
                mul *= 1.10   # 押し寄り
                add += 0.01 * (effective_bp / 1000.0)

        # agariyame: dealer wins and is 1st with target or more => game ends
        if ctx.allow_agariyame and first_after_gain and can_cross_target_on_win:
            mul *= 1.08  # winning lines favored
            if action == "reach":
                mul *= 1.02  # secure finish
        # tenpaiyame near exhaustive draw: keep tenpai to end
        if ctx.allow_tenpaiyame and first_after_gain and ctx.turns_left <= 2:
            add += 0.02 * max(0.0, min(1.0, ctx.tempai_rate)) * (effective_bp / 1000.0)
        # below target while leading: push to avoid west-in
        if is_first and not can_cross_target_on_win and ctx.turns_left <= 4:
            if action in ("reach", "call"):
                mul *= 1.04
            else:
                mul *= 0.98

    # Non-first: if this win can end by reaching target in oras, slightly favor
    if ctx.is_oras and (not is_first) and first_after_gain and can_cross_target_on_win:
        mul *= 1.05

    # West-in sudden death pressure (after oras): approximate by target proximity
    if (not ctx.is_oras) and ctx.sudden_death_after_west and ctx.west_in_target > 0:
        # encourage lines that cross target immediately
        if can_cross_target_on_win:
            mul *= 1.03

    return ev * mul + add


def _apply_table_speed(ctx: PolicyContext, win: float, bp: float):
    """場の速度タグを決める。
    - 早い巡目の複数リーチ
    - ツモ切り連打
    - 仕掛け速度 (call_speed_gain)
    """
    speed_tag = False

    # 基本: リーチ人数・残り巡数
    if ctx.riichi_declared_count >= 2 or ctx.turns_left <= 4:
        speed_tag = True

    # ツモ切り多い = 山が減っていて局進行が早い
    if ctx.table_tsumogiri_streak >= 3:
        speed_tag = True

    # 早い巡目のリーチが出ているか
    turns = ctx.riichi_turn_numbers or []
    if turns:
        earliest = min(turns)
        if earliest <= 6:
            speed_tag = True

    # 仕掛けで明らかに場が速いとき
    if getattr(ctx, "call_speed_gain", 0.0) >= 0.6:
        speed_tag = True

    return _clamp01(win), bp, speed_tag


# small: fallback when speed_adjusted_winrate is identity

def _speed_fallback_boost(win: float, ctx: PolicyContext, speed_tag: bool) -> float:
    if not speed_tag:
        return win
    boost = 1.0
    if ctx.is_ryanmen:
        boost *= 1.01
    if getattr(ctx, "call_speed_gain", 0.0) >= 0.5:
        boost *= 1.01
    return _clamp01(win * boost)


def _apply_wait_visibility(ctx: PolicyContext, win: float) -> float:
    return _clamp01(win * (0.9 + 0.2 * _remain_ratio(ctx)))


def _opponent_aware_lose(ctx: PolicyContext, lose: float) -> float:
    """相手のリーチ・手出し/ツモ切り・安全牌情報から放銃率を調整する。"""
    if not ctx.other_scores:
        return lose

    power = 0.0

    # 役牌リーチ or 直前の役牌手出し後のリーチなど（粗い近似）
    if ctx.last_discard_is_yakuhai:
        power += 0.02

    # リーチ人数で基本圧
    if ctx.riichi_declared_count >= 1:
        power += 0.06
    if ctx.riichi_declared_count >= 2:
        power += 0.05

    # リーチ巡目が早いほど危険寄り
    turns = ctx.riichi_turn_numbers or []
    if turns:
        earliest = min(turns)
        if earliest <= 6:
            power += 0.04
        elif earliest <= 9:
            power += 0.02

    # 無スジ枚数・スジ本数・共通安牌からの調整
    try:
        no_suji = max(0, int(getattr(ctx, "no_suji_tiles", 0)))
        safe_suji = max(0, int(getattr(ctx, "safe_suji_count", 0)))
        shared_safe = max(0, int(getattr(ctx, "shared_safe_tiles", 0)))
    except Exception:
        no_suji = safe_suji = shared_safe = 0

    # 無スジが多いほど危険（特に10枚以上は押しすぎ注意）
    if no_suji >= 10:
        power += 0.05
    elif no_suji >= 6:
        power += 0.03

    # スジ本数が少ないと危険、逆に多ければやや押しやすい
    if safe_suji < 4:
        power += 0.04
    elif safe_suji >= 8:
        power -= 0.02

    # 2家以上に通る安全牌がないときはかなり危険
    if shared_safe <= 1:
        power += 0.03

    # 直近の捨て牌情報から軽く補正（上流が埋めてくれたら効く）
    last_discards = ctx.last_discards or []
    if last_discards:
        recent = last_discards[-1]
        is_tsumogiri = bool(recent.get("is_tsumogiri", False))
        is_yakuhai = bool(recent.get("is_yakuhai", False))
        is_terminal = bool(recent.get("is_terminal", False))

        # 手出し役牌 or 端牌は攻めている・形が整っている可能性
        if (not is_tsumogiri) and (is_yakuhai or is_terminal):
            power += 0.02

    # 相手タイプによる補正（攻撃型は危険、守備型は少しマイルド）
    aggr = max(0.0, min(1.0, getattr(ctx, "opponent_aggressiveness", 0.0)))
    defe = max(0.0, min(1.0, getattr(ctx, "opponent_defense", 0.0)))
    power += 0.05 * aggr
    power -= 0.03 * defe

    return _clamp01(lose * (1.0 + power))




def _reach_components_bonus(ctx: PolicyContext) -> float:
    base = 1.0 + 0.02 * max(0.0, min(4.0, ctx.dora_visible_count)) + 0.02 * max(0.0, min(3.0, ctx.red_count))
    bonus = 0.0
    bonus += 0.01 * max(0.0, min(1.0, ctx.good_wait_quality))
    bonus += 0.008 * max(0.0, ctx.hidden_dora_expect)
    return base + bonus


def _future_keep_boost(ctx: PolicyContext, ev: float) -> float:
    return ev + 0.0005 * ctx.oya_future_gain

def _top_safety_buffer_adjust(ev: float, ctx: PolicyContext, bp: float, is_tenpai_line: bool = False) -> float:
    """
    トップ目での「安全牌1枚抱え」を少しだけ優遇する調整。
    - 南場・トップ目・小〜中打点のときだけ有効
    - 次巡以降の安全牌カバー coverage が 1 未満 (ほぼノーガード) の場合に EV を少し下げる。
      coverage >= 1 の選択肢はそのまま。
    """
    try:
        coverage = float(ctx.safe_tiles_next) + 0.7 * float(ctx.safe_tiles_next2)
    except Exception:
        coverage = 0.0

    # 一応 1枚分の安全牌があるならそのまま
    if coverage >= 0.9:
        return ev

    # トップ目でなければ無効
    if not _is_first(ctx):
        return ev

    lead = _lead_margin(ctx.my_score, ctx.other_scores)
    if lead <= 0:
        return ev

    # 東場ではまだ攻撃優先
    rn = max(1, int(getattr(ctx, "round_number", 1)))
    if rn <= 4:
        return ev

    # 打点が高い手はある程度リスクを取る
    try:
        bp_val = float(bp)
    except Exception:
        bp_val = 2000.0

    # 門前 3900 点相当までは「保険重視」、それ以上は基本そのまま
    if bp_val >= 4000.0 and not (ctx.is_oras and is_tenpai_line and lead > 0):
        return ev

    # 終盤ほど、一発放銃のコストが重い
    remain = _remain_ratio(ctx)
    stage = 1.0 - remain  # 0〜1
    stage = max(0.0, min(1.0, stage))

    # 基本ペナルティ 3〜10%
    base_penalty = 0.03 + 0.07 * stage

    # リードが大きいほど少し強くする（最大 +40%）
    lead_factor = 1.0 + min(0.4, max(0.0, (lead - 4000.0) / 20000.0))
    penalty = base_penalty * lead_factor

    factor = 1.0 - penalty
    factor = max(0.85, factor)  # 過度な弱体化はしない

    return ev * factor



def _auto_objective(ctx: PolicyContext) -> str:
    """
    局面から自動で「go_top / maintain / avoid_last」を切り替える。

    - 明確なラス目 → avoid_last
    - 南場・オーラスで 3着がラスと近い → avoid_last
    - 大きくトップ目 → maintain（無理に点棒を伸ばさない）
    - それ以外 → go_top（トップを素直に狙う）
    """
    others = ctx.other_scores or []
    if not others:
        return "go_top"

    all_scores = [ctx.my_score] + list(others)
    sorted_scores = sorted(all_scores, reverse=True)
    my_rank = sorted_scores.index(ctx.my_score) + 1  # 1〜4位
    top_score = sorted_scores[0]
    last_score = sorted_scores[-1]
    lead = _lead_margin(ctx.my_score, others)
    diff_from_last = ctx.my_score - last_score

    # 残りツモから終盤度を算出（東1付近 ≒1.0, 山の終わり ≒0.0）
    remain = _remain_ratio(ctx)

    # 明確なラス目
    if my_rank == 4:
        return "avoid_last"

    # 南場以降＋3着でラスと近い → 強ラス回避モード
    rn = max(1, int(getattr(ctx, "round_number", 1)))
    if rn >= 5 and my_rank == 3:
        # 序盤は3000点差以内、終盤は8000点差以内なら「ラスとほぼ一体」
        threshold = 3000 + 5000 * (1.0 - remain)
        if diff_from_last <= threshold:
            return "avoid_last"

    # かなり余裕のトップ目（トップ維持）
    if my_rank == 1 and lead >= 8000:
        return "maintain"

    # それ以外はトップ狙い
    return "go_top"



def _apply_goal_targeting(ev: float, action: str, ctx: PolicyContext, effective_bp: float, win: float) -> float:
    step = 1.0
    thresholds = [200, 500, 800, 1200, 2000, 3900, 5200, 8000, 12000, 16000]
    step_strength = 1.0 + 0.01 * max(0, 5 - min(5, ctx.turns_left))
    for th in thresholds:
        if effective_bp >= th:
            step *= (1.015 * step_strength)
        else:
            break
    step *= (1.0 + 0.03 * max(0.0, min(1.0, ctx.good_wait_quality))) * (0.9 + 0.1 * _remain_ratio(ctx))

    table = ctx.required_bp_table or {}
    need_top = table.get("top", ctx.required_points_for_top)
    need_second = table.get("second", ctx.required_points_for_next_rank)
    if ctx.is_oras:
        cls = (ctx.oras_target_class or "any")
        if cls == "mangan_tsmo" and effective_bp >= 8000: step *= 1.06
        if cls == "haneman_direct" and effective_bp >= 12000: step *= 1.08
        if cls == "baiman" and effective_bp >= 16000: step *= 1.10
    if need_second and need_second > 0:
        if effective_bp >= 0.9 * need_second: step *= 1.08
        elif effective_bp >= 0.7 * need_second: step *= 1.04
        else: step *= 0.96
    if ctx.is_oras and need_top and need_top > 0:
        if effective_bp >= 0.9 * need_top: step *= 1.08
        elif effective_bp < 0.6 * need_top: step *= 0.95
    step *= (0.9 + 0.2 * min(1.0, max(0.0, win)))
    step += _renchan_value(ctx)
    return ev * step * _goal_pressure(ctx, effective_bp)

def _tempai_noten_adjust(ev: float, ctx: PolicyContext, win_prob: float, is_tenpai_line: bool) -> float:
    """
    形式聴牌とノーテンの評価差を「局面依存」にする。

    - 序盤: 形式聴牌ボーナスはかなり弱く、クソ待ち形式聴牌はむしろマイナス寄り
    - 終盤(残りツモ少ない) / オーラスラス目 / オーラス一位でのテンパイ止め: 強くテンパイ優遇
    - win_prob が高いテンパイほどボーナスを大きく、低い場合はボーナス縮小 or ペナルティ
    """
    win_prob = _clamp01(win_prob)

    # 残りツモから局面ステージを算出
    # _remain_ratio: 序盤 ≒ 1.0 / 終盤 ≒ 0.0
    remain = _remain_ratio(ctx)
    stage = 1.0 - remain   # 序盤0.0 → 終盤1.0

    # テンパイ価値の基本スケール:
    #   序盤: TENPAI_VALUE_SCALE * 0.3
    #   終盤: TENPAI_VALUE_SCALE * 2.0 くらいまで上がる
    tenpai_scale = TENPAI_VALUE_SCALE * (0.3 + 1.7 * stage)

    # 「テンパイしないと死ぬ」ような状況を検出
    is_last = _is_last(ctx.my_score, ctx.other_scores)
    is_first = _is_first(ctx)
    must_tenpai = False

    # オーラスでラス目 → ノーテン終了は致命傷なのでテンパイをかなり優遇
    if ctx.is_oras and is_last:
        must_tenpai = True

    # オーラス親でテンパイ止めが効く場面（tenpaiyame）も優遇
    if ctx.is_oras and ctx.is_dealer and ctx.allow_tenpaiyame and is_first and ctx.turns_left <= 2:
        must_tenpai = True

    if must_tenpai:
        tenpai_scale *= 2.5  # ラス回避・トップ確定系のテンパイは非常に重くみる

    # --- テンパイ側の調整 ---
    if is_tenpai_line:
        # 序中盤で win_prob が極端に低い「形式だけのクソ待ち」は、
        # むしろ形維持・打点伸ばしのためにマイナス補正をかける。
        if (stage < 0.6) and (win_prob <= 0.12) and not must_tenpai:
            penalty = tenpai_scale * (0.5 - win_prob)  # 0.5付近から win_prob 分だけ差し引くイメージ
            return ev - penalty

        # 通常は win_prob が高いほどボーナスを増やす
        # （良形テンパイ＞悪形テンパイ）
        bonus = tenpai_scale * (0.25 + 0.75 * win_prob)
        return ev + bonus

    # --- ノーテン側の調整 ---
    # ノーテンペナルティは「終盤・テンパイ価値が高い局面」でだけ強く。
    noten_scale = tenpai_scale

    # 序盤はノーテンでもそこまでマイナスにしない（伸びしろ優先）
    if stage < 0.4 and not must_tenpai:
        noten_scale *= 0.3

    # 局が進むほど「ノーテンで流局を迎えるリスク」を重く見てペナルティ増加
    draw_pressure = stage  # 終盤ほど1に近づく
    loss = noten_scale * (0.2 + 0.8 * draw_pressure) * (0.5 + 0.5 * (1.0 - win_prob))

    return ev - loss


def _dynamic_scale(ctx: PolicyContext, action: str, ev: float) -> float:
    if action == "reach":   return ev * _getf("AKAGI_SCALE_REACH", 1.0)
    if action == "dama":    return ev * _getf("AKAGI_SCALE_DAMA", 1.0)
    if action == "call":    return ev * _getf("AKAGI_SCALE_CALL", 1.0)
    if action == "kan":     return ev * _getf("AKAGI_SCALE_KAN", 0.98)
    return ev


class ExpectedValueEngine:
    @staticmethod
    def _adjust_rates_by_shape_safety(ctx: PolicyContext, win: float, lose: float):
        # 形の基本補正
        if ctx.is_ryanmen:
            win *= SHAPE_RYANMEN_BONUS
        if ctx.shanten >= 2:
            win *= SHAPE_DEAD_SHANTEN_PENAL

        # ドラ・赤
        win *= (1.0 + ctx.dora_visible_count * DORA_VIS_BONUS_PER + ctx.red_count * RED_COUNT_BONUS_PER)

        # 将来改善確率（2巡先まで）
        win *= (1.0 + 0.02 * max(0.0, min(1.0, ctx.upgrade_prob_next2)))

        # 七対子系
        if ctx.is_chitoi:
            w = max(0, min(4, int(ctx.wait_tile_count)))
            win *= min(1.06, 0.74 + 0.08 * w)
            win *= (1.0 + 0.05 * max(0.0, min(1.0, ctx.chitoi_tanki_improve)))
            c = ctx.chitoi_tanki_class
            if c in ("honor","yakuhai"):
                win *= 1.08; lose *= 0.96
            elif c in ("terminal","edge"):
                win *= 1.03; lose *= 0.99
            win *= (1.0 + 0.03 * max(0, min(1, ctx.chitoi_tanki_dora_touch)))
            win *= (1.0 - 0.02 * max(0, min(3, ctx.chitoi_tanki_visible)))

        # 安全度・現物・筋
        safety_factor = (0.5 * ctx.safety_score) + (0.02 * ctx.genbutsu_count) + (0.01 * ctx.suji_count) + (0.1 * ctx.wall_info)
        if safety_factor > 0.6:
            lose *= SAFETY_GOOD_BONUS
        if ctx.turns_left <= 5:
            lose *= TURN_LATE_DEF_PENAL

        # 形の質（シャンテン質＆受け入れ・良形）を強めに反映
        if not ctx.is_chitoi:
            ukeire = max(0, ctx.ukeire_tiles)
            good_q = max(0.0, min(1.0, ctx.good_wait_quality))
            shan_q = max(0.0, min(1.0, ctx.shanten_quality))
            improve = max(0, ctx.improve_tiles)
            ryan_p = max(0.0, min(1.0, ctx.ryanmen_potential))

            # 受け入れが多い手は素直に勝率UP
            win *= (1.0 + 0.015 * min(12, ukeire))  # 12枚上限で+18%程度

            # 良形寄りのシャンテン質＋改善枚数＋リャンメン化ポテンシャル
            win *= (1.0 + 0.04 * good_q + 0.04 * shan_q)
            win *= (1.0 + 0.012 * min(10, improve))
            win *= (1.0 + 0.04 * ryan_p)

            # リスク勾配（押し返し過ぎ防止）
            win *= (1.0 - 0.02 * max(0.0, min(1.0, ctx.ukeire_risk_gradient)))

            # 序盤スリム化（安牌確保）補助：安全度が低くて受け入れがそこまででもないとき、
            # あまり攻めすぎないように微調整。
            remain = _remain_ratio(ctx)
            if remain > 0.6 and ctx.safe_tiles_next <= 1.0:
                win *= 0.98  # 序盤の無理攻めを少し抑える

        win = _apply_wait_visibility(ctx, win)
        return _clamp01(win), _clamp01(lose)


    @staticmethod
    def _reach_ev(ctx: PolicyContext) -> float:
        _win, _lose, _bp = _normalize_core(ctx)
        win = _win
        lose = _lose
        bp = _bp
        objective = _auto_objective(ctx)
        win, lose = ExpectedValueEngine._adjust_rates_by_shape_safety(ctx, win, lose)
        # 親番の押し補正
        win, bp = _parent_value_boost(ctx, win, bp)
        win = calibrated_probability(win, a=1.05, b=0.0)
        lose = calibrated_probability(lose, a=1.05, b=0.0)
        win = speed_adjusted_winrate(win, ctx)
        lose = _opponent_aware_lose(ctx, lose) * _risk_budget(ctx)
        bp  = max(1000.0, bp)
        reach_bonus = (1.3 if ctx.is_dealer else 1.2) * _reach_components_bonus(ctx)
        if _lead_margin(ctx.my_score, ctx.other_scores) >= EV_REACH_TOP_LEAD_MARGIN:
            reach_bonus *= EV_REACH_LEAD_UPWEIGHT
        bp = _apply_table_bonus_to_bp(bp, ctx)
        win, bp, _speed_tag = _apply_table_speed(ctx, win, bp)
        if _speed_tag:
            lose = _prob_affine(lose, mul=1.10)
            # fallback boost for win when deps are identity
            win = _speed_fallback_boost(win, ctx, True)
        if ctx.is_chitoi:
            w = max(0, min(4, int(ctx.wait_tile_count)))
            if w <= 1: lose *= 1.02
        win = _clamp01(win * _expected_ura_coef(ctx))

        gain = win * bp * reach_bonus
        cost = lose * bp * EV_REACH_RISK_AVERSION

        # リーチ棒（自己負担）の資本コスト：-cap*(1 - win)
        cap = float(_capital_cost(ctx))
        ev = (gain - cost) - cap * (1.0 - win)

        if _is_last(ctx.my_score, ctx.other_scores):
            ev *= EV_LAST_ESCAPE_BONUS

        ev = _apply_goal_targeting(ev, "reach", ctx, bp * reach_bonus, win)
        ev = _endgame_adjust(ev, "reach", ctx, bp * reach_bonus, win)
        ev = goal_driven_override(ev, "reach", ctx, bp * reach_bonus, win, getattr(ctx, 'ukeire_tiles', 0))
        keep_value = win * bp
        coverage = ctx.safe_tiles_next + 0.7 * ctx.safe_tiles_next2
        defend_value = (lose * DEFEND_VALUE_SCALAR) * (1.2 if coverage <= 1.5 else 0.9)
        ev *= _soft_defend_scale(defend_value, keep_value)
        bad_wait = (ctx.wait_tile_count <= 1 or ctx.good_wait_quality <= 0.2)
        hardness = int(max(0, min(3, ctx.chitoi_tanki_visible))) if ctx.is_chitoi else 0
        if bad_wait and hardness > 0: ev *= (1.0 - 0.05*hardness)
        ev = _future_keep_boost(ctx, ev)
        ev -= 0.01 * ctx.stasis_index
        ev = _top_safety_buffer_adjust(ev, ctx, bp * reach_bonus, is_tenpai_line=True)
        ev = _dynamic_scale(ctx, "reach", ev)
        return ev

    @staticmethod
    def _dama_ev(ctx: PolicyContext) -> float:
        _win, _lose, _bp = _normalize_core(ctx)
        win = _win * 0.8
        lose = _lose * 0.8
        bp = _bp
        objective = _auto_objective(ctx)
        win, lose = ExpectedValueEngine._adjust_rates_by_shape_safety(ctx, win, lose)
        # 親番の押し補正
        win, bp = _parent_value_boost(ctx, win, bp)
        win = calibrated_probability(win, a=1.05, b=0.0)
        lose = calibrated_probability(lose, a=1.05, b=0.0)
        win *= (1.0 + 0.03 * max(0.0, min(1.0, ctx.next_turn_upgrade_if_dama)))
        win = speed_adjusted_winrate(win, ctx)
        lose = _opponent_aware_lose(ctx, lose) * _risk_budget(ctx)
        bp  = max(1000.0, bp)
        bp = _apply_table_bonus_to_bp(bp, ctx)
        win, bp, _speed_tag = _apply_table_speed(ctx, win, bp)
        if _speed_tag:
            lose = _prob_affine(lose, mul=1.10)
            win  = _speed_fallback_boost(win, ctx, True)
        if ctx.is_chitoi:
            if _lead_margin(ctx.my_score, ctx.other_scores) >= 4000: lose *= 0.95
            if int(ctx.wait_tile_count) >= 3: win *= 1.03
            if ctx.chitoi_tanki_class in ("honor","yakuhai"): win *= 1.05; lose *= 0.97
        bad_wait = (ctx.wait_tile_count <= 1 or ctx.good_wait_quality <= 0.2)
        hardness = int(max(0, min(3, ctx.chitoi_tanki_visible))) if ctx.is_chitoi else 0

        # --- Yaku feasibility guard for closed dama (no riichi yaku) ---
        if max(ctx.tanyao_potential, ctx.honitsu_potential, ctx.toitoi_potential,
               ctx.yakuhai_seat_potential, ctx.yakuhai_round_potential, ctx.yakuhai_dragon_potential) < 0.5:
            win *= DAMA_NO_YAKU_WIN_MUL
            bp  *= DAMA_NO_YAKU_BP_MUL

        gain = win * bp
        cost = lose * bp * (EV_REACH_RISK_AVERSION - 0.1)
        if _is_last(ctx.my_score, ctx.other_scores): gain *= (EV_LAST_ESCAPE_BONUS - 0.05)
        ev = gain - cost
        ev = _apply_goal_targeting(ev, "dama", ctx, bp, win)
        ev = _endgame_adjust(ev, "dama", ctx, bp, win)
        ev = goal_driven_override(ev, "dama", ctx, bp, win, getattr(ctx, 'ukeire_tiles', 0))
        ev = _tempai_noten_adjust(ev, ctx, win, is_tenpai_line=True)
        keep_value = win * bp
        coverage = ctx.safe_tiles_next + 0.7 * ctx.safe_tiles_next2
        defend_value = (lose * DEFEND_VALUE_SCALAR) * (1.2 if coverage <= 1.5 else 0.9)
        ev *= _soft_defend_scale(defend_value, keep_value)
        if bad_wait and hardness > 0: ev *= (1.0 + 0.02*hardness)
        ev = _future_keep_boost(ctx, ev)
        ev += 0.01 * ctx.stasis_index
        is_tenpai_line = (ctx.shanten == 0)
        ev = _top_safety_buffer_adjust(ev, ctx, bp, is_tenpai_line=is_tenpai_line)
        ev = _dynamic_scale(ctx, "dama", ev)
        return ev

    @staticmethod
    def _call_ev(ctx: PolicyContext) -> float:
        # --- 基本の正規化 ---
        _win, _lose, _bp = _normalize_core(ctx)
        win = _win
        lose = _lose
        bp = _bp

        # 目的（トップ取り/ラス回避/維持）
        objective = _auto_objective(ctx)

        # --- 速度側の補F正（鳴きの本質: 速度＋場の支配） ---
        # call_speed_gain: 0〜1 想定（速い鳴きほど1に近い）
        speed_gain = max(0.0, min(1.0, ctx.call_speed_gain))

        # 良形化・変化ポテンシャル
        ryanmen_pot = max(0.0, min(1.0, getattr(ctx, "ryanmen_potential", 0.0)))
        improve_tiles = max(0, int(getattr(ctx, "improve_tiles", 0)))
        improve_factor = min(1.0, improve_tiles / 10.0)

        # 速度補正: 東場は攻撃寄りに、終盤はやや控えめ
        phase = 1.0 - _remain_ratio(ctx)  # 0=序盤,1=終盤
        speed_mul = 0.92 + 0.30 * speed_gain + 0.06 * ryanmen_pot + 0.04 * improve_factor
        # 終盤は「押しすぎ防止」で少し弱める
        speed_mul *= (1.02 - 0.06 * phase)
        speed_mul = max(0.80, min(1.30, speed_mul))

        win = min(0.96, win * speed_mul)

        # --- 放銃率側の補正（場の脅威＋鳴き読まれやすさ） ---
        threat = _table_threat(ctx)
        opp_aggr = max(0.0, min(1.0, getattr(ctx, "opponent_aggressiveness", 0.0)))
        opp_def  = max(0.0, min(1.0, getattr(ctx, "opponent_defense", 0.0)))

        lose_mul = 1.02
        if threat:
            lose_mul += 0.08  # リーチ/高い仕掛けがいる場は基本高め

        # 攻撃的な相手ほど押し返してくるので少しだけ放銃率アップ
        lose_mul += 0.05 * opp_aggr
        # 守備力が高い相手には相対的に放銃しづらい（間合いが遠くなる）
        lose_mul -= 0.03 * opp_def

        # 安全度（自分の守備の良さ）で少しだけ下げる
        safety = max(0.0, min(1.0, getattr(ctx, "safety_score", 0.5)))
        lose_mul *= (1.02 - 0.05 * safety)

        lose_mul = max(0.90, min(1.30, lose_mul))
        lose = _lose * lose_mul

        # --- 役の有無（「役あり鳴き」と「役怪しい鳴き」の切り分け） ---
        has_yaku = _has_any_call_yaku(ctx)
        role = ctx.call_role_hint or {}
        tanyao  = max(ctx.tanyao_potential, role.get("tanyao", 0.0))
        honitsu = max(ctx.honitsu_potential, role.get("honitsu", 0.0))
        toitoi  = max(ctx.toitoi_potential, role.get("toitoi", 0.0))

        if not has_yaku:
            # ほぼ役が無い/危うい鳴きは**かなり**抑制
            win *= CALL_NO_YAKU_WIN_MUL
            bp  *= CALL_NO_YAKU_BP_MUL

        # ちゃんとした仕掛け系の評価アップ
        if tanyao >= 0.7:
            win *= 1.03
            bp  *= 1.02
        if honitsu >= 0.7:
            win *= 1.04
            bp  *= 1.06
        if toitoi >= 0.7:
            win *= 1.03
            bp  *= 1.04

        # オタ風ポンなど、価値の低い鳴きは少し抑える
        if getattr(ctx, "calling_otakaze", False):
            win *= 0.94
            bp  *= 0.96

        # --- 形・安全による既存補正 ---
        win, lose = ExpectedValueEngine._adjust_rates_by_shape_safety(ctx, win, lose)

        # --- 親番の価値（連荘・南場親など） ---
        win, bp = _parent_value_boost(ctx, win, bp)

        # --- キャリブレーション（0〜1の範囲内で滑らかに） ---
        win = calibrated_probability(win, a=1.03, b=0.0)
        lose = calibrated_probability(lose, a=1.05, b=0.0)

        # --- 卓の速度タグ（早い場では勝率↑・放銃率↑） ---
        win = speed_adjusted_winrate(win, ctx)
        win, bp, speed_tag = _apply_table_speed(ctx, win, bp)
        if speed_tag:
            # 速度場では押し合いになるので放銃率も上振れ
            lose = _prob_affine(lose, mul=1.08)
            win  = _speed_fallback_boost(win, ctx, True)

        # --- 相手別リスク・ラス回避バイアス ---
        lose = _opponent_aware_lose(ctx, lose) * _risk_budget(ctx)

        # 目的別（トップ取り/ラス回避/維持）で微調整
        if objective == "go_top":
            win *= 1.02
        elif objective == "avoid_last":
            lose *= 1.03
        elif objective == "maintain":
            # トップ目維持など: 勝率と放銃率を両方少し保守的に
            win *= 0.99
            lose *= 1.02

        # 打点側のボーナス（供託棒・本場を軽く加味）
        bp = _apply_table_bonus_to_bp(bp, ctx)

        # --- Gain / Cost ---
        gain = win * bp
        # リーチよりは若干リスク係数を弱める（EV_REACH_RISK_AVERSION - 0.05）
        cost = lose * bp * (EV_REACH_RISK_AVERSION - 0.05)

        # ラス目は上がりの価値を少し上振れ（ラス抜けボーナス）
        if _is_last(ctx.my_score, ctx.other_scores):
            gain *= (EV_LAST_ESCAPE_BONUS - 0.03)

        ev = gain - cost

        # 目標点・必要打点に応じたボーナス（トップ/2着に届く打点を優遇）
        ev = _apply_goal_targeting(ev, "call", ctx, bp, win)

        # 南場・オーラスでの終了条件（アガリ止め/テンパイ止め・西入）を反映
        ev = _endgame_adjust(ev, "call", ctx, bp, win)

        # 目標に対するゴール指向オーバーライド（局面依存の押し引き）
        ev = goal_driven_override(ev, "call", ctx, bp, win, getattr(ctx, "ukeire_tiles", 0))

        # テンパイ/ノーテン価値（鳴きテンパイ線を評価）
        is_tenpai_line = (ctx.shanten == 0)
        ev = _tempai_noten_adjust(ev, ctx, win, is_tenpai_line=is_tenpai_line)

        # 守備リソースと比較して「押しすぎ」をソフトに抑制
        keep_value = win * bp
        coverage = ctx.safe_tiles_next + 0.7 * ctx.safe_tiles_next2
        defend_value = (lose * DEFEND_VALUE_SCALAR) * (1.2 if coverage <= 1.5 else 0.9)
        ev *= _soft_defend_scale(defend_value, keep_value)

        # 連荘・次局の親番価値など未来EV
        ev = _future_keep_boost(ctx, ev)

        # 局の膠着度指数（stasis）: 動かしたい局面では少しだけ押し寄り
        ev += 0.01 * ctx.stasis_index
        ev = _top_safety_buffer_adjust(ev, ctx, bp, is_tenpai_line=is_tenpai_line)


        # 最後に action ごとのスケール（環境変数で微調整できる）
        ev = _dynamic_scale(ctx, "call", ev)

        return ev


    @staticmethod
    def _kan_ev(ctx: PolicyContext) -> float:
        # Generic KAN EV (暗槓/加槓/明槓の詳細は上流で判定できない前提の安全近似)
        _win, _lose, _bp = _normalize_core(ctx)
        win = _win
        lose = _lose
        bp = _bp
        # ベース調整：カンは打点上昇(裏抽選/手役加点), ただし場速度+情報開示で放銃率↑
        # 打点側: +10% 基本, 裏/赤/ドラ可視に応じて微増
        ura_boost = _expected_ura_coef(ctx)
        shape_boost = 1.0 + 0.01 * max(0.0, min(1.0, ctx.good_wait_quality))
        bp = _apply_table_bonus_to_bp(bp, ctx) * (1.10 * ura_boost * shape_boost)

        # 勝率: 形と安全で補正、さらにカンで場が速くなる想定を軽く加味
        win, lose = ExpectedValueEngine._adjust_rates_by_shape_safety(ctx, win, lose)
        # 親番の押し補正
        win, bp = _parent_value_boost(ctx, win, bp)
        win = calibrated_probability(win, a=1.05, b=0.0)
        lose = calibrated_probability(lose, a=1.05, b=0.0)
        win = speed_adjusted_winrate(win, ctx)
        # 速度タグ寄りの場では放銃率↑
        win, bp, speed_tag = _apply_table_speed(ctx, win, bp)
        if speed_tag:
            lose = _prob_affine(lose, mul=1.12)
            win  = _speed_fallback_boost(win, ctx, True)

        # カン固有: 情報開示/追加ツモ巡/対リーチ場での危険度
        danger_mul = 1.0
        if _table_threat(ctx):
            danger_mul *= 1.08
        if ctx.turns_left <= 6:
            danger_mul *= 1.03
        lose = _opponent_aware_lose(ctx, lose) * danger_mul * _risk_budget(ctx)

        gain = win * bp
        # カンの押し寄り ⇒ リスク係数少し上振れ
        cost = lose * bp * (EV_REACH_RISK_AVERSION + 0.02)

        ev = gain - cost
        ev = _apply_goal_targeting(ev, "kan", ctx, bp, win)
        ev = _endgame_adjust(ev, "kan", ctx, bp, win)
        ev = goal_driven_override(ev, "kan", ctx, bp, win, getattr(ctx, 'ukeire_tiles', 0))
        # カンはテンパイ線で使うことが多い: わずかに加点
        ev = _tempai_noten_adjust(ev, ctx, win, is_tenpai_line=True)
        keep_value = win * bp
        coverage = ctx.safe_tiles_next + 0.7 * ctx.safe_tiles_next2
        defend_value = (lose * DEFEND_VALUE_SCALAR) * (1.2 if coverage <= 1.5 else 0.9)
        ev *= _soft_defend_scale(defend_value, keep_value)
        ev = _future_keep_boost(ctx, ev)
        ev = _top_safety_buffer_adjust(ev, ctx, bp, is_tenpai_line=True)
        ev = _dynamic_scale(ctx, "kan", ev)
        return ev

    @staticmethod
    def decide(ctx: PolicyContext) -> Dict[str, float]:
        reach_ev = ExpectedValueEngine._reach_ev(ctx)
        dama_ev  = ExpectedValueEngine._dama_ev(ctx)
        call_ev  = ExpectedValueEngine._call_ev(ctx)
        kan_ev   = ExpectedValueEngine._kan_ev(ctx)

        # Placement EV bonuses
        reach_pev = _placement_ev_for_action(ctx, "reach")
        dama_pev  = _placement_ev_for_action(ctx, "dama")
        call_pev  = _placement_ev_for_action(ctx, "call")
        kan_pev   = _placement_ev_for_action(ctx, "kan")

        reach_total = reach_ev + reach_pev
        dama_total  = dama_ev  + dama_pev
        call_total  = call_ev  + call_pev
        kan_total   = kan_ev   + kan_pev

        allow_reach = reach_total > max(dama_total, call_total, kan_total)
        allow_pon = call_total > max(reach_total, dama_total, kan_total) and should_push(ctx.win_rate, ctx.deal_in_rate, ctx)
        allow_chi = allow_pon

        allow_kan = kan_total > max(reach_total, dama_total, call_total)
        if EV_FORBID_KAN_TOP_LEAD and _lead_margin(ctx.my_score, ctx.other_scores) >= EV_REACH_TOP_LEAD_MARGIN:
            allow_kan = False

        return {
            "allow_reach": bool(allow_reach),
            "allow_pon": bool(allow_pon),
            "allow_chi": bool(allow_chi),
            "allow_kan": bool(allow_kan),
            "expected_basepoint": float(ctx.basepoint),
            "threat": _table_threat(ctx),
            "oras": bool(ctx.is_oras),
            "eval_mode": EV_MODE,
            "reach_ev": float(reach_ev),
            "dama_ev": float(dama_ev),
            "call_ev": float(call_ev),
            "kan_ev": float(kan_ev),
            "reach_placement_bonus": float(reach_pev),
            "dama_placement_bonus": float(dama_pev),
            "call_placement_bonus": float(call_pev),
            "kan_placement_bonus": float(kan_pev),
            "reach_total": float(reach_total),
            "dama_total": float(dama_total),
            "call_total": float(call_total),
            "kan_total": float(kan_total),
        }
