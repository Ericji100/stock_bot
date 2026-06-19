# MiniMax M3 低階資料整理重試任務

上一段低階資料整理失敗。請改用更保守的 JSON 格式修復方式重試。

你仍然只是資料整理員，不是最終分析師。

## 重試要求

- 不可以產出最終投資結論。
- 不可以產出買賣建議。
- 不可以產出最終評分。
- 不可以因為資料太多就刪除核心資料。
- 如果某些資料無法完整整理，請寫入 `warnings`。
- 如果資料不足或來源矛盾，請寫入 `missing_data` 或 `counter_evidence`。
- 請只輸出可被 `json.loads()` 解析的 JSON。

## 失敗資訊

- schema_version：{schema_version}
- segment_label：{segment_label}
- source_ids：{source_ids_json}
- first_error：{error}

## 請輸出 JSON

```json
{{
  "schema_version": "low_model_digest_v1",
  "status": "success",
  "model_role": "資料整理員重試",
  "facts": [],
  "events": [],
  "risk_evidence": [],
  "counter_evidence": [],
  "theme_hypotheses": [],
  "missing_data": [],
  "source_map": [],
  "warnings": []
}}
```

## 本段完整分段資料

```json
{compact_payload_json}
```
