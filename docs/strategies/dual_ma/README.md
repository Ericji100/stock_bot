# 雙均線交易策略治理入口

本目錄是雙均線策略的單一規格入口。研究、回測、選股與監控任務都必須引用明確版本，不得以對話記憶、口頭摘要或自行推論取代版本文件。

## 目前版本

- 版本：`v0.4.1-frozen`
- 狀態：`FROZEN`
- 完整規格：[`versions/v0.4.1-frozen/spec.md`](versions/v0.4.1-frozen/spec.md)
- 版本資訊：[`versions/v0.4.1-frozen/manifest.json`](versions/v0.4.1-frozen/manifest.json)
- 前一版本：[`versions/v0.4-frozen/spec.md`](versions/v0.4-frozen/spec.md)

`FROZEN` 只允許依凍結契約實作及回測，不得直接部署到正式監控。版本生命週期如下：

```text
DRAFT -> FROZEN -> BACKTESTED -> SHADOW -> ACTIVE -> RETIRED
```

## 治理文件

- [`decision-log.md`](decision-log.md)：已決議事項及理由。
- [`experiment-registry.md`](experiment-registry.md)：基準組、比較組與延後研究項目。
- [`coordination.md`](coordination.md)：四個任務角色、檔案所有權與合併順序。
- [`handoff-template.md`](handoff-template.md)：主控派工及執行任務回報格式。

## 權限原則

1. `雙均線交易系統｜00 研究與規則決策` 是唯一規則決策來源。
2. 執行任務可以發現問題、提出建議、實作指定版本及產生報告，但不得自行改變規則。
3. 對規格的任何實質變更都必須回到主控任務，寫入決策紀錄並升版。
4. 回測與監控必須記錄規格版本、Git commit、資料期間、股票母體、成本模型及執行 `run_id`。
5. `reports/`、`outputs/` 與大型行情資料是本機產物；只有經主控採納的精簡結果才提升到 Git 追蹤區。

## 版本狀態定義

| 狀態 | 意義 | 允許事項 |
|---|---|---|
| `DRAFT` | 規則仍有未決項目 | 盤點、設計、檢查 |
| `FROZEN` | 單次實驗規格已鎖定 | 實作及因果回測 |
| `BACKTESTED` | 已完成指定驗證 | 主控判讀與候選升級 |
| `SHADOW` | 只記錄實際訊號，不下單 | 前向驗證與營運演練 |
| `ACTIVE` | 正式允許使用 | 依已核准流程監控 |
| `RETIRED` | 不再使用 | 稽核與歷史重現 |
