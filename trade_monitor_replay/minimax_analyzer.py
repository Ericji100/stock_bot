from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping

import httpx


SYSTEM_PROMPT = """你是台指期歷史逐K回放的主要判讀模型。你只能根據輸入中已揭露的歷史K棒與前次已驗證狀態判斷，不得使用未來資料、外部資料或隱藏知識補價。ai_hybrid_state_continuity_lock.required=true時，必須逐字承接其中指定的大錨與控制級數。你必須且只能呼叫submit_replay_analysis一次，把完整結果放在函式參數；不得輸出一般文字、Markdown、解說或思考過程。"""
TOOL_NAME = "submit_replay_analysis"
REPLAY_EXECUTION_VERSION = "replay-execution-v5-provider-neutral-structured-output"
FULL_FIDELITY_EXECUTION_VERSION = "replay-execution-v6-production-contract-full-fidelity"
DETERMINISTIC_EXECUTION_VERSION = "replay-execution-v9-program-events"
ENTRY_GATE_EXECUTION_VERSION = "replay-execution-v10-entry-gate"
ANCHOR_LIFECYCLE_EXECUTION_VERSION = "replay-execution-v11-anchor-lifecycle"
COURSE_CHAIN_EXECUTION_VERSION = "replay-execution-v99-isolated-no-trade-minute"
AI_HYBRID_EXECUTION_VERSION = "replay-execution-v132-explicit-ai-control-continuity-lock"
AI_HYBRID_LONG_ONLY_EXECUTION_VERSION = (
    "replay-execution-v181-long-only-adapter-v33"
)
AI_HYBRID_LONG_ONLY_Q2_Q4_EXECUTION_VERSION = (
    "replay-execution-v197-profitable-protective-exit-semantics"
)


class MiniMaxReplayError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class MiniMaxReplayResult:
    payload: dict[str, Any]
    raw_text: str
    diagnostics: dict[str, Any]


class MiniMaxReplayAnalyzer:
    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str,
        timeout_seconds: float,
        max_output_tokens: int,
        output_schema: Mapping[str, Any] | None = None,
        post_json: Callable[[str, dict[str, str], dict[str, Any], float], dict[str, Any]] | None = None,
    ) -> None:
        if not str(api_key or "").strip():
            raise MiniMaxReplayError("minimax_key_missing", "MiniMax API Key尚未設定。")
        self._api_key = str(api_key).strip()
        self.model = str(model).strip()
        self.base_url = str(base_url).rstrip("/")
        self.timeout_seconds = float(timeout_seconds)
        self.max_output_tokens = int(max_output_tokens)
        self.output_schema = dict(output_schema or {})
        if not self.output_schema:
            raise MiniMaxReplayError("minimax_schema_missing", "MiniMax function schema尚未設定。")
        self._post_json = post_json or _post_json

    def analyze(self, prompt: str) -> MiniMaxReplayResult:
        started = time.perf_counter()
        request_payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.1,
            "max_tokens": self.max_output_tokens,
            "reasoning_split": True,
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": TOOL_NAME,
                        "description": "提交唯一一份TMF歷史逐K分析與下一根K使用的精簡狀態。",
                        "parameters": self.output_schema,
                    },
                }
            ],
            "tool_choice": {
                "type": "function",
                "function": {"name": TOOL_NAME},
            },
        }
        headers = {"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"}
        try:
            response = self._post_json(
                f"{self.base_url}/chat/completions",
                headers,
                request_payload,
                self.timeout_seconds,
            )
        except Exception as exc:
            raise MiniMaxReplayError("minimax_request_failed", _safe_error(exc)) from exc
        try:
            choice = (response.get("choices") or [{}])[0]
            message = choice.get("message") or {}
            tool_calls = message.get("tool_calls") or []
        except Exception as exc:
            raise MiniMaxReplayError("minimax_response_invalid", "MiniMax回應格式無效。") from exc
        if len(tool_calls) != 1 or not isinstance(tool_calls[0], Mapping):
            raise MiniMaxReplayError("minimax_tool_call_missing", "MiniMax沒有且只呼叫一次指定分析函式。")
        function = tool_calls[0].get("function")
        if not isinstance(function, Mapping) or function.get("name") != TOOL_NAME:
            raise MiniMaxReplayError("minimax_tool_call_invalid", "MiniMax呼叫了錯誤的函式。")
        arguments = function.get("arguments")
        raw_text = arguments if isinstance(arguments, str) else json.dumps(arguments, ensure_ascii=False)
        raw_text = raw_text.strip()
        if not raw_text:
            raise MiniMaxReplayError("minimax_output_empty", "MiniMax函式參數沒有可用內容。")
        payload = parse_json_object(raw_text)
        payload, normalization_stats = normalize_tool_arguments(payload)
        usage = response.get("usage") if isinstance(response, Mapping) else None
        diagnostics = {
            "model": response.get("model") or self.model,
            "finish_reason": choice.get("finish_reason"),
            "usage": usage if isinstance(usage, Mapping) else {},
            "elapsed_seconds": round(time.perf_counter() - started, 3),
            "prompt_chars": len(SYSTEM_PROMPT) + len(prompt),
            "output_chars": len(raw_text),
            "used_function_call": True,
            "text_content_present": bool(message.get("content")),
            "normalized_array_wrappers": normalization_stats["array_wrappers"],
            "normalized_null_fields": normalization_stats["null_fields"],
            "normalized_trailing_serialization_suffixes": normalization_stats[
                "trailing_serialization_suffixes"
            ],
        }
        return MiniMaxReplayResult(payload=payload, raw_text=raw_text, diagnostics=diagnostics)


def build_replay_prompt(
    *,
    rules_text: str,
    schema_text: str,
    runtime_context: Mapping[str, Any],
    correction: str | None = None,
    full_fidelity: bool = False,
    deterministic_contract: bool = False,
    course_chain_contract: bool = False,
    ai_hybrid: bool = False,
    trade_direction_policy: str = "BOTH",
    trade_setup_policy: str = "ALL",
) -> str:
    correction_block = ""
    if correction:
        correction_block = f"""
<VALIDATION_CORRECTION>
上一份輸出未通過驗證：{correction}
請重新輸出完整JSON。不得省略欄位，不得用改成資料不足的方式規避已由結構化K棒確認的事實。
</VALIDATION_CORRECTION>
"""
    replay_rules = replay_rule_text(rules_text)
    runtime_payload = dict(runtime_context)
    if ai_hybrid:
        # Put the small, deterministic presentation contract first so the
        # model does not have to infer card eligibility from several nested
        # ledger branches.  This does not decide anchors, quadrants, methods,
        # scenarios, or trades; those remain AI course judgments.
        runtime_payload = {
            "ai_hybrid_output_contract": _ai_hybrid_output_contract(runtime_payload),
            **{
                key: value
                for key, value in runtime_payload.items()
                if key != "ai_hybrid_output_contract"
            },
        }
    runtime_json = json.dumps(runtime_payload, ensure_ascii=False, separators=(",", ":"))
    if full_fidelity:
        contract_rules = """
- 回放輸出必須直接符合正式v8 ANALYSIS_SCHEMA：包含正式九欄、constitution_event及完整market_structure_state v6；不得使用精簡memory或自創回放欄位。
- market_structure_state.version固定為6，as_of必須逐字使用expected_latest_closed_k_iso，session_key必須逐字使用expected_session_key。
- previous_analysis_summary、market_structure_state與constitution_state是前一輪經正式驗證器接受的狀態；必須依正式狀態轉移規則延續，不得無故刪除仍有效的樞紐、定錨、防線、太極朝代、動能事件、情境或持倉。
- spot_market_context.status不是FRESH時，decision_chain_context.opening_context.cash_gap_source必須為UNAVAILABLE，gap_direction與gap_size為UNDEFINED，previous_cash_close、cash_open、gap_observed_at全部為null；即使PREOPEN輸入另列官方前收，也不能單獨塞入正式cash-gap狀態。只有status=FRESH且同時有官方前收與現貨開盤時，才可使用VERIFIED_CASH。
- 必須完成正式規則要求的定錨、大小級道氏防線、動態四象限、太極／一之、X流程、全部戰法掃描、主備情境及多空預案；不因回放而縮減任何分析步驟。
- cclass_context.taiji_context.active_leg_id若非null，必須指向legs中仍為FORMING或CONFIRMED的作用中leg，不得指向COMPLETED或INVALIDATED；若engine_mode=TAIJI_ORDERED，必須保留這個非終止作用中leg。不得把已完成的第五段仍填成active。
- prospective_context.taiji_evolution的複製或修正幅度／時間趨勢，只有各自至少保留兩個copy_outcomes或correction_outcomes時才可比較；少於兩個時相對應的amplitude_trend與duration_trend必須為UNDEFINED。
- preopen_snapshot=true時constitution_event.event_type必須為NONE，不得把夜盤中已走完的訊號追認為模擬成交；但仍須以完整夜盤建立state v6背景與日盤多空預案。
- 所有使用者可見九欄只可使用自然繁體中文，例如「09:07 高點46,417點」；不得顯示kind=、bar_time=、price=、confirmation_time=、JSON或完整ISO時間。機器欄位只可存在constitution_event及market_structure_state內。
""".strip()
    elif deterministic_contract and '"message_type"' in schema_text:
        contract_rules = """
- 回放輸出固定使用ANALYSIS_SCHEMA中的analysis＋memory v3。正式constitution_event與market_structure_state v6不由AI輸出；正式v2.1.8交易規則不得縮減。
- deterministic_evidence_ledger是程式從已揭露K棒建立的客觀證據帳本；不得改寫、回填或自創樞紐、段、防線、structure_event的ID、時間與價格。
- course_reading必須分別填background_quadrant與working_quadrant，並為大級、小級各自保存一組trend_dynamics與volatility_dynamics，再保存controlling_grade、grade_relation及小級主要／次要象限候選。不得把兩級共用同一組軸，也不得只因完整道氏尚未確認就長期填TRANSITION。
- 每一級四象限都使用自己的兩條軸：趨勢性INCREASING＋波動EXPANDING對應Q1；INCREASING＋CONTRACTING對應Q4；DECREASING＋EXPANDING對應Q2；DECREASING＋CONTRACTING對應Q3。小級證據互相衝突可使用TRANSITION，但primary_quadrant_candidate仍須指出最接近的Q1～Q4。
- memory.structure_control保存大小級象限實際改變時間；未變就保留，改變才填本輪時間。依新的同級結構證據更新，沒有固定8分鐘等待規則。
- ledger.quadrant_evidence的5／20根統計只是觀測窗，不等於大小級數，也不強制決定象限。不得以單一效率值、ATR或回撤比例取代定錨後的同級推進／修正比較。
- large_anchor_ref與small_anchor_ref保存作用中大小錨；只要ledger.control_candidates.forming_small_anchor_ref存在，working_anchor_ref必須逐字引用它。不得因新段尚未完成下一次配對，就繼續拿已失去控制力的舊小段當主錨。
- deterministic_evidence_events列出本輪第一次可知的客觀事件。若包含GRADE_UPGRADE，必須輸出NOTIFY＋message_type=STRUCTURE_UPGRADE，structure_event_ref引用該事件、large_anchor_ref引用事件的source_anchor_id、controlling_grade=LARGE，並依大級自己的兩軸把background_quadrant判為Q1至Q4；升級只代表改用較大級結構，不代表大級必然是Q1或Q4。不得把小級防線失守直接等同大級翻向，也不得繼續填TRANSITION。
- 若最新客觀事件是GRADE_DOWNGRADE，空手時必須輸出STRUCTURE_DOWNGRADE並引用該事件，控制級數不得繼續填LARGE；這只表示升級結構跌破替代防線，尚未等同反向趨勢。若最新事件是STRUCTURE_INVALIDATED，空手時使用INVALIDATION；只有父級起點也被已收盤K破壞，才可宣告原升級結構失效。
- 若最新客觀事件是FALSE_BREAK_RECLAIM，事件本身只證明已知邊界被越界後收復，不自動取得PREPARATION資格。空手且候選仍為FORMING、OBSERVATION_ONLY或不在ai_executable_setup_keys時，必須輸出OBSERVATION；只有AI選用的可執行setup已達ARMED、ENTRY_ELIGIBLE、AGGRESSIVE_CONFIRMED或CONSERVATIVE_CONFIRMED時才可輸出PREPARATION；已有持倉時固定依持倉狀態輸出MANAGEMENT（硬停損或已鎖定出場優先使用STOP／EXIT）。先說明哪個已知樞紐／防線／OR價位被收盤跌破、幾根內收復及掃低／掃高極值，再由課程邏輯判斷是否值得等待重新發動。
- ledger.trade_levels提供結構價位與工程緩衝參考，不是課程唯一停損公式。Q4必須用所選同級完整修正的外側；假突破使用掃低／掃高極值外，說明來源時間和緩衝。不能把最新微型樞紐加0.2ATR強制當Q4整段停損。
- memory.active_setups最多保存3套仍有意義的獨立劇本，至少容納當下相關的多方與空方條件；不得用單一active setup刪掉另一方向的合法條件。
- runtime_context.retired_setup_keys內的setup已過期、否決或失效，analysis.action與memory.active_setups都不得再次引用；刪除舊setup不等於改變既有持倉，持倉只保留position_state.active_setup_key所指向的原交易劇本。
- scenario的bull_plan、range_plan、bear_plan必須各自說清楚觸發與失效；view_change先寫目前主看法的降級／失效條件，再寫反向正式接管條件，不要先重複主方向增強條件。三個數字是情境權重、不是統計勝率，合計100。
- action.obstacles必須把客觀價位分成TRIGGER、CHECKPOINT、HARD_TARGET。順勢Q4／Q1中沿交易方向的已知前高、前低、開盤區間或夜盤邊界固定先列CHECKPOINT，不得列HARD_TARGET，也不得只因第一檢查點不足1.5R或停損超過1.2ATR就機械否決符合結構的延續交易；到達檢查點後依已收盤K是否直接突破、停滯或收回觸發區管理。
- Q4採完整同級修正外的結構停損及較小部位概念；entry、structural_stop、expected_behavior與max_wait_bars必須使用主控戰法的原生規則。
- 持倉後的停損改善由ledger.trade_levels.position_protection_candidates中的protection_required唯一決定；為true時逐點沿用stop_price，為false時維持既有停損。AI不得自行提早、延後或選擇其他保護價。
- program_trade_policy.actionable_setups為PROGRAM_ONLY或PROGRAM_CANDIDATES_AI_DECISION時，只有trade_levels.continuation_arm_candidate、entry_eligibility或既有持倉setup可以進入ARMED以上狀態。trade_levels內其他帶setup_key的候選是診斷／備用證據，只能保留為FORMING；其他太極、一之、左右、C班或X流程觀察仍可說明，但不得由AI自行建立可成交setup。
- 停損後同一setup最多再進場1次，且至少等待1根完整1分K。等待期用reentry_status=WAIT_ONE_BAR；重新收復且可評估時用AVAILABLE；實際再進場必須message_type=REENTRY、position_action=ENTER、entry_role=REENTRY並沿用同一setup_key。
- message_type必須依事件選擇：一般觀察OBSERVATION、準備PREPARATION、進場ENTRY／REENTRY、持倉MANAGEMENT、停損STOP、出場EXIT、升降級STRUCTURE_UPGRADE／STRUCTURE_DOWNGRADE、候選失效INVALIDATION、無實質變化UNCHANGED。空手且setup_stage已是ARMED、AGGRESSIVE_CONFIRMED或CONSERVATIVE_CONFIRMED時，除非本輪有優先級更高的結構升／降級事件，必須使用PREPARATION，不能仍用OBSERVATION。已有模擬持倉時，即使本輪停損、觸發區與結構都未改變，也必須使用MANAGEMENT＋NOTIFY，不能使用UNCHANGED或DONT_NOTIFY；持倉中的每個分析點都要留下可審核的管理決策。只有空手且真的沒有實質變化時才可使用UNCHANGED＋DONT_NOTIFY，其餘實質事件固定NOTIFY。
- PREPARATION只能在setup_stage為ARMED、ENTRY_ELIGIBLE、AGGRESSIVE_CONFIRMED或CONSERVATIVE_CONFIRMED，且action.setup_key可對應memory.active_setups同階段setup時使用。setup_stage為NONE、FORMING，或尚無可執行觸發／結構停損時只能輸出OBSERVATION，不得僅因方向性反向候選出現就標示多方／空方準備。
- focus_methods只選本輪真正影響決策的1～2個鏡頭。未成立且不影響操作的一之、左右、太極或道氏不要放進使用者可見主因，也不要逐項長篇解釋。
- action.position_action只有真的模擬進場、持倉停損或主動出場才用ENTER、STOP、EXIT；準備、候選失效與空手觀察固定NONE。ENTER以latest_closed_k.close作本輪收盤確認價並提供數值stop_price。
- 持倉MANAGEMENT若要把停損移到已完成的有利結構外，action.position_action維持NONE，但action.stop_price必須填入新的實際保護價，程式會從下一根K開始執行；只能朝降低風險的方向移動，不能放寬。若不調整，就填目前持倉停損或null。management文字不得宣稱一個與action.stop_price不同的實際停損。
- action.direction與entry_role只描述本輪新進場：position_action=ENTER才可填LONG及INITIAL／REENTRY；NONE、STOP、EXIT固定direction=NONE、entry_role=NOT_APPLICABLE。STOP／EXIT的message_direction仍可填BULL。
- preopen_snapshot=true時message_type=SNAPSHOT、position_action=NONE、direction=NONE，不追認夜盤已走完的模擬成交；仍須完成夜盤背景與日盤雙向預案。
- 所有使用者可見文字使用自然繁體中文與盤中簡稱：1分K、15分、21MA、105MA、ATR14、3～5根、Q1～Q4。只有課程專有名詞保留中文；不得顯示ledger ID、JSON鍵名、kind=、bar_time=、price=或完整ISO時間。
- 引用點位、定錨、樞紐、防線、開盤區間或障礙時，必須逐字複製ledger數值。若無法可靠抄錄就省略，不得補價。
- notification_reason只寫一個最重要變化。各文字欄位不要重複相同事實，讓程式依事件模板產生盤中可讀短訊息。
""".strip()
    elif deterministic_contract:
        contract_rules = """
- 回放輸出固定使用ANALYSIS_SCHEMA中的analysis＋memory v2。正式constitution_event與market_structure_state v6不由AI輸出。
- deterministic_evidence_ledger是程式逐分鐘從已揭露K棒建立的客觀證據帳本；樞紐first_seen_at等於因果確認時間，不得改寫、回填或自創ID、時間與價格。
- course_reading的定錨只能引用ledger.legs中的ID；道氏防線只能引用ledger.defenses中state=ACTIVE的ID。state=BROKEN代表程式已找到首次收盤破壞時間，禁止再把它當作用中防線引用；沒有合格證據時必須填null並用自然中文說明，不得猜測。
- ledger中status=FORMING的段可作「形成中定錨」判讀，但不得說成已確認。duration_minutes=0時應說首根形成中，不要顯示「0分鐘定錨」。
- 單根OHLC無法證明最高與最低出現順序；同一根1分K的高低點不得自行組成方向性定錨、工作段或反向候選。只有ledger.anchor_lifecycle已提供、且起訖時間跨越不同已收盤K的角色才能引用；單根大振幅只可描述為開盤波動或順序未明的觀察證據。
- ledger.dow是程式權威計算的大小級道氏狀態；AI負責解釋控制權、品質及課程意義，不得在文字中宣稱相反的已確認道氏方向。
- structured_market_data_context.status=FRESH且ledger.latest_closed_k存在時，代表行情資料完整；即使早盤樞紐或大級方向尚未形成，large_trend.classification也必須保守填「盤整」，不得填「資料不足」。只有行情來源本身缺失時才可使用「資料不足」。
- memory v2只保存作用中setup與看法維持／降級／翻向條件；客觀結構與模擬持倉由程式管理，不得在memory另造一份。
- action.position_action只有真的模擬進場、持倉停損或主動出場時才用ENTER、STOP、EXIT；setup失效但仍空手必須用NONE，不能冒充出場。
- ENTER代表本根收盤確認後以latest_closed_k.close作模擬進場價，必須提供正確方向的數值stop_price；其餘情況stop_price可為null。不得追認中斷期間的歷史成交。
- 三個scenario機率為當下條件權重，不是統計勝率，合計必須為100。必須同時說明主要、備用及看法改變條件。
- preopen_snapshot=true時position_action固定NONE、direction固定NONE；仍須完成夜盤背景、大／小結構、定錨、四象限、太極／一之／左右、道氏、主備情境與日盤計畫。
- 所有使用者可見文字只用自然繁體中文，不得顯示ledger ID、JSON鍵名、kind=、bar_time=、price=或完整ISO時間。
- 使用者可見文字的週期與均線統一寫成阿拉伯數字簡稱：`1分K`、`15分`、`21MA`、`105MA`；不要刻意寫成「一分鐘K」、「十五分鐘」、「二十一期均價」或「一百零五期均價」。
- 引用點位、定錨起訖、樞紐、防線、開盤區間或最近障礙時，必須逐字複製deterministic_evidence_ledger中的數值；尤其不得改動萬位或千位數字。若無法可靠抄錄，寧可省略該點位，不得自行補價。
- notification_reason只寫使用者看得懂的主要變化，不得抄入系統強制通知、驗證器、傳送政策或內部錯誤文字。
""".strip()
    else:
        contract_rules = """
- 回放輸出固定使用ANALYSIS_SCHEMA中的analysis＋memory；正式環境的constitution_event與market_structure_state v6不屬於本回放輸出，不得額外輸出。
- memory.version固定為1，memory.as_of必須逐字使用expected_latest_closed_k_iso。
- memory.session_key夜盤使用「交易日:NIGHT」，日盤使用「交易日:DAY」；前一監控時段只能作盤前參考脈絡，不得直接成為新時段的作用中定錨、防線、象限或太極朝代。
- memory必須精簡保存大小級方向、定錨、防線、象限、主要／備用情境、主控setup、模擬持倉與看法維持／降級／翻向條件；不得把上一輪仍有效資料無故刪除。
""".strip()
    if deterministic_contract and '"reverse_anchor_candidate_ref"' in schema_text:
        anchor_reference_rule = (
            "- AI_HYBRID只把ledger.anchor_control當作各角色的可引用候選集合；"
            "large／small／working／reverse與防線ref可選對應候選或null，"
            "由AI依課程判斷是否具有控制意義，不得改接其他歷史pivot或小波段。"
            if ai_hybrid
            else "- v8不再使用ledger.control_candidates挑定錨；大小錨、工作段、反向候選及防線一律逐字引用ledger.anchor_control。"
        )
        upgrade_reference_rule = (
            "structure_event_ref引用該事件；若AI接受其升級意義，large_anchor_ref只能引用"
            "anchor_control.active_background_anchor_ref或null，controlling_grade由AI依課程判斷"
            if ai_hybrid
            else "structure_event_ref引用該事件、large_anchor_ref仍引用anchor_control.active_background_anchor_ref、controlling_grade=LARGE"
        )
        contract_rules = contract_rules.replace(
            "- large_anchor_ref與small_anchor_ref保存作用中大小錨；只要ledger.control_candidates.forming_small_anchor_ref存在，working_anchor_ref必須逐字引用它。不得因新段尚未完成下一次配對，就繼續拿已失去控制力的舊小段當主錨。",
            anchor_reference_rule,
        ).replace(
            "structure_event_ref引用該事件、large_anchor_ref引用事件的source_anchor_id、controlling_grade=LARGE",
            upgrade_reference_rule,
        )
        if ai_hybrid:
            contract_rules = contract_rules.replace(
                "若包含GRADE_UPGRADE，必須輸出NOTIFY＋message_type=STRUCTURE_UPGRADE，structure_event_ref引用該事件；若AI接受其升級意義，large_anchor_ref只能引用anchor_control.active_background_anchor_ref或null，controlling_grade由AI依課程判斷，並依大級自己的兩軸把background_quadrant判為Q1至Q4；升級只代表改用較大級結構，不代表大級必然是Q1或Q4。不得把小級防線失守直接等同大級翻向，也不得繼續填TRANSITION。",
                "若包含GRADE_UPGRADE，它是程式確認的價格結構事件候選，不是自動採用的課程結論。AI接受時，structure_event_ref與large_anchor_ref必須成對引用ai_hybrid_output_contract.current_grade_upgrade_options的同一組event_ref／controlling_anchor_ref，並使用LARGE控制級數；通常輸出STRUCTURE_UPGRADE，但required_message_type非null時由STOP／EXIT／MANAGEMENT／PREPARATION優先，升級引用仍須完整保留。AI不接受其控制意義時，若先前沒有仍有效的大級控制，structure_event_ref與large_anchor_ref都填null、message_type使用OBSERVATION、controlling_grade不得為LARGE，並由strategy_reason說明仍缺少的級數或結構條件。引用GRADE_UPGRADE的structure_event_ref就表示正式採用，不能只把它當風險備註。不得把小級防線失守直接等同大級翻向。",
            )
            contract_rules += """

- AI_HYBRID若本時段anchor_control.active_background_anchor_ref為null，且本輪沒有採用GRADE_UPGRADE，large_anchor_ref必須為null、controlling_grade不得為LARGE、large_trend.classification必須為盤整；background_quadrant必須為UNDEFINED，background_trend_dynamics與background_volatility_dynamics必須為UNCLEAR，memory.structure_control須逐欄一致。這不限制working_quadrant及其候選依開盤證據作機率判讀。
- ai_hybrid_output_contract.active_grade_upgrade_options列出截至本輪仍未被GRADE_DOWNGRADE／STRUCTURE_INVALIDATED終止的升級候選。它可以在事件發生後的後續輪次繼續被AI採用，不代表事件重新發生；若採用，large_anchor_ref、large_defense_ref與controlling_grade必須引用同一候選的controlling_anchor_ref、replacement_defense_ref及LARGE。若不採用，必須在grade_control或strategy_reason留下課程理由。
""".rstrip()
    if deterministic_contract and '"ENTRY_ELIGIBLE"' in schema_text:
        contract_rules += """

- 這是新版回放執行門。memory.active_setups中的每個可執行setup必須提供來自ledger的trigger_level、trigger_operator與valid_bars；程式只會對前一輪已ARMED／已確認的setup，用後續已收盤K客觀計算entry_eligibility。
- entry_eligibility.status=ENTRY_ELIGIBLE時，course_reading.setup_stage必須填ENTRY_ELIGIBLE、message_type必須填PREPARATION。AI只能二選一：(1) action.position_action=ENTER並提供相同setup_key、方向、entry_role、程式可核對的stop_price，entry_rejection_reason=NONE；(2) position_action=NONE並從STOP_TOO_WIDE、HARD_OBSTACLE_TOO_CLOSE、GRADE_CONFLICT、CONSTITUTION_BLOCKED選唯一否決原因。不得再寫「繼續等待」規避決策。
- entry_eligibility.decision_authority=PROGRAM且required_position_action=ENTER時，不再有AI二選一：必須輸出ENTER、相同setup_key／方向／entry_role、required_stop_price及entry_rejection_reason=NONE；max_wait_bars必須等於required_behavior_max_wait_bars，obstacles必須依required_behavior_obstacles的順序逐項使用相同role與price，不得增刪或替換。AI只負責解釋，不能取消交易、移動程式停損或改變管理等待窗與檢查點。
- protective_stop_audit.status=TRIGGERED時，STOP及實際成交價由程式擁有；program_behavior_exit_audit.status=PENDING_FILL或TRIGGERED時，EXIT訊號、第一個失效收盤與下一根開盤成交由程式擁有。AI即使輸出續抱或其他事件，執行欄位仍會被程式覆蓋，只能解釋原因。
- PROGRAM_DEFENSE_AVAILABLE會在道氏防線第一次因果可見的收盤留下時間軸事件。若它是進場後新形成且更有利的同向防線，保護停損自該收盤後生效；不得回填到確認K盤中，也不得等下一次AI分析才開始保護。
- ENTER在這一輪只表示接受已收盤訊號，不代表用訊號K收盤價成交。程式會保存pending_entry，並在下一根1分K的第一個可成交open建立模擬倉與獨立ENTRY通知；若下一個可成交K超過expires_at或開盤已越過停損，程式取消，不追認成交。
- AI_HYBRID接受ENTRY_ELIGIBLE並輸出ENTER動作時，message_type仍必須是PREPARATION，因為本輪尚未成交；不得提前使用ENTRY卡。protective_stop_audit已TRIGGERED時必須使用原持倉方向的STOP卡，program_behavior_exit_audit已鎖定時必須使用原持倉方向的EXIT卡。這些是已發生的執行事實，驗證器不會代替AI改寫欄位。
- program_constitution.trading_locked=true時，action.reentry_status與memory.reentry.status都必須為NOT_APPLICABLE；舊setup只能NO_CHASE或INVALIDATED。STOP／EXIT說明必須揭露cooldown_until，並明確寫冷卻後仍須新的完整結構重新取得資格，不能只寫等待1根。
- entry_eligibility.status不是ENTRY_ELIGIBLE時不得自行填ENTRY_ELIGIBLE、不得直接ENTER，entry_rejection_reason固定NONE。若狀態為EXPIRED，只能當歷史事件說明，不得恢復成當下準備訊號。
- deterministic_event_lifecycle是程式擁有的事件消費帳本。status=ACTIVE且analyzed=false的事件才是本輪新事件；ANALYZED或EXPIRED事件不得再次冒充當下通知。事件是否已分析、有效根數、expires_at與consumed_at不得由AI改寫。
- course_reading.structure_event_ref只引用本輪真正主導事件卡的新結構事件；沒有新結構事件時填null。memory.structure_control.last_structure_event_ref必須複製同一值，不得改填帳本中較早但已過期的事件。
- STRUCTURE_UPGRADE、STRUCTURE_DOWNGRADE與INVALIDATION是正式事件卡：前兩者必須有本輪對應的GRADE_UPGRADE／GRADE_DOWNGRADE structure_event_ref；INVALIDATION必須有STRUCTURE_INVALIDATED ref或本輪PROGRAM_SETUP_INVALIDATED事件。只有主觀看法升降、但沒有上述新事件時，一律使用OBSERVATION；持倉中則使用MANAGEMENT。
- 戰法C班每輪必須先填cclass_mode，再分別完成taiji與yizhi：TAIJI_ORDERED處理有序複製／修正；YIZHI_MOMENTUM只在離心力、一條龍或生死門等異常動能成立時接管；UNORDERED／RESETTING不得硬套C班進場。
- X戰法是盤中決策流程而非另一個獨立招式。每輪必須先以x_stage標示目前步驟，再由x_process說明已走到開盤證據、第一次端點、定錨、主鏡頭、A／B／C級機會或執行管理的哪一步，以及下一個能推進或退回的條件；不得只把「X流程」三字塞進main_strategy。
- 太極、一之與X流程是必填且可稽核的內部判讀。事件卡會顯示其摘要；內容必須交代當下狀態，不可只寫「未確認」而省略已存在的父代、修正／複製、動能階段或流程位置。
""".strip("\n")
    if ai_hybrid and str(trade_direction_policy).upper() == "LONG_ONLY":
        contract_rules += """

- 本版交易方向政策固定為LONG_ONLY：市場結構仍須雙向辨識，但交易執行只有LONG或空手，永遠不得建立SHORT setup、空方準備、空方進場、空方再進場或SHORT持倉。
- 空方定錨、空方道氏、防線失守、向下Q1／Q4、空方太極或一之仍是必要風險證據，用來決定多方降級、退出、禁止做多及等待多方重新接管；不得因不做空而刪除這些市場事實。
- action.direction只允許LONG或NONE；memory.active_setups只允許direction=LONG。盤面偏空時message_direction、large_trend、current_trend及情境權重仍可誠實標示BEAR，但操作固定空手、退出多單或等待新的多方條件。
- 跌破多方小級道氏先是小級降級；只有父級多方結構仍守住，後續收復並沿原方向創高，才可判斷多方級數升級。破前低只可先判斷失效、Q2風險或假跌破候選，完成收復與多方重新發動後才可準備做多。
- scenario仍維護多／盤／空三種市場情境；bull_plan寫多方成立條件，range_plan寫保持空手或邊界等待，bear_plan只能寫多方禁止／退出及未來重新取得資格的條件，不得寫放空方式。
- main_strategy只選多方戰法或「禁止做多／等待多方重建」。空方訊號即使非常完整也不能成為可成交主控setup。
""".strip("\n")
    if ai_hybrid and str(trade_setup_policy).upper() == "LONG_Q2_Q4_ONLY":
        contract_rules += """

- 本階段可成交範圍固定為多方Q2與多方Q4。Q1／一之、開盤區間、Q3壓縮、箱型、左右或其他戰法仍須判讀，但只能作支持、衝突、風險或禁止做多證據，不得建立可成交setup。
- candidate_source為ANCHOR_LEG_SEQUENCE、CONFIRMED_PULLBACK_ENDPOINT_N2或Q4_AGGRESSIVE_PULLBACK_REVERSAL時，屬多方Q4設置；只有AI判讀為Q4，或TRANSITION且主候選為Q4並同時呈現趨勢增強、波動收縮，才可接受ENTER。
- candidate_source為FALSE_BREAK_RECLAIM、Q2_FAILED_REVERSE_CANDIDATE或Q2_SLOW_OUTER_EXPANSION_FAILURE時，屬多方Q2設置；只有AI判讀為Q2，或TRANSITION且主候選為Q2並同時呈現趨勢減弱、波動擴張，才可接受ENTER。
- Q2是同級趨勢性減弱且波動擴張的市場狀態；假跌破收復只是Q2中的一種可執行設置，不得把全部Q2縮寫成固定1～2根假跌破。setup有效窗與進場後2～3根行為檢查是本版事前凍結的執行政策，不是Q2市場定義。
- Q2與Q4必須沿用各自的停損及應有行為：Q2使用掃低／反向失敗極值外停損並要求快速離開低點；Q4使用完整同級修正低點外停損，前高通常只作CHECKPOINT。
- ai_hybrid_output_contract.open_position_management_contract是成交時已凍結的戰法家族、應有行為與本輪可核對的結構證據。accepted_main_strategy_family不得在持倉後更換；目前行情可以由Q2成功發展成同向趨勢管理，但這仍是原Q2單達標後的管理階段，不得事後改寫成另一筆Q4進場或另創出場理由。
- accepted_main_strategy_family=Q2且behavior_audit.status=ACHIEVED時，先依post_achievement_policy判斷：若同向控制錨與同級有效防線已形成，改以該防線、父代結構與太極複製品質管理；形成中的同向複製已延伸父代極值時，即使幅度較短、耗時較長，也只能先降級期待，不能僅憑「變慢／變弱」宣告FAILED、RESETTING或EXIT。反向加速但尚未形成反向錨、尚未破壞作用中同級防線，也只屬警告，維持MANAGEMENT且不得新增。
- Q2達標後主動EXIT必須引用進場前已聲明的目標＋動能停頓／行為失效、已完成的複製失敗、父代結構或作用中同級防線遭已收盤K破壞、或程式已鎖定的停損／行為退出。不得在持倉後才發明新的目標、等待窗或失效條件；若上述證據都不存在，使用MANAGEMENT＋position_action=NONE並維持不得放寬的保護停損。
""".strip("\n")
    if deterministic_contract and '"reverse_anchor_candidate_ref"' in schema_text:
        contract_rules += """

- 這是錨生命週期版回放。每一個分析點都先由程式依當下已揭露的已收盤K更新ledger.anchor_lifecycle。ledger.monitoring_session界定本輪獨立監控時段；日盤08:45、夜盤15:00及美股09:30 ET開盤都重設作用中結構。ledger.reference_anchor_lifecycle僅是前一時段的盤前參考，禁止複製其ID至course_reading或memory，禁止把它直接稱為本時段大錨、小錨、道氏防線、象限或太極父代。
- ledger.anchor_control是各角色唯一可引用的候選集合，不得自行改選其他歷史pivot或小波段。程式權威版必須逐字使用各角色ref；AI_HYBRID版則只能選用對應角色ref或null，null表示AI判定該候選尚不足以取得課程上的控制意義。
- course_reading.structure_event_ref只能引用ledger.structure_events陣列內的ID；ledger.anchor_events只供說明定錨生命週期，絕對不能填入structure_event_ref。若ledger.structure_events為空，analysis與memory中的structure_event_ref一律填null。
- 新時段前4根已收盤1分K只有開盤證據，active_background_anchor_ref、active_child_anchor_ref與working_leg_ref可以全部為null；此時不得宣稱定錨成立、不得建立ARMED或ENTRY_ELIGIBLE setup、不得輸出PREPARATION。OR5收完後，程式才可能用時段開盤價至OR5方向極值建立小級開盤錨；這不是大錨。反向行情在突破該錨防線並完成延續前，只能稱修正或反向候選，不得直接翻向。
- 背景大錨可由較大級推進－修正－延續取得資格，或由開盤有效攻擊的延伸極值確認為n=1二級樞紐，證明該段包含小級結構而辨認父級推進。OPENING_EXTREME_HIGHER_PIVOT是本版可稽核的父級辨認方式，不代表講師定義只有這一種；quality_status=REQUIRES_ANALYST_REVIEW仍須檢查乾淨度、比例與破壞性。它不自動建立大級道氏防線，invalidation_boundary只是錨起點。原始段、同向延伸與反向候選分開保存，不能把小級反彈直接當成父級翻向。
- child_anchor是背景內已取得小級控制權的子級錨；working_leg只是目前仍在形成或反向候選內的最新工作段，端點可逐輪更新。不得把working_leg說成新的背景定錨，也不得因它每輪更新而宣稱大錨反覆更換。
- reverse_candidate是相反方向的結構證據候選，不是已接管背景。內部修正不因幅度大就自動變成反向定錨；反向成立須說明被破壞的級數。Type2推進－修正－延續是目前程式可確認的一條路，不是Type3跨大小防線急轉也必須再等待完整四樞紐的課程規定。尚未程式確認的Type3只列待審查證據，不杜撰正式ID。
- background_anchor.status=DEGRADED或DEGRADED_RECLAIMED表示原道氏防線曾失守；DEGRADED_RECLAIMED還表示後續已收盤K收復防線。這是原背景降級後仍存續，不是重新建立同ID，也不是反向候選自動接管。
- TG可見文字要按角色寫「大錨／小錨／目前工作段／反向候選」，不得再把四者都叫定錨。若反向候選不存在就省略，不需長篇說明未成立原因。
""".strip("\n")
    if course_chain_contract and not ai_hybrid:
        contract_rules += """

- ledger.anchor_lifecycle.dow_context保存大小級、多空的正式道氏槽位，course_reading.dow必須核對其資格。道氏未成立不等於所有戰法或背景方向皆未成立；large_trend、current_trend須分別綜合相應級数定錨與行情證據，不能被單一道氏槽位強制改寫。開盤防線候選先通過n=2局部樞紐，再有同向攻擊創新極值才取得資格；候選不得冒充作用中防線。反向候選仍須多根結構，不由固定點數或單根OHLC成立。
- AI_HYBRID的course_reading.large_defense_ref與small_defense_ref各代表AI目前採用的該級主控防線，可從ai_hybrid_output_contract列出的同級作用中多方／空方候選擇一或填null；不必機械跟隨作用中錨的方向。dow文字仍須同時交代另一方向已存在的有效或已破防線，不得只挑最新局部低點／高點代表全部道氏。這只是引用合法性，哪一條具有目前控制意義仍由AI依課程判斷。
- 四象限必須逐字服從ledger.anchor_lifecycle.quadrant_context的程式結果。authority=PROGRAM_POLICY_V1時，background_primary／working_primary、兩條軸及candidates皆不可由模型改寫；程式只用同級段的方向端點延伸、幅度與斜率序位比較，不使用固定點數、固定K數或隱藏比率。TRANSITION／UNDEFINED是有效答案，不得為了完整性硬選Q1～Q4。
- 太極必須服從taiji_context.program_state、program_quality、last_copy_status與engine_mode。形成中的複製只可標示形成中及暫定品質，不得提前判失敗；複製失敗也不自動翻空／翻多。dynasty_anchor_ref是目前操作級數的朝代錨，background_dynasty_anchor_ref另保存大級背景；父代時間與點位由程式提供，不得改接其他樞紐。
- 若錨的anchor_origin_kind=SESSION_OPEN，起點是該時段開盤價，所有可見文字必須寫「HH:MM開盤價」，不得改標成該根高點或低點。任何帶時間的開／高／低／收角色都必須與該根已揭露K棒OHLC逐字實值一致。
- 目前工作段必須從ledger.anchor_lifecycle.working_leg逐字解讀；尚未出現新的同級n=2反向樞紐前，即使最近1～2根有反彈、回落或長影線，也不得把原推進提前切成已成立修正。可以描述品質轉差或正在嘗試反向，但工作段的起訖仍服從程式證據。
- scenario與主控文字所寫的「下一個觸發」必須在latest_closed_k之後仍未成立。若目前收盤早已位於某價位上方／下方，禁止再把「收上／收下該價」寫成等待中的未來觸發。拉回延續尚缺反向樞紐或修正極值時，只能寫等待修正結構完成；待可引用的新修正高／低與再發動線出現後，才能建立可執行觸發，不能以單純仍在壓力下／支撐上取代它。
- cclass_mode保留為內部稽核欄位，但使用者可見訊息不顯示「C班」標籤；只顯示「太極：...」。太極文字要直接寫父代起訖與目前修正／複製狀態。
""".strip("\n")
    ai_role_line = (
        "- 本版是AI_HYBRID：程式提供逐根OHLC、因果事件、可引用錨／防線候選、可執行setup候選、硬停損、成交順序與風控邊界；AI才是課程判讀與交易決策者。AI必須依課程判斷哪些候選具有控制意義，並決定觀察、準備、ENTER、續抱、降級或EXIT；不得捏造程式未提供的價格、時間、setup或成交。"
        if ai_hybrid
        else
        "- 本版的錨、防線、號盤、象限、戰法資格、進出場、停損與風控均由程式決定；AI只可解釋程式事實與產生可讀文字，不得成為第二套交易判讀者。original_decision必須服從程式事件與既有通知規則。"
        if course_chain_contract
        else "- 目前設定的AI provider是本回放唯一判讀者；original_decision必須由既有通知規則決定。"
    )
    if ai_hybrid:
        contract_rules += """

- RUNTIME_CONTEXT.ai_hybrid_output_contract是程式依本輪持倉、entry_eligibility、可執行setup集合及當輪事件整理的輸出契約。它只約束訊息類型、setup可執行資格與「尚無任何大級結構權限時，大級象限不得憑空成立」這項一致性，不替AI決定定錨、道氏、象限、太極、戰法、情境或是否採用尚未到期的可執行候選。required_message_type非null時必須逐字服從；allowed_message_types非空時必須從中擇一。持倉中若依課程判斷續抱，使用MANAGEMENT＋position_action=NONE；若判斷主動出場，使用EXIT＋position_action=EXIT，不得讓格式修正改變原交易判斷。PREPARATION只能引用preparation_allowed_setup_keys中的setup；background_quadrant_policy=MUST_BE_UNDEFINED_UNCLEAR時，analysis與memory的大級象限必須為UNDEFINED，兩軸必須為UNCLEAR。
- quadrant_axis_pair_policy=EXACT_QUADRANT_REQUIRED_FOR_COMPLETE_AXES是純輸出一致性契約，不替AI選象限：只要某一級兩軸都不是UNCLEAR／UNSTABLE，就必須依quadrant_axis_mapping填唯一Q1～Q4，禁止再填TRANSITION或UNDEFINED。若AI判定證據仍在轉換或不足，應把尚未確定的軸填UNCLEAR（波動證據不穩定可填UNSTABLE），象限才可填TRANSITION／UNDEFINED；不得一面填完整Q1兩軸、一面把象限標成TRANSITION。analysis.course_reading與memory.structure_control必須使用同一組結果。
- program_trade_policy.actionable_setups=PROGRAM_CANDIDATES_AI_DECISION。程式候選只是已通過客觀資料與安全守門的可評估方案，不是自動交易命令；AI可依課程把候選保存為ARMED，或維持FORMING／NONE並清楚說明缺少的課程條件。
- 只有trade_levels.ai_candidate_inventory中、且setup_key同時列在trade_levels.ai_executable_setup_keys的硬事實合格候選，才可由AI選為ARMED／AGGRESSIVE_CONFIRMED／CONSERVATIVE_CONFIRMED；舊saved run沒有inventory時才回退使用continuation_arm_candidate。空手時AI可以比較全部候選，但memory.active_setups最多只能保留1個可執行候選；entry_eligibility另可指定ENTRY_ELIGIBLE。其餘診斷候選只能作課程判讀證據，不得輸出PREPARATION或保存成可執行setup。
- AI對定錨具有資格判斷權，但引用仍受程式證據限制：large_anchor_ref、small_anchor_ref、working_anchor_ref及reverse_anchor_candidate_ref只能使用對應的ledger.anchor_control候選或null。null表示AI判定候選品質尚不足；不得另選歷史小波段冒充作用中定錨。
- ai_hybrid_output_contract.anchor_role_ref_options把course_reading與memory各角色本輪可填的完整ID逐項列出；兩處都必須從對應陣列選一個值。尤其working_anchor_ref只代表anchor_control目前工作段，不得改填Q4父代、已完成推進段或其他歷史leg；需要解釋父代時寫在taiji／strategy_reason，不改寫working角色ID。
- 模型輸入已移除程式的最終象限、太極、道氏、X階段與scenario_weights答案；保留的是OHLCV、樞紐、同級段比較、父代端點、防線與setup候選。AI必須據此自行判斷大小級象限、太極／一之／左右／道氏、X流程、情境權重及主控戰法；結論須與自己填寫的趨勢／波動兩軸一致，strategy_reason需留下可追溯證據。
- 四象限採固定的同級裁決順序，避免同一盤面任意更換比較尺度：(1)先選已接受的背景錨與控制級數；(2)背景象限只比較該背景錨內最近兩個已完成、同向且確實延伸同方向極值的可比攻擊段；(3)小級工作段另行決定working_quadrant，不得用尚在形成的小級段直接改寫background_quadrant；(4)若最新可比同向攻擊仍延伸且幅度／速度相對擴張，背景偏Q1；仍延伸但幅度／速度相對收縮，背景偏Q4；沒有兩個可比段時使用TRANSITION或UNDEFINED，不得改拿任意5／20根統計補答案。
- 象限轉換採證據遲滯而非逐根跳標籤：原working_quadrant為Q2時，僅看到1～2根K或ATR收斂、但尚未完成新的交替收斂段，只能把Q3列為primary／secondary候選，working_quadrant仍保留Q2；新的多方工作段完成收盤觸發並使Q4 setup進入ENTRY_ELIGIBLE時，小級可立即由修正狀態轉Q1，因為這是重新發動的可執行證據，但交易家族仍歸原Q4。背景象限只有新的同級可比段完成、正式級數事件，或原背景失效時才重判；否則逐字延續previous_semantic_memory的背景兩軸與象限。
- entry_eligibility.status=ENTRY_ELIGIBLE且decision_authority=AI_HYBRID時，AI必須在本輪二選一：ENTER，或以STOP_TOO_WIDE、HARD_OBSTACLE_TOO_CLOSE、GRADE_CONFLICT、CONSTITUTION_BLOCKED中的唯一固定原因否決。不得用「再等一根」「繼續觀察」逃避已到期的進場決策。
- 固定否決原因也必須有事前客觀依據：STOP_TOO_WIDE只在runtime已提供明確數值風險上限且必要結構停損超過上限時使用；沒有數值上限時，不得因固定1口或停損點數看起來大而臨場否決。HARD_OBSTACLE_TOO_CLOSE必須引用HARD_TARGET，CHECKPOINT與順勢前高不是硬障礙。GRADE_CONFLICT只在grade_relation=CONFLICT時使用；CONSTITUTION_BLOCKED只在program_constitution明確鎖定時使用。四者皆無依據時必須ENTER。
- v34的course_reading.main_strategy_family只能填NONE、Q2或Q4。ENTER時必須依entry_eligibility.required_entry_strategy填入對應Q2／Q4家族；太極、道氏、左右、X流程或一之可以支持判讀，但不得取代交易家族或改變績效歸屬。
- v34的ai_course_assessment必須逐字服從ai_hybrid_output_contract.assessment_policy。assessment_policy=MUST_BE_NULL時一律填null，即使正在管理既有持倉，也不得重評已成交或已離開本輪候選清單的舊setup；持倉本身改由action、course_reading與既定behavior_plan管理。assessment_allowed_candidates是本輪唯一可重新評估的setup_key／facts_hash配對；非null評估必須逐字引用其中同一組配對。assessment_policy=REQUIRED_ENTRY_ELIGIBLE_CANDIDATE時必須評估entry_eligibility指定的配對；OPTIONAL_CURRENT_CANDIDATE_ONLY時可評估其中1個或填null。評估時固定填滿anchor_control、grade_control、dow_defense、quadrant_axes、taiji_relation、location_quality、trigger_quality、stop_integrity、expected_behavior、course_permission共10項PASS／FAIL／UNKNOWN，不得省略、改名或用自由文字替代；任何必要項FAIL或UNKNOWN都不得ENTER。
- AI接受ENTER時，setup_key、direction、entry_role及required_stop_price不可改動；required_behavior_max_wait_bars是允許的最長等待窗，AI可使用相同或更短的等待窗。required_behavior_obstacles是程式可核對的觸發／檢查點，AI不得改價或變更角色。
- 本回放固定1口，無法在大小級衝突時再縮小部位。若你在course_reading.grade_relation填CONFLICT，該ENTRY_ELIGIBLE只能輸出position_action=NONE＋entry_rejection_reason=GRADE_CONFLICT；不得一面判定CONFLICT一面ENTER。這是本專案一口執行政策，不是課程原文。
- 硬停損由程式逐根執行。其餘持倉管理由AI依事前expected_behavior、max_wait_bars、太極複製品質、象限與結構變化決定；一旦自己宣告的行為窗失效，本輪必須EXIT，不得無限續抱。
- 停損後不得沿用已消費的舊交叉訊號。只有停損後新形成的回踩收復、假跌破站回、新修正完成或新同級結構觸發，才可建立entry_role=REENTRY；同一setup最多再進1次。
- 使用者可見內容不得稱「程式決定進場／象限／太極」。程式是證據與安全守門；盤勢結論、主鏡頭與交易取捨須明確由本輪AI判讀呈現。
- 使用者可見欄位也不得寫「程式候選」「程式提供」「程式化」或PROGRAM_*內部名稱；直接說目前是否有合格交易候選、缺少哪個課程條件，以及下一步怎麼做。
- message_direction是事件卡主體而非第二個盤勢判斷：ENTER時須跟action.direction一致；持倉中即使盤勢轉弱或中立，仍須跟實際持倉方向一致。市場方向另由large_trend、current_trend與scenario表達。
- 必須服從ai_hybrid_output_contract.action_tuple_policy：required_action_tuple非null時三欄逐字一致；allowed_action_tuples非空時只能選一組。此契約不決定HOLD／EXIT，只防止把既有持倉誤填成新進場。
- 若採用GRADE_UPGRADE並填入明確background_trend_dynamics，大趨勢摘要的強勢／回檔字樣須與自己選擇的升級方向及趨勢軸一致；不得在同一方向內填互相矛盾的重複標籤。
""".strip("\n")
    return f"""<REPLAY_EXECUTION_OVERRIDE>
本段是歷史回放執行限制，優先於MONITOR_RULES的即時取圖及傳輸要求。時段獨立、兩根抽樣、單口／再進、OR5早盤觀察門是本專案操作約定，不冒稱課程原文；本版修正不受課程支持的固定點數／象限覆蓋。正式規則檔唯讀。

- 商品固定為TMF；這是歷史資料模擬，不是即時監控。
- 不得呼叫工具、瀏覽器、網路、Telegram、Codex或讀寫檔案。
- RUNTIME_CONTEXT只包含模擬時間當下已收盤且已揭露的K棒，是唯一行情事實來源。
- structured_market_data_context若status=FRESH，OHLCV、均價、ATR14、開盤區間與因果樞紐均為精確權威值，不得改稱圖面估計或「資料不足」；方向尚未形成時應誠實填盤整或轉換中，驗證器不會替AI改寫這個語意答案。
- spot_market_context是證交所官方加權指數資料。status=PREOPEN時只能使用previous_close，當日現貨開高低收仍未知，可把09:00現貨列為未來確認；status=FRESH時只能使用其中已揭露的一分K；status=UNAVAILABLE時只能說不可得，不得把現貨列為等待或觸發。不得使用spot_market_context未提供的未來現貨值。
- 單根一分K的OHLC不代表盤內高低點發生順序；不得把本根敘述成「先突破再回測」或其他未提供的盤內路徑，只能陳述開高低收、觸及範圍及收盤位置。
- 支撐、壓力與最近障礙必須來自已揭露價格結構。不得把任意ATR投影或整數區間寫成已確認障礙；若只是距離參考，必須明確標示不是結構位。
- 引用structured_market_data_context.causal_structure_n2中的樞紐時，內部機器狀態的時間、kind與price必須精確一致，不得自行補價；使用者可見九欄必須改寫成自然繁體中文，不得顯示機器欄位名稱。
- 趨勢classification必須和details一致；若details判定寬幅區間、方向不明或大級TRANSITION，不得同時把大趨勢分類成偏多但回檔或偏空但反彈。
- detail_bars是replay.detail_bar_count指定的最近已揭露1分K滾動窗；overview_bars包含所有已完整收盤的15分K，較早大級結構另由因果樞紐與ledger保存。兩者均採columns定義欄位、rows依相同順序存值的無損表格格式。不存在圖片，也不得因沒有Chrome而回報資料不足。
- preopen_snapshot=true時，以前一夜盤完整資料建立盤前背景，不建立已經錯過的歷史模擬成交。
- preopen_snapshot=true表示夜盤已完全收盤、下一個有效期貨K是日盤08:45；不得宣稱05:00是未收盤K，不得把「下一根1分K」當成夜盤延續觸發；只可規劃待日盤開盤資料重建後再評估的情境。
- deterministic_constraints是程式依權威結構資料產生的硬限制。分析分類、memory方向、時段與持倉必須符合，不得由文字裁量覆蓋。
- 日盤逐K時，每輪處理newly_revealed_bar_count所指的一根或多根新收盤K，必須依時間順序檢查每根；expected_latest_closed_k_iso是本輪最後一根，不得使用其後K棒。
{contract_rules}
{ai_role_line}
- analysis只負責正式九欄可見內容；memory只供下一根K判讀，不得渲染成第十欄。
- 保留九欄與必要證據，但避免在多個欄位重複同一事實；每個趨勢以2至3項關鍵證據、其他欄位以必要條件為主，不要為填滿schema上限而增加條目。
- analysis.latest_closed_k_price_estimate只能寫價格或價格區間，禁止放入日期、HH:MM或K棒時間；時間由程式固定渲染。
- analysis.latest_closed_k_details不得重複expected_latest_closed_k_hhmm或current_unclosed_k_hhmm；若要描述該根K，直接以「本根已收盤K」起句，不要再寫時間。
- 最終只回傳符合ANALYSIS_SCHEMA的單一JSON object。
</REPLAY_EXECUTION_OVERRIDE>
<ANALYSIS_SCHEMA_REFERENCE>
完整schema已由submit_replay_analysis函式參數提供；必須以一次函式呼叫提交，不得自行改變頂層或欄位結構。schema字元數={len(schema_text)}。
</ANALYSIS_SCHEMA_REFERENCE>
<MONITOR_RULES>
{replay_rules}
</MONITOR_RULES>
{correction_block}
<RUNTIME_CONTEXT>
{runtime_json}
</RUNTIME_CONTEXT>
"""


_AI_HYBRID_ACTIONABLE_STAGES = (
    "ARMED",
    "ENTRY_ELIGIBLE",
    "AGGRESSIVE_CONFIRMED",
    "CONSERVATIVE_CONFIRMED",
)


def _ai_hybrid_output_contract(runtime_context: Mapping[str, Any]) -> dict[str, Any]:
    """Summarize deterministic card constraints for the current AI turn.

    The replay ledger intentionally exposes more than one diagnostic candidate.
    In particular, a FALSE_BREAK_RECLAIM can remain observation-only while a
    different continuation setup is executable.  Encoding that distinction in
    one small object avoids contradictory prompt inferences without moving any
    course interpretation or trade selection from AI to the program.
    """

    ledger = runtime_context.get("deterministic_evidence_ledger")
    ledger = ledger if isinstance(ledger, Mapping) else {}
    trade_levels = ledger.get("trade_levels")
    trade_levels = trade_levels if isinstance(trade_levels, Mapping) else {}
    position = runtime_context.get("simulated_position_state")
    position = position if isinstance(position, Mapping) else {}
    eligibility = runtime_context.get("entry_eligibility")
    eligibility = eligibility if isinstance(eligibility, Mapping) else {}

    position_status = str(position.get("status") or "UNKNOWN").upper()
    eligibility_status = str(eligibility.get("status") or "NONE").upper()
    executable_keys = sorted(
        {
            str(value)
            for value in trade_levels.get("ai_executable_setup_keys", [])
            if isinstance(value, str) and value.strip()
        }
    )
    armable_values = trade_levels.get("ai_armable_setup_keys")
    armable_keys = (
        {
            str(value)
            for value in armable_values
            if isinstance(value, str) and value.strip()
        }
        if isinstance(armable_values, list)
        else None
    )

    # ``ai_course_assessment`` evaluates the current hard-fact candidate
    # version; it is not a second copy of held-position management.  Publish
    # exact setup/hash pairs so a model cannot accidentally reassess a filled
    # setup that has already left the current candidate inventory.
    assessment_candidates_by_pair: dict[tuple[str, str], dict[str, str]] = {}
    raw_inventory = trade_levels.get("ai_candidate_inventory")
    if isinstance(raw_inventory, list):
        for item in raw_inventory:
            if not isinstance(item, Mapping):
                continue
            setup_key = str(item.get("setup_key") or "")
            facts_hash = str(item.get("facts_hash") or "")
            if (
                setup_key
                and facts_hash
                and (armable_keys is None or setup_key in armable_keys)
            ):
                assessment_candidates_by_pair[(setup_key, facts_hash)] = {
                    "setup_key": setup_key,
                    "facts_hash": facts_hash,
                }
    legacy_candidate = trade_levels.get("continuation_arm_candidate")
    if isinstance(legacy_candidate, Mapping):
        setup_key = str(legacy_candidate.get("setup_key") or "")
        facts_hash = str(legacy_candidate.get("facts_hash") or "")
        if (
            setup_key
            and facts_hash
            and (armable_keys is None or setup_key in armable_keys)
        ):
            assessment_candidates_by_pair.setdefault(
                (setup_key, facts_hash),
                {"setup_key": setup_key, "facts_hash": facts_hash},
            )

    gate_setup_key = str(eligibility.get("setup_key") or "")
    gate_facts_hash = str(eligibility.get("required_facts_hash") or "")
    if gate_setup_key and gate_facts_hash:
        assessment_candidates_by_pair[(gate_setup_key, gate_facts_hash)] = {
            "setup_key": gate_setup_key,
            "facts_hash": gate_facts_hash,
        }
    assessment_allowed_candidates = [
        assessment_candidates_by_pair[pair]
        for pair in sorted(assessment_candidates_by_pair)
    ]
    if eligibility_status == "ENTRY_ELIGIBLE" and gate_setup_key and gate_facts_hash:
        assessment_policy = "REQUIRED_ENTRY_ELIGIBLE_CANDIDATE"
    elif assessment_allowed_candidates:
        assessment_policy = "OPTIONAL_CURRENT_CANDIDATE_ONLY"
    else:
        assessment_policy = "MUST_BE_NULL"

    current_event_ids: set[str] = set()
    directly_named_event_types: set[str] = set()
    evidence_events = runtime_context.get("deterministic_evidence_events")
    if isinstance(evidence_events, list):
        for event in evidence_events:
            if not isinstance(event, Mapping):
                continue
            event_id = event.get("event_id") or event.get("id")
            if event_id:
                current_event_ids.add(str(event_id))
            event_type = str(event.get("event_type") or "")
            if event_type and event_type != "STRUCTURE_EVENT":
                directly_named_event_types.add(event_type)

    current_structure_events: list[Mapping[str, Any]] = []
    structure_events = ledger.get("structure_events")
    if isinstance(structure_events, list):
        for event in structure_events:
            if not isinstance(event, Mapping):
                continue
            event_id = event.get("id") or event.get("event_id")
            if event_id and str(event_id) in current_event_ids:
                current_structure_events.append(event)

    current_structure_types = {
        str(event.get("event_type") or "")
        for event in current_structure_events
        if event.get("event_type")
    }
    current_structure_types.update(
        event_type
        for event_type in directly_named_event_types
        if event_type
        in {
            "FALSE_BREAK_RECLAIM",
            "GRADE_UPGRADE",
            "GRADE_DOWNGRADE",
            "STRUCTURE_INVALIDATED",
        }
    )
    false_break_refs = sorted(
        str(event.get("id") or event.get("event_id"))
        for event in current_structure_events
        if event.get("event_type") == "FALSE_BREAK_RECLAIM"
        and (event.get("id") or event.get("event_id"))
    )
    current_upgrade_refs = sorted(
        str(event.get("id") or event.get("event_id"))
        for event in current_structure_events
        if event.get("event_type") == "GRADE_UPGRADE"
        and (event.get("id") or event.get("event_id"))
    )

    anchor_control = ledger.get("anchor_control")
    anchor_control = anchor_control if isinstance(anchor_control, Mapping) else {}
    active_background_ref = str(
        anchor_control.get("active_background_anchor_ref") or ""
    ) or None
    all_structure_events = [
        event
        for event in (structure_events if isinstance(structure_events, list) else [])
        if isinstance(event, Mapping)
    ]
    superseded_upgrade_refs = {
        str(event.get("source_upgrade_event_id"))
        for event in all_structure_events
        if event.get("event_type") in {"GRADE_DOWNGRADE", "STRUCTURE_INVALIDATED"}
        and event.get("source_upgrade_event_id")
    }
    active_upgrade_events = [
        event
        for event in all_structure_events
        if event.get("event_type") == "GRADE_UPGRADE"
        and event.get("from_level") == "SMALL"
        and event.get("to_level") == "LARGE"
        and event.get("direction") in {"BULL", "BEAR"}
        and event.get("source_anchor_id")
        and str(event.get("id") or "") not in superseded_upgrade_refs
    ]
    active_upgrade_options = [
        {
            "event_ref": str(event.get("id") or event.get("event_id")),
            "controlling_anchor_ref": str(event.get("source_anchor_id")),
            "direction": str(event.get("direction")),
            "replacement_defense_ref": (
                str(event.get("replacement_defense_pivot_id"))
                if event.get("replacement_defense_pivot_id")
                else None
            ),
        }
        for event in sorted(
            active_upgrade_events,
            key=lambda item: (str(item.get("first_seen_at") or ""), str(item.get("id") or "")),
        )
    ]
    current_upgrade_options = [
        {
            "event_ref": str(event.get("id") or event.get("event_id")),
            "controlling_anchor_ref": (
                active_background_ref
                or str(event.get("source_anchor_id") or "")
                or None
            ),
        }
        for event in current_structure_events
        if event.get("event_type") == "GRADE_UPGRADE"
        and (event.get("id") or event.get("event_id"))
    ]
    previous_memory = runtime_context.get("previous_semantic_memory")
    if not isinstance(previous_memory, Mapping):
        previous_memory = runtime_context.get("previous_replay_memory")
    previous_control = (
        previous_memory.get("structure_control")
        if isinstance(previous_memory, Mapping)
        else None
    )
    previous_large_ref = (
        str(previous_control.get("active_large_anchor_ref") or "") or None
        if isinstance(previous_control, Mapping)
        else None
    )
    background_quadrant_policy = (
        "MUST_BE_UNDEFINED_UNCLEAR"
        if not active_background_ref
        and not previous_large_ref
        and not active_upgrade_options
        else "AI_COURSE_JUDGMENT_WITH_VALID_LARGE_STRUCTURE"
    )

    def role_options(*values: object) -> list[str | None]:
        result: list[str | None] = [None]
        for value in values:
            ref = str(value or "") or None
            if ref is not None and ref not in result:
                result.append(ref)
        return result

    active_upgrade_anchor_refs = [
        item.get("controlling_anchor_ref")
        for item in active_upgrade_options
        if isinstance(item, Mapping)
    ]
    anchor_role_ref_options = {
        "large_anchor_ref": role_options(
            active_background_ref,
            previous_large_ref,
            *active_upgrade_anchor_refs,
        ),
        "small_anchor_ref": role_options(
            anchor_control.get("active_child_anchor_ref")
        ),
        "working_anchor_ref": role_options(anchor_control.get("working_leg_ref")),
        "reverse_anchor_candidate_ref": role_options(
            anchor_control.get("reverse_anchor_candidate_ref")
        ),
    }

    false_break_candidate = trade_levels.get("false_break_arm_candidate")
    false_break_candidate = (
        false_break_candidate if isinstance(false_break_candidate, Mapping) else {}
    )
    continuation = trade_levels.get("continuation_arm_candidate")
    continuation = continuation if isinstance(continuation, Mapping) else {}
    if continuation.get("candidate_source") == "FALSE_BREAK_RECLAIM":
        false_break_candidate = continuation

    false_break_setup_key = str(false_break_candidate.get("setup_key") or "") or None
    false_break_stage = str(false_break_candidate.get("stage") or "NONE").upper()
    false_break_execution_status = str(
        false_break_candidate.get("execution_status") or "UNSPECIFIED"
    ).upper()
    false_break_actionable = bool(
        false_break_setup_key in executable_keys
        and false_break_stage in _AI_HYBRID_ACTIONABLE_STAGES
        and false_break_execution_status != "OBSERVATION_ONLY"
    )

    protective_stop = ledger.get("protective_stop_audit")
    protective_stop = protective_stop if isinstance(protective_stop, Mapping) else {}
    behavior_exit = ledger.get("program_behavior_exit_audit")
    behavior_exit = behavior_exit if isinstance(behavior_exit, Mapping) else {}
    stop_status = str(protective_stop.get("status") or "NOT_APPLICABLE").upper()
    exit_status = str(behavior_exit.get("status") or "NOT_APPLICABLE").upper()

    required_message_type: str | None = None
    required_reason = "AI_COURSE_JUDGMENT"
    if stop_status == "TRIGGERED":
        required_message_type = "STOP"
        required_reason = "PROGRAM_HARD_STOP_TRIGGERED"
    elif exit_status in {"PENDING_FILL", "TRIGGERED"}:
        required_message_type = "EXIT"
        required_reason = "PROGRAM_EXIT_LOCKED"
    elif position_status in {"LONG", "SHORT"}:
        # With an open position the model owns the course-level choice between
        # continuing management and a discretionary exit.  A single required
        # MANAGEMENT label made an explicit EXIT action fail validation and
        # allowed a retry to change the trading decision.  Keep only the two
        # legal card types in the compact contract; program stops/locked exits
        # above remain mandatory and take precedence.
        required_message_type = None
        required_reason = "OPEN_POSITION_AI_MANAGEMENT_OR_EXIT"
    elif eligibility_status == "ENTRY_ELIGIBLE":
        required_message_type = "PREPARATION"
        required_reason = "ENTRY_ELIGIBLE_DECISION_DUE"
    elif false_break_refs and not (
        current_structure_types
        & {"GRADE_UPGRADE", "GRADE_DOWNGRADE", "STRUCTURE_INVALIDATED"}
    ):
        if not false_break_actionable:
            required_message_type = "OBSERVATION"
            required_reason = "FALSE_BREAK_RECLAIM_OBSERVATION_ONLY"

    false_break_policy = (
        "STOP_OVERRIDES_FALSE_BREAK"
        if required_message_type == "STOP"
        else "EXIT_OVERRIDES_FALSE_BREAK"
        if required_message_type == "EXIT"
        else "POSITION_MANAGEMENT_OR_AI_EXIT"
        if position_status in {"LONG", "SHORT"}
        else "PREPARATION_ALLOWED_IF_AI_SELECTS_ACTIONABLE_SETUP"
        if false_break_actionable
        else "OBSERVATION_ONLY"
        if false_break_refs
        else "NOT_CURRENT_EVENT"
    )
    lifecycle = ledger.get("anchor_lifecycle")
    dow_context = lifecycle.get("dow_context") if isinstance(lifecycle, Mapping) else None

    def directional_defenses(level: str) -> dict[str, str | None]:
        result: dict[str, str | None] = {"BULL": None, "BEAR": None}
        if isinstance(dow_context, Mapping):
            for direction, suffix in (("BULL", "bull"), ("BEAR", "bear")):
                defense = dow_context.get(f"{level}_{suffix}_defense")
                if (
                    isinstance(defense, Mapping)
                    and defense.get("state") == "ACTIVE"
                    and defense.get("id")
                ):
                    result[direction] = str(defense["id"])
        if level == "large":
            for event in active_upgrade_events:
                direction = str(event.get("direction") or "")
                defense_ref = event.get("replacement_defense_pivot_id")
                if direction in result and defense_ref:
                    result[direction] = str(defense_ref)
        return result

    def compact_fields(value: object, fields: tuple[str, ...]) -> dict[str, Any] | None:
        if not isinstance(value, Mapping):
            return None
        return {field: value.get(field) for field in fields}

    position_direction = (
        "BULL" if position_status == "LONG" else "BEAR" if position_status == "SHORT" else None
    )
    behavior_plan = position.get("behavior_plan")
    behavior_plan = behavior_plan if isinstance(behavior_plan, Mapping) else {}
    behavior_audit = ledger.get("position_behavior_audit")
    behavior_audit = behavior_audit if isinstance(behavior_audit, Mapping) else {}
    lifecycle = ledger.get("anchor_lifecycle")
    lifecycle = lifecycle if isinstance(lifecycle, Mapping) else {}
    child_anchor = lifecycle.get("child_anchor")
    child_anchor = child_anchor if isinstance(child_anchor, Mapping) else {}
    taiji_context = lifecycle.get("taiji_context")
    taiji_context = taiji_context if isinstance(taiji_context, Mapping) else {}
    leg_evidence = taiji_context.get("leg_evidence")
    leg_evidence = leg_evidence if isinstance(leg_evidence, Mapping) else {}
    current_leg = leg_evidence.get("current_leg")
    current_leg = current_leg if isinstance(current_leg, Mapping) else {}
    current_parent = leg_evidence.get("current_parent")
    current_parent = current_parent if isinstance(current_parent, Mapping) else {}

    def same_direction_defense_evidence() -> list[dict[str, Any]]:
        if position_direction is None or not isinstance(dow_context, Mapping):
            return []
        suffix = "bull" if position_direction == "BULL" else "bear"
        result: list[dict[str, Any]] = []
        for level in ("large", "small"):
            defense = dow_context.get(f"{level}_{suffix}_defense")
            if not isinstance(defense, Mapping) or defense.get("state") != "ACTIVE":
                continue
            result.append(
                {
                    "level": level.upper(),
                    "ref": defense.get("id"),
                    "time": defense.get("time"),
                    "price": defense.get("price"),
                    "state": defense.get("state"),
                }
            )
        return result

    current_end = current_leg.get("end_price")
    parent_end = current_parent.get("end_price")
    same_direction_leg_extends_parent: bool | None = None
    if (
        position_direction is not None
        and current_leg.get("direction") == position_direction
        and current_parent.get("direction") == position_direction
        and isinstance(current_end, (int, float))
        and not isinstance(current_end, bool)
        and isinstance(parent_end, (int, float))
        and not isinstance(parent_end, bool)
    ):
        same_direction_leg_extends_parent = (
            current_end > parent_end
            if position_direction == "BULL"
            else current_end < parent_end
        )

    open_position_management_contract: dict[str, Any] = {
        "status": "NOT_APPLICABLE"
    }
    if position_status in {"LONG", "SHORT"}:
        controller = None
        if (
            child_anchor.get("direction") == position_direction
            and str(child_anchor.get("status") or "").upper()
            in {"ACTIVE", "DEGRADED_RECLAIMED"}
        ):
            controller = compact_fields(
                child_anchor,
                (
                    "id",
                    "level",
                    "direction",
                    "status",
                    "origin_time",
                    "origin_price",
                    "latest_extreme_time",
                    "latest_extreme_price",
                ),
            )
        open_position_management_contract = {
            "status": "ACTIVE",
            "active_setup_key": position.get("active_setup_key"),
            "position_direction": position_direction,
            "entry_time": position.get("entry_time"),
            "entry_price": position.get("entry_price"),
            "current_stop_price": position.get("stop_price"),
            "accepted_main_strategy_family": behavior_plan.get("main_strategy_family"),
            "accepted_entry_strategy": behavior_plan.get("entry_strategy"),
            "accepted_behavior_plan": compact_fields(
                behavior_plan,
                (
                    "expected_behavior",
                    "max_wait_bars",
                    "trigger_level",
                    "obstacles",
                    "policy",
                    "post_achievement_policy",
                ),
            ),
            "behavior_audit": compact_fields(
                behavior_audit,
                (
                    "status",
                    "bars_since_entry",
                    "max_wait_bars",
                    "checkpoint_price",
                    "checkpoint_label",
                    "initial_r",
                    "achieved_at",
                    "failed_at",
                    "failure_reason",
                ),
            ),
            "same_direction_controller": controller,
            "active_same_direction_defenses": same_direction_defense_evidence(),
            "taiji_leg_evidence": {
                "relation": taiji_context.get("current_relation"),
                "current_leg": compact_fields(
                    current_leg,
                    (
                        "start_time",
                        "start_price",
                        "end_time",
                        "end_price",
                        "direction",
                        "amplitude_points",
                        "duration_minutes",
                        "slope_points_per_minute",
                        "status",
                    ),
                ),
                "parent_leg": compact_fields(
                    current_parent,
                    (
                        "start_time",
                        "start_price",
                        "end_time",
                        "end_price",
                        "direction",
                        "amplitude_points",
                        "duration_minutes",
                        "slope_points_per_minute",
                        "status",
                    ),
                ),
                "same_direction_leg_extends_parent_extreme": (
                    same_direction_leg_extends_parent
                ),
                "interpretation_authority": "AI_COURSE_JUDGMENT",
            },
            "decision_policy": (
                "FROZEN_ENTRY_FAMILY_AND_PREDECLARED_EXIT_BASIS"
            ),
        }

    return {
        "version": "ai-hybrid-output-contract-v7",
        "position_status": position_status,
        "entry_eligibility_status": eligibility_status,
        "required_message_type": required_message_type,
        "required_reason": required_reason,
        "allowed_message_types": (
            ["MANAGEMENT", "EXIT"]
            if position_status in {"LONG", "SHORT"}
            and stop_status != "TRIGGERED"
            and exit_status not in {"PENDING_FILL", "TRIGGERED"}
            else [required_message_type]
            if required_message_type
            else []
        ),
        "action_tuple_policy": "EXACT_POSITION_ACTION_DIRECTION_ENTRY_ROLE",
        "required_action_tuple": (
            {
                "position_action": "STOP",
                "direction": "NONE",
                "entry_role": "NOT_APPLICABLE",
            }
            if stop_status == "TRIGGERED"
            else {
                "position_action": "EXIT",
                "direction": "NONE",
                "entry_role": "NOT_APPLICABLE",
            }
            if exit_status in {"PENDING_FILL", "TRIGGERED"}
            else None
        ),
        "allowed_action_tuples": (
            [
                {
                    "position_action": "NONE",
                    "direction": "NONE",
                    "entry_role": "NOT_APPLICABLE",
                },
                {
                    "position_action": "EXIT",
                    "direction": "NONE",
                    "entry_role": "NOT_APPLICABLE",
                },
            ]
            if position_status in {"LONG", "SHORT"}
            and stop_status != "TRIGGERED"
            and exit_status not in {"PENDING_FILL", "TRIGGERED"}
            else []
        ),
        **(
            {"open_position_management_contract": open_position_management_contract}
            if open_position_management_contract["status"] == "ACTIVE"
            else {}
        ),
        "background_quadrant_policy": background_quadrant_policy,
        "quadrant_axis_pair_policy": (
            "EXACT_QUADRANT_REQUIRED_FOR_COMPLETE_AXES"
        ),
        "quadrant_axis_mapping": {
            "INCREASING|EXPANDING": "Q1",
            "DECREASING|EXPANDING": "Q2",
            "DECREASING|CONTRACTING": "Q3",
            "INCREASING|CONTRACTING": "Q4",
        },
        "active_background_anchor_ref": active_background_ref,
        "previous_ai_large_anchor_ref": previous_large_ref,
        "anchor_role_ref_options": anchor_role_ref_options,
        "current_grade_upgrade_refs": current_upgrade_refs,
        "current_grade_upgrade_options": current_upgrade_options,
        # Unlike current_grade_upgrade_options, this list is durable. It lets
        # a later supervised/targeted AI turn adopt an upgrade that remains
        # structurally active without pretending the event happened again.
        "active_grade_upgrade_options": active_upgrade_options,
        "directional_defense_policy": (
            "AI_SELECTS_ONE_ACTIVE_SAME_GRADE_BULL_OR_BEAR_DEFENSE_OR_NULL"
        ),
        "large_active_directional_defense_refs": directional_defenses("large"),
        "small_active_directional_defense_refs": directional_defenses("small"),
        "preparation_allowed_setup_keys": executable_keys,
        "assessment_policy": assessment_policy,
        "assessment_allowed_candidates": assessment_allowed_candidates,
        "actionable_setup_stages": list(_AI_HYBRID_ACTIONABLE_STAGES),
        "current_false_break_reclaim_refs": false_break_refs,
        "false_break_reclaim_setup_key": false_break_setup_key,
        "false_break_reclaim_stage": false_break_stage,
        "false_break_reclaim_execution_status": false_break_execution_status,
        "false_break_reclaim_policy": false_break_policy,
    }


def parse_json_object(text: str) -> dict[str, Any]:
    candidate = str(text or "").strip()
    fence = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", candidate, flags=re.DOTALL | re.IGNORECASE)
    if fence:
        candidate = fence.group(1).strip()
    try:
        payload = json.loads(candidate)
    except json.JSONDecodeError:
        first = candidate.find("{")
        if first < 0:
            raise MiniMaxReplayError("minimax_output_not_json", "MiniMax輸出不是JSON object。")
        try:
            payload, end = json.JSONDecoder().raw_decode(candidate[first:])
        except json.JSONDecodeError as exc:
            raise MiniMaxReplayError("minimax_output_not_json", "MiniMax輸出不是有效JSON object。") from exc
    if not isinstance(payload, dict):
        raise MiniMaxReplayError("minimax_output_not_object", "MiniMax輸出必須是JSON object。")
    return payload


def normalize_tool_arguments(value: dict[str, Any]) -> tuple[dict[str, Any], dict[str, int]]:
    """Normalize narrowly defined provider transport artifacts.

    The trailing ``},{`` repair is intentionally limited to a complete prose
    sentence ending in Chinese punctuation.  Embedded fragments, suffixes after
    an unfinished character, replacement characters and invisible controls are
    left untouched so the semantic corruption guard can reject them.
    """

    stats = {
        "array_wrappers": 0,
        "null_fields": 0,
        "trailing_serialization_suffixes": 0,
    }

    def visit(item: Any) -> Any:
        if isinstance(item, dict):
            if set(item) == {"item"} and isinstance(item["item"], list):
                stats["array_wrappers"] += 1
                return [visit(child) for child in item["item"]]
            return {key: visit(child) for key, child in item.items()}
        if isinstance(item, list):
            return [visit(child) for child in item]
        if isinstance(item, str):
            repaired = re.sub(r"(?<=[。！？])\s*\},\{\s*$", "", item)
            if repaired != item:
                stats["trailing_serialization_suffixes"] += 1
                return repaired
        return item

    normalized = visit(value)
    if not isinstance(normalized, dict):
        raise MiniMaxReplayError("minimax_output_not_object", "MiniMax函式參數必須是JSON object。")
    memory = normalized.get("memory")
    if isinstance(memory, dict):
        if isinstance(memory.get("active_setup"), str) and memory["active_setup"].strip().lower() in {"null", "none"}:
            memory["active_setup"] = None
            stats["null_fields"] += 1
        position = memory.get("position")
        if isinstance(position, dict):
            for key in ("entry_time", "entry_price", "stop_price"):
                item = position.get(key)
                if isinstance(item, str) and item.strip().lower() in {"null", "none", ""}:
                    position[key] = None
                    stats["null_fields"] += 1
    return normalized, stats


def replay_rule_text(rules_text: str) -> str:
    """Remove production-only I/O chapters while retaining trading semantics.

    The immutable source hash still covers the complete production rule file.  The
    replay execution override replaces only Chrome/dual-view and adapter/heartbeat
    mechanics, which cannot run against historical structured bars.
    """
    text = str(rules_text)
    text = re.sub(
        r"\n## 1\. 讀圖與操作安全\n.*?(?=\n## 2\. 資料紀律與時間錨定\n)",
        "\n",
        text,
        flags=re.DOTALL,
    )
    text = re.sub(
        r"\n## 16\. 結構化 adapter 固定介面\n.*?(?=\n## 17\. 執行期 fail-safe 與相容性\n)",
        "\n",
        text,
        flags=re.DOTALL,
    )
    return text.strip()


def _post_json(url: str, headers: dict[str, str], payload: dict[str, Any], timeout: float) -> dict[str, Any]:
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        response = client.post(url, headers=headers, json=payload)
        response.raise_for_status()
        data = response.json()
    if not isinstance(data, dict):
        raise ValueError("provider response is not an object")
    return data


def _safe_error(error: object) -> str:
    text = re.sub(r"(?:bot)?\d{5,}:[A-Za-z0-9_-]{20,}", "[REDACTED_TOKEN]", str(error or "request failed"))
    return re.sub(r"\s+", " ", text).strip()[:500]
