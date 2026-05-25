# Topic Maintain Update Prompt
（正式題材庫已有資料時使用）

任務：請根據以下完整資料，產生題材知識庫更新變更包。比對新資料與既有題材庫，判斷每個題材的處理方式。

---

## 資料基準

- 報告日期：{report_date}
- AI 模型：{model}
- 時間戳記：{iso_timestamp}

---

## 股票宇宙摘要

```json
{structured_data_json}
```

---

## Discovery 搜尋結果

以下是 AI 搜尋取得的來源摘要：

```json
{discovery_sources_json}
```

---

## WebFetch 提取正文

以下是 WebFetch 成功取得的來源正文內容：

```json
{web_fetched_sources_json}
```

---

## 既有題材庫

請與以下既有題材庫比對，判斷新增、更新、合併或忽略：

```json
{existing_topic_profiles_json}
```

---

## 公司-題材對應

```json
{company_topic_map_json}
```

---

## 供應鏈節點

```json
{supply_chain_nodes_json}
```

---

## 近期掃描摘要

```json
{recent_scans_json}
```

---

## 候選公司清單（成交量前50名）

```json
{candidate_companies_json}
```

---

## 搜尋診斷資訊

```json
{search_diagnostics_json}
```

---

## 語言規則

- **theme_id**：必須使用英文小寫 snake_case（如 `ai_server`、`semiconductor_advanced`）
- **theme_name**：必須使用繁體中文（如「AI伺服器」、「半導體先進製程」），不得使用英文
- **summary**：繁體中文一字摘要
- **reason**：繁體中文說明
- **warnings**：繁體中文陣列
- **risk_notes**：繁體中文陣列
- **missing_data**：繁體中文陣列
- **keywords**：繁體中文為主，產品代號與技術名詞可保留英文
- **industries**：繁體中文產業名稱

---

## 任務說明

請根據上述資料，逐一判斷每個新題材（或新證據）應該如何處理：

### action_type 判斷原則

| 情境 | action_type |
|------|-------------|
| 新題材，既有料中無相似 | `create_theme` |
| 新聞證據支援既有題材 | `update_theme` |
| 兩個題材高度重疊（>70%關鍵詞重疊） | `merge_theme` |
| 題材名稱需修正 | `rename_theme` |
| 題材已退燒且無營收連結 | `ignore` |
| 新聞否定了既有題材的證據 | `update_theme`（帶負面證據）|

### 具體要求

1. **不得捏造**：不得捏造客戶名單、營收占比、產能數字，缺資料應在 missing_data 標註
2. **信心不足時**：若某題材證據不足，設為 `ignore` 或 `low confidence`，不要硬建
3. **merge_theme**：需指定 `target_theme_id` 為被合併到的既有題材 ID，並附帶理由
4. **update_theme**：需同時提供新的 evidence 與 counter_evidence
5. **ignore**：需附帶 `reason` 說明為何忽略（無營收連結、純消息熱點、題材已退燒等）

## 輸出格式

**嚴格 JSON-only，不得輸出其他說明文字**：

```json
{
  "change_id": "{timestamp}",
  "parent_change_id": null,
  "mode": "update",
  "status": "pending",
  "model": "{model}",
  "created_at": "{iso_timestamp}",
  "updated_at": "{iso_timestamp}",
  "summary": "一字摘要",
  "confidence": "high|medium|low",
  "actions": [
    {
      "action_type": "create_theme|update_theme|merge_theme|rename_theme|ignore",
      "theme_id": "ai_server",
      "theme_name": "AI伺服器",
      "keywords": ["AI伺服器", "GB200"],
      "industries": ["半導體", "伺服器"],
      "supply_chain_role": "核心受惠",
      "confidence": "high|medium|low",
      "reason": "為何做此判斷（含與既有題材庫的比對）",
      "evidence": [
        {
          "source": "來源標題",
          "source_level": "L1_official|L2_media|L3_community",
          "content": "具體證據內容（50字以內）",
          "url": "https://來源網址",
          "publish_date": "YYYY-MM-DD",
          "score_contribution": 8.0
        }
      ],
      "counter_evidence": [],
      "affected_companies": ["2330", "3711", "2308"],
      "supply_chain_nodes": [{"company": "2330", "role": "晶片製造"}],
      "target_theme_id": "既有題材ID（merge/rename時必填）",
      "risk_notes": ["風險提示"],
      "missing_data": ["缺漏的關鍵資料"]
    }
  ],
  "warnings": ["警告1"],
  "sources": [],
  "adjustment_notes": "",
  "raw_response_path": "",
  "prompt_log_path": ""
}
```

## 常見決策邏輯

- **題材是否為新**：「AI伺服器」與「AI供應鏈」若 >70% 公司重疊，應 merge
- **題材是否退燒**：無近三個月的新聞或營收連結，應 ignore 或降 confidence
- **題材是否成立**：需有近六個月的新聞證據 + 至少 2 家公司的實際客戶/產品對應
- **高低信心判定**：高信心 = 3+ 個差異化來源 + 明確營收連結；低信心 = 只有論壇或消息來源