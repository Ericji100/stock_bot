# 台指期監控：時間錨定與固定排版正式介面

這份文件記錄正式監控新增的編排層。它不取代交易規則，也不重新定義趨勢、型態、觸發、停損、1R、單口管理、禁止條件、通知條件、Telegram 或中斷恢復規則。

正式流程：

1. 既有 heartbeat 先執行 `trade_monitor_resume_guard begin`。
2. Chrome 只擷取一次目前遠端桌面的純圖表畫面，不點擊、不懸停、不操作資訊框。
3. `trade_monitor_analysis_adapter prepare` 以實際擷取時間鎖定「最新已收盤 K」，並檢查同一 K、時間倒退與凍結畫面。
4. Codex 依原交易規則分析該張圖，只輸出結構化 JSON；不得自行撰寫最終 Markdown 的時間與標題。
5. `trade_monitor_analysis_adapter finalize` 驗證 JSON、產生唯一固定格式訊息、計算事件編號、最多呼叫一次既有 TG bridge，最後完成 resume guard。
6. Codex 對話與 Telegram 共用 finalize 回傳的同一份 canonical message。

## 追加至既有自動化提示詞的區塊

下列區塊只能追加在原提示詞末尾。追加前的原提示詞必須逐字保留。

<!-- TRADE_MONITOR_STRUCTURED_ADAPTER_V1_BEGIN -->
【正式時間錨定與固定排版介面】
本區塊只是前述既有規則的執行介面，不是新的交易策略。它不得修改、重新解讀、放寬、覆蓋或刪除本區塊之前的任何交易分析、四種型態、觸發／失效、停損、1R、單口管理、禁止條件、通知、Telegram、恢復通知、快速模式或啟動追最新規則。若本區塊與前述交易內容看似有衝突，交易語意一律以前述規則為準；本區塊只負責時間、格式與傳輸編排。

1. 每輪仍先依既有規則執行 `python -m trade_monitor_resume_guard begin --gap-seconds 180`，保留其實際 `run_id` 與 `force_notify`。不要建立虛構 run_id。
2. begin 後立即用目前已連線的 Chrome 分頁只擷取一張遠端桌面的純圖表畫面，不移動游標到 K 棒、不懸停、不點擊、不顯示資訊框，也不操作任何工具、設定、商品、週期、下單或帳務區。把 Chrome `tab.screenshot({fullPage:false})` 回傳的原始位元組以本機檔案方式保存到 `D:\code\stock_ai_bot\.runtime\trade_monitor_captures\`，並在位元組實際取得後立即記錄含時區的 ISO 8601 `captured_at`。資料夾與檔名不得包含帳務或個人財務資訊。
3. 在工作目錄 `D:\code\stock_ai_bot` 執行 `python -m trade_monitor_analysis_adapter prepare --image <本輪圖片絕對路徑> --captured-at <captured_at>`。等待並解析單行 JSON stdout。`context.expected_latest_closed_k_iso` 與 `context.expected_latest_closed_k_hhmm` 是本輪唯一權威的最新已收盤 K 時間；最終訊息中的時間不得由圖面、排程原定時間或模型自行猜測。`context.current_unclosed_k_hhmm` 只能標示為未收盤觀察。
4. `requires_analysis=true` 時，只分析同一張擷取圖，完全依本區塊之前的既有交易規則產生一次結論。分析結果必須是符合 `D:\code\stock_ai_bot\trade_monitor_analysis_schema.json` 的單一 JSON object，不得增加欄位，不得先生成另一份 Markdown。`latest_closed_k_price_estimate` 只能寫價格或價格區間與「圖面估計」標記，不得包含日期、K 棒時間、擷取時間或時區。各 details 欄位寫結論內容，不重複固定標題與免責聲明。若可見資料不足，必須如實填寫，不得杜撰。
5. JSON 的 `original_decision` 必須完全依前述既有通知規則判定，只能是 `NOTIFY` 或 `DONT_NOTIFY`；不得為了固定排版而改變通知條件。模型仍須使用本對話既有脈絡判斷型態、候選、模擬或使用者已回報的實際持倉；本介面不得自行新增、刪除或回補持倉。
6. 把第4點的完整 JSON 以 UTF-8 stdin 傳入以下命令，不能放在命令列：`python -m trade_monitor_analysis_adapter finalize --context-id <prepare回傳值> --force-notify <true或false> --run-id <begin回傳的實際run_id>`。若沒有有效 run_id，省略 `--run-id`；不得虛構。Windows PowerShell 呼叫前必須把 `[Console]::InputEncoding`、`[Console]::OutputEncoding` 與 `$OutputEncoding` 都設為 `New-Object System.Text.UTF8Encoding($false)` 等效的無 BOM UTF-8，並設定 `PYTHONUTF8=1`；不得依賴系統預設碼頁。管線必須關閉 stdin 並等待單行 JSON stdout。
7. `requires_analysis=false` 時不得再做圖面交易分析；以空 stdin 呼叫同一個 finalize 命令。finalize 會依 `SAME_BAR`、`STALE` 或 `REGRESSION` 產生安全狀態。若是 `SAME_BAR` 且 `force_notify=false`，應保持安靜，不重複長文。
8. finalize 是本模式唯一的格式與傳輸出口：它會驗證分析 JSON、以固定九欄順序建立唯一 canonical `message`、套用 `force_notify`、產生 event_id、依原條件最多呼叫一次既有 `trade_monitor_bridge`，並在有有效 run_id 時完成既有 `trade_monitor_resume_guard complete`。因此 finalize 成功或回傳明確結果後，本輪不得再直接呼叫 bridge、不得再次 complete、不得另算 event_id、不得另寫第二份分析訊息，也不得修改其 `message`。
9. Codex 對話只顯示 finalize 回傳的原樣 `message`。若 `telegram_failure=true`，只可在對話的 message 後另加既有文字「**TG傳送失敗：下一輪仍會繼續監控。**」，不可傳回 Telegram。heartbeat 的最終 decision 使用 finalize 回傳的 `decision`；heartbeat/XML 不得傳送 Telegram。若 `decision=DONT_NOTIFY`，依 heartbeat 規則只輸出一則簡短 quiet-status，不在對話外加長文。
10. prepare 或 finalize 無法執行、stdout 不是可解析 JSON、圖片遺失或分析驗證失敗時，採 fail-safe：不得沿用未驗證的方向、價位或交易訊號，不得第二次呼叫 bridge；對話回報圖表／分析不可用狀態。若有有效 run_id 但 finalize 未能完成，才依前述原規則直接執行一次 resume guard complete，`resume_delivered=false`。
11. 本介面不得讀取、輸出或記錄 Token、Chat ID、帳號、餘額、保證金、帳務持倉或其他個人財務資料。截圖只作本機本輪分析，不得上傳到額外第三方服務。所有價格與技術數值仍遵守原有圖面估計規則。
12. 本介面仍使用目前 heartbeat 回合中的 Codex 進行圖表判讀，不啟動第二個臨時 Codex 分析程序，也不變更原自動化提示詞中既有的模型判斷內容；目的在避免額外延遲並保留本對話中的交易脈絡。
<!-- TRADE_MONITOR_STRUCTURED_ADAPTER_V1_END -->

## 指令輸出契約

- `prepare`：回傳 `status`、`requires_analysis`、`context_id`、權威時間 `context` 以及上一輪的最小狀態摘要。
- `finalize`：回傳 `decision`、`original_decision`、`message`、`event_id`、TG 狀態與 resume 狀態。
- 完整分析訊息只保留最小狀態摘要；不在 adapter 狀態檔保存 Token、Chat ID 或帳務資料。
