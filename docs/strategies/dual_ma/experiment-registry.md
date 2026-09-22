# 雙均線策略實驗登錄

狀態值：`BASELINE`、`BACKLOG`、`READY`、`RUNNING`、`ACCEPTED`、`REJECTED`、`SUPERSEDED`。

| 實驗 ID | 主題 | 版本／組別 | 狀態 | 說明 |
|---|---|---|---|---|
| `ENTRY-001` | 收復快線進場 | `ENTRY_RECLAIM_ONLY` | `BASELINE` | 第一個完整日 K 收盤重新站上快線；第一輪以訊號日收盤價作成交代理。中期使用 5/21；長期使用 21/144。 |
| `ENTRY-002` | 收復且確認一個樞紐後進場 | `ENTRY_RECLAIM_PIVOT1_CONFIRMED` | `BACKLOG` | 參數只保留一組，與基準組比較；第一版不實作。 |
| `EXIT-001` | 道氏防線移動停利 | `EXIT_DOW_TRAIL` | `READY` | 日線 n=2 因果樞紐形成多方道氏防線；各腿有效停損只升不降。 |
| `EXIT-002` | 2R 保本加道氏防線 | `EXIT_2R_DOW_TRAIL` | `READY` | 每腿曾達 2R 後啟動含成本的保本防線，再依日線道氏防線上移。 |
| `EXIT-003` | 2R 保本加 21MA 雙收盤 | `EXIT_2R_21MA_2CLOSE` | `READY` | 波段總淨浮盈曾達總初始 R 的 2 倍後，連續兩日收盤低於 21MA 全數退出該策略。 |
| `EXIT-004` | ATR 移動停利 | `EXIT_ATR_TRAIL` | `BACKLOG` | 以持倉後最高價或最高收盤價減去 ATR 倍數形成移動防線；倍數與價格口徑後續討論。 |
| `EXIT-005` | N 日低點停利 | `EXIT_N_DAY_LOW` | `BACKLOG` | 收盤跌破前 N 個交易日低點或最低收盤價時退出；N 與 low／close 口徑後續討論。 |
| `EXIT-006` | 均線加結構混合停利 | `EXIT_MA_STRUCTURE_HYBRID` | `BACKLOG` | 跌破指定均線先警戒，再以結構防線失守確認退出；指定均線與確認順序後續討論。 |
| `EXIT-007` | 分批停利 | `EXIT_PARTIAL_SCALE_OUT` | `BACKLOG` | 達到指定 R 或結構條件時先退出部分腿，其餘部位續用移動防線；分批比例與剩餘部位管理後續討論。 |
| `EXIT-008` | 大小級道氏防線出場 | `EXIT_DOW_L1_L2_SCALE` | `BACKLOG` | 後續比較課程型 L1／L2 因果樞紐防線；候選方向為中期使用 L1 小級防線、長期使用 L2 大級防線，小級防線跌破可先警戒而不自動宣告大級失效。L1／L2 配對、確認、退出及是否分批的精確規則另行凍結，不以任意放大 n 取代級數定義。 |
| `STOP-001` | 進場拉回低點停損 | `STOP_ENTRY_PULLBACK_LOW` | `BASELINE` | 第一版結構停損。 |
| `STOP-002` | 慢線結構完全型態停損 | `STOP_SLOW_MA_STRUCTURE_ONLY` | `BACKLOG` | 中期進場後等待 21MA 拉回／收復形成結構；長期對應 144MA。慢線結構形成前的風險缺口尚未解決。 |
| `SIZE-001` | 固定名目投入 | `SIZE_FIXED_NOTIONAL_10000` | `BASELINE` | 每單位約 10,000 元，使用零股。 |
| `SIZE-002` | 固定風險投入 | `SIZE_FIXED_RISK` | `BACKLOG` | 股數依進場價與停損距離決定，另需處理收盤確認、缺口與零股成交。 |
| `ADD-001` | 總部位達 1R 後加碼 | `ADD_R1_AGGREGATE` | `READY` | 新訊號成立，且所有開放腿淨浮盈至少等於其初始 R 總和；最多三次。 |
| `ADD-002` | 不設損益門檻加碼 | `ADD_SIGNAL_ONLY` | `READY` | 新的獨立訊號成立即可加碼，允許虧損中新增部位，作為研究對照；最多三次。 |
| `FILTER-001` | 慢線斜率過濾 | `FILTER_SLOW_MA_SLOPE` | `BACKLOG` | 比較是否能降低橫盤假訊號。 |
| `REV-001` | Q2／均線轉換反轉 | `Q2_TRANSITION_REVERSAL` | `BACKLOG` | 獨立策略，不混入趨勢基準。 |
| `REV-002` | 左右戰法加 MACD 背離 | `MACD_DIVERGENCE_LEFT_RIGHT_REVERSAL` | `BACKLOG` | 參考既有機器人 MACD 背離候選，另案研究。 |

## 實驗最低要求

每次正式回測都必須產生唯一 `run_id`，並記錄：

- `strategy_version`
- `spec_sha256`
- `git_commit`
- `universe_id`、`data_snapshot_id`
- `data_start`、`data_end` 與 `data_hash`
- 矩陣、進場、出場、停損、加碼、部位、成交代理、成本模型及滑價設定 ID
- 是否使用還原價格及公司行動處理方式
- 交易明細、彙總指標、例外與資料缺口

不得依據同一測試期間的績效反覆修改股票名單或規則，再把結果當成樣本外績效。
