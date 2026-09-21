# HYBRID_MONITORING_PROTOCOL_V2（混合式監控協定 V2）

版本：`hybrid-monitoring-protocol-v2-draft`
狀態：`DRAFT_PENDING_VALIDATION（待驗證草案，不得用於正式績效）`
建立日期：2026-09-08

## 1. 目的與不可變邊界

本協定只修正混合式監控的責任分工、輸出契約、一致性驗收與可重播執行，不修改啟蒙策略 V1、V2、V3 的情境、gate、進場、加碼、停損、停利或成交規則。

既有 `HYBRID_MONITORING_PROTOCOL_V1` 及其輸入、三輪輸出、診斷與 manifest 保持唯讀。V1 一致性測試是失敗證據與 V2 校準資料，不得覆寫、刪除或重新包裝成通過結果。

本文件在 freeze manifest 建立前只是草案。任何未決 truth table、資料口徑或程式介面改變，都必須先完成測試再建立新的協定修訂版；正式一致性或全量執行開始後不得熱修。

## 2. 策略版本與監控協定版本分離

每份 ledger 與報告都必須同時標示：

- `strategy_rule_version`：`enlightenment-ai-judgement-v1/v2/v3`。
- `monitoring_protocol_version`：本協定版本。
- `objective_engine_version`、`atomic_schema_version`、`semantic_prompt_version`、`policy_reducer_version`。
- 模型、推理強度、來源、程式及全部凍結檔 SHA-256。

「監控協定 V2」不等於「啟蒙策略 V2」。策略 V1／V2／V3 原檔與雜湊若有任何變動，本協定立即失效。

## 3. 正式資料範圍與口徑

- 母體：2023-06-01～2023-12-31 上游選股清單去重後 1,029 檔。
- 模擬監控：各股票第一次入選日起至 2024-02-02，共 151,804 股票日。
- 每日仍掃描所有有效監控股票；事件日才要求 AI 更新語意，非事件日延續最近一次已鎖定狀態。
- 歷史預熱優先至少 750 根有效日 K；不足必須在資料品質中明示，需長級結構的路徑不得假裝資料完整。為了把 V2「750 根目標（200 根暖機＋500 根結構）」及「新上市股可獨立檢查新生定錨」變成可稽核規則，本協定的 operational minimum 為：`MATURE_TREND_PULLBACK／MACRO_COPY_RESONANCE／BEAR_REVERSAL_LEFT_RIGHT = 750`，`FRESH_Q1_EXPANSION = 200`。Fresh 的 200 根是工程最低門檻，不得宣稱為課程原句；達到根數也不會自動合格，仍須新生 hypothesis、唯一大小級比較窗、因果 stop 與其餘必要證據。
- 技術結構使用還原權值座標；現金損益使用原始成交價、現金股利、手續費與交易稅。
- MACD 固定為 21/55/55，只輔助切分作用／修正段，不能單獨成立定錨或交易訊號。一個完整週期的 endpoint 是最後一根同號柱，但只有第一根反號柱出現當日才能標為 `CONFIRMED（已確認）`；當日以前仍是 `FORMING（形成中）`。
- 所有 AI 與程式結構輸入只可使用 `date <= as_of` 的資料；期後 OHLC、MFE、MAE、損益、股票代號、名稱及排名在共同結構 ledger 鎖定前不可見。

### 3.1 本輪樞紐相容基線

本輪先採 `LEGACY_RADIUS_3_10_NOT_COURSE_L1_L2`：沿用既有凍結資料中的半徑 3 小級／半徑 10 大級已確認樞紐，目的是只比較監控責任重分配造成的差異。所有封包、ledger 與報告必須顯示這個限制，禁止把它宣稱為課程 L1/L2 的唯一正式定義。

課程型 `COURSE_CAUSAL_L1_L2` 只保留為未啟用的獨立實驗流。未完成候選召回率、因果時序及逐筆訊號日期差異稽核前，不得混入本輪，不得依個股或績效在兩種樞紐間切換。

## 4. 三層責任

### 4.1 程式客觀層

程式負責並鎖定：

- 日 K、MA、ATR、MACD 完成週期、公司行動及資料品質。
- 已確認樞紐的來源日、確認日、有效日、級數與價位。
- HH／HL／LL／LH、大小級 Dow 狀態、控制突破、攻擊與因果防線。
- 大、小級各唯一的工作比較窗：固定取最新兩段已完成的同向 legacy pivot legs，鎖定起訖日期、點位引用、價格、強度與波動指標。缺任一級唯一窗時，該級趨勢／波動原子題只能是 `UNKNOWN（證據不足）`。
- 左右狀態機、大小級客觀方向關係、候選 anchor／relation／stop 集合。
- 訊號事件、風險距離、監控生命週期與下一交易日成交閘門。

控制突破只能比較前一交易日已有效的控制線；當日才確認的樞紐不得在同一根日 K 又被視為已突破。

### 4.2 AI 原子語意層

AI 只能回答 manifest 列出的固定原子問題，例如：

- 定錨方向一致、乾淨、有肉、破壞性、可追溯、完成及控制性。
- 父代、修正、複製、新生定錨、長多慣性與長均線習慣關係。
- 大小級趨勢軸、波動軸及下一策略方向。
- 太極父子關係、獨立作用腿、位階空間與耗竭線索。
- 空頭末段證據、episode／campaign stop 歸屬及是否具有獨立結構。

每題只能輸出 `PASS／FAIL／UNKNOWN`，並引用封包中既有證據。AI 不得輸出 scenario、phase、route、permission、trigger、買賣、金額、部位、成交、加減碼、出場或 stop override。

### 4.3 程式衍生與政策層

程式由同一組原子答案：

1. 映射大小級象限、太極世代、位階及耗竭。
2. 完整計算四個情境的全部 gate matrix。
3. 依既有 V3 優先順序計算 `V2_CORE > NEAR_PASS_MACRO_COPY > NEAR_PASS_FRESH_Q1 > BEAR_REVERSAL_PROBE > NO_TRADE`。
4. 決定 `TRADE／WAIT／REMOVE`，再由客觀收盤事件產生下一交易日執行指令。

AI 的理由文字永遠不參與政策判斷。

## 5. 原子答案與證據語意

- `PASS`：必要正證據已存在；至少一個 supporting ref、沒有決定性反證、沒有缺證代碼。
- `FAIL`：存在明確反證；至少一個 contradicting ref。不能用「尚不知道」代替 FAIL。
- `UNKNOWN`：沒有明確反證，但必要證據不足；至少一個上下文 ref 及一個固定 missing-evidence code。
- 回答集合必須與 `required_question_manifest` 完全一致；漏題、多題、重複題、候選漏答或非法 ref 都使整份輸出無效。
- 合法但不同輪次不一致的 critical atom 保守合併為 `UNKNOWN`，不得以多數決把它升級成 PASS。

## 6. 確定性結構與情境推導

### 6.1 左右階段

左右階段由程式依事件順序推導：

- 大級空防未破、小級尚未翻多：`LL`。
- 大級空防未破、小級已建立因果向上控制：`LR`。
- 大級空防已破、其後第一個有效拉回低守住：`RL`。
- RL 後再突破已確認小級控制高：`RR`。
- 同一乾淨作用段依序穿越小／大空防且中間無可分離拉回：`DIRECT_TO_RIGHT`。
- 事實不足或事件順序衝突：`UNRESOLVED`。

AI 不得直接選 phase；`LL` 不具 V3 空頭反轉試單權限。

### 6.2 象限與太極

AI 僅回答趨勢增減及波動擴縮的固定比較題，程式映射 Q1～Q4；方向由 Dow／anchor 決定，不能從象限名稱推論多空。程式依已確認交替作用腿映射 `ANCHOR_LEG_1／COPY_LEG_3／COPY_LEG_5／LATER_GENERATION`，AI 不能自由命名世代。

### 6.3 情境互斥

程式對每個合法 hypothesis 都計算四情境，再依結構狀態區分：空頭左右反轉、父代後第一次大級複製、已建立重複成功的成熟長多、第一個破壞性新生向上定錨。若多個 hypothesis 會產生不同方向、訊號 ref、episode stop、campaign stop 或部位角色，固定標為 `MATERIAL_HYPOTHESIS_CONFLICT` 並等待；行動簽章完全相同者可按凍結優先序選顯示標籤。

## 7. V3 權限保持原規則

四情境必要 gate、V3 十項 common hard guards、兩種 NEAR_PASS 的 hard／soft gate、唯一 UNKNOWN、Fresh Q1 殘餘拆解、Bear Probe 的 LL 禁止與末段線索全部直接讀取既有策略 V2／V3 凍結規則。監控協定不得新增、刪除、放寬或改寫策略 gate。

任一必要 FAIL、未允許 UNKNOWN、兩個以上 UNKNOWN、Q3、末代耗竭、方向衝突、單一指標訊號、無因果 stop 或重大 hypothesis 衝突，都只能 `WAIT（繼續監控）`。大級 campaign 因果防線確認失效才可 `REMOVE（移出監控）`。

## 8. 每日事件與監控生命週期

事件包括首次／再次入選、新樞紐確認、MACD 週期切換、大小級控制突破、長短級區間變化、大級防線警報及停損後新獨立結構。同股同日多事件合成一個 review point。

- 小級／episode stop 失效只結束該 episode；大級 campaign 仍有效時轉 `REENTRY_WATCHING（等待再進）`。
- 再進必須有不同確認日的新樞紐或獨立控制權轉換，不能重用舊訊號。
- 大級／父代防線確認失效才 `CAMPAIGN_INVALIDATED（大結構週期失效）` 並移出。
- 移出後只有新的上游入選可以重建 campaign，且仍須重新判讀。

## 9. 成交、部位與出場

本協定不改變既有策略執行：

- `V3_FIXED（固定母單版）`：每 episode 首次成交約 10,000 元，不加碼。
- `V3_ADD2（最多加碼兩次）`：母單後最多兩份約 10,000 元加碼；必須不同日期、原持倉已有淨浮盈且是新的獨立結構，虧損不得加碼，同股同日不得重複買。
- 訊號日收盤成立，下一交易日開盤成交；開盤跌破防線或超過既定追價線則取消並保留稽核紀錄。
- +2R 前使用 episode 因果防線；曾達 +2R 後成本防線只升不降，小級失守先警戒，波段期連續兩日收盤跌破 21MA 才全數出場。
- 手續費、交易稅、現金股利與除權息依既有凍結交易引擎處理。

## 10. 不使用未來資料的正確性驗證

課程符合性與投資績效分兩階段，禁止混用：

1. 在 `as_of` 資料上驗證因果合法、課程不變量、原子答案一致性及事先凍結的人工黃金案例；此階段不能讀期後行情或身分。所有會影響 scenario、phase、方向、stop、部位角色、campaign 存續或交易權限的 34 個原子題均列為 critical atom，不得用大量非關鍵題的一致性稀釋分數。
2. 共同結構 ledger 完整鎖定並計算 SHA-256 後，才解除身分與期後價格以計算績效；後來漲跌不能回頭證明或修改當時語意。

三輪一致只證明可重複，不等於符合課程。正式通過必須同時滿足三輪一致性、100% deterministic hard course invariants、研究校準，以及獨立的盲化課程 gold holdout。

- `RESEARCH_CALIBRATION` 至少 20 案，其中至少 10 個建設性案例及 10 個負面／模糊邊界案例。每案的 machine-readable rubric 必須在人類只看 `as_of` 快照與課程內容的情況下先行複核並凍結；未定義、未複核或仍有模糊命題者只能記為 `N/A（不可評分）` 並使校準 fail-closed，不得充作通過。
- `BLINDED_HISTORICAL_COURSE_GOLD` 至少 20 案且涵蓋至少 4 個 objective-only strata。案例以固定 seed 在任何人工標註與 AI 輸出前抽定；標註者只看匿名 `as_of` 封包與課程條款，不看股票身分、舊 AI、期後行情或績效。AI 對 gold 原子 assertions 的一致率至少 90%，由其答案衍生的重大交易權限不得與 gold 衝突。

既有台半、百容、康舒等事後挑選案例只可檢查系統是否重現已明示的課程觀點，不能證明樣本外效益。正式 AI 只接收與 production 完全相同的匿名封包；案例焦點、股票身分、舊 AI 結論、期後高低點、MFE、MAE、出場及損益必須與 AI 執行面隔離。若另提供焦點問題，只能標成 `ASSISTED_COURSE_COMPREHENSION（輔助課程理解）`，不得替代正式校準分數。

正確性報告必須分層揭露，不得用後一層掩蓋前一層：`L0 CAUSAL_DATA_INTEGRITY`、`L1 DETERMINISTIC_COURSE_INVARIANT`、`L2 RESEARCH_CALIBRATION_SEMANTIC_REPRODUCTION`、`L3 BLINDED_COURSE_GOLD_HOLDOUT`、`L4 THREE_RUN_REPEATABILITY`、`L5 FORWARD_PERFORMANCE_VALIDATION`。歷史期後行情只能在完整共同結構 ledger 鎖定後用於策略績效，不能回頭改 L0～L4 的答案。

舊純 AI ledger 只能在新 ledger 鎖定後作行為差異稽核，不是答案，也不得用其已知損益調整新規則。

## 11. 一致性 holdout 與防退化門檻

V1 的 60 案只作 calibration。正式 V2 使用新的 120 案、固定 seed、與 V1 review_id 不重疊且優先 stock-disjoint 的匿名 holdout；只按截至日客觀事實分層，不讀未來績效。預定分層為：V2 Core 客觀候選 20、Macro Copy 15、Fresh Q1 15、Bear Reversal 15、Macro Defense／Remove 20、一般 Wait 邊界 35。

完全相同輸入獨立執行三輪，門檻在第一輪前凍結：

- schema、因果、禁止欄位、題目完整性及證據引用合法率 100%。
- 最終 `TRADE／WAIT／REMOVE` 三輪完全一致率至少 95%。
- 程式衍生 scenario＋phase 三輪完全一致率至少 90%。
- 所有會改變方向、stop、部位角色或 campaign 存續的 critical atoms 一致率至少 95%，逐題揭露。
- 保守合併後至少 16 筆 TRADE，且至少涵蓋兩種實際可用 V3 route；合格 TRADE 的訊號 ref、stop ref、方向與部位角色須 100% 一致。
- 至少 8 筆一致 WAIT；若樣本具有可確認移除候選，至少 8 筆一致 REMOVE。

若客觀候選實際不足，結果只能是 `INCONCLUSIVE_NO_POSITIVE_COVERAGE（正向覆蓋不足）`，不得以全 WAIT 通過，也不得在看過答案後挑選較容易案例。

## 12. 可重播執行與復原

- 每案以 canonical JSON 建立 packet hash、case key 及各輪 run-case key。
- 每案是獨立原子執行單位；合法後才 atomic publish immutable envelope。
- 三輪輸出空間完全隔離；技術失敗才可按凍結次數重試，合法答案不得因結果不理想而重抽。
- 正式 full run 以來源 manifest 決定性分成三片；worker 不得修改規則、樣本或 manifest。
- coordinator 按 `source_ordinal` 原序合併，要求 expected = valid = unique = covered、missing/duplicate/unexpected/terminal/hash mismatch 全為 0。
- 語意一致性與營運可靠性分開報告；任一失敗都不得由另一項高分抵銷。

## 13. 凍結、full run 與模型遷移

正式 freeze 前必須完成：新 schema／prompt／objective engine／policy reducer／runner／sample selector／consistency evaluator 的測試，確認策略 V1／V2／V3 保護雜湊未變，再建立 protocol、execution、source、sample 與 run manifests。

模型及推理強度是協定的一部分。模型更換必須建立新 execution version，在 frozen shadow suite 比較 critical atoms、permission、route、signal ref、stop ref、方向與部位角色；超過預先門檻不得切換，不能挑績效較好的模型答案。

正式 V2 holdout 通過後，才可對 V2 客觀引擎由 151,804 股票日重新產生並預先凍結的全部 review points 建立共同語意 ledger；ledger 完整鎖定後只先回放策略 V3。V1 的 10,476 個政策邊界案例只作行為對照，不是 V2 必須沿用的事件數。V3 達到事先門檻後，才能使用同一份 ledger 回放策略 V1／V2，不再呼叫 AI。

## 14. V3 初步可行門檻與交付

扣成本淨利須為正、Profit Factor 至少 1.20、固定版中位報酬不得明顯為負，並揭露最大回撤、尖峰資金、各路徑、Top1／Top3 集中度、扣除最大贏家、MFE／MAE與獲利回吐。加碼版須改善淨利或資金效率，不能只是放大風險。

最終交付包括 V3 總報告、固定／加碼逐筆 MD、已實現／未實現損益、交易數、勝率、PF、MFE、MAE、報酬分布、最大同時持股與份數、尖峰資金、最大回撤、四情境／V3 路徑／選股策略交叉績效、全部監控生命週期及五檔爭議股票稽核。

通過只代表本歷史區間初步可行，仍須其他互斥期間與前向影子監控。
