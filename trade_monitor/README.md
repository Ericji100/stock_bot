# 台指期 1 分 K 監控套件

本目錄集中 Google 遠端桌面純圖表擷取後的結構化分析、固定九欄排版、Telegram bridge、中斷恢復、交易憲法持久狀態、規則版本及測試支援。專案根目錄同名 Python 檔只作舊版 Automation 相容入口；正式整合版使用 `python -m trade_monitor.<module>`。

## 目錄

- `analysis_adapter.py`：擷取時間定錨、schema／contract 驗證、持久事件、重大結構強制通知、九欄 canonical message 與傳輸編排。
- `analysis_contract.py`：固定 enums、驗證與九欄 Markdown renderer。
- `bridge.py`：既有 Telegram Bot 的 UTF-8 傳輸與 event ID 去重。
- `resume_guard.py`：中斷恢復通知及跨回合租約。
- `constitution_state.py`：期交所交易日、模擬進場統計、連續停損、冷卻、返場確認及事件去重；沒有每日進場硬性上限。
- `policy.py`：號盤、四象限 × 四型態與 S／S／T／V 可測試准入矩陣。
- `market_structure_state.py`：樞紐生命週期、大小級多空防線、左右成熟度、候選階段、情境路徑與點位區的原子狀態驗證；正式 v1.6 維持 state v2，RC 可明確升級至 v3。
- `anchor_state.py`：v1.7 RC 的因果定錨、反向定錨、大小級控制權與動態象限候選狀態；不另外產生第二套進場訊號。
- `cclass_state.py`：v1.8 RC 的完整太極段序、五項複製／修正品質、一之動能階段及工作看法狀態；只補強同一決策，不新增型態或通知欄位。
- `local_scheduler.py`：每分鐘第 05 秒啟動純圖表擷取、Codex 結構化分析、finalize 與 outbox。
- `dual_scale.py`：Chrome DETAIL／OVERVIEW 的15分鐘排程、共享租約、恢復確認、全局摘要時效及 fail-safe；正式 `v1.6-dual-scale` 已啟用此協調層，Automation 狀態仍由使用者控制。
- `chart_capture.ps1`：Windows 固定純圖表裁切與像素驗證，不擷取右側委託／帳務區。
- `local_outbox.py`：將 finalize 的同一份 canonical message 交給 Automation heartbeat relay。
- `start_local_scheduler.ps1`：Windows 登入後隱藏啟動本機常駐程序。
- `schemas/`：歷史相容 schema、正式 v4 與定錨 RC v5 schema；使用者可見排版仍固定九欄。
- `rules/`：不可變版本、目前正式指標與啟用歷史；分類導覽見 `rules/VERSION_INDEX.md`。
- `docs/`：架構與傳輸文件；`chrome-dual-scale-rc.md` 說明Chrome雙視角RC。
- `experiments/`：不屬於正式執行流程的時鐘測試。

執行期狀態只放在 `.runtime/trade_monitor/`，不提交 Git，也不得保存帳務或 Telegram 憑證。

目前正式版本以 `rules/active-version.json` 為唯一權威；現值是 `enlightenment-integrated-v2.1.8-anchor-origin`（schema v8／market structure state v6），Automation `1-k` 狀態為 `ACTIVE`，本機 scheduler config 亦鎖定同版本。正式版、回測版與歷史封存版的分層說明見 `rules/VERSION_INDEX.md`。
