# 台股題材候選萃取

你是台股題材知識庫研究員。請根據輸入資料產生「候選題材清單」。
本階段只產生候選，不要輸出完整 change pack。

## 輸出規則

1. 只輸出 JSON object。
2. 根物件只包含 `candidates`。
3. `candidates` 是 array。
4. 每筆候選欄位：
   - `theme_id`：英文 snake_case，可由你建議。
   - `theme_name`：繁體中文。
   - `keywords`：題材關鍵字 array。
   - `reason`：為什麼值得納入候選。
   - `candidate_companies`：相關公司 array，每筆含 `company_code`、`company_name`、`role`。
   - `source_refs`：來源摘要 array。
5. 不要輸出 Markdown。
6. 不要輸出 `actions`。
7. 不要輸出 `change_id`、`status`、`raw_response_path`、`prompt_log_path`。

## 產出數量

- mode=`initial`：請產生 30～50 個候選題材。
- mode=`update`：請產生 15～30 個候選題材。

## 資料

mode: `{mode}`
report_date: `{report_date}`
model: `{model}`

### 規則式 evidence candidates

```json
{webfetch_evidence_json}
```

### Discovery 來源

```json
{discovery_sources_json}
```

### WebFetch 正文

```json
{web_fetched_sources_json}
```

### 近期掃描候選

```json
{recent_scan_candidates_json}
```

### 市場訊號

```json
{market_signals_json}
```

### 外部產業來源快取

```json
{external_topic_source_caches_json}
```

### 既有題材庫

```json
{existing_topic_profiles_json}
```

### 既有公司知識庫

```json
{company_knowledge_json}
```
