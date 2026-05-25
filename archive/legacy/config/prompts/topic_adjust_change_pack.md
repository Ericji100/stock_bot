# Topic Adjust Change Pack Prompt

任務：請根據原始變更包與使用者調整意見，產生修正版變更包。

原始變更包：
{original_change_pack_json}

資料基準日期：{report_date}
AI 模型：{model}
使用者調整意見：
{adjustment_text}

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

## 調整規則

1. 只輸出合法 JSON，不得輸出其他文字說明
2. 不得捏造來源或公司資料
3. 不得直接寫入正式題材庫
4. 新 change_id 必須與原始 change_id 不同，加上尾碼，例如 `change_xxx_r1`
5. parent_change_id 必須填入原始 change_id
6. mode 固定為 adjust
7. 根據調整意見修改 actions、warnings 或 adjustment_notes
8. 每個 action 必須包含 confidence、reason、evidence
9. theme_id 必須為英文小寫 snake_case

### 調整意見數量要求

- 如果使用者要求「至少 N 個題材」，actions 數量不得少於 N
- 如果資料不足，仍需建立低信心基礎題材，並在 `warnings` 說明缺少資料
- `affected_companies`、`risk_notes`、`missing_data`、`supply_chain_nodes` 不得省略，資料不足時填入候選值並標註低信心

### 嚴禁行為

- 不得只重複原始 change pack 的 actions 而不依調整意見修改
- 不得輸出英文題材名稱
- 不得在 theme_name 使用未翻譯的英文

---

輸出格式：
```json
{{
  "change_id": "change_xxx_r1",
  "parent_change_id": "change_xxx",
  "mode": "adjust",
  "status": "pending",
  "model": "{model}",
  "created_at": "{iso_timestamp}",
  "updated_at": "{iso_timestamp}",
  "summary": "一字摘要",
  "confidence": "high|medium|low",
  "actions": [
    {{
      "action_type": "create_theme|update_theme|...",
      "theme_id": "ai_server",
      "theme_name": "AI伺服器",
      "keywords": ["關鍵詞1"],
      "industries": ["半導體"],
      "supply_chain_role": "核心受惠",
      "confidence": "high|medium|low",
      "reason": "為什麼成立",
      "evidence": [
        {{
          "source": "來源名稱",
          "source_level": "L1_official|L2_media|L3_community",
          "content": "證據內容摘要",
          "url": "https://...",
          "publish_date": "YYYY-MM-DD",
          "score_contribution": 10.0
        }}
      ],
      "counter_evidence": [],
      "affected_companies": ["2330"],
      "supply_chain_nodes": [],
      "target_theme_id": null,
      "risk_notes": [],
      "missing_data": []
    }}
  ],
  "warnings": [],
  "sources": [],
  "adjustment_notes": "根據調整意見：{adjustment_text}，做了以下修改：...",
  "adjustment_check": {{
    "user_request_summary": "調整意見摘要",
    "changes_made": ["已完成項目"],
    "not_fully_satisfied": ["未完成項目"],
    "satisfaction": "satisfied|partial|not_satisfied"
  }},
  "raw_response_path": "",
  "prompt_log_path": ""
}}
```

### adjustment_check 欄位規則

- **user_request_summary**：簡要說明如何理解使用者的調整意見。
- **changes_made**：列出已完成或實質修改的項目。
- **not_fully_satisfied**：列出未完成、資料不足或無法確實修改的項目。
- **satisfaction**：AI 自我評估
  - `satisfied`：已完全按調整意見修改。
  - `partial`：部分修改，部分項目未完成或無法完全滿足。
  - `not_satisfied`：只重複原 change pack，未依調整意見實質修改。
- **不得假裝完成**：如果只複製原始 actions 而未實質改寫，`satisfaction` 必須為 `not_satisfied`。
- **不得省略**：所有調整都必須提供 `adjustment_check`，不得留空。