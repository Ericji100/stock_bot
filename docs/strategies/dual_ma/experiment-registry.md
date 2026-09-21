# 雙均線策略實驗登錄

狀態值：`BASELINE`、`BACKLOG`、`READY`、`RUNNING`、`ACCEPTED`、`REJECTED`、`SUPERSEDED`。

| 實驗 ID | 主題 | 版本／組別 | 狀態 | 說明 |
|---|---|---|---|---|
| `ENTRY-001` | 收復快線進場 | `ENTRY_RECLAIM_ONLY` | `BASELINE` | 第一個完整日 K 收盤重新站上快線，下一交易日開盤成交。中期使用 5/21；長期使用 21/144。 |
| `ENTRY-002` | 收復且確認一個樞紐後進場 | `ENTRY_RECLAIM_PIVOT1_CONFIRMED` | `BACKLOG` | 參數只保留一組，與基準組比較；第一版不實作。 |
| `EXIT-001` | 純趨勢退出 | `EXIT_A_TREND_ONLY` | `READY` | 不先鎖利，持有至快線不再高於慢線或結構停損。 |
| `EXIT-002` | 2R 後抬高保護 | `EXIT_B_2R_BREAKEVEN` | `READY` | 浮盈達 2R 後把最低保護提高至損益兩平加成本；細節須凍結。 |
| `STOP-001` | 進場拉回低點停損 | `STOP_ENTRY_PULLBACK_LOW` | `BASELINE` | 第一版結構停損。 |
| `STOP-002` | 慢線結構完全型態停損 | `STOP_SLOW_MA_STRUCTURE_ONLY` | `BACKLOG` | 中期進場後等待 21MA 拉回／收復形成結構；長期對應 144MA。慢線結構形成前的風險缺口尚未解決。 |
| `SIZE-001` | 固定名目投入 | `SIZE_FIXED_NOTIONAL_10000` | `BASELINE` | 每單位約 10,000 元，使用零股。 |
| `SIZE-002` | 固定風險投入 | `SIZE_FIXED_RISK` | `BACKLOG` | 股數依進場價與停損距離決定，另需處理收盤確認、缺口與零股成交。 |
| `ADD-001` | 只在獲利後加碼 | `ADD_PROFIT_ONLY_MAX3` | `BASELINE` | 需要獨立的新拉回／收復訊號，最多三次；獲利拉開門檻待凍結。 |
| `FILTER-001` | 慢線斜率過濾 | `FILTER_SLOW_MA_SLOPE` | `BACKLOG` | 比較是否能降低橫盤假訊號。 |
| `REV-001` | Q2／均線轉換反轉 | `Q2_TRANSITION_REVERSAL` | `BACKLOG` | 獨立策略，不混入趨勢基準。 |
| `REV-002` | 左右戰法加 MACD 背離 | `MACD_DIVERGENCE_LEFT_RIGHT_REVERSAL` | `BACKLOG` | 參考既有機器人 MACD 背離候選，另案研究。 |

## 實驗最低要求

每次正式回測都必須產生唯一 `run_id`，並記錄：

- `strategy_version`
- `git_commit`
- `universe_id`
- `data_start`、`data_end` 與資料雜湊
- 進場、出場、停損、加碼、部位與成本模型 ID
- 是否使用還原價格及公司行動處理方式
- 交易明細、彙總指標、例外與資料缺口

不得依據同一測試期間的績效反覆修改股票名單或規則，再把結果當成樣本外績效。
