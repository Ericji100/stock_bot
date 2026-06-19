from pathlib import Path

from research_center.command_parser import parse_command_text
from research_center.search_query_service import build_search_discovery_tasks, flatten_task_queries
from research_center.news_service import build_news_discovery_queries


def _queries(command: str, structured_data: dict | None = None) -> str:
    request = parse_command_text(command)
    tasks = build_search_discovery_tasks(request, structured_data or {})
    return "\n".join(flatten_task_queries(tasks))


def test_research_queries_cover_official_business_and_counter_evidence():
    text = _queries("/research 2330 --deep --model minimax")

    for keyword in ["公開資訊觀測站", "月營收", "財報", "法說會", "產品", "客戶", "供應鏈", "毛利率", "庫存", "反證"]:
        assert keyword in text


def test_macro_queries_cover_vix_derivatives_fear_greed_and_macro_risk():
    text = _queries("/macro 台股 --model minimax")

    for keyword in ["VIX", "美債殖利率", "美元指數", "台指期", "台指選擇權", "Put Call", "未平倉", "外資期貨", "恐慌", "貪婪", "關稅", "地緣政治"]:
        assert keyword in text


def test_theme_queries_cover_supply_chain_catalyst_and_cooling_risk():
    text = _queries("/theme AI電源 --model minimax")

    for keyword in ["題材", "產業趨勢", "供應鏈", "產品", "客戶", "營收", "退燒", "估值過高", "需求不如預期"]:
        assert keyword in text


def test_theme_flow_queries_cover_expansion_and_failed_rotation():
    text = _queries("/theme_flow AI電源 --model minimax")

    for keyword in ["上游", "下游", "擴散", "資金輪動", "退潮", "法人", "賣超", "輪動 失敗"]:
        assert keyword in text


def test_theme_radar_queries_demote_social_and_cover_early_catalyst():
    request = parse_command_text("/theme_radar --model minimax")
    tasks = build_search_discovery_tasks(request, {})
    text = "\n".join(flatten_task_queries(tasks))
    excludes = "\n".join(" ".join(task.get("exclude") or []) for task in tasks)

    for keyword in ["熱門題材", "資金輪動", "主流財經", "新產品", "新訂單", "早期", "過熱", "反證"]:
        assert keyword in text
    for keyword in ["YouTube", "Facebook", "Threads", "論壇"]:
        assert keyword in excludes


def test_sector_strength_queries_cover_flow_and_counter_evidence():
    data = {"sector_rankings": [{"sector": "半導體"}, {"sector": "電源"}]}
    text = _queries("/sector_strength --model minimax", data)

    for keyword in ["類股", "資金流", "法人", "買賣超", "過熱", "賣超", "輪動 失敗"]:
        assert keyword in text


def test_value_scan_queries_cover_rerating_labels_and_failure_evidence():
    data = {"ai_candidates": [{"code": "2330", "name": "台積電"}, {"code": "2308", "name": "台達電"}]}
    text = _queries("/value_scan 精選選股 --deep --top 10 --model minimax", data)

    for keyword in ["公告", "月營收", "財報", "法說會", "舊標籤", "新標籤", "新產品", "新客戶", "營收占比", "法人", "估值 過高", "反證"]:
        assert keyword in text


def test_topic_maintain_queries_cover_new_topic_and_counter_evidence():
    text = _queries("/topic_maintain --model minimax")

    for keyword in ["新題材", "供應鏈", "新產品", "客戶", "營收", "退燒", "反證"]:
        assert keyword in text


def test_news_refresh_queries_cover_vix_options_and_pre_market_risk():
    tasks = build_news_discovery_queries("latest")
    text = "\n".join(flatten_task_queries(tasks))

    for keyword in ["VIX", "台指期", "台指選擇權", "Put Call", "未平倉", "盤前", "夜盤"]:
        assert keyword in text


def test_search_query_service_no_mojibake_markers():
    text = Path("research_center/search_query_service.py").read_text(encoding="utf-8")

    for marker in ["嚗", "憿", "蝣", "�"]:
        assert marker not in text
