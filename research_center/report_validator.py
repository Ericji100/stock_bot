from __future__ import annotations

import re
from typing import Any

from .final_output_contracts import contract_for_command
from .models import CommandRequest, SourceItem

REQUIRED_SCHEMA_KEYS = {
    "report_title",
    "report_type",
    "target",
    "mode",
    "report_date",
    "summary",
    "sections",
    "scores",
    "risks",
    "positive_factors",
    "watch_items",
    "sources",
    "schema_version",
    "report_metadata",
    "data_source_summary",
    "candidate_snapshot",
}

FORBIDDEN_PATTERNS = ("保證獲利", "必漲", "一定買入", "自動下單", "穩賺", "保證達成")

EXPECTED_SECTIONS = {
    "research": ["摘要", "基本資料", "股價", "營收", "財報", "籌碼", "風險", "資料來源"],
    "macro": ["市場", "指數", "波動", "資金", "風險", "資料來源"],
    "theme": ["題材", "供應鏈", "受惠", "風險", "資料來源"],
    "value_scan": ["價值重估", "候選", "排名", "舊市場標籤", "新市場標籤", "風險", "資料來源"],
}

SECTION_ALIASES = {
    "波動": ("波動", "VIX", "波動率", "市場風險", "風險偏好", "期貨與波動率"),
    "資料來源列表": ("資料來源列表", "資料來源", "來源列表", "完整資料來源清單"),
    "來源列表": ("來源列表", "資料來源", "完整資料來源清單"),
    "資料來源與可信度": ("資料來源與可信度", "資料來源", "來源品質", "可信度"),
    "反證與風險": ("反證與風險", "風險與反證", "風險", "反證"),
    "主要風險與反證": ("主要風險與反證", "風險與反證", "風險", "反證"),
    "風險、反證與資料缺口": ("風險、反證與資料缺口", "風險與反證", "資料缺口", "風險"),
    "本地量化底稿與 AI 最終評分差異": ("本地量化底稿與 AI 最終評分差異", "本地量化底稿", "本地量化評分"),
    "AI 評分細項拆解": ("AI 評分細項拆解", "評分細項", "細項分數", "分項分數", "評分拆解", "分數拆解", "評分細項拆解表", "細項分數拆解表"),
    "合理股價與目標價區間": ("合理股價與目標價區間", "目標價", "合理價", "合理股價", "估值區間", "目標價區間", "股價目標區間"),
    "獲利預估與三情境推演": ("獲利預估與三情境推演", "獲利預估", "保守情境", "中性情境", "樂觀情境", "三情境"),
    "題材水分與財報裂縫": ("題材水分與財報裂縫", "題材水分", "財報裂縫", "題材水分檢查"),
    "潛在催化因素": ("潛在催化因素", "催化因素", "催化劑", "潛在催化"),
    "市場資金輪動與題材熱度": ("市場資金輪動與題材熱度", "市場資金輪動", "題材熱度", "資金輪動"),
    "策略適配初判": ("策略適配初判", "策略適配", "操作策略", "投資策略適配"),
    "本地量化底稿與 AI 最終重估差異": ("本地量化底稿與 AI 最終重估差異", "本地量化底稿", "AI 最終重估"),
    "候選池與資料狀態": ("候選池與資料狀態", "候選池", "資料狀態", "候選來源"),
    "舊市場標籤與新市場標籤總表": ("舊市場標籤與新市場標籤總表", "舊市場標籤", "新市場標籤", "市場標籤"),
    "重估證據交叉驗證": ("重估證據交叉驗證", "交叉驗證", "重估證據", "證據交叉"),
    "營收與財報驗證": ("營收與財報驗證", "營收財報", "營收與財報", "財報驗證"),
    "法人、籌碼與技術確認": ("法人、籌碼與技術確認", "法人籌碼技術", "法人", "籌碼", "技術確認"),
    "是否只是蹭題材": ("是否只是蹭題材", "蹭題材", "題材水分", "題材驗證"),
    "低分或未入選候選快速說明": ("低分或未入選候選快速說明", "低分", "未入選", "未入選候選"),
    "觀察名單與後續研究優先度": ("觀察名單與後續研究優先度", "觀察名單", "後續研究優先度", "研究優先度"),
    "股價與技術面": ("股價與技術面", "股價", "技術面"),
    "籌碼與法人": ("籌碼與法人", "籌碼", "法人"),
    "題材與未來想像空間": ("題材與未來想像空間", "題材", "未來想像空間"),
    "後續研究候選清單": ("後續研究候選清單", "後續應執行", "後續觀察", "研究候選"),
    "全球市場與國際局勢": ("全球市場與國際局勢", "全球市場", "國際局勢", "海外市場"),
    "央行利率、匯率與美元流動性": ("央行利率、匯率與美元流動性", "央行", "利率", "匯率", "美元流動性"),
    "能源、原物料與通膨壓力": ("能源、原物料與通膨壓力", "能源", "原物料", "通膨"),
    "房地產、銀行與信用風險": ("房地產、銀行與信用風險", "房地產", "銀行", "信用風險"),
    "非美市場與美股科技股": ("非美市場與美股科技股", "非美市場", "美股科技股", "科技股"),
    "國際變數與匯率利率": ("國際變數與匯率利率", "匯率、利率、美股與國際變數", "匯率與利率", "國際變數", "美元流動性"),
    "產業輪動與族群": ("產業輪動與族群", "台股產業輪動", "產業輪動", "族群輪動", "資金流入族群"),
    "台股族群輪動與資金風格": ("台股族群輪動與資金風格", "台股族群輪動", "資金風格", "族群輪動"),
    "熱門題材與資金偏好": ("熱門題材與資金偏好", "熱門題材", "資金偏好", "市場題材"),
    "題材結論": ("題材結論", "題材簡介", "近期為什麼熱門", "題材研究報告"),
    "全球需求變化": ("全球需求變化", "全球需求", "需求變化", "終端需求", "全球產業趨勢", "需求驅動"),
    "主要大廠資本支出方向": ("主要大廠資本支出方向", "大廠資本支出", "資本支出方向", "CAPEX", "資本支出", "產能投資與資本支出", "北美 CSP 資本支出"),
    "供應鏈輪廓": ("供應鏈輪廓", "產業鏈拆解", "供應鏈", "台股相關族群"),
    "核心受惠公司": ("核心受惠公司", "真正受惠", "核心受惠", "可能受惠公司"),
    "核心受惠股逐檔評分": ("核心受惠股逐檔評分", "核心受惠股", "逐檔評分", "核心受惠公司", "核心受惠"),
    "次級受惠公司": ("次級受惠公司", "次核心受惠", "中等證據"),
    "次受惠股逐檔評分": ("次受惠股逐檔評分", "次受惠股", "次級受惠公司", "逐檔評分", "次核心受惠"),
    "僅觀察或不應納入公司": ("僅觀察或不應納入公司", "沾邊觀察", "不宜列入", "資料不足"),
    "題材證據": ("題材證據", "近期為什麼熱門", "題材水分檢查", "證據來源"),
    "資金流入情況": ("資金流入情況", "資金流", "資金流入", "法人資金", "主流資金", "資金開始"),
    "可能被價值重估的公司": ("可能被價值重估的公司", "價值重估", "重估公司", "可能重估"),
    "樂觀、基準與悲觀情境推演": ("樂觀、基準與悲觀情境推演", "樂觀情境", "基準情境", "悲觀情境", "多情境推演", "情境推演", "多情境", "情境判斷"),
    "催化劑與追蹤指標": ("催化劑與追蹤指標", "催化因素", "後續觀察指標"),
    "資料缺口": ("資料缺口", "資料不足", "資料不足與限制", "待驗證缺口", "資料不足聲明"),
    "題材流入與流出": ("題材流入與流出", "題材流入 / 流出", "題材流入", "題材流出"),
    "族群承接": ("族群承接", "族群與個股承接狀況", "資金承接", "承接狀況"),
    "個股與子族群承接狀況": ("個股與子族群承接狀況", "族群與個股承接狀況", "個股承接", "子族群承接"),
    "資料過少警示與缺口": ("資料過少警示與缺口", "資料過少", "樣本不足", "資料缺口", "關聯股過少"),
    "多情境推演": ("多情境推演", "情境判斷", "正向情境", "反向情境", "樂觀情境", "悲觀情境"),
    "本地核心資料包摘要": ("本地核心資料包摘要", "本地核心資料", "核心資料包", "核心資料摘要"),
    "命中公司與代表股": ("命中公司與代表股", "命中公司", "代表股"),
    "低階模型底稿": ("低階模型底稿", "MiniMax", "資料整理底稿", "低階整理"),
    "事實整理": ("事實整理", "事實", "已驗證事實"),
    "事件整理": ("事件整理", "事件", "事件脈絡"),
    "風險證據": ("風險證據", "風險", "風險來源"),
    "反證整理": ("反證整理", "反證", "反向證據"),
    "缺漏資料": ("缺漏資料", "資料缺漏", "資料不足", "資料缺口"),
    "來源對照": ("來源對照", "來源索引", "資料來源", "來源列表"),
    "代表股與排除股": ("代表股與排除股", "代表股", "排除股"),
    "資金集中度判斷": ("資金集中度判斷", "資金是否集中", "集中度結論", "資金集中", "集中度"),
    "反證與後續觀察": ("反證與後續觀察", "反證", "後續觀察", "觀察條件"),
}

AI_FINAL_SECTION_KEYWORDS = (
    "AI 最終推薦買入評分",
    "AI 最終財務與題材評分",
    "AI 最終飆股基因評分",
    "AI 最終價值重估評分",
    "AI 最終投研評分",
    "AI 最終重估判斷",
    "AI 最終重估排序",
)


def validate_report(markdown: str, request: CommandRequest, sources: list[SourceItem], report_json: dict[str, Any]) -> dict[str, Any]:
    qa_markdown = _main_markdown_for_qa(markdown)
    headings = _headings(markdown)
    source_refs = sorted(set(re.findall(r"\[S\d{3}\]", qa_markdown)))
    contract = contract_for_command(request.command)
    if request.command == "research" and request.mode not in {"score", "deep"}:
        contract = None
    if request.command == "value_scan" and request.mode == "source_only":
        contract = None
    expected = list(contract.sections) if contract else EXPECTED_SECTIONS.get(request.command, [])
    missing_sections = [section for section in expected if not _contains_heading(headings, section)]
    missing_content_terms = [
        term for term in ((contract.content_terms if contract else ()) or ())
        if not _contains_required_content(qa_markdown, headings, term)
    ]
    schema_errors = _schema_errors(report_json)
    forbidden_hits = [pattern for pattern in FORBIDDEN_PATTERNS if pattern in qa_markdown]
    source_list_present = any("資料來源" in heading or "來源" in heading for heading in headings) or "[S001]" in markdown
    has_scores_when_required = True
    if ((request.command == "research" and request.mode in {"score", "deep"}) or request.command == "value_scan") and not report_json.get("scores"):
        has_scores_when_required = False
    has_ai_final_scoring = any(_contains_heading(headings, kw) for kw in AI_FINAL_SECTION_KEYWORDS)
    requires_ai_final_scoring = request.command == "research" and request.mode in {"score", "deep"}

    warnings = []
    if not source_list_present:
        warnings.append("缺少資料來源章節或來源引用。")
    if sources and not source_refs:
        warnings.append("報告未引用任何 [Sxxx] 來源代號。")
    if not has_scores_when_required:
        warnings.append("評分模式缺少 scores 結構化資料。")
    if not has_ai_final_scoring and requires_ai_final_scoring:
        warnings.append("缺少 AI 最終投研評分章節；請確認報告有「AI 最終推薦買入評分」、「AI 最終財務與題材評分」等章節。")
    if missing_content_terms:
        warnings.append("缺少必要報告內容：" + ", ".join(missing_content_terms))
    missing_value_scan_candidates = _missing_value_scan_candidates(markdown, request, report_json)
    if missing_value_scan_candidates:
        warnings.append("/value_scan missing per-candidate rerating analysis: " + ", ".join(missing_value_scan_candidates))
    if forbidden_hits:
        warnings.append("報告含禁止語句：" + ", ".join(forbidden_hits))

    passed = (
        not missing_sections
        and not schema_errors
        and not forbidden_hits
        and source_list_present
        and has_scores_when_required
        and not missing_value_scan_candidates
        and (has_ai_final_scoring or not requires_ai_final_scoring)
        and not missing_content_terms
    )
    return {
        "passed": passed,
        "missing_sections": missing_sections,
        "missing_content_terms": missing_content_terms,
        "schema_errors": schema_errors,
        "forbidden_hits": forbidden_hits,
        "source_refs": source_refs,
        "source_list_present": source_list_present,
        "missing_value_scan_candidates": missing_value_scan_candidates,
        "warnings": warnings,
    }


def append_qa_notes(markdown: str, qa: dict[str, Any]) -> str:
    if qa.get("passed"):
        return markdown
    lines = [markdown.rstrip(), "", "## 規格檢查提醒"]
    for warning in qa.get("warnings") or []:
        lines.append(f"- {warning}")
    missing = qa.get("missing_sections") or []
    if missing:
        lines.append("- 缺少或未明確命名章節：" + ", ".join(missing))
    schema_errors = qa.get("schema_errors") or []
    if schema_errors:
        lines.append("- JSON schema 修補提醒：" + ", ".join(schema_errors))
    return "\n".join(lines).strip() + "\n"



def _missing_value_scan_candidates(markdown: str, request: CommandRequest, report_json: dict[str, Any]) -> list[str]:
    if request.command != "value_scan":
        return []
    metadata = report_json.get("metadata") or {}
    candidates = metadata.get("value_scan_candidates") or []
    if not candidates:
        return []
    missing: list[str] = []
    for item in candidates:
        code = str(item.get("code") or "").strip()
        name = str(item.get("name") or "").strip()
        if code and code in markdown:
            continue
        if name and name in markdown:
            continue
        missing.append(" ".join(part for part in [code, name] if part) or "unknown")
    return missing

def _headings(markdown: str) -> list[str]:
    return [line.lstrip("#").strip() for line in markdown.splitlines() if line.startswith("#")]


def _main_markdown_for_qa(markdown: str) -> str:
    return re.split(r"\n## (完整資料來源清單|規格檢查提醒)\b", markdown, maxsplit=1)[0]


def _contains_heading(headings: list[str], keyword: str) -> bool:
    aliases = SECTION_ALIASES.get(keyword, (keyword,))
    return any(any(alias in heading for alias in aliases) for heading in headings)


def _contains_required_content(markdown: str, headings: list[str], keyword: str) -> bool:
    if keyword in markdown or _contains_heading(headings, keyword):
        return True
    aliases = SECTION_ALIASES.get(keyword, ())
    return any(alias in markdown for alias in aliases)


def _schema_errors(report_json: dict[str, Any]) -> list[str]:
    errors = []
    missing = sorted(REQUIRED_SCHEMA_KEYS - set(report_json))
    if missing:
        errors.append("missing keys: " + ", ".join(missing))
    if not isinstance(report_json.get("sections"), list):
        errors.append("sections must be list")
    if not isinstance(report_json.get("scores"), list):
        errors.append("scores must be list")
    if not isinstance(report_json.get("sources"), list):
        errors.append("sources must be list")
    if not isinstance(report_json.get("data_source_summary"), list):
        errors.append("data_source_summary must be list")
    if not isinstance(report_json.get("candidate_snapshot"), list):
        errors.append("candidate_snapshot must be list")
    if not isinstance(report_json.get("report_metadata"), dict):
        errors.append("report_metadata must be dict")
    return errors
