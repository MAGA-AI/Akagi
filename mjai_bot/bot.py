# -*- coding: utf-8 -*-
import json
import os
from mjai import Bot
from dataclasses import dataclass
from mjai.mlibriichi.state import PlayerState  # type: ignore
from .logger import logger
from .akagi_policy import PolicyContext, ExpectedValueEngine


# --- ラス回避 / 安全評価 ---
from .strategy.last_avoid import TableState, MoveCandidate, LastAvoidConfig, choose_with_last_avoid
from .strategy.safety import SafetyContext  # 型ヒントだけ使う

class AkagiBot(Bot):
    """
    This bot tracks game states and picks a discard via last-avoid safety layer.
    """
    def __init__(self):
        super().__init__()
        self.is_3p = False

        # --- 局面トラッキング ---
        self.__rivers = {0: [], 1: [], 2: [], 3: []}  # [(tile, tsumogiri), ...]
        self.__riichi_actors = set()
        self.__riichi_early_turns = {}  # actor -> 宣言時のざっくり順目カウンタ
        self.__dealer = 0
        self.__scores = [25000, 25000, 25000, 25000]
        self.__round_wind = "E"
        self.__honba = 0
        self.__kyotaku = 0
        self.__remaining_tiles_live = 0  # live ツモ枚数
        self.__discard_events = []
        self.__call_events = []
        self.__dora_indicators = []
        self.__cfg_last_avoid = LastAvoidConfig()
       # policy出力（UI層が読む想定）
        self.policy_allow_reach = True
        self.policy_allow_pon   = True
        self.policy_allow_chi   = True
        self.policy_allow_kan   = True
        # policy内部デフォルト（既存propertyが無い/未初期化時のフォールバック）
        self._policy_default_scores     = [25000, 25000, 25000, 25000]
        self._policy_default_player_id  = 0
        self._policy_current_basepoint  = 2600.0
        self._policy_is_oras            = False
        self._policy_opponent_threat    = False
        # 精密化に使う内部推定（自由に上書き可。既存property名と被らないよう _policy_ 接頭）
        self._policy_turns_left         = 12
        self._policy_is_ryanmen         = True
        self._policy_shanten            = 1
        self._policy_safety_score       = 0.5
        self._policy_genbutsu_count     = 3
        self._policy_suji_count         = 6
        self._policy_wall_info          = 0.0
        self._policy_red_count          = 0
        self._policy_dora_visible_count = 0
        self._policy_est_win_rate       = 0.18
        self._policy_est_deal_in_rate   = 0.07
        self._policy_est_tempai_rate    = 0.45
        self._policy_est_basepoint      = 2600.0
        self._policy_est_call_speed     = 1.0

        # ざっくり順目カウンタ（配牌後0、以後各打牌で+1）
        self.__turn_counter = 0

    # -------------------------
    # 思考
    # -------------------------
    def think(self) -> str:
        """
        Safety-first discard with last-avoid layer. Fallback: tsumogiri.
        """
        try:
            self.update_policy()
        except Exception as e:
            # フェイルセーフ：ポリシーで落ちないように
            pass
        if self.can_discard:
            try:
                # 候補: 手牌の各牌 + ツモ切り（必ず1つはある）
                candidates = list(dict.fromkeys(self.tehai_mjai))
                if self.last_self_tsumo and self.last_self_tsumo not in candidates:
                    candidates.append(self.last_self_tsumo)

                riichi_flags = [False, False, False, False]
                for a in self.__riichi_actors:
                    if 0 <= a <= 3:
                        riichi_flags[a] = True

                ts = TableState(
                    round_wind=self.__round_wind,
                    honba=self.__honba,
                    kyotaku=self.__kyotaku,
                    dealer=self.__dealer,
                    turn=self.__turn_counter,
                    remaining_tiles=self.__remaining_tiles_live,
                    scores=self.__scores[:],
                    me=self.player_id,
                    riichi_flags=riichi_flags,
                    rivers={k: v[:] for k, v in self.__rivers.items()},
                    my_tiles=self.tehai_mjai[:],
                    dora_indicators=self.__dora_indicators[:],
                    riichi_early_turns=self.__riichi_early_turns.copy(),
                )
                move_cands = [MoveCandidate(tile=c, kind="discard", ev_point=0.0) for c in candidates]
                best = choose_with_last_avoid(move_cands, ts, self.__cfg_last_avoid)
                return self.action_discard(best.tile)
            except Exception as _e:
                logger.warning(f"[LAST-AVOID] fallback to tsumogiri due to: {_e}")
                return self.action_discard(self.last_self_tsumo)
        else:
            return self.action_nothing()

    # -------------------------
    # イベント処理
    # -------------------------
    def react(self, input_str: str = None, input_list: list[dict] = None) -> str:
        try:
            if input_str:
                events = json.loads(input_str)
            elif input_list:
                events = input_list
            else:
                raise ValueError("Empty input")
            if len(events) == 0:
                raise ValueError("Empty events")

            for event in events:
                et = event["type"]

                if et == "start_game":
                    self.player_id = event["id"]
                    self.player_state = PlayerState(self.player_id)
                    self.is_3p = False
                    # リセット
                    self.__discard_events = []
                    self.__call_events = []
                    self.__dora_indicators = []
                    self.__rivers = {0: [], 1: [], 2: [], 3: []}
                    self.__riichi_actors = set()
                    self.__riichi_early_turns = {}
                    self.__scores = [25000, 25000, 25000, 25000]
                    self.__honba = 0
                    self.__kyotaku = 0
                    self.__round_wind = "E"
                    self.__remaining_tiles_live = 0
                    self.__turn_counter = 0

                if et == "start_kyoku":
                    # 3P 判定（既存ロジック）
                    if (
                        event["scores"][0] == 35000 and
                        event["scores"][1] == 35000 and
                        event["scores"][2] == 35000 and
                        event["scores"][3] == 0
                    ):
                        self.is_3p = True

                    # 局情報
                    self.__dealer = event.get("oya", self.__dealer)
                    self.__scores = event.get("scores", self.__scores)
                    self.__honba = event.get("honba", 0)
                    self.__kyotaku = event.get("kyotaku", 0)
                    self.__round_wind = event.get("bakaze", "E")
                    self.__rivers = {0: [], 1: [], 2: [], 3: []}
                    self.__riichi_actors = set()
                    self.__riichi_early_turns = {}
                    self.__turn_counter = 0

                    # 残り live ツモ枚数 初期化
                    init_4p = int(os.getenv("AKAGI_INIT_LIVE_TILES_4P", "70"))
                    init_3p = int(os.getenv("AKAGI_INIT_LIVE_TILES_3P", "83"))
                    self.__remaining_tiles_live = init_3p if self.is_3p else init_4p

                # dora 表示（カン後の新ドラ含む）を即時反映
                if et == "start_kyoku" or et == "dora":
                    self.__dora_indicators.append(event["dora_marker"])

                # ツモは live -1
                if et == "tsumo":
                    self.__remaining_tiles_live = max(0, self.__remaining_tiles_live - 1)

                if et == "dahai":
                    self.__discard_events.append(event)
                    actor = event["actor"]
                    pai = event["pai"]
                    tsumogiri = bool(event.get("tsumogiri", False))
                    self.__rivers.setdefault(actor, []).append((pai, tsumogiri))
                    # 打牌でざっくり順目+1
                    self.__turn_counter += 1

                if et in ["chi", "pon", "daiminkan", "kakan", "ankan"]:
                    self.__call_events.append(event)

                # カン時は live -1（補助ツモは王牌）
                if et in ["daiminkan", "kakan", "ankan"]:
                    self.__remaining_tiles_live = max(0, self.__remaining_tiles_live - 1)

                # 立直追跡（早い順目を記録）
                if et in ["reach", "reach_accepted"]:
                    actor = event["actor"]
                    self.__riichi_actors.add(actor)
                    self.__riichi_early_turns.setdefault(actor, self.__turn_counter)

                # 3P用の nukidora パッチ（既存）
                if et == "nukidora":
                    logger.debug(f"Event: {event}")
                    replace_event = {
                        "type": "dahai",
                        "actor": event["actor"],
                        "pai": "N",
                        "tsumogiri": self.last_self_tsumo == "N" and event["actor"] == self.player_id,
                    }
                    self.__discard_events.append(replace_event)
                    self.__rivers.setdefault(event["actor"], []).append(("N", False))
                    # 打牌扱いで順目+1
                    self.__turn_counter += 1
                    self.action_candidate = self.player_state.update(json.dumps(replace_event))
                    continue

                logger.debug(f"Event: {event}")
                self.action_candidate = self.player_state.update(json.dumps(event))

            # 自分のリーチ後、限定状況はそのままツモ切り（既存ロジック）
            if (
                self.self_riichi_accepted
                and not (self.can_agari or self.can_kakan or self.can_ankan)
                and self.can_discard
            ):
                return self.action_discard(self.last_self_tsumo)

            resp = self.think()
            return resp

        except Exception as e:
            logger.error(f"Exception: {str(e)}")
            logger.error("Brief info:")
            logger.error(self.brief_info())

        return json.dumps({"type": "none"}, separators=(",", ":"))

    # ============================================= #
    #                Custom Methods                 #
    # ============================================= #
    @dataclass
    class ChiCandidates:
        chi_low_meld: tuple[str, tuple[str, str]] = None
        chi_mid_meld: tuple[str, tuple[str, str]] = None
        chi_high_meld: tuple[str, tuple[str, str]] = None

    # 既存の副露探索メソッドはそのまま（省略せず残しています）
    def find_chi_candidates_simple(self) -> "AkagiBot.ChiCandidates":
        chi_candidates: AkagiBot.ChiCandidates = AkagiBot.ChiCandidates()
        color = self.last_kawa_tile[1]
        chi_num = int(self.last_kawa_tile[0])
        if (
            self.can_chi_high
            and f"{chi_num-2}{color}r" in self.tehai_mjai
            and f"{chi_num-1}{color}" in self.tehai_mjai
        ):
            consumed = (f"{chi_num-2}{color}r", f"{chi_num-1}{color}")
            chi_candidates.chi_high_meld = (self.last_kawa_tile, consumed)
        if (
            self.can_chi_high
            and f"{chi_num-2}{color}" in self.tehai_mjai
            and f"{chi_num-1}{color}r" in self.tehai_mjai
        ):
            consumed = (f"{chi_num-2}{color}", f"{chi_num-1}{color}r")
            chi_candidates.chi_high_meld = (self.last_kawa_tile, consumed)
        if (
            self.can_chi_high
            and f"{chi_num-2}{color}" in self.tehai_mjai
            and f"{chi_num-1}{color}" in self.tehai_mjai
        ):
            consumed = (f"{chi_num-2}{color}", f"{chi_num-1}{color}")
            chi_candidates.chi_high_meld = (self.last_kawa_tile, consumed)
        if (
            self.can_chi_mid
            and f"{chi_num-1}{color}r" in self.tehai_mjai
            and f"{chi_num+1}{color}" in self.tehai_mjai
        ):
            consumed = (f"{chi_num-1}{color}r", f"{chi_num+1}{color}")
            chi_candidates.chi_mid_meld = (self.last_kawa_tile, consumed)
        if (
            self.can_chi_mid
            and f"{chi_num-1}{color}" in self.tehai_mjai
            and f"{chi_num+1}{color}r" in self.tehai_mjai
        ):
            consumed = (f"{chi_num-1}{color}", f"{chi_num+1}{color}r")
            chi_candidates.chi_mid_meld = (self.last_kawa_tile, consumed)
        if (
            self.can_chi_mid
            and f"{chi_num-1}{color}" in self.tehai_mjai
            and f"{chi_num+1}{color}" in self.tehai_mjai
        ):
            consumed = (f"{chi_num-1}{color}", f"{chi_num+1}{color}")
            chi_candidates.chi_mid_meld = (self.last_kawa_tile, consumed)
        if (
            self.can_chi_low
            and f"{chi_num+1}{color}r" in self.tehai_mjai
            and f"{chi_num+2}{color}" in self.tehai_mjai
        ):
            consumed = (f"{chi_num+1}{color}r", f"{chi_num+2}{color}")
            chi_candidates.chi_low_meld = (self.last_kawa_tile, consumed)
        if (
            self.can_chi_low
            and f"{chi_num+1}{color}" in self.tehai_mjai
            and f"{chi_num+2}{color}r" in self.tehai_mjai
        ):
            consumed = (f"{chi_num+1}{color}", f"{chi_num+2}{color}r")
            chi_candidates.chi_low_meld = (self.last_kawa_tile, consumed)
        if (
            self.can_chi_low
            and f"{chi_num+1}{color}" in self.tehai_mjai
            and f"{chi_num+2}{color}" in self.tehai_mjai
        ):
            consumed = (f"{chi_num+1}{color}", f"{chi_num+2}{color}")
            chi_candidates.chi_low_meld = (self.last_kawa_tile, consumed)
        return chi_candidates

    def find_chi_consume_simple(self) -> list[list[str]]:
        chi_candidates = []
        color = self.last_kawa_tile[1]
        chi_num = int(self.last_kawa_tile[0])
        tehai_mjai = self.tehai_mjai
        if (
            self.can_chi_high
            and f"{chi_num-2}{color}r" in tehai_mjai
            and f"{chi_num-1}{color}" in tehai_mjai
        ):
            chi_candidates.append([f"{chi_num-2}{color}r", f"{chi_num-1}{color}"])
        if (
            self.can_chi_high
            and f"{chi_num-2}{color}" in tehai_mjai
            and f"{chi_num-1}{color}r" in tehai_mjai
        ):
            chi_candidates.append([f"{chi_num-2}{color}", f"{chi_num-1}{color}r"])
        if (
            self.can_chi_high
            and f"{chi_num-2}{color}" in tehai_mjai
            and f"{chi_num-1}{color}" in tehai_mjai
        ):
            chi_candidates.append([f"{chi_num-2}{color}", f"{chi_num-1}{color}"])
        if (
            self.can_chi_mid
            and f"{chi_num-1}{color}r" in tehai_mjai
            and f"{chi_num+1}{color}" in tehai_mjai
        ):
            chi_candidates.append([f"{chi_num-1}{color}r", f"{chi_num+1}{color}"])
        if (
            self.can_chi_mid
            and f"{chi_num-1}{color}" in tehai_mjai
            and f"{chi_num+1}{color}r" in tehai_mjai
        ):
            chi_candidates.append([f"{chi_num-1}{color}", f"{chi_num+1}{color}r"])
        if (
            self.can_chi_mid
            and f"{chi_num-1}{color}" in tehai_mjai
            and f"{chi_num+1}{color}" in tehai_mjai
        ):
            chi_candidates.append([f"{chi_num-1}{color}", f"{chi_num+1}{color}"])
        if (
            self.can_chi_low
            and f"{chi_num+1}{color}r" in tehai_mjai
            and f"{chi_num+2}{color}" in tehai_mjai
        ):
            chi_candidates.append([f"{chi_num+1}{color}r", f"{chi_num+2}{color}"])
        if (
            self.can_chi_low
            and f"{chi_num+1}{color}" in tehai_mjai
            and f"{chi_num+2}{color}r" in tehai_mjai
        ):
            chi_candidates.append([f"{chi_num+1}{color}", f"{chi_num+2}{color}r"])
        if (
            self.can_chi_low
            and f"{chi_num+1}{color}" in tehai_mjai
            and f"{chi_num+2}{color}" in tehai_mjai
        ):
            chi_candidates.append([f"{chi_num+1}{color}", f"{chi_num+2}{color}"])
        return chi_candidates

    def find_pon_consume_simple(self) -> list[list[str]]:
        pon_candidates = []
        if self.last_kawa_tile[0] == "5" and self.last_kawa_tile[1] != "z":
            if self.tehai_mjai.count(self.last_kawa_tile[:2]) >= 2:
                pon_candidates.append([self.last_kawa_tile[:2], self.last_kawa_tile[:2]])
            if (
                self.tehai_mjai.count(self.last_kawa_tile[:2]) >= 1 and
                self.tehai_mjai.count(self.last_kawa_tile[:2] + "r") == 1
            ):
                pon_candidates.append([self.last_kawa_tile[:2] + "r", self.last_kawa_tile[:2]])
            return pon_candidates
        else:
            pon_candidates.append([self.last_kawa_tile, self.last_kawa_tile])
        return pon_candidates

    @property
    def can_act_3p(self) -> bool:
        return (
            self.can_discard or
            self.can_riichi or
            self.can_pon or
            self.can_agari or
            self.can_ryukyoku or
            self.can_kan
            # self.tehai_vec34[9*3+3] > 0 # nukidora
        )

    def _estimate_shape_features(self):
        """
        形に関する粗い推定値を返す。
        既存に強い推定器があればそちらを使ってOK。
        """
        # 例: 聴牌率・和了率・放銃率・基礎打点を既存評価器から取得
        # 無ければ簡易に現状の牌姿やドラ枚数などから推定する。

        win_rate = self._get_float_safe("est_win_rate",       getattr(self, "_policy_est_win_rate", 0.18))
        deal_in  = self._get_float_safe("est_deal_in_rate",   getattr(self, "_policy_est_deal_in_rate", 0.07))
        tempai   = self._get_float_safe("est_tempai_rate",    getattr(self, "_policy_est_tempai_rate", 0.45))
        # basepoint は本体が expose していないことも多い → policy内のデフォルトへフォールバック
        basept_raw = getattr(self, "est_basepoint", getattr(self, "_policy_est_basepoint", 2600.0))
        basept_cur = getattr(self, "current_hand_basepoint", getattr(self, "_policy_current_basepoint", 2600.0))
        basept   = float(basept_raw if basept_raw is not None else basept_cur)
        speed    = self._get_float_safe("est_call_speed_gain", getattr(self, "_policy_est_call_speed", 1.0))
        return win_rate, deal_in, tempai, basept, speed

    def update_policy(self):
        """毎巡呼び出し、UI層が参照するポリシーフラグを更新する"""
        win, lose, tempai, basept, speed = self._estimate_shape_features()
        scores = self._get_scores_safe()
        pid = self._get_player_id_safe()
        my_score = int(scores[pid]) if 0 <= pid < len(scores) else 25000
        other_scores = [int(s) for i, s in enumerate(scores) if i != pid]
        ctx = PolicyContext(
            my_score=my_score,
            other_scores=other_scores,
            player_id=int(pid),
            is_oras=bool(self._get_is_oras_safe()),
            is_dealer=bool(getattr(self, "is_dealer", False)),
            riichi_declared_count=int(self.riichi_declared_count),
            opponent_threat=bool(self.opponent_threat),
            last_discard_is_yakuhai=bool(self.last_discard_is_yakuhai),
            win_rate=float(win),
            deal_in_rate=float(lose),
            tempai_rate=float(tempai),
            basepoint=float(basept),
            call_speed_gain=float(speed),
            turns_left=int(getattr(self, "turns_left", getattr(self, "_policy_turns_left", 12))),
            is_ryanmen=bool(getattr(self, "is_ryanmen_flag", getattr(self, "_policy_is_ryanmen", True))),
            shanten=int(self._get_shanten_safe()),
            safety_score=float(getattr(self, "safety_score", getattr(self, "_policy_safety_score", 0.5))),
            genbutsu_count=int(getattr(self, "genbutsu_count", getattr(self, "_policy_genbutsu_count", 3))),
            suji_count=int(getattr(self, "suji_count", getattr(self, "_policy_suji_count", 6))),
            wall_info=float(getattr(self, "wall_info", getattr(self, "_policy_wall_info", 0.0))),
            red_count=int(getattr(self, "red_count", getattr(self, "_policy_red_count", 0))),
            dora_visible_count=int(getattr(self, "dora_visible_count", getattr(self, "_policy_dora_visible_count", 0))),
        )
        dec = ExpectedValueEngine.decide(ctx)
        # UI層で使う公開属性に反映
        self.policy_allow_reach = bool(dec.allow_reach)
        self.policy_allow_pon   = bool(dec.allow_pon)
        self.policy_allow_chi   = bool(dec.allow_chi)
        self.policy_allow_kan   = bool(dec.allow_kan)
        # 補助情報も公開（UI層のgetattrで読まれる）
        # 書き戻しは property かもしれないので try/except で安全に。失敗時は _policy_* に保存
        try:    self.current_hand_basepoint = float(dec.expected_basepoint)
        except Exception: self._policy_current_basepoint = float(dec.expected_basepoint)
        try:    self.opponent_threat = bool(dec.threat)
        except Exception: self._policy_opponent_threat = bool(dec.threat)
        # is_oras は大抵フレーム側で管理されるので上書きしない方が安全
        # try:    self.is_oras = bool(dec.oras)
        # except Exception: self._policy_is_oras = bool(dec.oras)
        # 任意: デバッグ出力
        try:
            from .logger import logger
            logger.debug(f"[POLICY] mode={dec.eval_mode} EV(reach/dama/call)={dec.reach_ev:.1f}/{dec.dama_ev:.1f}/{dec.call_ev:.1f} "
                        f"allow R/P/C/K={self.policy_allow_reach}/{self.policy_allow_pon}/{self.policy_allow_chi}/{self.policy_allow_kan} "
                        f"bp={self.current_hand_basepoint:.0f} turns_left={self.turns_left}")
        except Exception:
            pass

    # ---- scores / player_id を取得するヘルパ ----
    def _get_scores_safe(self) -> list[int]:
        """本体に scores があればそれを使い、無ければデフォルト。"""
        try:
            s = getattr(self, "scores")  # 既存 property（read-only想定）
            if isinstance(s, (list, tuple)) and len(s) >= 4:
                return [int(x) for x in s]
        except Exception:
            pass
        # いくつか別名で持っているケースに対応（無ければデフォルト）
        for name in ("_scores", "__scores", "_state_scores"):
            try:
                s = getattr(self, name)
                if isinstance(s, (list, tuple)) and len(s) >= 4:
                    return [int(x) for x in s]
            except Exception:
                pass
        return list(getattr(self, "_policy_default_scores", [25000,25000,25000,25000]))

    def _get_player_id_safe(self) -> int:
        """本体に player_id があればそれを使い、無ければデフォルト。"""
        try:
            return int(getattr(self, "player_id"))
        except Exception:
            return int(getattr(self, "_policy_default_player_id", 0))
        
    def _get_is_oras_safe(self) -> bool:
        try:
            return bool(getattr(self, "is_oras"))
        except Exception:
            return bool(getattr(self, "_policy_is_oras", False))

    def _get_shanten_safe(self) -> int:
        # 本体に shanten property があるが read-only のケースがある → 参照のみ
        try:
            return int(getattr(self, "shanten"))
        except Exception:
            return int(getattr(self, "_policy_shanten", 1))
    def _get_float_safe(self, name: str, default: float) -> float:
        try:
            return float(getattr(self, name))
        except Exception:
            return float(default)