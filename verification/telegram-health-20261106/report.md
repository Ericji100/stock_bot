# Telegram 錯誤處理補強驗證

日期：2026-11-06（台北時間）。開發與離線驗證完成後，使用者確認無工作並同意重啟，**09:10已重啟正式Bot並確認補強生效**。原生輪詢及API成功狀態、監護心跳、43項指令及一則連線驗收通知通過。不新增付費AI／搜尋，沒有更改模型、帳務、交易規則或既有回測結果。

## 修改範圍

| 檔案 | 修改 |
|---|---|
| `stock_ai_bot/telegram/telegram_health_service.py` | 共用雙通道健康追蹤、完整診斷、去重及通知狀態；觀察原請求，不自行重跑業務 |
| `stock_ai_bot/telegram/telegram_push_service.py` | 沿用原敏感資訊遮蔽，增加完整診斷模式；原發送摘要仍最多500字，重試／分段不改 |
| `main.py` | 註冊全域錯誤處理及雙請求觀察器；沿用原每分鐘心跳工作，不新增探測排程 |
| `tests/test_telegram_health_service.py` | 34個測試案例，含真實PTB／HTTPX搭配模擬網路及原生輪詢重連；健康服務缺失／故障時仍留原始完整遮密診斷 |
| `README.md` | 說明診斷位置、狀態含義、去重、通知未知及啟用限制 |

程式回復基準：Git `0db668427a2d0e451151db4a5c5d6f0d19e815ec`。其他既有未提交內容維持原樣，不重設或混入本次變更。

## 逐項結果

| 項目 | 已驗證內容 | 結果 |
|---|---|---|
| 全域錯誤處理 | 原生`Application.process_error`接到同一個錯誤物件；已記錄傳輸錯誤不重複計數，未處理的程式錯誤另存完整診斷 | 通過 |
| 完整診斷 | 每次重複斷線各存例外鏈、時間、操作、事件編號；超過1200字仍完整，不只剩500字摘要 | 通過 |
| 隱私 | Bot Token、指定敏感值、Bearer及常見API金鑰遮蔽；不保存完整Update、業務訊息正文或請求參數 | 通過 |
| 去重 | 同次異常CMD提示預設五分鐘去重；Telegram異常／恢復各一次，快速恢復時合併；歷史診斷不刪 | 通過 |
| 真實恢復條件 | `getMe`不清除輪詢異常；只有有效成功的`getUpdates`回應才恢復，空結果亦可。錯誤HTTP／壞JSON不得算成功 | 通過 |
| 自動重連 | 真實PTB Updater配MockTransport：首次讀取失敗，由原生輪詢自動重試，後續空結果成功；同一Application不重啟 | 通過（離線模擬） |
| 不誤重送 | 不新增AI／交易重跑；健康通知自身錯誤不遞迴告警；通知取消／連線錯誤保留unknown，不自動重送 | 通過 |
| 重啟延續 | 發送中的舊通知視為結果未知；舊健康狀態先改未確認；舊恢復通知須等當前成功才發，不以過去成功冒充現在正常 | 通過 |
| 記錄失敗 | 狀態檔損壞保留原檔；診斷落盤失敗回退遮密的完整主控台診斷，不取代原請求例外；無法保存發送標記則不發提示 | 通過 |
| 啟動接線 | 真實ApplicationBuilder註冊兩個觀察器與全域handler；接收池1／一般池256，原接收讀取5秒／一般60秒，原bootstrap設定不變 | 通過（未連外） |
| 周邊回歸 | 選單、UTF-16分段、Radar推播、排程佇列、心跳、Luna／Terra路由及AI工作流 | 通過 |
| 正式生效與接收連線 | 新主PID13928，原生`getUpdates`與API成功、狀態及心跳均實際落盤；監護healthy，原43項指令逐項核對及一則通知送出。未另外呼叫getUpdates搶占polling | 通過（連線）；未代使用者執行`/start` |

健康通知只說明Telegram連線，不表示先前訊息已送達、AI工作已成功、排程已補跑或投資判斷正確。不為製造正式斷線而中斷網路或發付費請求。

## 測試證據

- 使用既有`tools/run_ai_offline_tests.py`，所有測試暫存與工作資料導向本次owned區；HTTP只准Mock／ASGI，外部HTTP禁止。
- 最終14個測試模組：**381 passed、99 subtests passed、1 warning、exit0**，約16.38秒。不是完整專案pytest，也不把重跑的重疊測試數相加。
- 最終交付紀錄：`delivery-final/session.json`、`delivery-final/source-sha256.json`，取代先前重疊的378／379項結果。596個受檢來源檔在測試前後一致。
- 警告來自既有python-telegram-bot `_updater.py:278`使用Python 3.14已棄用的`asyncio.iscoroutinefunction`；原生輪詢驗證仍通過。未改套件、未安裝依賴，也不以此擴大升級範圍。
- 語法及差異空白檢查：通過。正式設定、私密設定及其他未提交內容以`baseline.json`及`protection-check.json`核對。

## 運作與後續

新程式預設狀態在`.workdata/prod/runtime/telegram/state.json`；完整診斷在同目錄`diagnostics/YYYY-MM-DD.jsonl`。心跳的`schedule_health.telegram_transport`提供接收／API兩通道狀態、最後成功及通知計數。沿用工作資料設定檔隔離，不混入AI費用帳本。

經告知及使用者同意後，先保存`restart-preflight.json`、核對受測來源雜湊及無排程工作，再以原`tools/bot_watchdog.py --stop`停止舊PID22548／監護11580，沿`啟動機器人.bat`啟動新主PID13928／監護28500。Windows另有venv父轉接程序，不是重複的Bot。未更改啟動器、未補跑已錯過的AI排程；原晨報啟動檢查因已超出時間窗未補發。

`live-heartbeat.json`、`live-transport-state.json`保存實際健康證據。09:12:45監護主動喚醒檢查回報healthy；新狀態無待發或未知通知、無診斷儲存警告。`live-telegram-acceptance.json`確認getMe與43項指令清單一致，測試通知10284成功送出一則；這是外部唯讀／發送測試，不冒充原生polling恢復證據，後者由正式程序自身記錄。

`restart-acceptance.json`核對OpenAI帳本所有表／列與重啟前完全相同，沒有新增AI費用或未知預留，未改正式分額。真正斷線／恢復通知已在原生PTB離線情境驗證；沒有刻意斷開正式網路或模擬真實用戶輸入，後續可傳送`/start`確認實際指令回覆，並觀察自然發生的連線事件。

程式已在正式工作樹載入。依使用者後續授權，Git發布範圍限定上述五個程式／測試／README檔案及本驗證文件，共六檔；私密設定、帳務、原始機器證據及其他未提交內容不納入。機器證據留本機本目錄，非全部隨Git提供；是否成功推送及遠端版本，以`git-publication.json`與本輪最終回報的遠端完整SHA核對為準。此發布只提交與推送，不再次重啟Bot、變更模型或新增付費測試。
