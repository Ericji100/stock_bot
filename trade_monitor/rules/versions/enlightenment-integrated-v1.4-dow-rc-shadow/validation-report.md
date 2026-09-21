# v1.4-dow-rc-shadow 驗證報告

驗證時間：2026-09-02（Asia/Taipei）
結論：**RC 技術門檻通過，可建立不可變正式候選版；正式切換仍須將 Automation 執行層與本機分析設定成組部署。**

## 自動測試

- `python -m pytest tests/trade_monitor -q`：105 passed。
- 版本 prompt SHA-256 驗證：通過。
- 版本目錄 `sha256.txt`：全部通過。
- v2 payload 相容、v3 schema、固定九欄、adapter 原子寫入、損壞 fail-safe、狀態轉移、排程設定預設關閉等測試均包含在上述集合。

## n=2／n=3 因果回放

資料：2026 年 8 月台指期全日盤 22,791 根 1 分 K；只使用當時以前資料。

| 指標 | n=2 | n=3 |
|---|---:|---:|
| 局部確認樞紐 | 5,101 | 3,680 |
| 每千根 K 樞紐數 | 223.816 | 161.467 |
| 中位確認延遲 | 2 根 | 3 根 |
| 二級樞紐 | 722 | 587 |

- 共同局部樞紐 3,680；n=2 獨有 1,421；n=3 獨有 0；Jaccard 0.7214。
- 嚴格左右條件下，n=3 是 n=2 的子集合。n=2 快一根且較靈敏，因此正式候選維持 n=2；但必須接受較高結構密度，並由二級樞紐、正式防線、號盤、象限與原四型態過濾。
- Type 1 結構代理的誤警率很高，不能當成勝率或進場訊號；正式規則維持 Type 1 只警告、不反手。

## 結構化輸出修正

- 第一個隔離試跑被 Codex structured output 拒絕，原因是 v3 schema 的 `market_structure_state.version` 只有 `const`、缺少明確 `type`。
- 已改為 `{"type":"integer","const":1}`，重新驗證後所有測試與隔離試跑通過。
- 修正後 schema SHA-256：`12b5c54df45aee539ea640e594f1958fc06509a8a2b2f20b898164c61cae3b43`。

## 五輪連續 Chrome＋Codex 影子測試

隔離 runtime：`.runtime/trade_monitor/dow_rc_shadow_sequence`
Telegram：停用設定＋`dry-run`；沒有正式傳送。
正式 production runtime、constitution state、analysis state 與 outbox：未修改。

| 最新已收盤 K | 啟動偏差 | 分析耗時 | 樞紐狀態 |
|---|---:|---:|---|
| 12:18 | +0.001 秒 | 47.004 秒 | 新增一級低點，局部成立 |
| 12:19 | +0.000 秒 | 38.663 秒 | 保留同一樞紐 |
| 12:20 | +0.000 秒 | 39.570 秒 | 保留同一樞紐 |
| 12:21 | +0.000 秒 | 37.873 秒 | 保留同一樞紐 |
| 12:22 | +0.000 秒 | 43.335 秒 | 保留同一樞紐 |

- 平均分析耗時約 41.289 秒；全部在下一分鐘以前完成。
- 五輪均為合法 v3 JSON，finalize 成功，`as_of` 每輪前進一分鐘。
- 樞紐 ID 五輪固定為 `P1L-20260902T115700+0800`。
- `bar_time` 固定 11:57；`first_seen_at` 與 `locally_confirmed_at` 固定 12:18，沒有事後回填或重寫。
- 配對樞紐不足，因此大小正式防線與二級樞紐保持空值；未為通過測試而硬湊防線。
- dry-run 的 resume 通知不會標記為已送達，因此 scheduler 外層 decision 可能被升級為 NOTIFY；這是隔離測試語意，不代表正式 TG 重複發送。

## 剩餘限制

- 純截圖只能在 K 棒邊界、連續性與最右側時間錨清楚時反推樞紐時間；否則必須保持未定。
- 圖面價格仍是估計；無可靠價軸時 `price_estimate` 必須為 `null`。
- 回放只驗證因果性、密度、延遲與結構代理，不證明交易策略可獲利。
- 正式部署必須同步切換 local scheduler 的分析 prompt/schema/狀態開關與 Automation 的 dual-mode 自足 prompt，不能只改其中一邊。
