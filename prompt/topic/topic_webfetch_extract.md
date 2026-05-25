# 題材 WebFetch 正文抽取提示詞

任務：請根據以下來源正文，提取與主題相關的事實資訊。

主題：{theme}
資料基準日期：{report_date}

輸入来源：
{discovery_sources_json}

WebFetch 正文：
{web_fetched_sources_json}

既有題材庫（用於評估新證據）：
{existing_topic_profiles_json}

提取要求：
1. 從每個 URL 的正文中提取關鍵事實（facts）
2. 識別與主題相關的公司與供應鏈關係
3. 記錄發布日期與來源等級
4. 標注風險提示與反證資訊
5. 不得捏造 URL 內容
6. 不得直接寫入正式題材庫
7. 若 report_date 指定，核心證據需 published_date <= report_date；無日期來源作輔助

輸出格式：
```json
{{
  "extracted_facts": [
    {{
      "fact": "事實描述",
      "source": "來源標題",
      "source_level": "L1_official|L2_media|L3_community",
      "publish_date": "YYYY-MM-DD",
      "url": "https://..."
    }}
  ],
  "topic_signals": ["信號1", "信號2"],
  "company_relations": [
    {{
      "company_code": "2330",
      "company_name": "台積電",
      "relation_type": "direct_product|supply_chain|brand_owner|indirect|unclear",
      "confidence": "high|medium|low",
      "reason": "為什麼受惠"
    }}
  ],
  "risk_notes": ["風險1", "風險2"],
  "missing_data": ["明顯缺漏的資料1"]
}}
```
