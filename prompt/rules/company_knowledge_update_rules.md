# 公司知識庫自動補全規則

本規則只用於整理可寫入 `config/company_knowledge.json` 的公司知識欄位，不得產出投資結論、推薦評分、目標價或買賣建議。

## 可寫入 JSON 的欄位

只允許輸出以下欄位：

```json
{
  "companies": {
    "股票代號": {
      "company_name": "公司名稱",
      "product_lines": ["產品或服務"],
      "customers": ["已具名且可驗證客戶"],
      "revenue_exposure": ["已揭露營收曝險或占比"],
      "supply_chain_roles": ["供應鏈角色"],
      "evidence_sources": [
        {
          "title": "來源標題",
          "url": "來源網址",
          "source_level": "Level 1 | Level 2 | Level 3",
          "published_date": "YYYY-MM-DD 或 null"
        }
      ],
      "missing_data": ["仍缺資料"],
      "confidence": "auto_high | auto_medium | auto_low"
    }
  }
}
```

## 寫入限制

1. 只可使用官方公告、公司 IR、交易所、MOPS、財報、法說會、可信財經媒體或產業媒體。
2. PTT、Dcard、Mobile01、社群貼文、未具名傳聞、無 URL 來源不得寫入正式公司知識庫。
3. 若只有題材推論，最多寫入 `supply_chain_roles`，不得寫入具名客戶或營收占比。
4. 若資料不足，欄位留空並寫入 `missing_data`，不得自行補故事。
5. 既有欄位不得覆蓋，只能補空欄位或追加新的 evidence source。
6. 本規則輸出只供資料庫更新，不可混入投資建議。
