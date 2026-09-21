# v1.9 X 流程 RC 回放與隔離 Shadow 驗證

驗證時間：2026-09-02（Asia/Taipei）

## 結論

- RC：`rc_shadow_validated_requires_forward_observation`
- 正式 active version：`enlightenment-integrated-v1.8-cclass`
- Automation `1-k`：`PAUSED`
- 正式 scheduler config SHA-256：`c6f7925518ad13611694e62f12b96cd09f61cad6f0fbfae65359f3a6d8dfbcea`
- Automation TOML SHA-256：`882e6f1d514cf4caa7ff665fbe407c2d9ac3a06bd3926c54722d6106ce53b4fd`
- RC transport：隔離 runtime、假 ACTIVE 驗證檔、Telegram `dry_run`
- 正式 Automation、scheduler、runtime、outbox、TG 與 active-version：未修改

## 完整性

- X 是同一流程控制器，不是第五種型態：開盤證據 → 第一次 DH／DL 樣本 → 已確認腳數選主鏡頭 → 家族 DNA／共振 → 原四型態 → 質性 A／B／C → 應有行為／時間失效。
- 來源紀律：只有權威現貨資料才可用 `VERIFIED_CASH`；期貨缺口必須標 `FUTURES_PROXY`；沒有來源就 `UNAVAILABLE`。
- 主鏡頭：少於兩腳只用開盤證據，2～3 腳太極優先，4 腳以上四象限優先；一之只能在動能因果確認後 override。
- A／B／C：A=高勝率證據×高賠率證據，B=高×中，C=中×高；不輸出百分比，不繞過原型態收盤觸發與結構停損。
- 風險：沒有多口、攤平、放寬停損或無資料的委託簿推論。

## 2026/8 逐時因果回放

來源：`C:\Users\紀成達\Downloads\tmf_chart_b7d18f803ee54d838cc3194ef647a8bb.html`

- 原始 22,791 根 1 分 K；本 X 開盤流程報告納入 20 個日盤、5,991 根日盤 K。
- 20 個日盤的第一次 DH／DL 均保存首次突破及最多兩根後首次可分類時間；不使用盤後最終高低回填。
- HTML 沒有現貨前收／開盤，因此 cash gap 全部維持 `UNAVAILABLE`；只另列明確的期貨代理診斷。

| 指標 | n=2 | n=3 |
|---|---:|---:|
| 因果已知 legs | 809 | 664 |
| 完整五腳序列 proxy | 155 | 125 |
| 第一次端點可分類 sessions | 20 | 20 |
| TRUE_LIKE／FALSE_LIKE | 8／12 | 8／12 |
| OPENING／TAIJI／QUADRANT 主鏡頭 bars | 371／301／5,319 | 477／301／5,213 |

n=3 比 n=2 少 145 個已確認 legs，且開盤證據主鏡頭等待更久；本報告沒有足夠證據取代現行 n=2。這些數字是流程密度，不是交易機會、勝率或獲利能力。

完整證據：

- `trade_monitor/reports/xprocess-v19-n2-n3-2026-08.md`
- `trade_monitor/reports/xprocess-v19-n2-n3-2026-08.json`

## 隔離 Shadow

首輪 RC 找到既有 C 班 `UNDEFINED` 時間戳約束未在新執行提示說清楚；contract 正確 fail-safe，不採用分析、不寫入 state v5、TG 仍 dry-run。修正提示與 X 結構建立階段相容性後，使用全新隔離 runtime 完成三輪：

| 權威已收盤 K | 分析秒數 | 總秒數 | schema／state | Telegram |
|---|---:|---:|---|---|
| 20:30 | 70.061 | 70.642 | v7／v5 accepted | dry_run |
| 20:31 | 71.955 | 72.535 | v7／v5 transition accepted | dry_run |
| 20:33 | 98.270 | 98.849 | v7／v5 transition accepted | dry_run |

- 平均 Codex 分析 80.095 秒；範圍 70.061～98.270 秒。
- 三輪都保存合法 state v5，X 第一次事件未回填或重寫。
- 圖面日期／時間與 runtime 權威時間不一致，因此模型正確保持資料不足、X 流程未定、setup NONE；沒有用舊圖硬猜方向或價位。
- `dual_scale_context=UNAVAILABLE`，沒有被當成有效全局證據。

## 測試

- schema v7／contract v7、state v4→v5 記憶中遷移、第一次端點不可回填、主鏡頭腳數門檻、A／B／C 兩軸語意、原四型態映射與九欄不變：通過。
- local scheduler／adapter 支援 state v5；正式 v4 相容性仍通過。
- `python -m pytest tests/trade_monitor -q`：**197 passed**。

## 限制與正式部署門檻

- 完整分析平均仍超過一分鐘，無法保證逐根完成；不得宣稱交易所等級即時。
- 回放不是獲利回測；沒有模擬完整四型態、成交、成本、滑價、停損、出場與 R。
- 三輪 Shadow 因圖面時間不一致而保持空 X 狀態，尚未驗證真實非空開盤／端點／主鏡頭／A-B-C 事件的前向生命週期。
- 需至少五個完整交易日前向 Shadow。完成前不更新 Automation、正式 scheduler config、active-version 或 Telegram。
