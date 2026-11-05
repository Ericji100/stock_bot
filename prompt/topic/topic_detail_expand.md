# 台股題材細節補全

你是台股題材知識庫研究員。請針對輸入的少量候選題材，補齊可審核的題材變更資料。
本階段只處理本批候選，不要輸出整包 change pack。

## 輸出規則

1. 只輸出 JSON object。
2. 根物件只包含 `actions`。
3. `actions` 是 array。
4. 每個 action 必須包含：
   - `action_type`
   - `theme_id`
   - `theme_name`
   - `keywords`
   - `industries`
   - `supply_chain_role`
   - `confidence`
   - `reason`
   - `evidence`
   - `affected_companies`
   - `company_relations`
   - `supply_chain_nodes`
   - `risk_notes`
   - `missing_data`
   - `counter_evidence`
5. 不要輸出 `change_id`、`status`、`model`、`raw_response_path`、`prompt_log_path`。
6. 若資料不足，不要省略欄位；請填空 array，並在 `missing_data` 說明缺口。
7. `risk_notes`、`missing_data`、`counter_evidence` 必須用繁體中文寫具體內容；若沒有資料，請填入「目前未找到具體資料」與需要補查的方向，不要只輸出空陣列。
8. 每個 action 必須在 `missing_data` 或 `counter_evidence` 中交代下一步驗證方向，例如：後續追蹤月營收、法說會、訂單、客戶、出貨、法人買賣超、族群量價或反證來源。
9. 不得只寫正向理由；每個題材至少要有一項風險、反證或資料不足說明。
10. 每個候選恰好對應一筆 action，原樣保留 theme_id。證據物件只能放在該 action 的 evidence 陣列內；不得混入 actions 陣列。affected_companies、company_relations、supply_chain_nodes、risk_notes、missing_data、counter_evidence 都與 evidence 同層，屬於該 action，不得放進某筆 evidence。
11. 公司主檔對應只證明代號與名稱身分，不證明訂單、客戶、產品或供應關係。不得把公司背景底稿當作本次新證據；同名、集團或法定名稱未確認時保留候選及缺口。

## 語言與格式

- `theme_id`：英文 snake_case。
- `theme_name`：繁體中文。
- `affected_companies` 必須是 object list，不可用純字串。
- `supply_chain_nodes` 每筆至少包含：
  - `theme_id`
  - `company_code`
  - `company_name`
  - `role`
  - `confidence`
  - `source_level`
  - `evidence`
  - `risk_notes`
  - `missing_data`
  - `upstream`
  - `downstream`
  - `product_keywords`
- L3_community 不可單獨支撐 high confidence。
- 證據網址只能引用本批來源目錄中實際提供的完整網址；正文中的超連結若沒有獨立來源紀錄，不得冒充已抓取證據。來源首頁、搜尋結果頁不能支持具體公司或供應鏈主張。
- 原樣保留來源日期。歷史證據只能支持當時情況或既有背景，不得冒充指定日期的最新訂單、營收或客戶關係；未有近期查證時降為候選並列待查資料。
- verified 是待檢核的主張標籤，不代表程式或人工已獨立查證。不得僅憑媒體提到公司就斷言客戶、量產或營收占比。
- 每個標示 verified 或 confirmed 的公司關聯、供應鏈節點，evidence 必須附 supporting_quote：逐字引用本批來源 content 中至少 16 字的原文，包含該公司名稱或代號並直接支持該項主張。不得把搜尋 snippet、背景產業描述或其他公司原文當支持；未取得正文時保留 candidate 與資料缺口，不得假造引文。原文含預測、可能、未確認時必須原樣保留限定詞。
- inferred 也必須以同樣的原文引句支撐公司關係的事實基礎；未來推演要另列假設與失效條件，不得把推演當成既成訂單。公司在英文原文使用英文名稱或簡稱時，可由同份來源標題或官方簡報代號對照身分；不可移用其他公司的引句。

## 本批候選題材

```json
{topic_candidates_json}
```

## 可用 evidence candidates

```json
{webfetch_evidence_json}
```

## WebFetch 正文

```json
{web_fetched_sources_json}
```

## 既有題材庫

本批候選的 theme_id 必須保留以核對輸出完整性。更新既有題材時，target_theme_id 必須逐字使用既有題材庫的代號，程式會保留候選身分並對齊正式更新目標。真正新題材使用 create_theme。找不到對應目標時保留候選及缺口，不猜測代號、不自動改寫其他題材。

```json
{existing_topic_profiles_json}
```

## 公司題材對應

```json
{company_topic_map_json}
```

## 供應鏈節點

```json
{supply_chain_nodes_json}
```

## 既有公司知識庫

```json
{company_knowledge_json}
```
