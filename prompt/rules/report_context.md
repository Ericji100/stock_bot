## 指令 JSON

```json
{request_json}
```

## 結構化資料

```json
{structured_data_json}
```

## 來源清單

{source_text}

## 報告可讀性規則

- 報告正文不得直接輸出內部欄位名稱、英文狀態碼、JSON key、snake_case ID 或 `key = value` 除錯格式。
- 若需要引用內部狀態，必須改寫成繁體中文投研語句。例如 `verified` 寫成「已驗證」、`candidate` 寫成「候選觀察」、`missing` 寫成「資料缺口」、`L2_media` 寫成「媒體來源」、`market_validated = false` 寫成「盤面尚未明確驗證」。
- 原始欄位可保留在 JSON、metadata 或技術附錄，但正文要以投資研究讀者可理解的語氣呈現。

請產出完整 Markdown 報告，必須遵守來源引用與資料不足規則。
