# 2026-11-12 排程新聞搜尋派送修正驗證

## 交付範圍與狀態

- 隔離分支：`codex/ai-schedule-stability`，基準 `c4e73f7`。
- 僅修正新聞搜尋的派送時間檢查、來源／缺口保存、分類銜接及對應通知判定。
- 本輪沒有外部搜尋、付費模型呼叫、正式帳務遷移、Git 合併、部署或重啟。
- 模型、分額、搜尋總期限、交易規則、0050 與既有回測預期均不改。
- 修正與離線驗證已完成；完整測試 3,310 通過、9 略過、363 子案例通過，無失敗。此文件不是部署完成證明。

## 今晚原始紀錄

工作識別碼：`e1a7052b712a43249058d7ce97d9983c`。

| 項目 | 原始結果 |
|---|---|
| 搜尋請求 | 19 筆，18 成功、1 筆讀取逾時且費用未知 |
| AI 分類 | 0 次；搜尋異常後原流程停止 |
| 已結算費用 | US$0.232455610，屬今晚正式排程，不是本輪測試支出 |
| 未知預留 | US$0.288884，實付仍未知，未解除 |
| 來源保存 | 2,037 筆版本、828 個唯一網址，含先前來源種子，不全是今晚新增 |
| 最後請求可用讀取時間 | 約 37.049 秒，實際等待約 37.283 秒後逾時 |

未知請求 `d4a9b7e11da346dc8d4f0d6241c7e0a4` 僅有請求紀錄，沒有回應，不虛構內容或實付。

## 修正與逐項驗證

| 項目 | 修改位置 | 驗收結果 |
|---|---|---|
| 共用派送檢查 | `research_center/ai_execution_control.py` | 僅背景新聞啟用；預設需要 90 秒請求時間及 5 秒派送／存檔餘裕，較短明確等待設定仍有效 |
| 預留前與送出前雙檢查 | `research_center/openai_service.py`、`research_center/deepseek_service.py` | 快取可先命中；不足不新建預留；若預留後時間耗盡，未送出記為零費用，不標成未知 |
| 保存未完成工作 | `research_center/deepseek_search_service.py` | 保存來源、事件、當前未送出查詢、任務／階段、原因、未執行查詢數；覆蓋仍不完整 |
| 分類接續 | `research_center/news_service.py` | 搜尋子期限結束後回到原工作期限；只對未送出的時間不足接續分類；使用者取消、外層期限或真實未知費用仍停止 |
| 通知語意 | `main.py`、`news_service.py` | 正常搜尋時間截止不冒充供應商異常；分類失敗與未知費用仍正常警示；整輪仍為部分完成 |
| 範圍保護 | 正式檔案與特定工作紀錄唯讀核對 | 正式來源、模型／設定、股票清單、帳本該輪紀錄及未知列均未變更 |

這是降低「快到搜尋截止時仍送新請求」的風險，不保證所有未來請求都能在期限內完成，也不放寬既有取消或未知費用停止條件。

## 今晚紀錄離線重播

證據：[tonight-replay.json](tonight-replay.json)、[正式資料保護快照](formal-protection-before.json)。

1. 18 份真實成功回應以 `httpx.MockTransport` 通過原生 OpenAI 回應解析與費用處理；輸入逐筆比對原始請求，沒有新網路請求。
2. 按原讀取時間限制重播判定：第 18 筆約 69.729 秒、第 19 筆約 37.049 秒時，新規則均不派送。這是反事實邊界檢查，不撤銷今晚已送出的請求或費用。
3. 全部 2,037 筆來源版本逐筆保留；相同網址的不同內容、原文與事件沒有刪除。已有來源仍照既有日期、相關性及待辦規則處理。
4. 經原生搜尋檢查點、搜尋子期限退出、新聞刷新及分類流程離線接續：10 個模擬分類批次、30 則選定新聞完成控制流程，其餘 177 則保留待辦；搜尋仍不完整，整輪標示部分完成。
5. 分類回應是明確標記的測試替身，並非新的 AI 分析或品質背書；測試沒有補跑今晚正式任務。
6. 原始請求／成功回應雜湊、正式該輪帳務及來源列均保持一致；未知列仍為 `unknown`、實付 `NULL`、預留 US$0.288884。

## 測試

- 相關回歸：`tools/run_ai_offline_tests.py`，涵蓋新增派送測試、新聞、排程、混合路由、OpenAI 與 DeepSeek；330 通過、123 子案例通過、0 失敗。
- 新增測試：`tests/test_news_search_dispatch.py`，涵蓋門檻邊界、快取、初搜／補查、預留後再次檢查、未知費用不解除、來源與缺口、分類及排程通知。
- 完整測試：`verify.py full --task-root <owned> --result-root verification/news-dispatch-20261112/full tests`，使用專案既有隔離測試器；3,310 通過、9 略過、363 子案例通過，421.786 秒，回傳碼 0，測試期間 622 個來源檔雜湊均未變。外部 HTTP 僅允許 Mock／ASGI，見 [完整結果](full/session.json)。
- 另有 1 則既有 Telegram 套件 `asyncio.iscoroutinefunction` 棄用警告，非測試失敗；本輪未安裝或更新套件。
- 語法檢查：6 個正式程式檔及新增測試檔均通過；`git diff --check` 通過。
- 完整測試必要封存素材於 owned 目錄還原：2,531 檔、303,867,431 bytes，逐檔 SHA-256 符合原清單；未修改規則、雜湊預期或回測結果。

### 九項略過

| 測試 | 原因與本輪關係 |
|---|---|
| `test_formal_full_history_live_replay_exact_parity` | 隔離目錄未安裝正式 ETF 凍結登錄；本輪未改 ETF，其他既有回測仍執行，不冒充正式完整歷史重播驗收 |
| `test_config_json_is_valid` | 私密設定不複製到 worktree；本輪未改正式設定，已唯讀雜湊核對 |
| `test_changed_proof_stops_completion` | 下列七項為共用備份工具測試，缺本輪新建的 system-management 合成測試根目錄；本輪未改備份流程，未放寬或強行略過相關防護 |
| `test_changed_source_after_prepare_stops_completion` | 同上 |
| `test_changed_source_stops_preparation` | 同上 |
| `test_existing_logs_junction_still_rejected` | 同上 |
| `test_failed_checks_produce_no_completion` | 同上 |
| `test_new_plain_path_completes_with_original_manager` | 同上 |
| `test_project_mismatch_is_rejected` | 同上 |

完整原因見 [pytest 診斷](full/details.json)。本輪新聞派送新增測試無略過。

### 證據與回復

重播／素材還原腳本保存在 [offline-replay-helper.zip](offline-replay-helper.zip)，使用前需解出到新的本專案 owned 暫存；既有素材還原與完整測試器沿用原封存腳本。正式保護最終核對見 [formal-protection-final.json](formal-protection-final.json)。隔離修改以基準 `c4e73f7` 的 Git 版本作回復依據，不覆蓋其他任務的修改。

## 使用與限制

本輪新增實付 US$0，沒有解除任何未知預留。今晚真實分類未完成的事實不因重播而改寫。正式 Bot 仍使用原部署版本；如需套用，須另行授權合併、部署及安全重啟。本輪不以付費複驗作為結案條件。

本輪無用測試副本／合成暫存共 5,699 檔、574,412,914 bytes 已透過 Windows 原生回收驗證，狀態 `RECYCLED_VERIFIED`；不永久刪除，回收不等同立即釋放磁碟空間。原始正式資料、封存素材、必要重播腳本與上述證據均保留；計畫及收據位於本專案 `.codex-cleanup/ds/news-dispatch-1112/`。沒有接管或清理其他任務暫存。
