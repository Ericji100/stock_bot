# Chrome 雙視角監控 RC

## 目的

本 RC 保留每分鐘第 05 秒的本機 DETAIL 擷取與既有 v1.5 交易規則，額外由 Automation 使用目前已連線的 Chrome 分頁，每 15 分鐘建立一次 OVERVIEW 全局摘要。它不使用 Computer Use、Windows UI Automation、SendInput 或 PowerShell 控制滑鼠鍵盤。

## 視角責任

| 視角 | 圖表大小 | 判斷責任 |
|---|---:|---|
| DETAIL | 約 180 根 K | 最新已收盤 K、5～20 分鐘當前趨勢、候選階段、觸發、進場、結構停損、R 與一口單管理 |
| OVERVIEW | 約 296 根 K | 大趨勢、波段階段、象限、大級道氏防線、所在位置與級數一致性背景 |

OVERVIEW 不得直接觸發進場、建立模擬倉或估算最新成交區。兩個視角不一致時，OVERVIEW 只決定背景，DETAIL 只決定時機；級數不一致時降級為觀望或條件式。

## 時序

```text
每分鐘 :05
  → coordinator 確認 DETAIL 可用
  → 本機擷取 DETAIL
  → Codex 分析／finalize／TG／outbox

每 15 分鐘且 DETAIL 已完成
  → coordinator plan
  → 若有 ARMED、確認、持倉或風險事件，延後最多 2 分鐘
  → acquire OVERVIEW lease
  → 同一個 Chrome 控制呼叫：180 → 296 → 保留 OVERVIEW 圖 → 180 → 最右側 → 恢復確認圖
  → 先恢復，再分析已保留的 OVERVIEW 圖
  → 畫面確認恢復後 complete-overview
  → 下一輪 DETAIL 只讀取 20 分鐘內的 FRESH 摘要
```

OVERVIEW 縮放、擷取與恢復必須放在同一個 Chrome 控制呼叫中，不可停在 296 等待 Codex 判讀。影子實測中這可把圖表離開 DETAIL 的時間縮到約 1～2 秒。OVERVIEW 租約存在時，本機 DETAIL 最多等待 3 秒；仍未恢復就 fail-safe。租約逾時、狀態損壞或恢復未確認時，狀態轉成 `RECOVERY_REQUIRED`，不得在未知縮放下繼續產生交易訊號。

OVERVIEW 失敗時使用同 owner 的 `abort-overview` 記錄結果；畫面已確認回到 180 才能傳 `--restored-detail true`。`restore-detail` 只供初始視角確認或租約逾時後的可見復原，不能越過仍有效的其他 owner 租約。

## Coordinator CLI

所有時間都必須是含時區 ISO 8601。以下只示意參數；正式 heartbeat 應使用本輪實際時間及唯一 owner id。

```powershell
python -m trade_monitor.dual_scale --state .runtime/trade_monitor/dual_scale_state.json `
  restore-detail --owner chrome-initial-check --now 2026-09-02T15:00:00+08:00 --detail-bars 180

python -m trade_monitor.dual_scale --state .runtime/trade_monitor/dual_scale_state.json `
  plan --now 2026-09-02T15:15:10+08:00

python -m trade_monitor.dual_scale --state .runtime/trade_monitor/dual_scale_state.json `
  acquire-overview --owner overview-20260902-1515 --now 2026-09-02T15:15:11+08:00
```

Chrome 已恢復 DETAIL 後，將下列完整 JSON 以 UTF-8 stdin 交給 `complete-overview`：

```json
{
  "latest_closed_bar_time": "2026-09-02T15:14:00+08:00",
  "captured_at": "2026-09-02T15:15:15+08:00",
  "session_key": "2026-09-02-NIGHT",
  "large_trend": "資料不足",
  "wave_stage": "資料不足",
  "quadrant": "資料不足",
  "position": "全局圖可見資料不足，暫不建立可靠位置",
  "large_defense_context": [],
  "key_zones": [],
  "source": "CHROME_CONTROL_OVERVIEW"
}
```

```powershell
python -m trade_monitor.dual_scale --state .runtime/trade_monitor/dual_scale_state.json `
  complete-overview --owner overview-20260902-1515 --now 2026-09-02T15:15:18+08:00 `
  --restored-detail true --detail-bars 180 --overview-bars 296
```

## 安全邊界

- 只使用目前已連線的 Chrome 分頁。
- 只操作圖表下方的大小數字與水平位置。
- 不把游標移入 K 棒，不開啟或追讀資料框。
- 不操作商品、週期、指標、工具、設定、下單或帳務區。
- 不保存帳號、餘額、保證金、Token、Chat ID 或實際損益。
- OVERVIEW 不發 TG、不另產生使用者訊息；正式訊息仍只由 finalize 建立一次。

## 部署狀態

`enlightenment-integrated-v1.6-dual-scale-rc` 目前只供影子驗證。正式 `trade_monitor/local_scheduler_config.json`、Automation `1-k`、`active-version.json` 與 Telegram 均不得因 RC 測試而切換。

RC 驗證完成後另建立 `enlightenment-integrated-v1.6-dual-scale` 正式版本；正式部署只指向該版本，不直接修改或啟用 RC 版本檔。
