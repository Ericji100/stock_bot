# 官方 DeepSeek 遷移驗證

日期：2026-11-02。分支：`codex/deepseek-unification`。

## 範圍與現況

- 開發位置：`D:/code/stock_ai_bot/.worktrees/deepseek-unification`。
- 基準版本：`3db72c5c99949882a9ecca4e813c7d459f570642`。
- 本分支未合併；未啟動、停止或重啟正式 Bot，未發送 Telegram，未套用正式題材庫更新。
- 主工作區既有 ETF、題材設定與其他未提交內容均不屬本次修改。
- 未安裝依賴；使用專案既有 `.venv`。未複製正式秘密設定、資料庫或 junction。
- 離線開發之後已使用使用者提供的本機金鑰完成官方連線與單一研究 Live；本輪已消耗少量官方額度。第一份報告技術成功、人工品質未通過，其他九入口尚未擴跑，不能宣稱整體驗收完成。

## 完成檢核

| 項目 | 實作與離線驗證 | 真實驗證 |
|---|---|---|
| 七種投研報告 | research、value_scan、macro、theme、theme_flow、theme_radar、sector_strength 接同一官方客戶端；模擬 HTTP 跑過報告寫檔與 metadata | research 已真實產出，但人工品質需修正；其餘待測 |
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

## 首次官方 Live 結果

金鑰由使用者填入本機忽略檔後讀取，未顯示、提交或複製至正式主工作區。

- 官方餘額查詢可用；Search 真實回傳 10 筆來源、Fetch 真實回傳 2,384 字元正文。
- 非思考 JSON 模式真實呼叫成功，官方用量 83 輸入／5 輸出 token。
- `/research 2330 --deep` 跑完真實資料收集、搜尋、正文補抓、六段思考分析及最終整合，沒有 AI timeout 或 fallback。
- 耗時 746.1 秒。原始來源 271 筆；正式產生 Markdown、HTML、JSON、sources、prompt 與階段記錄。
- 官方回傳用量：261,374 輸入 token、84,217 輸出 token（包含模型計費的思考用量，不能只用報告字數估費）。七次模型呼叫、36 次搜尋、一次 Fetch。
- 單份研究估計 US$0.1159863；連同獨立連線探測，本輪合計估計 US$0.11750175。不含其他時間的使用，也不能當每日平均或最終帳單。
- 報告：`reports/stock/2330/research_2330_20261102_201010_5593a1ed.*`。
- 個案紀錄：`logs/deepseek_live_validation/20261102_195755_174692/`。
- 用量、原始進度、修正後離線測試與標題保真檢核：`logs/deepseek_unification/20261102_live/`。

### 品質檢閱與必要修正

報告保留四項 AI 評分、細項權重、營收財報、估值、情境推演、風險、觀察與資料缺口，且未把缺失法人資料當作零。但人工檢閱結果仍是 `needs_revision`，不是可上線驗收通過：

1. 既有 CMoney 論壇擷取將 JavaScript／CSS 誤作標題，長達 372,319 字元。回查證明不是官方 Search 的回傳標題，而是本地論壇來源混入；資料被多處索引重複展開，浪費分析段落。
2. 已於共用來源標題整理、SourceItem 正規化、DeepSeek 搜尋／Fetch 及網頁標題擷取補防護。只隔離明確程式碼標題，使用網站名稱標示，不截斷正常財務長文；來源與正文都保留。
3. 用本次 271 筆實際來源重放，不呼叫 API：來源數仍為 271，網址與正文完全相同，標題總長由 385,744 降為 13,444 字元。只修正一筆程式碼標題；原報告與原始證據保留，未重寫或掩蓋失敗報告。
4. 估值表有算術一致性問題，例如 EPS 100 × 本益比 18–20 的乘算區間應為 1,800–2,000，報告卻列為 1,850–2,050，未解釋額外調整。章節 QA 雖通過，不代表數值已驗算。
5. 法人與融資融券各有 45 秒來源逾時；本地財報仍到第二季。官方 Fetch 探測已取得第三季公告，研究流程卻未有效選入，顯示仍需補最新官方財報的缺口搜尋與優先取證，而不是只增加來源總數。

因此未繼續擴跑其他九指令，未自動調整 AI 評分／目標價，也未合併或重啟正式 Bot。接續須先驗證標題修正後的實際入模包與重新產報結果，再處理財報新鮮度與數值一致性；在單項人工品質通過後才擴大付費驗證。

Live 後修正回歸：1,415 項執行完成，`OK (skipped=1)`，47.675 秒；共 1,414 項通過。新增／現有 DeepSeek 專項共 38 項包含在內。本輪單元測試於子程序覆寫測試金鑰並封鎖真實官方 HTTP 傳輸，只允許 MockTransport；不是再次消耗正式額度的 Live。紀錄在 `post-live-unittest.txt`。

本輪 owned 合成測試暫存另有 109 檔、3,257,603 bytes，已依計畫回收且收據為 `RECYCLED_VERIFIED`；真實報告、來源、prompt、用量帳本與回歸紀錄均保留，不當暫存回收。

未完成：修正後單一報告重產與品質通過、其他九入口 Live 報告審稿與費用比較、Telegram 實際收送及排程時段觀察、使用者確認合併與正式 Bot 切換。
