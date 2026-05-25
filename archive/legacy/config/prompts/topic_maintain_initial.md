# Topic Maintain Initial Prompt
（正式題材庫為空時使用）

任務：請根據以下完整資料，產生第一版題材知識庫初始化變更包。這不是研究報告，而是建立題材知識庫的結構化變更指令。

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

## 既有題材庫（目前為空）

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

## 數量要求（初始化模式）

- 初始化模式不得只產 3～5 個題材
- **建議產生 12～20 個基礎題材**
- 每個 action 必須盡量補：
  - `affected_companies`：不得省略
  - `risk_notes`：不得省略
  - `missing_data`：不得省略
  - `supply_chain_nodes`：不得省略
- 若資料不足，仍需建立低信心基礎題材，並在 `warnings` 說明缺少資料

### AI 伺服器類題材建議拆分

AI 伺服器為綜合題材，應依供應鏈角色拆分子題材：
- 先進封裝（CoWoS）
- 散熱（液冷/氣冷）
- 電源供應（BBU、電源管理）
- PCB/CCL（高速材料）
- 伺服器代工（ODM、OEM）
- 網通（高速交換器）
- ASIC/GPU（AI 晶片）
- 記憶體（HBM、DRAM）

---

## 任務說明

請根據上述所有資料，判斷並建立題材知識庫。特別注意：

1. **題材識別**：從 discovery_sources、web_fetched_sources、行業分布中找出真正有營收連結的題材
2. **供應鏈角色**：每個題材需對應到實際的台灣供應鏈公司（從候選清單中比對）
3. **證據品質**：每個 create_theme action 必須有 2+ 個差異化來源（官網、財報、媒體），不得只靠單一來源
4. **題材合併**：若多個題材高度重疊，應合併而非重複建立
5. **題材拒絕**：若某題材無明確營收連結或僅是短期消息熱點，應設為 ignore 或 low confidence
6. **不得捏造**：不得捏造客戶名單、營收占比、產能數字等，缺資料應在 missing_data 標註

## 輸出格式

**嚴格 JSON-only，不得輸出其他說明文字**：

```json
{
  "change_id": "{timestamp}",
  "parent_change_id": null,
  "mode": "initial",
  "status": "pending",
  "model": "{model}",
  "created_at": "{iso_timestamp}",
  "updated_at": "{iso_timestamp}",
  "summary": "一字摘要",
  "confidence": "high|medium|low",
  "actions": [
    {
      "action_type": "create_theme",
      "theme_id": "ai_server",
      "theme_name": "AI伺服器",
      "keywords": ["AI伺服器", "GB200", "AI伺服器供應鏈"],
      "industries": ["半導體", "伺服器", "散熱"],
      "supply_chain_role": "核心受惠",
      "confidence": "high|medium|low",
      "reason": "為何成立此題材（含營收連結）",
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
      "risk_notes": ["風險提示1"],
      "missing_data": ["缺漏的資料欄位"]
    }
  ],
  "warnings": ["警告1", "警告2"],
  "sources": [],
  "adjustment_notes": "",
  "raw_response_path": "",
  "prompt_log_path": ""
}
```

## 欄位說明

- `theme_id`：英文小寫 snake_case，長度 ≤ 40，不可重複
- `action_type`：僅限 `create_theme`（初始化模式不包含 update/merge）
- `affected_companies`：使用公司代碼陣列，至少 2 家
- `confidence`：高/中/低，低信心應附帶說明原因
- `missing_data`：客觀列出缺漏的關鍵資料