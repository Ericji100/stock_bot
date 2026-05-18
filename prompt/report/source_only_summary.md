任務：請將以下資料整理成乾淨、可供後續 AI 或龍蝦調用的資料集合。

模式：source-only
資料基準日期：{report_date}

---

## 一、模式定位

source-only 模式只負責整理資料，不負責產出投資判斷。

本模式的目標是產出一份乾淨、可追溯、可供後續 AI 分析或龍蝦 API 調用的資料包。

允許執行：

1. 整理結構化資料。
2. 摘要來源內容。
3. 標示來源日期。
4. 標示來源可信度。
5. 標示資料缺口。
6. 標示後續可分析方向。
7. 整理論壇與社群情緒，但只能標示為市場情緒。

禁止執行：

1. 不得做主觀投資分析。
2. 不得做投資結論。
3. 不得做買入、賣出、加碼、減碼建議。
4. 不得做買入評分。
5. 不得做飆股基因分數、價值重估分數或推薦買入評分。
6. 不得做股票排名。
7. 不得產出持股水位或操作策略。
8. 不得將論壇與社群資料當作事實依據。

---

## 二、資料整理規則

1. 請優先整理 Level 1 與 Level 2 來源。
2. 若同一事件有多個來源，請合併為同一事件摘要，並優先保留較高可信來源。
3. 若資料只有 snippet，需標示為 snippet_only，不得寫成完整來源。
4. 若來源沒有明確日期，需標示為「日期不可驗證」。
5. 若資料互相矛盾，只需標示矛盾點，不得自行判斷哪一方一定正確，除非有 Level 1 官方來源可確認。
6. 若資料不足，需列入 missing_data。
7. 所有論壇、社群、討論區資料只能歸類為 forum_sources 或 market_sentiment，不得歸類為事實來源。

---

## 三、輸出格式

請只輸出一個 JSON，不要輸出 Markdown，不要輸出 JSON 以外的文字。

{
  "target": "{target}",
  "report_date": "{report_date}",
  "mode": "source_only",
  "data_completeness": {
    "level": "高 | 中 | 低 | 不足",
    "reason": "string"
  },
  "structured_data_summary": {
    "stock_basic": "string",
    "price_technical": "string",
    "revenue": "string",
    "financials": "string",
    "institutional_trading": "string",
    "margin_trading": "string",
    "other": "string"
  },
  "official_sources": [
    {
      "source_id": "S001",
      "title": "string",
      "url": "string",
      "source_type": "MOPS | TWSE | TPEx | company_ir | financial_report | monthly_revenue | investor_conference | annual_report | other",
      "source_level": "Level 1",
      "published_date": "YYYY-MM-DD 或 日期不可驗證",
      "retrieval_method": "beautifulsoup | tavily_extract | search_snippet | gemini_fallback | existing_source | local_structured_data | unknown",
      "content_status": "full_text | partial_text | snippet_only | structured_data | unknown",
      "summary": "string",
      "limitations": "string"
    }
  ],
  "news_sources": [
    {
      "source_id": "S101",
      "title": "string",
      "url": "string",
      "source_type": "news",
      "source_level": "Level 2 | Level 3",
      "published_date": "YYYY-MM-DD 或 日期不可驗證",
      "retrieval_method": "beautifulsoup | tavily_extract | search_snippet | gemini_fallback | existing_source | unknown",
      "content_status": "full_text | partial_text | snippet_only | unknown",
      "summary": "string",
      "limitations": "string"
    }
  ],
  "industry_sources": [
    {
      "source_id": "S201",
      "title": "string",
      "url": "string",
      "source_type": "industry_report | industry_news | research_summary | other",
      "source_level": "Level 2 | Level 3",
      "published_date": "YYYY-MM-DD 或 日期不可驗證",
      "retrieval_method": "beautifulsoup | tavily_extract | search_snippet | gemini_fallback | existing_source | unknown",
      "content_status": "full_text | partial_text | snippet_only | unknown",
      "summary": "string",
      "limitations": "string"
    }
  ],
  "forum_sources": [
    {
      "source_id": "S301",
      "title": "string",
      "url": "string",
      "source_type": "PTT | Dcard | Mobile01 | X | Threads | Telegram | forum | social",
      "source_level": "Level 4 | Level 5",
      "published_date": "YYYY-MM-DD 或 日期不可驗證",
      "retrieval_method": "beautifulsoup | tavily_extract | search_snippet | gemini_fallback | existing_source | unknown",
      "content_status": "full_text | partial_text | snippet_only | unknown",
      "summary": "string",
      "sentiment_note": "string",
      "limitations": "只能作為市場情緒參考，不得作為事實依據"
    }
  ],
  "source_reliability": {
    "highest_available_level": "Level 1 | Level 2 | Level 3 | Level 4 | Level 5 | none",
    "has_level_1_source": true,
    "has_full_text_source": true,
    "has_official_financial_data": true,
    "has_conflicting_sources": false,
    "reliability_summary": "string"
  },
  "conflicting_information": [
    {
      "topic": "string",
      "source_ids": ["S001", "S101"],
      "conflict_summary": "string"
    }
  ],
  "missing_data": [
    {
      "data_type": "financials | revenue | investor_conference | news | industry | institutional_trading | margin_trading | forum | other",
      "description": "string",
      "impact": "string"
    }
  ],
  "next_analysis_suggestions": [
    {
      "suggestion": "string",
      "purpose": "string",
      "allowed_next_step": "/research --score | /research --deep | /theme | /macro | /value_scan | other"
    }
  ]
}

---

## 四、next_analysis_suggestions 限制

next_analysis_suggestions 只能提出後續可分析方向，不得包含投資結論。

允許範例：

1. 建議後續使用 /research --score 檢查財務與題材評分。
2. 建議補充最新法說會資料後再進行深度分析。
3. 建議追蹤未來 1～3 個月月營收變化。
4. 建議使用 /theme 分析相關產業題材。

禁止範例：

1. 建議買入。
2. 建議加碼。
3. 建議布局。
4. 此股值得追價。
5. 此股具備明確獲利機會。