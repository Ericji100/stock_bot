from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import tomllib
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence, TextIO
from zoneinfo import ZoneInfo

from .analysis_adapter import finalize_analysis, prepare_capture
from .analysis_contract import AnalysisValidationError, validate_analysis_payload
from .bridge import BridgeError, DeliveryFileLock, execute_bridge, generate_event_id
from .dual_scale import (
    DualScaleError,
    detail_capture_guard,
    load_overview_context,
    record_detail_capture,
)
from .local_outbox import publish_event
from .market_structure_state import MarketStructureStateError, validate_market_structure_transition
from .resume_guard import begin_monitor_run, complete_monitor_run
from .presentation_zh import operational_error_label
from .structured_market_data import load_structured_market_snapshot


TAIPEI = ZoneInfo("Asia/Taipei")
DEFAULT_CONFIG_PATH = Path("trade_monitor/local_scheduler_config.json")
DEFAULT_RUNTIME_ROOT = Path(".runtime/trade_monitor")
DEFAULT_AUTOMATION_FILE = Path.home() / ".codex" / "automations" / "1-k" / "automation.toml"
DEFAULT_TELEGRAM_CONFIG = Path("config.json")
FAILURE_NOTIFY_INTERVAL_SECONDS = 300


class LocalSchedulerError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class SchedulerConfig:
    project_root: Path
    automation_id: str
    align_second: int
    analysis_timeout_seconds: float
    keep_capture_count: int
    prompt_version: str
    prompt_sha256: str
    prompt_path: Path
    schema_path: Path
    capture_script_path: Path
    capture_options: dict[str, int | float]
    market_structure_state_enabled: bool = False
    market_structure_state_version: int = 2
    unified_lens_selection_enabled: bool = False
    dual_scale_enabled: bool = False
    dual_scale_state_path: Path | None = None
    dual_scale_overview_max_age_minutes: int = 20
    structured_market_data_enabled: bool = False
    structured_market_data_source_path: Path | None = None
    structured_market_data_max_age_seconds: int = 90


def next_minute_slot(now: datetime, align_second: int) -> datetime:
    if now.tzinfo is None or now.utcoffset() is None:
        raise LocalSchedulerError("clock_timezone_missing", "Scheduler clock must include a timezone")
    if not 0 <= align_second <= 59:
        raise LocalSchedulerError("align_second_invalid", "align_second must be between 0 and 59")
    target = now.replace(second=align_second, microsecond=0)
    if target <= now:
        target += timedelta(minutes=1)
    return target


def load_scheduler_config(path: Path) -> SchedulerConfig:
    config_path = path.resolve()
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise LocalSchedulerError("config_missing", "Local scheduler config is missing") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise LocalSchedulerError("config_invalid", "Local scheduler config is invalid") from exc
    if not isinstance(raw, dict) or raw.get("version") != 1:
        raise LocalSchedulerError("config_invalid", "Local scheduler config version is invalid")

    project_root_value = raw.get("project_root")
    if project_root_value is None:
        relative_paths = tuple(
            Path(value)
            for value in (raw.get("prompt_path"), raw.get("schema_path"), raw.get("capture_script_path"))
            if isinstance(value, str) and value.strip() and not Path(value).is_absolute()
        )
        project_root = config_path.parent.parent
        if len(relative_paths) == 3:
            for candidate in (config_path.parent, *config_path.parents):
                if all((candidate / relative_path).is_file() for relative_path in relative_paths):
                    project_root = candidate.resolve()
                    break
    elif isinstance(project_root_value, str) and project_root_value.strip():
        configured_root = Path(project_root_value.strip())
        project_root = (
            configured_root.resolve()
            if configured_root.is_absolute()
            else (config_path.parent / configured_root).resolve()
        )
    else:
        raise LocalSchedulerError("config_invalid", "project_root must be a non-empty path string")
    capture = raw.get("capture")
    if not isinstance(capture, dict):
        raise LocalSchedulerError("config_invalid", "Local scheduler capture config is invalid")
    capture_options: dict[str, int | float] = {}
    for key in (
        "crop_x",
        "crop_y",
        "crop_width",
        "crop_height",
        "minimum_window_width",
        "minimum_window_height",
        "minimum_signal_samples",
    ):
        value = capture.get(key)
        if not isinstance(value, int):
            raise LocalSchedulerError("config_invalid", f"Capture option {key} must be an integer")
        capture_options[key] = value
    dark_ratio = capture.get("minimum_dark_ratio")
    if not isinstance(dark_ratio, (int, float)):
        raise LocalSchedulerError("config_invalid", "minimum_dark_ratio must be numeric")
    capture_options["minimum_dark_ratio"] = float(dark_ratio)

    market_structure_state_enabled = raw.get("market_structure_state_enabled", False)
    if not isinstance(market_structure_state_enabled, bool):
        raise LocalSchedulerError("config_invalid", "market_structure_state_enabled must be boolean")
    market_structure_state_version = raw.get("market_structure_state_version", 2)
    if (
        isinstance(market_structure_state_version, bool)
        or not isinstance(market_structure_state_version, int)
        or market_structure_state_version not in {2, 3, 4, 5, 6}
    ):
        raise LocalSchedulerError("config_invalid", "market_structure_state_version must be 2, 3, 4, 5, or 6")
    unified_lens_selection_enabled = raw.get("unified_lens_selection_enabled", False)
    if not isinstance(unified_lens_selection_enabled, bool):
        raise LocalSchedulerError("config_invalid", "unified_lens_selection_enabled must be boolean")
    dual_scale = raw.get("dual_scale")
    dual_scale_enabled = False
    dual_scale_state_path: Path | None = None
    dual_scale_overview_max_age_minutes = 20
    if dual_scale is not None:
        if not isinstance(dual_scale, dict):
            raise LocalSchedulerError("config_invalid", "dual_scale must be an object")
        dual_scale_enabled = dual_scale.get("enabled", False)
        if not isinstance(dual_scale_enabled, bool):
            raise LocalSchedulerError("config_invalid", "dual_scale.enabled must be boolean")
        state_path_value = dual_scale.get("state_path", ".runtime/trade_monitor/dual_scale_state.json")
        dual_scale_state_path = _resolve_project_path(project_root, state_path_value)
        max_age_value = dual_scale.get("overview_max_age_minutes", 20)
        if not isinstance(max_age_value, int) or max_age_value <= 0:
            raise LocalSchedulerError(
                "config_invalid",
                "dual_scale.overview_max_age_minutes must be a positive integer",
            )
        dual_scale_overview_max_age_minutes = max_age_value
    structured_market_data = raw.get("structured_market_data")
    structured_market_data_enabled = False
    structured_market_data_source_path: Path | None = None
    structured_market_data_max_age_seconds = 90
    if structured_market_data is not None:
        if not isinstance(structured_market_data, dict):
            raise LocalSchedulerError("config_invalid", "structured_market_data must be an object")
        structured_market_data_enabled = structured_market_data.get("enabled", False)
        if not isinstance(structured_market_data_enabled, bool):
            raise LocalSchedulerError("config_invalid", "structured_market_data.enabled must be boolean")
        source_path_value = structured_market_data.get(
            "source_path", ".runtime/trade_monitor/structured-market-data.json"
        )
        structured_market_data_source_path = _resolve_project_path(project_root, source_path_value)
        data_max_age = structured_market_data.get("max_age_seconds", 90)
        if not isinstance(data_max_age, int) or isinstance(data_max_age, bool) or data_max_age <= 0:
            raise LocalSchedulerError(
                "config_invalid", "structured_market_data.max_age_seconds must be a positive integer"
            )
        structured_market_data_max_age_seconds = data_max_age
    config = SchedulerConfig(
        project_root=project_root,
        automation_id=str(raw.get("automation_id") or "").strip(),
        align_second=int(raw.get("align_second", -1)),
        analysis_timeout_seconds=float(raw.get("analysis_timeout_seconds", 0)),
        keep_capture_count=int(raw.get("keep_capture_count", 0)),
        prompt_version=str(raw.get("prompt_version") or "").strip(),
        prompt_sha256=str(raw.get("prompt_sha256") or "").strip(),
        prompt_path=_resolve_project_path(project_root, raw.get("prompt_path")),
        schema_path=_resolve_project_path(project_root, raw.get("schema_path")),
        capture_script_path=_resolve_project_path(project_root, raw.get("capture_script_path")),
        capture_options=capture_options,
        market_structure_state_enabled=market_structure_state_enabled,
        market_structure_state_version=market_structure_state_version,
        unified_lens_selection_enabled=unified_lens_selection_enabled,
        dual_scale_enabled=dual_scale_enabled,
        dual_scale_state_path=dual_scale_state_path,
        dual_scale_overview_max_age_minutes=dual_scale_overview_max_age_minutes,
        structured_market_data_enabled=structured_market_data_enabled,
        structured_market_data_source_path=structured_market_data_source_path,
        structured_market_data_max_age_seconds=structured_market_data_max_age_seconds,
    )
    if not config.automation_id:
        raise LocalSchedulerError("config_invalid", "automation_id is required")
    if not 0 <= config.align_second <= 59:
        raise LocalSchedulerError("config_invalid", "align_second must be between 0 and 59")
    if config.analysis_timeout_seconds <= 0:
        raise LocalSchedulerError("config_invalid", "analysis_timeout_seconds must be positive")
    if config.keep_capture_count <= 0:
        raise LocalSchedulerError("config_invalid", "keep_capture_count must be positive")
    if len(config.prompt_sha256) != 64:
        raise LocalSchedulerError("config_invalid", "prompt_sha256 is invalid")
    for required_file in (config.prompt_path, config.schema_path, config.capture_script_path):
        if not required_file.is_file():
            raise LocalSchedulerError("config_invalid", f"Required file is missing: {required_file}")
    return config


def read_automation_status(path: Path, expected_automation_id: str) -> str:
    try:
        with path.open("rb") as handle:
            payload = tomllib.load(handle)
    except FileNotFoundError as exc:
        raise LocalSchedulerError("automation_missing", "Automation 1-k configuration is missing") from exc
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise LocalSchedulerError("automation_invalid", "Automation 1-k configuration is invalid") from exc
    automation_id = str(payload.get("id") or payload.get("automation_id") or "").strip()
    if automation_id and automation_id != expected_automation_id:
        raise LocalSchedulerError("automation_mismatch", "Automation id does not match local scheduler")
    status = str(payload.get("status") or "").strip().upper()
    if status not in {"ACTIVE", "PAUSED"}:
        raise LocalSchedulerError("automation_status_invalid", "Automation status is invalid")
    return status


def capture_chart(
    config: SchedulerConfig,
    runtime_root: Path,
    *,
    now: datetime | None = None,
    command_runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, Any]:
    timestamp = (now or datetime.now(TAIPEI)).astimezone(TAIPEI)
    capture_dir = runtime_root / "captures"
    capture_dir.mkdir(parents=True, exist_ok=True)
    output_path = capture_dir / f"chart-{timestamp.strftime('%Y%m%d-%H%M%S-%f')}.png"
    options = config.capture_options
    command: Sequence[str] = (
        "powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(config.capture_script_path),
        "-OutputPath",
        str(output_path),
        "-CropX",
        str(options["crop_x"]),
        "-CropY",
        str(options["crop_y"]),
        "-CropWidth",
        str(options["crop_width"]),
        "-CropHeight",
        str(options["crop_height"]),
        "-MinimumWindowWidth",
        str(options["minimum_window_width"]),
        "-MinimumWindowHeight",
        str(options["minimum_window_height"]),
        "-MinimumDarkRatio",
        str(options["minimum_dark_ratio"]),
        "-MinimumSignalSamples",
        str(options["minimum_signal_samples"]),
    )
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    completed = command_runner(
        command,
        text=True,
        encoding="utf-8-sig",
        errors="replace",
        capture_output=True,
        timeout=15,
        check=False,
        creationflags=flags,
    )
    try:
        payload = json.loads(completed.stdout.strip())
    except json.JSONDecodeError as exc:
        raise LocalSchedulerError("capture_output_invalid", "Pure chart capture returned invalid output") from exc
    if completed.returncode != 0 or not isinstance(payload, dict) or payload.get("ok") is not True:
        status = payload.get("status") if isinstance(payload, dict) else "capture_failed"
        raise LocalSchedulerError(str(status or "capture_failed"), "Pure chart capture failed")
    captured_path = Path(str(payload.get("path") or "")).resolve()
    if captured_path != output_path.resolve() or not captured_path.is_file():
        raise LocalSchedulerError("capture_path_invalid", "Pure chart capture path is invalid")
    try:
        captured_at = datetime.fromisoformat(str(payload["captured_at"]))
    except (KeyError, ValueError) as exc:
        raise LocalSchedulerError("capture_time_invalid", "Pure chart capture time is invalid") from exc
    if captured_at.tzinfo is None or captured_at.utcoffset() is None:
        raise LocalSchedulerError("capture_time_invalid", "Pure chart capture time must include a timezone")
    payload["path"] = str(captured_path)
    payload["captured_at"] = captured_at.astimezone(TAIPEI).isoformat()
    return payload


def build_analysis_prompt(
    *,
    monitor_rules: str,
    prompt_sha256: str,
    prepare_payload: Mapping[str, Any],
    market_structure_state_enabled: bool = False,
    market_structure_state_version: int = 2,
    unified_lens_selection_enabled: bool = False,
    dual_scale_context: Mapping[str, Any] | None = None,
    structured_market_data_context: Mapping[str, Any] | None = None,
) -> str:
    runtime_context = {
        "context": prepare_payload.get("context"),
        "previous_analysis_summary": prepare_payload.get("previous_analysis_summary"),
        "constitution_state": prepare_payload.get("constitution_state"),
    }
    if market_structure_state_enabled:
        runtime_context["market_structure_state"] = prepare_payload.get("market_structure_state")
    if dual_scale_context is not None:
        runtime_context["dual_scale_context"] = dict(dual_scale_context)
    if structured_market_data_context is not None:
        runtime_context["structured_market_data_context"] = dict(structured_market_data_context)
    context_json = json.dumps(runtime_context, ensure_ascii=False, indent=2)
    return f"""你正在執行台指期 1 分 K 正式本機讀秒監控。本段執行限制優先於下方規則中的瀏覽器、adapter、Telegram 與 heartbeat 操作指令。

- 本機程序已完成純圖表擷取、時間定錨與狀態讀取；附加圖片永遠是約 180 根 K 的 DETAIL 細節圖。
- 不得呼叫工具、命令、瀏覽器、網路、Telegram、bridge、resume guard，不得讀寫檔案或啟動 automation。
- 下方 MONITOR_RULES 的交易判斷、風險、通知決策與九欄內容規則保持完整有效；其中執行工具與傳輸步驟由本機程序接手，不由你重複執行。
- RUNTIME_CONTEXT 是最新已收盤 K、前次摘要與交易憲法狀態的唯一權威來源。不得從圖面猜測另一個時間。
- 若 RUNTIME_CONTEXT 含 dual_scale_context，只有 status=FRESH 的 CHROME_CONTROL_OVERVIEW 可用來校準大趨勢、波段階段、象限、大級防線與所在位置。它不得觸發進場、估算最新成交區或覆蓋 DETAIL 圖的最新已收盤 K；STALE、UNAVAILABLE、INVALID、RECOVERY_REQUIRED 或 FUTURE_CONTEXT 一律視為不可用。
- 若 RUNTIME_CONTEXT 含 structured_market_data_context，只有 status=FRESH 且來源時間逐字等於 expected_latest_closed_k_iso 才是價格、OHLC、均價21／均價105、ATR14、開盤區間與因果 n=2 樞紐的數值權威。圖面只負責形態與相對位置交叉驗證；兩者衝突時撤回進場點位並說明資料不一致。STALE、FUTURE、INVALID 或 UNAVAILABLE 只能回報來源狀態，不得引用其中數值。未啟用或無新鮮結構化來源時沿用原圖面估計紀律，不得假裝已有即時行情介接。
- 若圖面右端時間與 expected_latest_closed_k_iso 明顯不相符，採 fail-safe，不提供進場方向或精確價位。
- anchor_context 的 trend_dynamics、volatility_dynamics、working_quadrant 與主要象限候選必須全部使用同一個 controlling_grade、同一作用中錨之後的波段。trend_dynamics 表示目前控制錨方向的趨勢強弱，不是舊大錨方向是否仍在推進：例如大級多方背景內由小級空方錨控制，且最近 5～20 分鐘次高點與低點持續下移時，趨勢性應列增加；若振幅收斂就是向下第四象限，不能因大多方走弱誤列第三象限。controlling_grade=SMALL 或 grade_relation=ONLY_SMALL 時，以 DETAIL 最近約 5～20 分鐘與作用中小錨判斷工作象限；較早的 45～90 分鐘高波動或 OVERVIEW 只留在大趨勢背景，不得拿來把小級明確趨勢誤列成第二象限。趨勢性增加時主要候選只可是第一／第四象限；趨勢性降低時只可是第二／第三象限。波動擴張時主要候選只可是第一／第二象限；波動收斂或穩定時只可是第三／第四象限。若其中一軸不明，TRANSITION 的主要候選仍須符合另一個已知軸；次要候選只可表示其中一軸改變後的相鄰狀態並列出接管條件，不得混用不同級數的趨勢與波動證據。主要與次要候選只可相差一個軸：Q1 只鄰接 Q2／Q4，Q2 只鄰接 Q1／Q3，Q3 只鄰接 Q2／Q4，Q4 只鄰接 Q1／Q3。
- 大錨與小錨必須以父子關係同時保存，不可互相取代。先從最近 45～90 分鐘的時段極值或同級轉折辨識父級大錨，再從最近一次完整修正的次低點／次高點辨識內部小錨。若一段標為 SMALL 的錨歷時至少 30 分鐘，且同時造成 STRUCTURE_BREAK 與 SESSION_EXTREME_BREAK，或已造成 LARGE_DEFENSE_BREAK／TYPE_3_DOUBLE_DEFENSE，就必須另建作用中大錨並讓小錨的 parent_anchor_id 指向它；不得只留下 ONLY_SMALL。父級大錨起點必須使用整段主推進的發動極值，小錨起點使用內部最後一次完整修正極值；兩者可共用最新極值但不可把任意中途 K 當成大錨起點。
- 跨級建立新大錨時，若同一方向推進先使前一反向錨失效，再完成時段極值／重要結構／大級防線／Type3 跨級破壞，新大錨必須承接前一反向錨的最後極值作為發動起點，不得直接沿用較晚的小錨起點而截掉前半段。若前一空方錨低點在 10:00、完整多方推進至 11:21，則大錨從 10:00 起算；10:42 只能作內部小錨或後續段落，空方鏡像。
- 每個錨的 start_bar_time、start_price_estimate、extreme_bar_time、extreme_price_estimate 必須成對對齊同一根可見 K。先以 expected_latest_closed_k_iso 從最右側逐根回推時間，再核對該 K 的垂直價格位置；時間或價格任一無法可靠辨識時，價格填 null 並註明圖面估計／點位未定，不得把其他時間的價格配到該錨。若本輪發現既有作用中錨時間或級數錯誤，不得改寫不可變欄位；必須先將舊錨標為 REPLACED，再以本輪 first_seen_at 建立正確的大錨與子級小錨。
- 已確認且尚未終止的大錨必須保留為大趨勢方向背景：多方只可分類強勢偏多／偏多但回檔，空方只可分類強勢偏空／偏空但反彈；不能因內部反向小錨、太極複製暫停或大級防線點位無法精估就降成盤整／資料不足。若大錨已失效，先終止錨再改方向。大小錨方向衝突時，prospective_context 的主要與備用情境必須分別涵蓋原大錨回檔／反彈與新反向小錨延續兩個方向，並各列確認、取消及接管條件。
- primary_pivots 與 secondary_pivots 必須各自嚴格依 bar_time 由早到晚排列；二級樞紐即使晚於其他點才被確認，也按樞紐 K 的 bar_time 排序，不按 first_seen_at 或確認時間排序。taiji_evolution 的 copy_outcomes 少於兩筆時，copy_amplitude_trend 與 copy_duration_trend 都必須為 UNDEFINED；correction_outcomes 少於兩筆時，兩個 correction 趨勢欄位也都必須為 UNDEFINED，不得用單一樣本宣稱穩定、變長或變短。
- 圖中可能殘留游標先前停留位置所產生的舊資料框。先比對資料框內的日期／時間；只有資料框時間與 context.expected_latest_closed_k_hhmm 完全一致，才可引用其中 OHLC、均價或其他數值。時間不同、時間被遮住、無法辨識或資料框不在最右端最新已收盤 K 時，必須把整個資料框視為無效舊資訊，不能拿它校準、推算或延續任何最新價位。
- 沒有時間相符的最新資料框時，只有在畫面具備足以交叉校準的可靠價位證據（例如至少兩個清楚可見、分屬不同垂直位置的價位標記，或清楚的最新價／右側價軸）才可使用圖面估計區間。若只有單一價位錨點、價位標記互相矛盾或無法可靠定位最新 K，latest_closed_k_price_estimate 必須寫「點位無法可靠估計」，所有進場、停損、支撐壓力與 1R 欄位也不得填入推測數字，只能描述結構與尚缺條件。
- 任何數值區間都要先和可見日內高低點、最新 K 的垂直位置做量級交叉檢查；不通過就撤回數值而不是沿用前一輪或舊資料框。不得讀取、推測或回報任何帳務與個人財務資訊。
- latest_closed_k_details 不得包含任何日期或 HH:MM 時間；最新已收盤 K 與未收盤 K 的時間只由固定 renderer 插入。latest_closed_k_price_estimate 也不得包含時間。
- 免責聲明只由固定 renderer 加入，不得寫入任何 JSON 欄位。
- 最終只回傳符合 output schema 的單一 JSON object；不得輸出 Markdown、XML、前言、結語或程式碼圍欄。
- constitution_event.latest_closed_bar_time 必須逐字使用 context.expected_latest_closed_k_iso；同一事件必須使用穩定 event_id。
{('- market_structure_state 是不顯示給使用者的跨分鐘結構與情境快照；必須以既有狀態增量更新，保留 pivot_id、首次確認時間、大小級多空四向防線、左右成熟度、scenario_context 與 setup 階段。as_of 必須逐字使用 context.expected_latest_closed_k_iso。ACTIVE 防線、已配對樞紐與未終止 setup 不得無事件消失。新辨識的舊 K 樞紐可保留推回後的 bar_time，但 first_seen_at、locally_confirmed_at 與 paired_confirmed_at 最早只能是本輪 context.expected_latest_closed_k_iso，不得假裝先前已知。這種「現在首次建檔可見歷史結構」是必要因果復原，不得因樞紐 K 在過去而拒絕建檔。若某級 dow_state 已為 BULL 或 BEAR，同級對應的作用中防線與已配對來源樞紐必須同時存在；不得輸出「已確認三腳／多波創低」卻又宣稱尚無配對樞紐或空頭防線，多方完全鏡像。精確點位不足只影響 price_estimate，不影響可驗證的高低順序、樞紐資格與防線存在性。最近造成同級創低／創高的次高點／次低點是作用中小級防線；舊同級樞紐保留為歷史結構，大級防線獨立依二級樞紐判斷，不得因大級尚未確認就把小級一併清空。價格無法可靠估算時，點位區使用 null、UNAVAILABLE 與明確原因，不得為了填數字而捨棄可驗證的相對結構。' if market_structure_state_enabled else '')}
{('- 本輪 market_structure_state.version 必須與設定目標一致，並增量更新 anchor_context。定錨只能使用已收盤 K；anchor_id、起點與首次看見時間不得重寫，反向錨只重置被破壞的級數。anchor.status 只有 REPLACED 才可填 replaced_by；INVALIDATED 等其他狀態必須填 null。active 大小錨、控制級數、動態趨勢／波動與象限候選必須形成同一情境，不得另產生平行交易答案。若本輪尚無任何因果有效錨，working_quadrant 與 primary_candidate 必須為 UNDEFINED、secondary_candidate 必須為 null、eliminated_candidates 必須為空陣列。只有已存在本輪可用的 FORMING 或更成熟錨後，才可進入 TRANSITION 或排序象限候選。若尚未辨識出實際型態，scenario_context.setup.stage 必須為 NONE，pattern 必須為 NONE，且 setup_id、direction、first_seen_at、stage_changed_at 全部必須為 null；NO_CHASE 只可用於已辨識且仍在追蹤的既有型態，並必須保留 setup_id、pattern、direction、first_seen_at，再依本輪已收盤 K 因果更新 stage_changed_at。' if market_structure_state_enabled and market_structure_state_version in {3, 4, 5, 6} else '')}
- first_seen_at 必須是本輪第一次建檔該樞紐的權威已收盤 K 時間；從目前圖面復原過去樞紐時不得倒填首次看見時間。
- 使用者可見的大趨勢若為強勢偏多、偏多但回檔、偏空但反彈或強勢偏空，至少必須有一條同方向作用中道氏防線；當前趨勢若為偏多或偏空，必須同時設定同方向小級道氏狀態、已配對來源樞紐與作用中小級防線，不得以未定繞過。
{('- scenario_context.locations 若包含 UNDEFINED，該陣列就只能有 UNDEFINED 一項；只要已有任何具體位置，就不得再加入 UNDEFINED。所有陣列型列舉（包括 supporting_methods）不得放入重複值。' if market_structure_state_enabled else '')}
{('- 本輪必須完整增量更新 cclass_context。太極與一之戰法只能作為同一情境狀態：太極保存朝代、定錨、1～5 段與父子關係，逐項保留幅度、時間、斜率、乾淨度及破壞性比較；一之保存離心力、一條龍、生死門、日內外位置、發動早中末段、樞紐壓力與左右力量。不得另產生平行交易答案，也不得把三個動能階段新增為第五種型態。無有效太極段時不得使用 TAIJI_ORDERED；無有效動能事件時不得使用 YIZHI_MOMENTUM。engine_mode=UNDEFINED 時 engine_changed_at 必須為 null；沒有任何太極 legs／dynasty／anchor 時 taiji_context.assessment_changed_at 也必須為 null。複製失敗或動能失效只能先降級／中立，必須等反向防線、反向錨及延續確認後才可翻向。cclass_context、錨、型態與工作看法的 first_seen／changed 時間都不得回填。若尚未辨識實際 setup，stage 與 pattern 必須為 NONE 且識別與時間皆為 null。' if market_structure_state_enabled and market_structure_state_version in {4, 5} else '')}
{('- 本輪必須完整增量更新 cclass_context。太極保存朝代、定錨、1～5 段、父子關係及幅度／時間／斜率／乾淨度／破壞性；一之保存離心力、一條龍、生死門、日內外位置、初中末段、樞紐壓力與左右力量。兩者都必須先判讀；只選一個主模式。第一次啟動、恢復或舊狀態遺漏時，「不回填」只禁止追認歷史通知／交易與首次知悉時間，不是禁止重建目前仍有效的可見歷史結構；只要圖上能切分定錨、修正、複製與目前修正，就以歷史 K 作 start／extreme 時間、以本輪時間作 first_seen／confirmed，一次建檔且不產生歷史模擬成交。作用中已確認大錨至少必須有對應太極定錨段，不能留下空 legs 或 UNDEFINED 秩序；taiji_context.anchor_id 指向作用中大錨時，ANCHOR_1.start_bar_time 必須與大錨 start_bar_time 完全相同；反向子級小錨須作目前修正段。當其被選為主控戰法時，直接使用該戰法自己的觸發、停損、時間／動能失效與出場，不得硬映射四型態，也不得產生平行答案。無有效太極段或動能事件時不得杜撰；複製失敗或動能失效先降級／中立，須等反向防線、反向錨及延續確認才翻向。momentum_context 只有 stage 為 DRAGON_EARLY／DRAGON_MIDDLE／DRAGON_LATE 時，dragon_grade 才可使用 GOLD／K_GOLD／EARTH／CROOKED；其他 stage 必須填 NOT_APPLICABLE。所有首次看見與變更時間不得回填。' if market_structure_state_enabled and market_structure_state_version == 6 else '')}
{('- 本輪必須完整增量更新 decision_chain_context。整合決策流程只是一條決策鏈，不是第五種型態：可靠現貨前收／開盤、期貨代理或資料不可用必須明確分流；第一次當日高點／當日低點樣本與首次看見／確認時間不得回填或重寫。少於兩個已確認腳不得使用太極／象限結構鏡頭；兩腳以上必須依結構清晰度選擇唯一主判讀工具，不得再以「四腳」作為四象限主判讀的硬門檻。太極主判讀用於段落複製、修正與父子關係較清楚時；四象限主判讀用於定錨後趨勢與波動的相對變化較清楚時；三腳以上且兩者獨立一致才可共同確認。一之動能優先判讀必須與 cclass_context 的一之動能結構一致。mapped_pattern 必須等於既有 scenario setup 並限原四型態；A／B／C 級點只依勝率證據×賠率證據質性映射，且必須保存觸發後預期行為、1～10 根最大等待 K 棒及行為失效條件。完整流程狀態只供原九欄摘要，不得另產生平行答案；除課程慣用的 ATR14 與 A／B／C 級點外，所有使用者可見文字只能使用繁體中文，不得輸出內部英文列舉值。' if market_structure_state_enabled and market_structure_state_version == 5 and unified_lens_selection_enabled else '')}
{('- 本輪必須完整增量更新 decision_chain_context。依序處理可靠開盤資料、第一次日內端點樣本、至少兩腳後的唯一主判讀工具、家族結構特徵、共振與機會品質；所有首次看見／確認時間不得回填。OPENING_EVIDENCE_ONLY 只可用於 SESSION_OPEN_PENDING／OPENING_EVIDENCE／FIRST_ENDPOINT_SAMPLE／STRUCTURE_BUILDING；流程進入 SETUP_EVALUATION 或 LATE_OR_RESETTING 時必須改用當下有效鏡頭，沒有有效鏡頭就填 UNDEFINED。mapped_pattern 在 state v6 代表第 3F 戰法目錄中唯一選定的主控戰法，必須等於 scenario setup，不限四型態；主模式已 RESETTING／UNDEFINED 且舊 setup 已 INVALIDATED 時，opportunity_context.mapped_pattern 必須填 NONE，不得沿用已失效戰法。A／B／C 級點依勝率證據×賠率證據質性映射，並保存戰法自己的預期行為、1～10 根最大等待 K 棒、行為失效與執行方式。所有戰法都先判讀，但只輸出一套主決策；除課程慣用的 ATR14 與 A／B／C 級點外，所有使用者可見文字只能使用繁體中文，pattern_observation.pattern 也不得輸出 YIZHI_CENTRIFUGAL 等內部代碼。' if market_structure_state_enabled and market_structure_state_version == 6 and unified_lens_selection_enabled else '')}
- decision_chain_context.structure_context.analysis_lens 必須與 cclass_context.engine_mode 一致：只有 TAIJI_ORDERED 可使用 TAIJI_PRIMARY／COMBINED_CONFIRMATION，只有 YIZHI_MOMENTUM 可使用 YIZHI_OVERRIDE；RESETTING／UNDEFINED／UNORDERED 不得沿用太極鏡頭，有合格同級象限結構時改用 QUADRANT_PRIMARY，否則填 UNDEFINED。
{('- 本輪 market_structure_state.version 必須為 6，並完整增量更新 prospective_context。每輪同時建立偏多與偏空預案，再依所有課程戰法的證據選主要與備用情境；不得省略相反方向鏡像。大小錨為 CONFLICT 時，兩個 hypothesis 方向必須剛好涵蓋作用中大錨與小錨方向：一條評估原大錨的回檔／反彈，另一條評估反向小錨成為新方向。hypothesis 必須保存潛在演化／增強／接近確認／確認／降級／取消、支持與衝突證據及接管情境；若建立新的 hypothesis_id，first_seen_at 與 status_changed_at 必須使用本輪 context.expected_latest_closed_k_iso，不得沿用舊情境時間。long_playbook 與 short_playbook 都必須填入唯一主控戰法、執行方式、選用理由、觀察位置、K 棒行為、已收盤觸發、下一根評估、同級停損、第一障礙、不追價、應有行為、等待根數、動機失效、退出及切換條件；可使用四型態、四象限、箱型、道氏／左右、太極或一之方法，不得強制映射原四型態。playbook 只有在 mapped_pattern 不是 NONE，且 trigger_zone 與 invalidation_zone 都可用時才能標成 ARMED；任一區域為 UNAVAILABLE 時必須使用 WATCHING 或 WAITING_STRUCTURE。taiji_evolution 必須與 cclass 因果段序對齊，保存複製／修正成功失敗序列、各自幅度與時間演化、綜合評估、延續與變盤條件；active_sequence=NONE 時 structural_assessment 必須為 UNDEFINED。沒有可執行結構時可等待／觀察／禁止，但不得只寫「沒有型態」。可校準價軸時，market_state 的位置說明必須同時給現價區、最近上方壓力、最近下方支撐／防線及可否執行，不得只寫「介於」或「靠近先前區域」。' if market_structure_state_enabled and market_structure_state_version == 6 else '')}
{('- 本輪必須完整增量更新 decision_chain_context。X 只是一條決策鏈，不是第五種型態：可靠現貨前收／開盤、期貨代理或 UNAVAILABLE 必須明確分流；第一次 DH／DL 樣本與 first_seen／confirmed 時間不得回填或重寫。少於兩個已確認腳不得使用太極／象限結構鏡頭，少於四腳不得使用 QUADRANT_PRIMARY；YIZHI_OVERRIDE 必須與 cclass_context 的 YIZHI_MOMENTUM 一致。mapped_pattern 必須等於既有 scenario setup 並限原四型態；A／B／C 只依勝率證據×賠率證據質性映射，且必須保存觸發後 expected_behavior、1～10 根 max_wait_bars 及 behavior_invalidation。完整 X 狀態只供原九欄摘要，不得另產生平行答案。' if market_structure_state_enabled and market_structure_state_version == 5 and not unified_lens_selection_enabled else '')}

MONITOR_RULES_SHA256={prompt_sha256}
<RUNTIME_CONTEXT>
{context_json}
</RUNTIME_CONTEXT>
<MONITOR_RULES>
{monitor_rules}
</MONITOR_RULES>
    """


def _retain_terminal_anchor_history(
    previous: Mapping[str, Any] | None,
    current: Mapping[str, Any],
) -> dict[str, Any]:
    """Deterministically retain omitted terminal anchors from the same session.

    Only INVALIDATED/REPLACED history is carried forward.  Missing active anchors
    are deliberately not restored so transition validation still catches an
    unannounced lifecycle loss.
    """

    result = deepcopy(dict(current))
    if not isinstance(previous, Mapping) or previous.get("session_key") != result.get("session_key"):
        return result
    prior_context = previous.get("anchor_context")
    current_context = result.get("anchor_context")
    if not isinstance(prior_context, Mapping) or not isinstance(current_context, dict):
        return result
    prior_anchors = prior_context.get("anchors")
    current_anchors = current_context.get("anchors")
    if not isinstance(prior_anchors, list) or not isinstance(current_anchors, list):
        return result

    current_by_id = {
        item.get("anchor_id"): item
        for item in current_anchors
        if isinstance(item, Mapping) and item.get("anchor_id")
    }
    merged: list[Any] = []
    consumed: set[str] = set()
    for prior_anchor in prior_anchors:
        if not isinstance(prior_anchor, Mapping):
            continue
        anchor_id = prior_anchor.get("anchor_id")
        if not isinstance(anchor_id, str):
            continue
        candidate = current_by_id.get(anchor_id)
        if candidate is not None:
            merged.append(candidate)
            consumed.add(anchor_id)
        elif prior_anchor.get("status") in {"INVALIDATED", "REPLACED"}:
            merged.append(deepcopy(dict(prior_anchor)))
    merged.extend(
        item
        for item in current_anchors
        if not isinstance(item, Mapping) or item.get("anchor_id") not in consumed
    )
    current_context["anchors"] = merged
    return result


def run_codex_analysis(
    config: SchedulerConfig,
    *,
    image_path: Path,
    prepare_payload: Mapping[str, Any],
    runtime_root: Path,
    dual_scale_context: Mapping[str, Any] | None = None,
    structured_market_data_context: Mapping[str, Any] | None = None,
    command_runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> tuple[dict[str, Any], dict[str, Any]]:
    monitor_rules = config.prompt_path.read_text(encoding="utf-8")
    actual_hash = hashlib.sha256(monitor_rules.encode("utf-8")).hexdigest()
    if actual_hash != config.prompt_sha256:
        raise LocalSchedulerError("prompt_hash_mismatch", "Local analysis prompt hash does not match config")
    effective_prompt = build_analysis_prompt(
        monitor_rules=monitor_rules,
        prompt_sha256=actual_hash,
        prepare_payload=prepare_payload,
        market_structure_state_enabled=config.market_structure_state_enabled,
        market_structure_state_version=config.market_structure_state_version,
        unified_lens_selection_enabled=config.unified_lens_selection_enabled,
        dual_scale_context=dual_scale_context,
        structured_market_data_context=structured_market_data_context,
    )
    context_id = str(prepare_payload.get("context_id") or "")
    analysis_dir = runtime_root / "analysis"
    workspace_dir = runtime_root / "codex_workspace"
    analysis_dir.mkdir(parents=True, exist_ok=True)
    workspace_dir.mkdir(parents=True, exist_ok=True)
    output_path = analysis_dir / f"{context_id}.json"
    codex_executable = resolve_codex_executable()
    command: Sequence[str] = (
        str(codex_executable),
        "exec",
        "--ephemeral",
        "--sandbox",
        "read-only",
        "--ignore-user-config",
        "--ignore-rules",
        "-C",
        str(workspace_dir.resolve()),
        "-i",
        str(image_path.resolve()),
        "--output-schema",
        str(config.schema_path.resolve()),
        "--output-last-message",
        str(output_path.resolve()),
        "-",
    )
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    started = time.perf_counter()
    attempts = 0
    correction_reason: str | None = None
    completed: subprocess.CompletedProcess[str] | None = None
    payload: dict[str, Any] | None = None
    while attempts < 2:
        attempts += 1
        attempt_prompt = effective_prompt
        if correction_reason is not None:
            attempt_prompt += (
                "\n<VALIDATION_CORRECTION>\n"
                "上一份 JSON 未通過內部市場結構一致性驗證："
                f"{correction_reason}。請重新輸出完整 JSON；不得只把已由圖面確認的趨勢改寫為未定。"
                "從本輪可見歷史建檔攻擊樞紐時，使用過去 bar_time，但將 first_seen_at、"
                "locally_confirmed_at 與 paired_confirmed_at 設為本輪權威已收盤 K 時間。"
                "使用者可見的大趨勢若為強勢偏多／偏多但回檔／偏空但反彈／強勢偏空，至少必須有一條同方向作用中道氏防線；"
                "當前趨勢若為偏多／偏空，必須有同方向的小級道氏狀態與防線。"
                "空頭防線指向造成同級創低的樞紐高點，多頭完全鏡像；"
                "精確價不可得時 price_estimate 使用 null，不得否定防線存在。"
                "若錯誤涉及定錨，先終止既有錯級或錯時錨，再以本輪 first_seen_at 重建父級大錨與內部小錨；"
                "跨級大錨必須承接前一反向錨的最後極值作為起點，不得直接沿用較晚的小錨起點；"
                "歷時至少30分鐘且同時刷新時段極值並突破結構的 SMALL 錨不得是 ONLY_SMALL。"
                "錨的時間與價格必須指向同一根 K；無法可靠校準價格時填 null。"
                "已確認作用中大錨不得被降成盤整／資料不足，且至少要保留對應太極定錨段；"
                "太極 anchor_id 指向作用中大錨時，ANCHOR_1 起點必須與大錨起點一致；"
                "TAIJI_PRIMARY／COMBINED_CONFIRMATION 只可搭配 TAIJI_ORDERED；若主模式已 RESETTING／UNDEFINED／UNORDERED，改用合格的 QUADRANT_PRIMARY 或 UNDEFINED；"
                "目前可見歷史的修正／複製段應現在建檔，以本輪作首次知悉時間，不追認歷史交易。"
                "若既有 scenario_context.setup 尚未終止，本輪發現新 setup 時只能先保留原 setup_id 並把原 stage 改為 INVALIDATED 或 NO_CHASE；"
                "同一個單一 setup 槽不得在一輪內同時終止舊 setup 又換入新 setup，新 setup 必須等下一輪再以新的 first_seen_at 建立。"
                "終止舊 setup 的過渡輪，decision_chain_context.opportunity_context.mapped_pattern 只能保留舊 pattern 或填 NONE；"
                "若主模式已與舊 pattern 不相容，course_grade 填 OBSERVE 且 mapped_pattern 填 NONE。"
                "若因錯誤大錨而必須重建太極朝代，只有在舊大錨於本輪明確標為 REPLACED、replaced_by 指向新作用中大錨，"
                "且新太極 dynasty／anchor 指向同一新大錨時，才可整體替換錯誤朝代。若同輪先修復歷史大錨再由反向大錨接管，"
                "修復錨必須以本輪 first_seen_at 建立並於本輪終止，反向大錨必須從修復錨極值起算，且新太極綁定反向作用中大錨；"
                "不符合上述直接替換或修復後接力條件時，既有未終止 leg 仍不得消失。"
                "工作象限的趨勢性以目前控制錨方向衡量；主要與次要象限只可相差一個軸。"
                "大小錨方向衝突時，主要與備用情境必須涵蓋兩個錨的方向。"
                "所有 primary_pivots／secondary_pivots 依 bar_time 由早到晚排序；"
                "太極複製或修正結果少於兩筆時，對應幅度與時間趨勢必須填 UNDEFINED。\n"
                "</VALIDATION_CORRECTION>\n"
            )
        try:
            completed = command_runner(
                command,
                input=attempt_prompt,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                timeout=config.analysis_timeout_seconds,
                check=False,
                creationflags=flags,
            )
        except subprocess.TimeoutExpired as exc:
            raise LocalSchedulerError("codex_timeout", "Codex analysis timed out") from exc
        except OSError as exc:
            raise LocalSchedulerError("codex_start_failed", "Codex analysis process could not start") from exc
        if completed.returncode != 0:
            raise LocalSchedulerError("codex_failed", "Codex analysis failed")
        try:
            candidate = json.loads(output_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise LocalSchedulerError("codex_output_invalid", "Codex analysis output is invalid") from exc
        if not isinstance(candidate, dict):
            raise LocalSchedulerError("codex_output_invalid", "Codex analysis output must be an object")
        payload = candidate
        if not config.market_structure_state_enabled:
            break
        try:
            validated_payload = validate_analysis_payload(payload)
            candidate_structure = validated_payload.get("market_structure_state")
            if isinstance(candidate_structure, Mapping):
                previous_structure = prepare_payload.get("market_structure_state")
                candidate_structure = _retain_terminal_anchor_history(
                    previous_structure if isinstance(previous_structure, Mapping) else None,
                    candidate_structure,
                )
                context = prepare_payload.get("context")
                expected_as_of = (
                    str(context.get("expected_latest_closed_k_iso") or "")
                    if isinstance(context, Mapping)
                    else ""
                )
                if not expected_as_of:
                    expected_as_of = str(candidate_structure.get("as_of") or "")
                payload["market_structure_state"] = validate_market_structure_transition(
                    previous_structure if isinstance(previous_structure, Mapping) else None,
                    candidate_structure,
                    expected_as_of=expected_as_of,
                )
            break
        except (AnalysisValidationError, MarketStructureStateError) as exc:
            correction_reason = str(exc)
            if attempts >= 2:
                raise LocalSchedulerError(
                    "codex_output_inconsistent",
                    "Codex analysis remained internally inconsistent after correction",
                ) from exc
    duration = time.perf_counter() - started
    assert completed is not None and payload is not None
    metrics = {
        "duration_seconds": round(duration, 3),
        "stdout_chars": len(completed.stdout),
        "stderr_chars": len(completed.stderr),
        "prompt_sha256": actual_hash,
        "output_path": str(output_path.resolve()),
        "attempts": attempts,
    }
    return payload, metrics


async def run_monitor_once(
    config: SchedulerConfig,
    *,
    runtime_root: Path,
    automation_file: Path = DEFAULT_AUTOMATION_FILE,
    telegram_config: Path = DEFAULT_TELEGRAM_CONFIG,
    dry_run: bool = False,
    scheduled_at: datetime | None = None,
) -> dict[str, Any]:
    started_at = datetime.now(TAIPEI)
    if read_automation_status(automation_file, config.automation_id) != "ACTIVE":
        result = {
            "ok": True,
            "status": "paused",
            "scheduled_at": None if scheduled_at is None else scheduled_at.isoformat(),
            "started_at": started_at.isoformat(),
        }
        _append_jsonl(runtime_root / "scheduler_events.jsonl", result)
        return result

    runtime_state = runtime_root / "runtime_state.json"
    begin_result = begin_monitor_run(runtime_state, gap_seconds=180, lease_seconds=180)
    if begin_result.get("status") == "busy":
        result = {
            "ok": True,
            "status": "busy",
            "scheduled_at": None if scheduled_at is None else scheduled_at.isoformat(),
            "started_at": started_at.isoformat(),
            "active_run_age_seconds": begin_result.get("active_run_age_seconds"),
        }
        _append_jsonl(runtime_root / "scheduler_events.jsonl", result)
        return result

    run_id = str(begin_result.get("run_id") or "")
    force_notify = begin_result.get("force_notify") is True
    capture_started = time.perf_counter()
    try:
        if config.dual_scale_enabled:
            if config.dual_scale_state_path is None:
                raise LocalSchedulerError("dual_scale_config_invalid", "Dual-scale state path is missing")
            guard = _wait_for_detail_guard(config.dual_scale_state_path, timeout_seconds=3.0)
            if guard.get("ok") is not True:
                raise LocalSchedulerError(
                    "dual_scale_detail_not_ready",
                    f"Detail chart view is unavailable: {guard.get('status')}",
                )
        capture = capture_chart(config, runtime_root)
        capture_duration = time.perf_counter() - capture_started
        image_path = Path(capture["path"])
        captured_at = datetime.fromisoformat(capture["captured_at"])
        prepare = prepare_capture(
            image_path,
            captured_at,
            state_path=runtime_root / "analysis_state.json",
            constitution_state_path=runtime_root / "constitution_state.json",
            target_market_structure_version=config.market_structure_state_version,
        )
        dual_scale_context: Mapping[str, Any] | None = None
        if config.dual_scale_enabled:
            assert config.dual_scale_state_path is not None
            expected_closed = datetime.fromisoformat(
                str((prepare.get("context") or {}).get("expected_latest_closed_k_iso") or "")
            )
            try:
                record_detail_capture(
                    config.dual_scale_state_path,
                    captured_at=captured_at,
                    latest_closed_bar_time=expected_closed,
                )
            except (DualScaleError, BridgeError) as exc:
                code = exc.code if isinstance(exc, DualScaleError) else "dual_scale_state_locked"
                raise LocalSchedulerError(code, "Dual-scale detail capture state could not be recorded") from exc
            dual_scale_context = load_overview_context(
                config.dual_scale_state_path,
                at=captured_at,
                expected_latest_closed_bar_time=expected_closed,
                max_age_minutes=config.dual_scale_overview_max_age_minutes,
            )
        structured_market_data_context: Mapping[str, Any] | None = None
        if config.structured_market_data_enabled:
            if config.structured_market_data_source_path is None:
                raise LocalSchedulerError(
                    "structured_market_data_config_invalid",
                    "Structured market data source path is missing",
                )
            expected_iso = str((prepare.get("context") or {}).get("expected_latest_closed_k_iso") or "")
            structured_market_data_context = load_structured_market_snapshot(
                config.structured_market_data_source_path,
                expected_latest_closed_k_iso=expected_iso,
                max_age_seconds=config.structured_market_data_max_age_seconds,
            )
        analysis_payload: Mapping[str, Any] | None = None
        analysis_metrics: dict[str, Any] = {"duration_seconds": 0.0, "skipped": True}
        if prepare.get("requires_analysis") is True:
            analysis_payload, analysis_metrics = run_codex_analysis(
                config,
                image_path=image_path,
                prepare_payload=prepare,
                runtime_root=runtime_root,
                dual_scale_context=dual_scale_context,
                structured_market_data_context=structured_market_data_context,
            )
            analysis_metrics["skipped"] = False
        finalized = await finalize_analysis(
            context_id=str(prepare["context_id"]),
            analysis_payload=analysis_payload,
            force_notify=force_notify,
            run_id=run_id or None,
            state_path=runtime_root / "analysis_state.json",
            config_path=telegram_config,
            delivery_state_path=runtime_root / "delivery_state.json",
            runtime_state_path=runtime_state,
            constitution_state_path=runtime_root / "constitution_state.json",
            dry_run=dry_run,
        )
        published_at = datetime.now(TAIPEI)
        outbox_event = publish_event(
            runtime_root / "outbox",
            {
                "event_id": finalized["event_id"],
                "automation_id": config.automation_id,
                "source": "local_second_scheduler",
                "decision": finalized["decision"],
                "message": finalized["message"],
                "latest_closed_bar_time": finalized["latest_closed_bar_time"],
                "capture_status": finalized["capture_status"],
                "telegram_status": finalized["telegram_status"],
                "published_at": published_at.isoformat(),
            },
        )
        completed_at = datetime.now(TAIPEI)
        result = {
            "ok": True,
            "status": "completed",
            "scheduled_at": None if scheduled_at is None else scheduled_at.isoformat(),
            "started_at": started_at.isoformat(),
            "capture_started_offset_seconds": (
                None
                if scheduled_at is None
                else round((started_at - scheduled_at).total_seconds(), 3)
            ),
            "captured_at": capture["captured_at"],
            "capture_duration_seconds": round(capture_duration, 3),
            "analysis_duration_seconds": analysis_metrics.get("duration_seconds"),
            "analysis_attempts": analysis_metrics.get("attempts", 0),
            "completed_at": completed_at.isoformat(),
            "total_duration_seconds": round((completed_at - started_at).total_seconds(), 3),
            "decision": finalized["decision"],
            "event_id": outbox_event["event_id"],
            "latest_closed_bar_time": finalized["latest_closed_bar_time"],
            "telegram_status": finalized["telegram_status"],
            "dry_run": dry_run,
            "dual_scale_overview_status": (
                None if dual_scale_context is None else dual_scale_context.get("status")
            ),
            "structured_market_data_status": (
                None
                if structured_market_data_context is None
                else structured_market_data_context.get("status")
            ),
        }
        _append_jsonl(runtime_root / "scheduler_events.jsonl", result)
        _cleanup_captures(runtime_root / "captures", config.keep_capture_count)
        return result
    except Exception as exc:
        error_code = exc.code if isinstance(exc, LocalSchedulerError) else "local_scheduler_failed"
        if run_id:
            try:
                complete_monitor_run(runtime_state, run_id=run_id, resume_delivered=False)
            except Exception:
                pass
        failure_result = await _publish_operational_failure(
            config=config,
            runtime_root=runtime_root,
            telegram_config=telegram_config,
            dry_run=dry_run,
            error_code=error_code,
            now=datetime.now(TAIPEI),
        )
        result = {
            "ok": False,
            "status": "failed",
            "error_code": error_code,
            "scheduled_at": None if scheduled_at is None else scheduled_at.isoformat(),
            "started_at": started_at.isoformat(),
            "failure_notification": failure_result,
        }
        _append_jsonl(runtime_root / "scheduler_events.jsonl", result)
        return result


async def _publish_operational_failure(
    *,
    config: SchedulerConfig,
    runtime_root: Path,
    telegram_config: Path,
    dry_run: bool,
    error_code: str,
    now: datetime,
) -> dict[str, Any]:
    state_path = runtime_root / "failure_state.json"
    state = _read_json_object(state_path)
    last_notified = _parse_datetime(state.get("last_notified_at"))
    same_error = state.get("last_error_code") == error_code
    within_throttle = (
        same_error
        and last_notified is not None
        and (now - last_notified.astimezone(TAIPEI)).total_seconds() < FAILURE_NOTIFY_INTERVAL_SECONDS
    )
    decision = "DONT_NOTIFY" if within_throttle else "NOTIFY"
    expected_closed = now.replace(second=0, microsecond=0) - timedelta(minutes=1)
    message = _render_operational_failure(expected_closed, error_code)
    event_id = generate_event_id(config.automation_id, expected_closed.isoformat(), decision, message)
    telegram_status = "not_called"
    if decision == "NOTIFY":
        try:
            _, bridge_result = await execute_bridge(
                event_id=event_id,
                decision=decision,
                message=message,
                config_path=telegram_config,
                state_path=runtime_root / "delivery_state.json",
                dry_run=dry_run,
            )
            telegram_status = str(bridge_result.get("status") or "failed")
        except Exception:
            telegram_status = "failed"
        state = {
            "version": 1,
            "last_error_code": error_code,
            "last_notified_at": now.isoformat(),
        }
        _write_json_atomic(state_path, state)
    publish_event(
        runtime_root / "outbox",
        {
            "event_id": event_id,
            "automation_id": config.automation_id,
            "source": "local_second_scheduler",
            "decision": decision,
            "message": message,
            "latest_closed_bar_time": expected_closed.isoformat(),
            "capture_status": "UNAVAILABLE",
            "telegram_status": telegram_status,
            "published_at": now.isoformat(),
        },
    )
    return {"decision": decision, "event_id": event_id, "telegram_status": telegram_status}


def _render_operational_failure(expected_closed: datetime, error_code: str) -> str:
    error_label = operational_error_label(error_code)
    return "\n\n".join(
        (
            f"**時間／最新已收盤 K**\n\n- {expected_closed.strftime('%H:%M')}；本機讀秒監控資料不可用。",
            "**大趨勢**\n\n- 資料不足，不沿用前次方向。",
            "**當前趨勢**\n\n- 轉換中；本輪不判定多空。",
            f"**市場狀態**\n\n- 本機監控作業失敗：{error_label}。",
            "**觀察型態與狀態**\n\n- **觀望**；本輪沒有可驗證型態。",
            "**尚缺條件／觸發**\n\n- 等待純圖表擷取與結構化分析恢復。",
            "**進場與結構停損**\n\n- 不提供進場或停損價位。",
            "**一倍初始風險與最近障礙**\n\n- 無有效進場與停損，無法計算。",
            "**單口管理／禁止原因**\n\n- 監控資料不可用時禁止新增模擬交易。\n\n一般技術分析，非個人化投資建議；遠端畫面可能延遲，非交易所等級即時訊號。",
        )
    )


async def run_daemon(
    config: SchedulerConfig,
    *,
    runtime_root: Path,
    automation_file: Path,
    telegram_config: Path,
    dry_run: bool,
    cycles: int | None,
) -> int:
    lock_path = runtime_root / "local_scheduler.lock"
    runtime_root.mkdir(parents=True, exist_ok=True)
    completed_cycles = 0
    try:
        with DeliveryFileLock(lock_path, timeout_seconds=0.1):
            while cycles is None or completed_cycles < cycles:
                target = next_minute_slot(datetime.now(TAIPEI), config.align_second)
                delay = max(0.0, (target - datetime.now(TAIPEI)).total_seconds())
                time.sleep(delay)
                await run_monitor_once(
                    config,
                    runtime_root=runtime_root,
                    automation_file=automation_file,
                    telegram_config=telegram_config,
                    dry_run=dry_run,
                    scheduled_at=target,
                )
                completed_cycles += 1
    except BridgeError as exc:
        if exc.code == "delivery_lock_timeout":
            raise LocalSchedulerError("scheduler_already_running", "Local scheduler is already running") from exc
        raise
    return completed_cycles


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the formal local second-aligned trade monitor")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    parser.add_argument("--automation-file", type=Path, default=DEFAULT_AUTOMATION_FILE)
    parser.add_argument("--telegram-config", type=Path, default=DEFAULT_TELEGRAM_CONFIG)
    subparsers = parser.add_subparsers(dest="command", required=True)
    once = subparsers.add_parser("once")
    once.add_argument("--dry-run", action="store_true")
    daemon = subparsers.add_parser("daemon")
    daemon.add_argument("--dry-run", action="store_true")
    daemon.add_argument("--cycles", type=int)
    return parser


def main(argv: list[str] | None = None, *, stdout: TextIO | None = None) -> int:
    output = stdout or sys.stdout
    parser = build_argument_parser()
    try:
        args = parser.parse_args(argv)
        config = load_scheduler_config(args.config)
        if args.command == "once":
            result: Any = asyncio.run(
                run_monitor_once(
                    config,
                    runtime_root=args.runtime_root,
                    automation_file=args.automation_file,
                    telegram_config=args.telegram_config,
                    dry_run=args.dry_run,
                )
            )
            exit_code = 0 if result.get("ok") is True else 2
        else:
            if args.cycles is not None and args.cycles <= 0:
                raise LocalSchedulerError("cycles_invalid", "cycles must be positive")
            completed = asyncio.run(
                run_daemon(
                    config,
                    runtime_root=args.runtime_root,
                    automation_file=args.automation_file,
                    telegram_config=args.telegram_config,
                    dry_run=args.dry_run,
                    cycles=args.cycles,
                )
            )
            result = {"ok": True, "status": "stopped", "completed_cycles": completed}
            exit_code = 0
    except LocalSchedulerError as exc:
        result = {"ok": False, "status": "failed", "error_code": exc.code, "error": str(exc)}
        exit_code = 2
    except SystemExit:
        raise
    except Exception:
        result = {
            "ok": False,
            "status": "failed",
            "error_code": "local_scheduler_internal_error",
            "error": "Local scheduler failed",
        }
        exit_code = 2
    output.write(json.dumps(result, ensure_ascii=False, separators=(",", ":")) + "\n")
    output.flush()
    return exit_code


def _resolve_project_path(project_root: Path, value: object) -> Path:
    text = str(value or "").strip()
    if not text:
        raise LocalSchedulerError("config_invalid", "Required project path is missing")
    path = Path(text)
    return path.resolve() if path.is_absolute() else (project_root / path).resolve()


def resolve_codex_executable() -> Path:
    from_path = shutil.which("codex")
    if from_path:
        resolved = Path(from_path).resolve()
        if resolved.is_file():
            return resolved

    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        bin_root = Path(local_app_data) / "OpenAI" / "Codex" / "bin"
        candidates = [
            path.resolve()
            for path in bin_root.glob("*/codex.exe")
            if path.is_file()
        ]
        if candidates:
            return max(candidates, key=lambda path: path.stat().st_mtime_ns)
    raise LocalSchedulerError("codex_executable_missing", "Codex CLI executable could not be located")


def _append_jsonl(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(dict(payload), ensure_ascii=False, separators=(",", ":")) + "\n")


def _read_json_object(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(
            json.dumps(dict(payload), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _parse_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _wait_for_detail_guard(
    state_path: Path,
    *,
    timeout_seconds: float,
    now_provider: Callable[[], datetime] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    if timeout_seconds < 0:
        raise LocalSchedulerError("dual_scale_wait_invalid", "Dual-scale wait timeout is invalid")
    clock = now_provider or (lambda: datetime.now(TAIPEI))
    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            guard = detail_capture_guard(state_path, at=clock())
        except (DualScaleError, BridgeError) as exc:
            code = exc.code if isinstance(exc, DualScaleError) else "dual_scale_state_locked"
            raise LocalSchedulerError(code, "Dual-scale state is unavailable") from exc
        if guard.get("ok") is True or guard.get("status") != "OVERVIEW_BUSY":
            return guard
        if time.monotonic() >= deadline:
            return guard
        sleep(0.1)


def _cleanup_captures(capture_dir: Path, keep_count: int) -> None:
    if not capture_dir.exists():
        return
    resolved_dir = capture_dir.resolve()
    captures = sorted(
        (path for path in capture_dir.glob("chart-*.png") if path.is_file()),
        key=lambda path: path.stat().st_mtime_ns,
        reverse=True,
    )
    for path in captures[keep_count:]:
        resolved = path.resolve()
        if resolved.parent != resolved_dir or not resolved.name.startswith("chart-"):
            raise LocalSchedulerError("capture_cleanup_unsafe", "Capture cleanup target is unsafe")
        resolved.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
