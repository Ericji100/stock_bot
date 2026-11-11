# 0050 影子觀察結案紀錄（2026-11-11）

目前狀態：修補與部署完成，下一完整交易日驗收待完成。監控維持 `enabled=true`、`mode=shadow`，12:45／13:55／22:00 不變。

## 已驗證

- 正式監控自 10/23 起，至 11/11 共經過 12 個交易日，11 個已完整結束。原庫 41 則通知皆有 DELIVERED 回執，未出現真實 BUY／SELL。這不代表已驗證實際交易獲利或實際成交。
- 11/9 約 21:26 停機，隔日 09:34 恢復，原程式只處理已存在的失敗 job，因此漏掉完全未建立的 22:00 job。新恢復檢查可找出這類缺口。
- 正式庫一致性研究副本先驗證 11/9 完整官方收盤 97.45、HOLD 與當日正式判讀相符；較新策略狀態不變，未向 Telegram 發送測試。
- 11/11 16:32 部署後自動補核 11/9，事件 `etf-0050-09c144cfbcb1ea9d1e33a72f50e8d93c`，16:32:35 收到 Telegram 回執 `10498`，attempts=1。既有 outbox 全欄檢查與較新策略狀態相同，SQLite quick_check=ok，schema=7；正式／晚間無待恢復日期。10/27 的盤中資料失敗紀錄保留，未事後補造盤中報價。
- 受影響測試 289 passed、1 skipped。跳過的是需環境變數的完整歷史測試，另以本機 frozen 資料實跑 2006-01-02 至 2026-09-30 共 5,093 日、15,279 個 phase job，結果 `PASS_EXACT`、183 筆訊號、差異數 0，Telegram 嘗試數 0。策略模型補帳結果與原 accepted artifacts 一致。
- 新案例涵蓋缺 job 的跨日恢復、歷史交易待覆核、歷史逾時後重試、schema 6 升級保留原紀錄、開盤前／後送達及未知時間、人工確認、重試訊息與更正失效、唯讀驗收和一次性驗收通知。
- 新 Bot 心跳 READY、JobQueue 正常，既有 watchdog 已回報新 PID 健康。沒有實際 BUY／SELL，相關邊界以隔離測試驗證。

## 有限驗收與結案條件

官方交易日 API 已確認 2026-11-12 為開市日。此次驗收接入原恢復 loop，不增加 OS 或 Codex 排程；Production request 為 `runtime/etf_monitor/shadow-closeout.json`，設定 SHA-256 為 `002bbda4c5513a32044e686c8435e7aa4684392a233836c4d82486d2408e1593`。

11/12 的 12:45、13:55、22:00 全部準時成功、三時段通知均送達、完整官方日資料通過且正式判讀一致或有正式更正、既有晚間缺口全清、無待處理送達、設定維持核准 shadow，才可通過。最早於 22:21 後的五分鐘恢復檢查完成判定，通常約 22:25。通過或待覆核都保存證據並向原 0050 群組通知一次，結束此次驗收；日常監控持續執行。待覆核不能宣告結案。

不重新要求 5–10 日、不等待自然出現 BUY／SELL。正式交易建議模式仍待使用者另行決定。

## 證據與回復

本機必要機器證據保存於 `.workdata/qa/etf-shadow-closeout-20261111/`：`tests.xml`、`full-history-replay.json`、`deployment-before.json`、`deployment-after.json`、`target-calendar.json`、`backup-acceptance.json`、`runtime-ready.json`、`git-publication.json`。未來實盤驗收結果保存於 `.workdata/prod/runtime/etf_monitor/shadow-closeout-acceptance.json`，對應通知回執保存在 request 與 SQLite outbox。

冷備份前已停止 Bot 寫入，WAL checkpoint=(0,0,0)，schema 6 資料庫 SHA-256 `e543c0cbeefcfd09848222bcd092f371cc5b3500a68999946440dc4255111f85`。備份管理器回傳 `SCOPE_ROOT_TOO_BROAD_OR_MISSING`；保留未納管的必要回復副本 `archive/codex-backups/0050-closeout-20261111-schema6-unmanaged/0050-final-v1.7.sqlite3`，雜湊一致且 quick_check=ok。尚待完整交易日驗收，保留此副本，不自動回收。不修改中央政策或偽造納管收據。

程式回復基準為修改前 main `cec3afc`。若須回退 schema 7，先停寫並保存部署後新事件／回執，再評估還原冷備份與程式；不可直接覆蓋而遺失新紀錄。其他任務的設定、研究與暫存未納入此次提交。

## 暫存收尾與流程例外

停止本次測試／回放寫入者、保存證據並核對引用後，本次 owned 暫存的 2,187 個檔案、84,237,483 bytes 已由 `ITransferSource.RecycleItem` 回收，收據 `RECYCLED_VERIFIED`，原路徑已不存在。計畫及收據保留在 `.codex-cleanup/01a0c332-c7b4-7273-afc7-c51e7c2a52d4/close1111/`；必要冷備份及其他任務資料保留。

流程例外：既有 `replay.py` 第 294 行使用 `TemporaryDirectory`，成功退出會直接清除內部測試庫。本次 Agent 沒有預先攔住，其成功回放的內部臨時庫已由該工具自行移除，未遵守本次要求的回收方式；以上回收數量不包含它。正式庫、冷備份及 parity 結果證據仍完整。未修改此既有 helper；下次使用前須先處理其自動清理行為，不能宣稱本次所有中間檔都經回收。
