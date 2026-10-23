# 0050 趨勢策略：背景工作啟停修正

## 問題與修正

舊整合在 Telegram `post_init`、主 Application 尚未 running 時呼叫
`Application.create_task`，因此出現 PTBUserWarning。任務仍會執行，但不在
PTB 的自動 stop 等待集合；原本也沒有 ETF 專屬的關閉清理 hook。

本修正明確保有 ETF 自己的 startup 與 scheduled background task 集合：

- `post_init` 以 asyncio 建立並持有 ETF 背景任務，不阻塞原 Bot 啟動。
- 任務完成時移除引用、取回例外；失敗僅關閉 ETF 並遮蔽錯誤細節。
- `post_stop` 先阻止新 ETF 工作及移除 ETF 三個排程，再取消並等待 ETF 任務，
  最長等待5秒；不取消原 Bot 的任務、handler 或排程。
- `post_shutdown` 是啟動失敗、Application 未曾 start 的清理保險，重複呼叫安全。
- 尚未發送時取消，outbox 留 RETRY；已開始發送時取消，留 DELIVERY_UNCERTAIN，
  必須人工確認，不自動重送。
- 啟動成功會明確印出「背景準備完成，監控排程已就緒」。
- 原 Bot heartbeat 的 `schedule_health.etf_monitor` 另列 ETF 的
  PENDING／READY／STOPPED 及三個排程時間，不能再只靠 Bot 心跳判斷 ETF 就緒。

正常關閉可清理 asyncio 背景工作；作業系統強制結束或直接關閉視窗無法保證執行 hook，
仍由既有 SQLite 重啟恢復處理。已在執行的同步網路執行緒不能被 Python 強殺，
可能完成當次原子匯入；取消後的啟動 coroutine 不會再進入群組發送或 READY。
這不是「所有執行緒均可立即終止」的承諾。

## 不變範圍

策略核心、買賣規則、持倉與公司行動計算不變；不重算或改寫歷史研究。
只有同一個 Application、bot token、polling 與原 BAT 啟動鏈；原群組設定不變。
0050 唯一目標仍為 `-5527055845`；排程仍為台北時間12:45、13:55、22:00。
本機已啟用 shadow 的設定保留，隔離工作樹的 Git 預設設定仍為停用。
不更動或中斷02的背景程序、來源、輸入或產物。

## 驗收證據

本機完整稽核、測試 XML、原檔雜湊與實際重新啟動檢查保存於：
`.workdata/reports/dual-ma/qa/0050-task-lifecycle-qa00-20261023/`。
本文件本身不宣稱尚未發生的22:00實際排程已成功；晚間驗收另行核對。
