# HYBRID_MONITORING_PROTOCOL_V2 可重播執行草案

版本：`hybrid-monitoring-execution-v2-draft`
狀態：`DRAFT_NOT_FROZEN_NOT_AUTHORIZED_FOR_PERFORMANCE`
建立日期：2026-09-07

## 1. 目的與不變邊界

本草案只定義新的監控「執行與驗收機制」，不修改以下內容：

- 啟蒙策略 V1、V2、V3 的情境、gate、進場、加碼、停損、停利及成交規則。
- `HYBRID_MONITORING_PROTOCOL_V1` 及其所有 frozen 文件、程式、輸入、輸出與 manifest。
- 2023H2 的 1,029 檔母體、151,804 個每日監控股票日及未來績效遮蔽原則。V1 的 10,476 個政策邊界案例只作行為對照，不是 V2 正式事件母體。

新機制必須使用新的版本號、檔名與輸出目錄，例如 `hybrid_monitoring_v2`；不得覆寫或接續 V1 ledger。報表必須同時標示「策略規則版本」與「監控執行協定版本」，避免把策略 V2 與監控協定 V2 混為一談。

## 2. V1 已觀察到的問題

V1 三輪均完成 60 個匿名案例，schema／因果／證據合法率為 100%，但正式一致性未通過：

- 最終權限一致率 93.33%，低於 95% 門檻。
- 主要情境＋左右階段一致率 53.33%，低於 90% 門檻。
- 保守合併後 60 案全為 `WAIT/NO_TRADE`，沒有驗證到任何正向交易路徑。
- 4 個權限不一致案例中，3 個是可選的重大替代情境有時出現、有時未出現，直接改變 reducer 結果。
- V1 以三案一批執行；一案逾時或非法引用會使整批重試。Run 1／2／3 各有 2／6／3 次嘗試錯誤。
- 三輪成功批次平均耗時約 294／479／529 秒，最長成功批次約 796 秒；三輪成功批次時間合計已超過約 7 小時，尚未計入失敗嘗試。

因此，V2 必須同時解決兩件不同的事：語意輸出要更穩定；執行系統要能在逾時、驗證拒絕與 agent 中斷後精確復原。兩者不得用同一個指標混在一起判斷。

## 3. 凍結順序

正式測試前依序建立並鎖定：

1. `semantic_protocol_version`：原子問題、證據規則、schema、提示詞、程式導出欄位與 reducer。
2. `execution_protocol_version`：模型、推理強度、Codex CLI 版本、單案逾時、最大嘗試數、固定退避表、worker 數、shard 數與合併程式。
3. `source_manifest`：來源 JSONL、原始列數、原始順序及 SHA-256。
4. `sample_manifest`：校準排除集、正式 holdout、預留擴充區塊、固定 seed、分層與所有案例雜湊。
5. `run_manifest`：本輪使用上述每個 manifest 的雜湊及唯一 `execution_id`。

任何一項改變都建立新版本與新輸出根目錄，不能在既有正式 run 中熱修。策略規則檔須以 SHA-256 納入 manifest，但仍保持原檔不動。

## 4. 單案例原子執行

### 4.1 案例身分

每個來源案例先固定：

- `source_ordinal`：來源 JSONL 中從 0 起算的位置。
- `review_id` 與 `anonymous_stock_id`。
- `packet_sha256`：該案例 canonical JSON 的 SHA-256。
- `case_key`：對 `source_manifest_sha256 + source_ordinal + review_id + packet_sha256 + protocol_sha256 + prompt_sha256 + schema_sha256 + model + reasoning_effort` 計算 SHA-256。
- `run_case_key`：對 `case_key + run_number` 計算 SHA-256。三輪輸入完全相同，但身分及輸出空間互斥。

canonical JSON 必須凍結成 UTF-8、欄位排序、禁止 NaN、固定小數表示的單一規範。未來只用雜湊判斷是否為同一輸入，不能以檔名或行數推測。

### 4.2 原子狀態

每案只有以下狀態：

`PENDING（待執行） → RUNNING（執行中） → VALID（已驗證）`

失敗時只可轉成：

`RETRYABLE_ERROR（可重試錯誤） → RUNNING`，或嘗試上限後 `TERMINAL_ERROR（終止錯誤）`。

AI 回覆先寫入該案的唯一暫存目錄；通過 JSON 解析、schema、review_id、證據引用、日期因果、禁止欄位與原子 gate 完整性檢查後，才以 atomic rename 發布為一個 immutable case envelope。未通過驗證的回覆只留在 audit，不可進入正式 ledger。

case envelope 至少保存：

- 全部輸入與凍結檔 SHA-256。
- model、reasoning effort、CLI 版本與 run number。
- attempt 編號、開始／結束時間、elapsed seconds 與結果狀態。
- 原始 AI 結構化輸出及 canonical output SHA-256。
- 驗證器版本、驗證結果及程式導出的 scenario／phase／route／permission 預覽。

每案一個正式 envelope，禁止多個 worker 共同 append 同一份 JSONL。共享 ledger 只能由 coordinator 在全部案例完成後建立。

## 5. 三輪一致性隔離

- Run 1、Run 2、Run 3 使用相同匿名輸入、模型、推理強度、提示詞、schema、reducer 與逾時設定。
- 三輪分別使用 `consistency/run_1/cases`、`run_2/cases`、`run_3/cases`，不得讀取其他輪 AI 輸出、錯誤、permission 或摘要。
- 三輪可以同時由三個 agent 執行；平行只縮短牆鐘時間，不改變案例內容與合併規則。
- 同一輪恢復時可以重用該輪已驗證的 `VALID` envelope；不得把 Run 1 的結果複製成 Run 2／3。
- 三輪完成前不產生合併語意、不揭露股票身分、不讀取未來價格或績效。
- coordinator 只負責凍結驗證、分派、覆蓋率檢查與最終合併；worker／子 agent 不得修改規則、樣本或全域 manifest。

## 6. 決定性分片與 agent 分工

### 6.1 分片規則

正式 full run 預設三個 shard，數量必須在 execution manifest 中凍結。案例分配公式為：

`shard_id = uint64(SHA256(source_manifest_sha256 + "|" + review_id) 的前 16 個 hex) mod shard_count`

每個 shard manifest 保存成員的 `source_ordinal`、`review_id`、`case_key`、packet SHA-256，並依 `source_ordinal` 排序。執行前必須證明：

- shard 交集為空。
- shard 聯集恰好等於來源全部案例。
- 無重複 review_id、無未知 review_id、無缺漏。
- 每個 shard manifest 與總 assignment manifest 的 SHA-256 相符。

若 worker 中斷，只能把「原 shard 中尚未 VALID 的案例」交給其他 agent；不得重新計算 shard，也不得改變輸入順序。所有權轉移寫入 recovery audit。

### 6.2 一致性與全量的分工

- 一致性測試：三個 agent 各自執行完整的一輪，天然形成三輪隔離；每個 agent 內部仍以單案為原子單位。
- 10,476 案例 full run：三個 agent 各執行一個 deterministic shard；root/coordinator 不直接生成 AI 語意，只做監控、復原與合併。
- agent 只寫自己被分配的輸出目錄。任何規則或程式修改皆由 root 在 run 開始前完成，重新凍結後才可分派。

agent 數減少只會延長時間，不得改變 shard membership；剩餘 agent 可依序接手多個既有 shard。agent 數增加也不得在正式 run 中重分片。

## 7. 逾時、重試與復原

- 每案最多三次嘗試；timeout 與退避表在 execution manifest 中固定，建議先以 V1 最長成功批次 796 秒為依據，pilot 後再凍結單案 timeout，不直接沿用未驗證數值。
- 重試必須使用完全相同 prompt、schema、packet、model 與 reasoning effort；不得縮短輸入、換模型、降低推理、移除 gate 或手工補值。
- 退避使用預先固定的 deterministic schedule；不得依 AI 回覆內容調參。
- timeout、CLI 非零結束、JSON 解析失敗、schema 拒絕、證據引用非法及 ID 不符都寫入 append-only attempt audit。
- 逾時後 coordinator 只終止本案建立的 subprocess tree；其他案例與 shard 不受影響。
- 第三次仍失敗時標記 `TERMINAL_ERROR`，整輪不得合併成 complete，也不得以 `UNKNOWN` 偽裝成成功。
- 重啟時掃描 envelope，而不是相信進度計數器。只有 manifest 全符且 envelope 驗證通過的 `VALID` 案例可跳過；殘缺暫存檔一律隔離後重跑。

## 8. 原序合併與完整性驗證

合併只有 coordinator 可執行，且流程固定：

1. 重新驗證 protocol、execution、source、sample、assignment 及各 run/shard manifest。
2. 讀取所有 case envelope，逐筆重算 packet、output、validator 與 policy preview SHA-256。
3. 驗證每個 `source_ordinal` 恰有一個 VALID 結果，review_id 與來源完全相符。
4. 依 `source_ordinal` 排序；worker 完成時間與 shard 編號不得影響正式 ledger 順序。
5. 寫入暫存 merged JSONL，重新逐行解析並檢查列數、唯一性、source exact coverage 與 canonical SHA-256。
6. 全部通過才 atomic rename 成正式 ledger，並建立只讀 merge manifest。

完整合併的硬條件為：`expected = valid = unique = covered`、missing = 0、duplicate = 0、unexpected = 0、terminal = 0、hash mismatch = 0。任一條不滿足，performance replay 一律鎖住。

三輪語意合併仍採安全原則：各原子欄位三輪完全一致才保留；不一致降為 `UNKNOWN`，再由同一 frozen 程式導出 scenario、phase、route 與 permission。不得直接多數決出 `PASS`，也不得相信 AI 自報的交易權限。

## 9. Holdout sample 與正向路徑覆蓋

V1 的 60 案已用於找出協定缺陷，只能作 calibration，不可再充當 V2 正式驗收樣本。V2 應建立不重疊的匿名 holdout：

- 至少排除 V1 的 60 個 review_id；建議再排除其 anonymous_stock_id，形成較嚴格的 stock-disjoint holdout。
- 只使用截至 as_of 的客觀事件、均線區間、候選防線、已確認樞紐與完整性標記分層；不得使用未來報酬、MFE、排名或交易績效。
- 固定 seed、分層配額、抽樣程式與候補區塊先寫入 manifest，再開啟案例內容。
- 初始建議 120 案：V2_CORE 客觀候選 20、MACRO_COPY 客觀候選 15、FRESH_Q1 客觀候選 15、BEAR_REVERSAL 客觀候選 15、MACRO_DEFENSE／REMOVE 候選 20、一般政策邊界／WAIT 對照 35。這些只是 pre-AI 客觀 proxy，不預先指定正確答案。
- 為避免看到結果後挑案例，另在同一 manifest 預先抽好固定順序的 reserve blocks。若初始樣本缺乏正向路徑，只能依預先順序整塊加入；不得挑選「容易通過」的個案。

若客觀 proxy 尚無法可靠建立，正向 challenge set 可參考舊純 AI 的「訊號日期」分層，但 sampling 程式與 reviewer 必須完全隔離，且只能讀訊號欄位、不能讀績效。此組只能驗證正向路徑重複性，不能取代客觀 holdout，也不能把舊純 AI 當標準答案。

為避免「全部 WAIT 也通過」，正式一致性除原門檻外應加入 anti-degenerate coverage：至少 10 個案例三輪一致導出 `TRADE`，且涵蓋至少兩種 V3 route；不足時結果為 `INCONCLUSIVE_NO_POSITIVE_COVERAGE（正向覆蓋不足）`，不得解封 full run。確切案例數與 route 規則須在看結果前凍結。

## 10. 語意一致性與營運可靠性分開驗收

### 10.1 語意一致性

建議保留或提高以下預先凍結門檻：

- schema、因果聲明、禁止欄位與證據引用合法率：100%。
- 最終 `TRADE／WAIT／REMOVE` 權限三輪完全一致率：至少 95%。
- 程式導出的主要 scenario＋phase 完全一致率：至少 90%。
- 正向 challenge cases 的 permission＋route 完全一致率：至少 90%。
- 所有會改變方向、停損來源、部位角色或 campaign 存續的 critical atomic gates，逐欄揭露一致率及 UNKNOWN 率。
- anti-degenerate 正向覆蓋及 route 覆蓋符合第 9 節。

語意不一致不得用重試洗掉；只要輸出已合法，該次就是一個有效獨立判讀。重試只處理技術失敗，不處理「答案不喜歡」。

### 10.2 營運可靠性

獨立統計：

- 首次嘗試成功率、最終成功率。
- timeout、CLI error、validation rejection、terminal error 率。
- p50／p95／p99 耗時、每小時 throughput、各 shard 不均衡度。
- 恢復後重做案例數、重複執行但未發布的 attempt 數。
- manifest／hash／coverage 完整率。

硬條件應至少為：最終合法輸出 100%、terminal = 0、coverage = 100%、所有 hash 驗證 100%。首次成功率及 SLA 門檻應先以 100 案 non-holdout pilot 量測，再在正式一致性前凍結；不得在正式結果出來後放寬。

營運失敗代表系統不能穩定每日運行；語意失敗代表判讀不一致。任一失敗都不得用另一項的高分抵銷。

## 11. 正式 V2 全量 review points 安全並行流程

只有 V2 正式一致性通過後才執行：

1. 從 1,029 檔、151,804 股票日重新執行 V2 objective-only 每日掃描及 lossless lazy 邊界，另建 V2 review-point manifest；不沿用 V1 的 10,476 個事件日期或語意輸出。
2. review-point manifest 鎖定 `expected_v2_review_points`、來源順序與每案 packet hash 後，以第 6 節公式產生三個 shard 與 assignment manifest；每片實際數量只由該凍結來源與雜湊分配決定。
3. 先用與正式 full 相同設定跑 100 案 operational pilot，只驗證時間與失敗率，不看股票身分或績效；若要改 timeout／worker 數，改版並重新凍結。
4. 三個 agent 同時執行各自 shard，每案獨立暫存、驗證、發布與 audit。
5. 中斷時只接續未 VALID 案例；禁止從頭覆寫已發布結果。
6. 全片完成後由 coordinator 原序合併，產生完整 semantic ledger 及完整性 manifest。
7. ledger 鎖定後才由 frozen reducer／交易引擎回放 V3，並解封股票身分與未來績效。
8. V3 初步可行性通過後，再以同一份已鎖定共同語意 ledger 重播 V1／V2；不得重新叫 AI 形成另一套語意。
9. 最後才執行 behavioral-equivalence audit，比較舊純 AI 與新機制的訊號日、route、scenario、phase、stop、成交與出場差異。舊純 AI 是參考，不是答案，也不能回饋調整本輪。

## 12. 預期加速與限制

在目前最多三個子 agent 加 root coordinator 的條件下：

- 三輪一致性可由三個 agent 各跑一輪；理想牆鐘時間從三輪相加降為最慢一輪，依 V1 成功批次時間約有 2～3 倍加速空間。
- full run 可分成三個固定 shard；理論上接近 3 倍，實務上較合理先估 2～2.7 倍，因同帳號／同主機可能受模型併發、服務限流、CLI 啟動與長尾案例影響。
- 單案例原子化提升的是復原精度，不保證單次推理更快；它可能增加 CLI 啟動開銷。正式 ETA 必須用 100 案 pilot 的實測 p50／p95 推算，不能直接把 V1 三案批次時間除以三。
- V1 每三案成功批次平均約 294～529 秒；若沿用原吞吐，10,476 案即使三片並行仍可能是數日工作。子 agent 可顯著縮短等待與避免整批重跑，但不能把高推理成本消失。

## 13. 主要風險與防護

| 風險 | 防護 |
|---|---|
| agent 各自改規則造成標準不同 | 規則與執行檔先凍結；worker 僅讀，root 唯一可整合 |
| 平行完成順序改變 ledger | 只依 `source_ordinal` 合併 |
| 同案被兩個 agent 重複發布 | deterministic ownership；case envelope atomic rename；重複視為完整性失敗 |
| timeout 後混入半份輸出 | 每案唯一暫存目錄；合法後才發布 |
| 重試被用來挑有利答案 | 只有技術失敗可重試；合法答案不得重抽 |
| 全部 WAIT 造成虛假一致 | 正向路徑 challenge、anti-degenerate 門檻及 reserve blocks 預先凍結 |
| 換模型後判讀漂移 | 模型與推理強度納入 case key；換模型等同新協定，重跑一致性與行為等價稽核 |
| rate limit 使三 agent 更慢 | pilot 實測後凍結 worker 數；不在正式 run 中臨時增減分片 |
| V2 執行機制改變原策略行為 | 策略檔雜湊鎖定；ledger 後做逐筆 signal／stop／entry／exit 行為等價稽核 |
| 舊純 AI 或未來績效污染新判讀 | reviewer 永遠只讀匿名截至日資料；舊結果只在新 ledger 鎖定後由 audit 程式讀取 |

## 14. 解封條件

只有下列條件同時成立，才可開始 V3 full performance：

1. 新 protocol／schema／prompt／policy／execution manifest 全部鎖定且驗證通過。
2. 全新 holdout 的三輪語意一致性通過，並有足夠正向與 route 覆蓋。
3. 三輪營運可靠性通過，沒有遺漏、重複、終止或 hash mismatch。
4. 正式 full run 的 worker/shard 設定已由 pilot 決定並凍結。

full ledger 亦須達成 `expected_v2_review_points／expected_v2_review_points` 完整、原序、唯一、可重算及雜湊一致，才可開啟交易回放。任何人工補判、換模型、改 prompt、縮短封包或跳過案例，都必須另建版本，不能稱為同一輪正式結果。
