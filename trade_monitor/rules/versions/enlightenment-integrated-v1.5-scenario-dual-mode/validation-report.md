# v1.5 部署驗證報告

- 驗證時間：2026-09-02 14:15（Asia/Taipei）
- Automation：`1-k`
- 部署前後狀態：`PAUSED → PAUSED`
- RRULE：`FREQ=MINUTELY;INTERVAL=1`（未變）
- target task：`01a05aa6-d062-7df3-88dd-8e3023be05f1`（未變）
- 執行 prompt SHA-256：`9db29f522f50cf8cb0c15f3c88324c890cc6ff1c31d5621854bbf80e1d3be139`
- 分析 prompt SHA-256：`33f1405aecdd0ed49fa78131aea8f2b1c1a69ebb09b00da10062e09072d37ffa`
- v4 schema SHA-256：`9985f15c2ce426d73210a84bd334aca6d46804cc5caeccb3ceae4c9dc74c31d8`
- scheduler config SHA-256：`a12cb456833d51d80bf734778ff404bee2c85263ab5b04536d63c42ed49a7617`

## 驗證結果

- v1 market structure 可因果遷移至 v2，不杜撰另一方向防線。
- 小／大級多空四向防線可同時保存；ACTIVE 防線不可無事件消失。
- 已配對樞紐不可在同一時段無故消失。
- setup 階段、波段階段、位置、級數一致性與點位區變動可強制完整通知。
- v4 合法範例通過 JSON schema 並渲染原九欄，內部欄位不產生第十欄。
- 本機排程候選 config dry-run 回報 `paused`，未擷取、未分析、未傳送 Telegram。
- 專用 Windows daemon 重啟後只有一個 Python scheduler 程序；Automation 仍為 PAUSED。
- `python -m py_compile` 通過。
- `tests/trade_monitor` 加上 encoding health 共 118 tests passed。

本報告驗證規則、狀態轉移、格式與部署一致性，不構成策略獲利能力或實盤績效證明。
