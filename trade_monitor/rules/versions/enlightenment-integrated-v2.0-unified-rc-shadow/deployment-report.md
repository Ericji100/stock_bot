# v2.0 候選部署狀態

- Automation ID：`1-k`
- 候選版：`enlightenment-integrated-v2.0-unified-rc-shadow`
- 是否部署：**否**
- 正式 active version：`enlightenment-integrated-v1.9-xprocess`
- Automation 狀態：`PAUSED`
- 正式提示詞、RRULE、target task 與通知政策：未變更
- 監控：未啟動

正式部署前仍需隔離前向 Shadow，且必須依版本庫流程成組切換 analysis prompt、schema、scheduler config 與 Automation execution prompt；不得直接修改 `automation.toml`。
