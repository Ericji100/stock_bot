# 台指期根目錄相容層封存

本目錄保存 2026-09-22 從專案根目錄撤出的舊模組入口與 v1 schema。這些檔案不屬於目前正式執行路徑，只供歷史提示詞、還原稽核與問題追查使用。

目前正式入口如下：

| 舊入口 | 正式入口 |
|---|---|
| `trade_monitor_analysis_adapter` | `trade_monitor.analysis_adapter` |
| `trade_monitor_analysis_contract` | `trade_monitor.analysis_contract` |
| `trade_monitor_bridge` | `trade_monitor.bridge` |
| `trade_monitor_resume_guard` | `trade_monitor.resume_guard` |
| `trade_monitor_analysis_schema.json` | `trade_monitor/schemas/analysis-v1.json`（歷史 v1）；目前正式版本依 active manifest 使用版本化 schema |

正式 Automation `1-k`、本機 scheduler、目前文件與測試都必須使用 `trade_monitor.*`。若需精確重現仍引用舊模組名稱的歷史提示詞，應先在隔離環境建立還原分支並驗證，不得直接把本目錄加入正式執行路徑。
