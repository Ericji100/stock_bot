# 官方 DeepSeek 遷移驗證

日期：2026-11-02。分支：`codex/deepseek-unification`。

## 範圍與現況

- 開發位置：`D:/code/stock_ai_bot/.worktrees/deepseek-unification`。
- 基準版本：`3db72c5c99949882a9ecca4e813c7d459f570642`。
- 本分支未合併；未啟動、停止或重啟正式 Bot，未發送 Telegram，未套用正式題材庫更新。
- 主工作區既有 ETF、題材設定與其他未提交內容均不屬本次修改。
- 未安裝依賴；使用專案既有 `.venv`。未複製正式秘密設定、資料庫或 junction。
- 本次只有離線模擬傳輸與程式驗證，未使用真實 DeepSeek／搜尋額度。正式品質驗收待 API Key。

## 完成檢核

| 項目 | 實作與離線驗證 | 真實驗證 |
|---|---|---|
| 七種投研報告 | research、value_scan、macro、theme、theme_flow、theme_radar、sector_strength 接同一官方客戶端；模擬 HTTP 跑過報告寫檔與 metadata | 待金鑰與正式內容審稿 |
| Radar | 官方搜尋與 JSON 短評；過大批次先對半拆分，保留既有單股護欄與評分規則；餘額不足保留先前成功短評與本地名單 | 待真實候選池、短評及推播內容檢查 |
| 新聞 | 非思考 JSON 分類；逐篇分類快取，未分類項目存入待補；後續 AI 分類更新同 URL 舊 fallback | 待真實搜尋與資料日期檢查 |
| 題材庫 | 同一官方模型的候選整理與最終思考審核；審核未完成不得套用更新包；維護錯誤不再一律回報 success | 待完整變更包、證據與反證審稿 |
| 舊模型停用 | 正式設定載入 `deepseek_only=true` 時，不建立 Gemini、MiniMax、OpenCode、Tavily 客戶端；舊明確模型旗標拒絕；舊按鈕失效提示 | 待實際 Telegram 舊按鈕操作 |
| 模型選單 | 報告、新聞、題材與 Radar 改為直接使用 DeepSeek；其餘日期／來源等互動保留 | 待 Telegram 實際操作 |
| 背景新聞 | 排程仍執行資料更新，`scheduled_news_push_enabled=false` 時不呼叫 Telegram 發送 | 模擬排程已驗證；待真實時段觀察 |
| 費用保護 | 官方餘額查詢、每日預算、背景餘額保留、402／401 阻擋、429／5xx 有界重試；逾時費用未知且不盲目重送 | 待官方回應／帳單交叉核對 |
| 共用快取 | 相同請求合併，成功分析依日期／模型／內容快取；搜尋與正文快取；錯誤與截斷輸出不當成成功快取 | 待真實 token 與耗時對比 |
| 搜尋品質 | 沿用來源分級、日期與反證檢查、必備資料缺口；來源很多但品質低仍補搜；正文不相關或太短不得標成健康 | 待真實來源品質抽查 |
| 狀態與證據 | 正式 AI、partial、fallback、失敗分開；保存來源、prompt、階段成果、token usage、重試與費用估計 | 待完整 Live 鏈核對 |

原報告 prompt、評分標準、輸出合約、HTML 分頁與本地資料共用層沿用。本次不另設核心資料刪除／壓縮策略，也不保證更換模型後品質與舊模型完全相同；品質必須由真實報告比較確認。

## 主要修改

- `research_center/deepseek_service.py`：官方 HTTP、共享餘額、預算預留、SQLite 用量帳本、成功請求快取與安全錯誤。
- `research_center/deepseek_search_service.py`：官方 Search / Fetch，依既有來源規則標準化，不把搜尋引擎 authority 當成本地可信度。
- `research_center/config.py`、`config/research_center.json`：官方金鑰與模型設定、每日預算、新聞通知開關。歷史直接建構的相容測試設定保留舊路徑；正常設定載入預設開啟 DeepSeek-only。
- `research_center/orchestrator.py`、`ai_workflow_service.py`、`segmented_analysis_service.py`：統一報告與整理角色、執行緒費用情境傳遞、額度不足提前停止、成功階段重用。
- `research_center/news_service.py`、`news_repository.py`、`web_fetch_service.py`：單篇分類重用、待補佇列、正文品質與日期、背景費用標記。
- `research_center/topic_maintain_service.py`、`topic_pipeline_service.py`、`topic_apply_service.py`：官方雙角色、完整審核護欄，不把本地 fallback 更新視為已通過 AI 審核。
- `stock_ai_bot/monitoring/radar_service.py`：搜尋與短評遷移、平衡批次、失敗短路與已完成短評保留；不改本地股票分數。
- `main.py`、`telegram_handlers.py`、`command_parser.py`：排程、靜默新聞、停用模型選單及舊回呼；保留歷史指令讀取相容性。
- `report_builder.py`、README、啟動 runner：診斷 metadata 與文件；不再自動啟動／安裝 MiniMax MCP。
- `tests/`、既有稽核工具：新增官方 API 模擬與接入回歸、更新舊模型選單預期；修正題材稽核誤讀其他執行的輸出。
- `tools/deepseek_live_validation.py`：需明確執行的付費驗證入口；不發 Telegram、不套用題材變更，失敗停止後續案例，品質仍標示待人工檢閱。

## 測試

使用：`D:/code/stock_ai_bot/.venv/Scripts/python.exe -B -X utf8`。

1. `-m unittest tests.test_deepseek_service`：33 項通過。HTTP 由 `httpx.MockTransport` 模擬，不是官方連線。
2. `-m unittest discover tests`：1,410 項執行完成，`OK (skipped=1)`，耗時 36.186 秒；1,409 項通過，1 項私有設定測試跳過。
3. 修改的 28 個 Python 檔使用 AST 語法解析通過；`git diff --check` 通過。
4. `tools/deepseek_live_validation.py --check-config`：缺金鑰時回傳 2，沒有建立付費連線。
5. 私有 `config.json` 不存在的檔案內容測試跳過；未為了測試複製正式秘密設定。跳過項不代表已驗證正式 Bot 設定。

證據保存：`logs/deepseek_unification/20261102_offline/`。完整紀錄含模擬資料與測試診斷，不代表 Live 抓取成果。

本次 owned 測試暫存 435 檔、12,527,527 bytes 已依計畫回收，收據狀態 `RECYCLED_VERIFIED`。計畫與收據留在 `.codex-cleanup/`；必要測試證據留在上述 logs。既有測試自行產生、但不在 owned 區內的隔離 worktree 快取不擴大清理；正式資料與其他任務未處理。回收不等於立即釋放同量磁碟空間。

## API Key 與後續驗收

請在此 worktree 的 `config/secrets.json` 設定 `deepseek_api_key`，或為本次測試程序設定 `DEEPSEEK_API_KEY`；不要貼在聊天。該檔被 Git 忽略，不會提交。既有其他服務金鑰不刪除、不提交。

先跑 `python tools/deepseek_live_validation.py --check-config`（不連線），再跑 `--case research`（付費）。檢閱來源、日期、章節、AI 細項評分／情境／反證、HTML 與 usage 後，再執行 `--all`。雷達需先有有效候選池；隔離 worktree 不使用正式資料 junction，缺資料必須先補足，不把空名單當通過。

工具不會自動判定「精準、可信且有想像力」；只能記錄機械式成功。人工需逐份比較事實來源、數據一致性、反證、情境假設、章節完整與繁體中文呈現，不以檔案存在代替品質驗收。

餘額查詢快取 60 秒；儲值後可等快取過期再執行。費用帳本以公開 Flash 費率估算，尖峰保守處理，中國節日折扣未套用；人民幣換算 7.5 為設定假設，不是即時匯率。最終費用以官方帳單為準。API Search / Fetch 的帳戶可用性也必須真實驗證；若官方未開放，不偷偷切回已停用服務。

未完成：官方金鑰連線、十入口 Live 報告審稿與費用比較、Telegram 實際收送及排程時段觀察、使用者確認合併與正式 Bot 切換。
