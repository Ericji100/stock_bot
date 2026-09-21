# v2.0 正式部署狀態

- Automation ID：`1-k`
- 正式版：`enlightenment-integrated-v2.0-unified`
- 來源候選：`enlightenment-integrated-v2.0-unified-rc-shadow`
- 是否部署：**是，已反向讀回驗證**
- 正式 active version：`enlightenment-integrated-v2.0-unified`
- Automation 狀態：`ACTIVE`
- RRULE：`FREQ=MINUTELY;INTERVAL=1`，未變
- target task：`01a05aa6-d062-7df3-88dd-8e3023be05f1`，未變
- 通知政策：`null`，未變
- 監控：已於 `2026-09-02T22:44:16.9607759+08:00` 啟動

## 驗證結果

- execution prompt SHA-256：`e1407c4ad1ec3770d64dcbe439760f82d938d08919c21f14955cd2edbd7c8ee0`
- analysis prompt SHA-256：`6506bf6ed8ff990e86db691c4d109d15be34678b4a53f62d4ac8f03f90d3e67b`
- schema SHA-256：`50bce9344459eb20a36a771a4e8f48beb89dcf7a45c09f042b86c4912f7bc564`
- scheduler config SHA-256：`41cc3960813de82bab7d8ab4be6a9013a2a5e34f774426fbab133d32f2667f52`
- Automation TOML SHA-256：`dcbf0c09efb047450868dcf7d88383a1b32a0068d1ecc94f144430d496e373cc`
- active-version SHA-256：`410c608df74852796b5b11affbf972cdd9780977bb5f9f6665fc07701acedd9b`
- 測試：`214 passed`
- prompt 檔尾：Automation 儲存時移除一個尾端 LF；版本檔已正規化成實際讀回位元組，語義未變且雜湊完全一致。
- 本機排程：單一 daemon 執行中；PAUSED 驗證未新增圖表擷取，未分析、未發送。
- 啟動後第一輪：22:42:05 擷取、最新已收盤 K 為 22:41，總耗時 97.451 秒，`NOTIFY`；Telegram 已送出，恢復通知已完成。
- 尚未完成多日即時前向 Shadow；使用者已明確接受此限制並要求正式升級。這不代表獲利能力已驗證。

本次以正式 Automation 更新功能切換同一 Automation `1-k`；沒有直接修改 `automation.toml`，也沒有建立第二個排程。
