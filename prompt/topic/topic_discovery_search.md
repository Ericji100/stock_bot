# 題材 Discovery 搜尋提示詞

任務：請搜尋與整理以下主題的相關資料來源。

主題：{theme}
資料基準日期：{report_date}

搜尋目標：
1. 全球產業趨勢與需求變化
2. 台股相關受惠族群
3. 主要廠商動態與產能擴張
4. 政策環境與總經因素
5. 供應鏈與上下游廠商
6. 近期重大新聞與產業事件

要求：
- 只搜尋與整理來源，不做正式題材決策
- 記錄每個來源的標題、URL、出處、等級、发布日期
- 區分不同類型的來源（L1 官方、L2 媒體、L3 社群）
- 不得捏造來源 URL
- 不得直接寫入正式題材庫
- 為每個來源標注適合的主題信號

輸出格式：
```json
{{
  "sources": [
    {{
      "source_id": "src_001",
      "title": "標題",
      "url": "https://...",
      "source_level": "L1_official|L2_media|L3_community",
      "published_date": "YYYY-MM-DD",
      "snippet": "摘要",
      "provider": "出處",
      "found_by": ["gemini_search", "web_fetch"]
    }}
  ],
  "topic_signals": ["信號1", "信號2"],
  "risk_notes": ["風險注意1"]
}}
```
