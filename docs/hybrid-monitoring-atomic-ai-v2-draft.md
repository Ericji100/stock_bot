# HYBRID_MONITORING_PROTOCOL_V2：AI 原子語意判讀草案

版本：`hybrid-monitoring-atomic-ai-v2-draft`
狀態：`DRAFT_ONLY（僅供設計審查，尚未凍結、不得用於正式績效）`
建立日期：2026-09-07

## 1. 定位與不可變邊界

本草案只設計新的「混合監控執行協定」，不修改任何交易策略內容。下列規則仍是獨立、唯讀的策略來源：

- `enlightenment-ai-judgement-v1`
- `enlightenment-ai-judgement-v2`
- `enlightenment-ai-judgement-v3`

尤其不得因本草案而改寫 V2 四情境的定義、V3 的路徑優先序、允許的 UNKNOWN、母單／加碼、成交、停損、停利或監控生命週期。正式產物必須同時顯示兩個版本欄位：

```text
strategy_rule_version = enlightenment-ai-judgement-v1 | v2 | v3
monitoring_protocol_version = hybrid-monitoring-protocol-v2
```

因此，「V2」若出現在 `HYBRID_MONITORING_PROTOCOL_V2`，指的是執行協定版本，不是 `enlightenment-ai-judgement-v2` 策略被改版。

本草案的目標是把會影響交易權限的自由文字分類拆成固定問題，使模型只判讀仍無法可靠程式化的結構語意；主要情境、左右階段、V3 路徑與最終 `TRADE／WAIT／REMOVE` 全由凍結程式重算。

## 2. V1 失敗所要求的直接修正

V1 三輪 60 筆一致性結果為：schema／因果／證據 100%，交易權限 93.33%，主要情境＋左右階段 53.33%。問題不在 JSON 是否合法，而在輸出契約把下列政策關鍵欄位交給模型自由生成：

1. `primary_scenario` 三輪完全一致率只有 60%。
2. `stage_location` 與 `taiji.generation` 都只有 35%。
3. `scales.relationship` 只有 51.67%，大小級象限也只有 56.67%／61.67%。
4. 四個情境只輸出「模型當輪選中的情境」之 gates；情境改變時，其餘情境的證據消失，程式無法在同一事實面重算。
5. `alternative_scenarios` 是可填可不填，卻可阻擋交易。4 個權限漂移案例中，至少 3 個主要情境、左右階段及必要 gates 並未改變，只因某輪有填重大替代情境而由 `V2_CORE` 改成等待。
6. 事件分層樣本未保證正向路徑覆蓋，三輪保守合併後 60 筆全為等待；即使形式一致，也無法證明「該交易時能穩定交易」。

V2 必須逐項消除以上自由度，而不是降低驗收門檻或把不一致全部變成等待後宣稱通過。

## 3. 新責任邊界

| 工作 | 程式 | AI |
|---|---|---|
| 截至日裁切、還原權值、指標、樞紐確認日 | 唯一負責 | 不得更動 |
| 大小級樞紐序列、HH/HL/LL/LH、道氏防線候選 | 唯一負責 | 只能引用 |
| 收盤是否突破防線、是否完成 retest、訊號日期 | 唯一負責 | 不得宣告觸發 |
| 定錨候選是否乾淨、有肉、具破壞性、可追溯 | 提供固定候選與量化事實 | 逐候選回答固定原子問題 |
| 父代、修正、複製關係是否成立 | 列出候選關係 | 回答固定關係問題 |
| 趨勢性／波動率相對前段增加或降低 | 提供分段量化資料 | 對固定比較題作 PASS/FAIL/UNKNOWN |
| 象限、太極 phase／generation、左右階段 | 依原子答案與事件狀態機推導 | 不直接輸出標籤 |
| 四情境 gate matrix | 依同一組原子答案完整產生 | 不選主要情境、不漏填其他情境 |
| 替代假說與重大衝突 | 列舉所有可成立組合並比較行動簽章 | 不提供可選式 alternative 陣列 |
| V1／V2／V3 交易路徑、權限、部位與出場 | 唯一負責 | 禁止輸出買賣建議 |

所有自由說明只能放在 `non_policy_note`，不得參與 gate、路徑、權限或雜湊後的政策重算。

## 4. 輸入封包契約

每一個 `review_id` 是可獨立執行、重試、驗證及合併的最小單位。不得再把三個股票日綁成一個失敗單位。

每包固定包含：

1. 匿名股票識別、`as_of`、協定／schema／prompt hash。
2. 截至 `as_of` 的可見日 K 與程式計算欄位，不含名稱、代號、未來資料、報酬、MFE、排名。
3. 已確認的大、小級樞紐，且 `confirmation_date <= as_of`。
4. 程式產生的定錨候選 `anchor_candidate_id`；候選含固定起訖 ref、方向、級數、狀態與量化品質事實。AI 不得新增端點。
5. 程式產生的關係候選 `relation_candidate_id`，例如父代→修正、修正→複製、小級反向定錨。
6. 大小級道氏序列、防線 ref、突破／守住事件與確認日。
7. 分段趨勢性、波動率、ATR、斜率、效率、內部樞紐、反向 K、影線、量價與 MACD 輔助事實。
8. 可選的 episode stop 候選；每個候選皆有來源 ref、確認日、價位與距離。AI 只能判斷是否屬於目前 episode，不能另造防線。
9. 上一個已鎖定語意狀態與其 hash。只有新事件可要求更新；沒有新事件則沿用，不重問 AI。

輸入候選生成器、級數定義、樞紐參數、分段方式與量化欄位都必須版本化。若其中任何一項改變，必須建立新協定版本並重跑一致性與行為等價驗收。

## 5. 原子回答的唯一格式

每個原子問題都使用相同物件：

```json
{
  "question_id": "ANCHOR_CLEAN",
  "subject_ref": "ANCHOR_CANDIDATE:A-001",
  "result": "PASS | FAIL | UNKNOWN",
  "supporting_evidence_refs": ["..."],
  "contradicting_evidence_refs": ["..."],
  "missing_evidence_codes": ["..."],
  "reason_code": "固定列舉值",
  "non_policy_note": "可選繁體中文說明"
}
```

固定規則：

- `PASS`：必要正證據已出現在封包，至少一個 supporting ref，沒有足以推翻的 contradicting ref，`missing_evidence_codes` 必須為空。
- `FAIL`：有明確反證或定義中的必要事實為假，至少一個 contradicting ref；「不屬於此候選」應填 FAIL，不得使用 NOT_APPLICABLE。
- `UNKNOWN`：沒有決定性反證，但必要證據尚缺；至少一個可核對的上下文 ref 及一個固定 missing code。不得把「看起來較像」填成 PASS。
- 不允許 `NOT_APPLICABLE`、信心分數、機率或自由新增結果值。情境不適用時，必然有一項前提原子題為 FAIL。
- 所有 ref 必須存在於當包、日期不晚於 `as_of`。AI 新造日期、價格、候選、ref 或防線時整包拒收。
- `reason_code` 與 missing code 使用 schema 固定 enum；自由文字永遠不參與政策。
- 回答集合必須與 `required_question_manifest` 完全相等：不得少題、多題、重複題或只回答自己偏好的情境。

## 6. 固定原子問題目錄

正式 schema 應依每個輸入候選展開固定 manifest。以下是 V2 草案的最小問題集；問題文字、適用 subject、結果語意與證據最低要求在凍結後不得由模型自行改寫。

### 6.1 定錨候選（每一個 anchor candidate 都必答）

| question_id | 固定問題 |
|---|---|
| `ANCHOR_DIRECTION_COHERENT` | 候選起點到目前／完成端點的主要作用方向，是否與候選方向一致，而非穿頭破底的混合段？ |
| `ANCHOR_CLEAN` | 反向 K、影線、內部樞紐與來回重疊是否足以支持「乾淨」？ |
| `ANCHOR_MEATY` | 幅度、ATR 倍數、歷時與效率是否足以構成可操作作用段，而非雜訊？ |
| `ANCHOR_DESTRUCTIVE` | 是否實際破壞封包列出的同級區間或因果道氏防線？ |
| `ANCHOR_TRACEABLE` | 起點、作用方向、被破壞對象、目前失效是否都能由 refs 追溯？ |
| `ANCHOR_COMPLETED` | 截至日是否已有可確認完成端點；形成中不得因未來資料補成完成？ |
| `ANCHOR_CONTROLS_CURRENT_CONTEXT` | 此候選是否仍控制目前大級／小級背景，而非已被失效或取代？ |

### 6.2 父代、修正、複製與長多（每一個 relation candidate 都必答）

| question_id | 固定問題 |
|---|---|
| `REL_PARENT_TO_CORRECTION` | 後段是否可被視為指定父代之修正，而非新反向朝代？ |
| `REL_CORRECTION_PRESERVES_PARENT` | 修正是否仍守住父代起點與當時有效的大級多頭防線？ |
| `REL_CURRENT_LEG_IS_REPLICATION` | 當前上行是否為指定修正後的獨立同向複製，而非父代未完成的同一段？ |
| `REL_CURRENT_LEG_IS_FRESH_ANCHOR` | 當前上行是否為背景中第一個破壞性新錨，而非成熟長多或既有父代複製？ |
| `REL_SMALL_UP_REANCHOR_VALID` | 小級由空轉多是否形成可追溯反向定錨，而非單根紅 K／均線／MACD？ |
| `REL_FIRST_SHALLOW_CORRECTION` | 是否為新錨／Q1 延伸後第一次良性淺修正，而非多代末段？ |
| `LONG_CAMPAIGN_REPEATED_SUCCESS` | 大級向上慣性是否已有至少一次成功推進＋修正／複製或足夠時間的 HH+HL 證明？ |
| `LONG_MA_HABIT_SUPPORTED` | 105MA、144MA 或該股慣用均線的歷史方向與反覆支撐是否支持長多；今日單次站上不得 PASS？ |

### 6.3 動態象限與大小級策略方向

AI 不直接回答 Q1/Q2/Q3/Q4；只回答相鄰二至三段的固定比較題，程式依 `(trend_delta, volatility_delta)` 映射象限。

| question_id | 固定問題 |
|---|---|
| `LARGE_TREND_STRENGTH_INCREASED` | 大級目前段的方向效率／延續性是否較比較段增加？ |
| `LARGE_VOLATILITY_EXPANDED` | 大級目前段的有效波動是否較比較段增加？ |
| `SMALL_TREND_STRENGTH_INCREASED` | 小級目前段的方向效率／延續性是否較比較段增加？ |
| `SMALL_VOLATILITY_EXPANDED` | 小級目前段的有效波動是否較比較段增加？ |
| `LARGE_NEXT_UP_DIRECTION_SUPPORTED` | 在大級防線與目前作用／修正關係下，下一可交易方向向上是否有結構支持？ |
| `SMALL_NEXT_UP_DIRECTION_SUPPORTED` | 小級目前控制權是否支持下一可交易方向向上？ |

若趨勢或波動比較為 UNKNOWN，對應象限必須 `UNRESOLVED`，不得由均線位置代填。程式由大小級「下一方向」答案計算 `DIRECTION_RESONANCE／STRATEGY_RESONANCE／CONFLICT／UNRESOLVED`，AI 不再輸出 relationship。

### 6.4 太極、位階與耗竭

| question_id | 固定問題 |
|---|---|
| `TAIJI_RELATION_TRACEABLE` | 指定父代、修正、複製關係是否都有可追溯端點與因果次序？ |
| `TAIJI_CURRENT_LEG_INDEPENDENT` | 當前攻擊是否由不同日期的新修正與新發動構成獨立一代？ |
| `LOCATION_REMAINING_SPACE_ADEQUATE` | 至鄰近大級壓力／失效所形成的剩餘空間，是否仍支持「早期、有空間」？ |
| `LOCATION_NOT_EXTENDED_FROM_ORIGIN` | 距新錨起點、相對 ATR 與同向攻擊序號是否仍未明顯延伸？ |
| `EXH_ATTACK_SHORTENING` | 相對前一同向攻擊，幅度／歷時是否明顯縮短？ |
| `EXH_SLOPE_DECAY` | 相對前一同向攻擊，斜率／效率是否明顯衰退？ |
| `EXH_PRICE_VOLUME_DIVERGENCE` | 是否存在可核對的價量背離或末端量價失衡？ |
| `EXH_FAILED_CONTINUATION` | 是否已發生創高／創低後無法延續的結構失敗？ |
| `EXH_TIME_SPACE_EXHAUSTION` | 累積時間、ATR 空間、攻擊次數與鄰近壓力是否構成明確耗竭？ |

程式只計算已確認獨立作用腿數與同向攻擊序號，並映射 `ANCHOR_LEG_1／COPY_LEG_3／COPY_LEG_5／LATER_GENERATION`。未完成的腿不得計數。是否「弱化／耗竭」由上述固定原子證據依凍結 truth table 組合，不能再由 AI 自由選 `stage_location`。

建議 truth table（正式採用前須以課程校準案例審查，不得看績效調整）：

- `EARLY`：第一作用腿或第一代複製早段；`LOCATION_NOT_EXTENDED_FROM_ORIGIN=PASS`，且沒有任何明確耗竭原子為 PASS。
- `MIDDLE`：不是 EARLY，世代尚未達 COPY_LEG_5／LATER，且無明確耗竭。
- `LATE`：COPY_LEG_5／LATER，或第三次以上同向攻擊已有任一弱化原子 PASS。
- `EXHAUSTED`：`EXH_TIME_SPACE_EXHAUSTION=PASS`、`LOCATION_REMAINING_SPACE_ADEQUATE=FAIL`，或後代同時有兩項以上弱化原子 PASS。
- 證據不足或互相矛盾為 `UNRESOLVED`。

### 6.5 左右、停損與訊號品質

| question_id | 固定問題 |
|---|---|
| `BEAR_ATTACK_IS_LATE_STAGE` | 大級空頭攻擊是否已有完整末段證據，而非仍健康延伸的初期空頭？ |
| `BEAR_LATE_STAGE_PARTIAL` | 是否至少有一項可量化、方向正確但尚不足完成末段 gate 的線索？ |
| `STOP_BELONGS_TO_CURRENT_EPISODE` | 指定 stop 候選是否為目前小級／部位作用段的因果失效，而非遠端父代低點？ |
| `STOP_BELONGS_TO_PARENT_CAMPAIGN` | 指定大級 stop 是否為父代／campaign 的因果失效？ |
| `SIGNAL_HAS_INDEPENDENT_STRUCTURE` | 訊號是否同時包含獨立樞紐／道氏／定錨結構，而非只有均線、MACD、量、新高或單 K？ |

左右階段由程式狀態機產生，不再詢問 AI：

```text
large_bear_defense 未破 + small_bull_control 未成立 -> LL
large_bear_defense 未破 + small_bull_control 已成立 -> LR
large_bear_defense 已破 + 其後第一次有效拉回守住 -> RL
RL 完成 + 其後再突破確認小高 -> RR
同一乾淨作用段先後穿越 small 與 large defense，且沒有可分離 retest -> DIRECT_TO_RIGHT
其餘 -> UNRESOLVED
```

「乾淨作用段」必須引用已通過的 anchor quality 原子；程式不得只因同日同時突破兩條線就判 `DIRECT_TO_RIGHT`。

`BEAR_LATE_STAGE_EVIDENCE` 的三值映射固定為：

- `PASS`：`BEAR_ATTACK_IS_LATE_STAGE=PASS` 且至少一項末段原子有完整正證據。
- `UNKNOWN`：沒有早期空頭反證，`BEAR_LATE_STAGE_PARTIAL=PASS`，但完整末段證明不足。
- `FAIL`：健康的早期空頭延伸已有明確反證，或資料充分時所有末段線索皆 FAIL。

## 7. 程式推導的客觀狀態

以下狀態不得再交給模型自由輸出：

1. 大、小級 `dow_state`：由截至日已確認樞紐的 HH+HL／LL+LH 推導。
2. 大、小級 `dow_defense_ref`：只有攻擊已造成同級創高／創低後，才回指發動樞紐。
3. 收盤突破、低不破低／高過高、retest 守住及確認日期。
4. 左右 phase：依第 6.5 節狀態機。
5. 四象限：由固定分段與四個 trend／volatility 原子結果映射。
6. 太極 generation：由已確認、可追溯且獨立的作用腿計數。
7. 同向攻擊次數、是否第三次以上、是否第五段／更後代。
8. 觸發日期、下一交易日、訊號收盤、stop 距離百分比與 ATR 距離。
9. `single_indicator_only`：若沒有任何定錨／樞紐／道氏的獨立結構證據即為 true。
10. 監控資格、campaign／episode、停損後再進、同日去重及部位角色。

若程式所需的上游事實不足，狀態只能 `UNRESOLVED`；不得要求 AI 猜一個標籤填滿欄位。

## 8. 四情境完整 gate matrix

每次審核都必須從相同原子答案產出下列完整矩陣。AI 不再選情境，也不能只輸出一條路徑。

### 8.1 `MATURE_TREND_PULLBACK`

| gate | 固定來源 |
|---|---|
| `ACTIVE_LARGE_UPTREND` | 有控制中的大級 UP anchor，anchor 核心品質與 campaign stop 合法，且大級多頭防線未失效 |
| `LONG_TREND_PERSISTENCE` | `LONG_CAMPAIGN_REPEATED_SUCCESS` |
| `LONG_MA_HABIT` | `LONG_MA_HABIT_SUPPORTED`；不得由今日站線代替 |
| `CORRECTION_WITHIN_CAMPAIGN` | `REL_PARENT_TO_CORRECTION` + `REL_CORRECTION_PRESERVES_PARENT` |
| `TAIJI_GENERATION_MAPPED` | `TAIJI_RELATION_TRACEABLE` + 程式 generation 非 UNRESOLVED |
| `DYNAMIC_QUADRANTS_SUPPORT` | 程式象限非 Q3，且大、小級下一方向沒有衝突 |
| `SMALL_UP_CONTROL_CAUSAL` | `REL_SMALL_UP_REANCHOR_VALID` + 已完成因果小級翻多／突破事件 |
| `EPISODE_STOP_CAUSAL` | `STOP_BELONGS_TO_CURRENT_EPISODE` + stop 客觀欄位合法 |

### 8.2 `MACRO_COPY_RESONANCE`

| gate | 固定來源 |
|---|---|
| `COMPLETED_PARENT_ANCHOR` | UP parent 的 anchor 核心品質均 PASS 且 `ANCHOR_COMPLETED=PASS` |
| `CORRECTION_INTACT` | `REL_PARENT_TO_CORRECTION` + `REL_CORRECTION_PRESERVES_PARENT` |
| `TAIJI_GENERATION_MAPPED` | `REL_CURRENT_LEG_IS_REPLICATION` + `TAIJI_RELATION_TRACEABLE` + generation 非 UNRESOLVED |
| `CORRECTION_BEAR_DOW_LINE_CAUSAL` | 程式已建立修正段空頭防線，且其確認日在突破日前 |
| `SMALL_UP_REANCHOR_BREAK` | `REL_SMALL_UP_REANCHOR_VALID` + 收盤完成突破／等價低不破低高過高 |
| `DUAL_SCALE_LONG_ALIGNMENT` | 大、小級下一方向皆支持 UP，或大級允許、小級 UP 的策略共振 |
| `NOT_Q3_OR_EXHAUSTED` | 程式象限非 Q3，且 stage 非 EXHAUSTED、無複製轉修正 |
| `EPISODE_STOP_CAUSAL` | `STOP_BELONGS_TO_CURRENT_EPISODE` + stop 客觀欄位合法 |

### 8.3 `BEAR_REVERSAL_LEFT_RIGHT`

| gate | 固定來源 |
|---|---|
| `ACTIVE_LARGE_BEAR_ANCHOR` | 控制中的 DOWN anchor 核心品質可追溯且尚未失效 |
| `LARGE_BEAR_DOW_DEFENSE_CAUSAL` | 程式的大級空頭防線 ref 與建立日合法 |
| `BEAR_LATE_STAGE_EVIDENCE` | 第 6.5 節固定三值映射 |
| `LEFT_RIGHT_PHASE_MAPPED` | 程式 phase 非 UNRESOLVED；LL 仍不得取得 V3 試單權限 |
| `DUAL_SCALE_SEPARATED` | 大級仍為 bear、small 狀態獨立保存；沒有把 small 翻多改寫成 large bull |
| `PHASE_STOP_CAUSAL` | phase 對應 stop 候選通過 `STOP_BELONGS_TO_CURRENT_EPISODE` |

### 8.4 `FRESH_Q1_EXPANSION`

| gate | 固定來源 |
|---|---|
| `FRESH_UP_ANCHOR` | `REL_CURRENT_LEG_IS_FRESH_ANCHOR` + UP anchor 方向、控制與追溯合法 |
| `CLEAN_MEATY_DESTRUCTIVE_TRACEABLE` | 四個對應 anchor 品質原子全部 PASS |
| `DYNAMIC_Q1_EXPANSION` | 程式由大小級 trend／volatility 原子映射的必要 Q1 擴張成立 |
| `EARLY_TAIJI_GENERATION` | generation 為 ANCHOR_LEG_1 或第一次修正後 COPY_LEG_3 早期，且 stage=EARLY |
| `MACD_SUPPORT_ONLY` | `SIGNAL_HAS_INDEPENDENT_STRUCTURE=PASS`；MACD 只能是佐證，不是唯一依據 |
| `EARLY_LOCATION_WITH_SPACE` | `LOCATION_NOT_EXTENDED_FROM_ORIGIN` + `LOCATION_REMAINING_SPACE_ADEQUATE` |
| `FRESH_ANCHOR_STOP_CAUSAL` | 新錨起點／首次有效修正低的 stop 通過 episode 歸屬與因果確認 |

組合規則統一為：子項全 PASS 才是 gate PASS；有任一明確 FAIL 即 gate FAIL；沒有 FAIL 但至少一項 UNKNOWN 則 gate UNKNOWN。這個 truth table 不得由模型更動。

### 8.5 V3 試單共同硬條件

四情境矩陣完成後，程式再計算既有 V3 的十項共同硬條件；AI 不得另交一份可能與 atoms 矛盾的答案。

| V3 common hard guard | 固定來源 |
|---|---|
| `ACTIVE_WATCHLIST` | 程式 campaign／上游重新入選 ledger |
| `DATA_SUFFICIENT_FOR_ROUTE` | 程式依該候選路徑的預熱與可見結構資料等級判定 |
| `TRIGGER_COMPLETED` | 程式收盤事件與 confirmation date；只有 ARMED 時不是 PASS |
| `CAUSAL_EPISODE_STOP` | 路徑選定的 stop 通過 `STOP_BELONGS_TO_CURRENT_EPISODE`，且日期、價位、級數合法 |
| `NOT_Q3` | 程式 derived quadrants；明確 Q3 為 FAIL，UNRESOLVED 為 UNKNOWN |
| `NOT_LATE_OR_EXHAUSTED` | 程式 generation／stage／exhaustion truth table |
| `NO_SCALE_DIRECTION_CONFLICT` | 大小級下一方向的固定 atoms 與 derived relationship |
| `NOT_SINGLE_INDICATOR_SIGNAL` | `SIGNAL_HAS_INDEPENDENT_STRUCTURE` 與程式的結構證據種類計數 |
| `RISK_EXECUTABLE` | 程式 signal／stop 價格、百分比、ATR 距離及已凍結風險參數 |
| `NO_V2_FAIL` | 對目前正在評估的那一個情境 gate matrix 檢查；任一必要 gate FAIL 即 FAIL |

`NO_V2_FAIL` 不再依賴 AI 先選 `primary_scenario`。每一個 route hypothesis 都有自己的值；程式只有在完成情境互斥與重大衝突檢查後，才把選定 hypothesis 的結果帶入 V3。這可避免「換一個主要情境便把原本 FAIL 隱藏」的漂移。

## 9. 情境與路徑的確定性推導

程式先建立所有合法 anchor／relation hypotheses，再對每一 hypothesis 計算四張 gate matrix。主要情境不是 AI 欄位，而是由結構狀態決定：

1. 大級仍為 BEAR 且有效 DOWN anchor 控制：候選為 `BEAR_REVERSAL_LEFT_RIGHT`。
2. 已有完成 UP parent，現在處於其後第一次明確修正／複製，尚未累積成熟慣性：候選為 `MACRO_COPY_RESONANCE`。
3. 已有至少一次成功推進＋修正／複製，成熟 UP campaign 仍控制：候選為 `MATURE_TREND_PULLBACK`。
4. 沒有成熟 campaign 或可複製 parent，當前是第一個破壞性 UP anchor：候選為 `FRESH_Q1_EXPANSION`。
5. 前提不足為 `UNRESOLVED_NO_TRADE`；明確沒有任何合格結構為 `NO_TRADE`。

這不是新增交易條件，而是把 V2 第 8 節的互斥邊界改寫成可重播狀態機。若正式校準發現此順序與既有 V2 語意不等價，只能修訂本協定草案，不能修改 V2/V3 策略來配合程式。

在情境確定後，V3 仍照既有順序計算：

```text
V2_CORE
> NEAR_PASS_MACRO_COPY
> NEAR_PASS_FRESH_Q1
> BEAR_REVERSAL_PROBE
> NO_TRADE
```

V3 的 hard／soft gates、唯一 UNKNOWN、殘餘拆解、LL 禁止、共同硬條件、同日去重與部位規則全部沿用既有 V3，協定不得放寬。

## 10. 替代假說的確定性處理

V2 移除 AI 的 `alternative_scenarios` 欄位。程式必須從所有已回答的候選定錨、關係與四情境矩陣，自動列出 `qualifying_hypotheses`，每個 hypothesis 產生不可變的行動簽章：

```text
(direction, signal_event_ref, episode_stop_ref, campaign_stop_ref, position_role)
```

處理規則：

1. 零個合格 hypothesis：等待／不交易。
2. 一個合格 hypothesis：依策略規則繼續判斷。
3. 多個 hypothesis，但行動簽章完全相同：不阻擋；依凍結情境／V3 precedence 選顯示標籤，其餘保存為 `EQUIVALENT_INTERPRETATION`。
4. 多個 hypothesis 的方向、訊號、episode stop、campaign stop 或部位角色任一不同：`MATERIAL_HYPOTHESIS_CONFLICT`，只能 WAIT。
5. PASS 與 UNKNOWN 組合造成的候選衝突，也必須被列舉；不能因模型少填 alternative 而消失。

因此，是否有替代解讀完全由固定回答集合決定，不再因某輪模型「有想到／沒想到要寫 alternative」而改變交易權限。

## 11. 三輪合併與正式每日運行

### 11.1 一致性測試

相同封包、prompt、schema、模型版本與 reasoning 設定獨立執行三次。每個原子題的合併規則：

- 三輪同值：採該值。
- 任一輪 schema、證據或因果不合法：該輪拒收並以同一 request identity 重試；不得用不同 prompt 補答案。
- 合法三輪不一致：合併為 UNKNOWN，保留三份原始值，絕不以多數決升級 PASS。
- 程式只對合併後原子 ledger 計算 gate、情境、路徑及權限。

### 11.2 正式監控

- 沒有結構事件時延續上次已鎖定狀態，不重問模型。
- 新事件只重問 manifest 中受該事件影響的原子題；未受影響欄位沿用其版本化答案。
- 任何會新建母單、加碼、移出 campaign 或更換 stop 的政策關鍵更新，建議採兩次獨立判讀；若不一致即降 UNKNOWN 並等待下一個事件。此項成本／延遲政策須在正式協定凍結前決定。
- 每次接受的原始輸入、原始輸出、正規化 atom ledger、derived gate matrix、政策決策與 hash 都 append-only 保存。
- timeout、validation error 只影響作業狀態，不得改成語意 FAIL；重試單位為單一 review_id。

## 12. 禁止「全 WAIT 也算通過」的驗收設計

V1 的 60 筆樣本可作 V2 開發校準，但不得再作 V2 正式 holdout。正式 V2 使用未看過、互斥的新匿名樣本；抽樣 seed、selector、事件 manifest 與門檻均在第一輪前凍結，禁止使用未來績效、MFE、贏家名稱或輸家名稱。

建議正式樣本至少 96 筆：

- 48 筆正向路徑候選：四情境各 12 筆，僅依截至日結構事件與 blinded as-of 校準標籤分層。
- 24 筆一般 WAIT／NO_TRADE 邊界。
- 12 筆大級失效／REMOVE 邊界。
- 12 筆停損後 re-entry／新 episode 邊界。

候選分層不代表預先保證交易；它只保證樣本真的考到每條路徑。正式門檻建議同時包含：

1. schema、因果、ref、題目完整率 100%。
2. 三輪最終 `TRADE／WAIT／REMOVE` 一致率至少 95%。
3. 程式推導情境＋左右 phase 一致率至少 90%；由於這兩者已程式化，若輸入相同卻不一致應視為實作錯誤，目標實際應為 100%。
4. 所有會影響交易權限的 critical atoms 三輪一致率至少 95%，逐題揭露，不得只報平均。
5. 保守合併後至少 16 筆 TRADE，且每個實際有候選的策略路徑至少 2 筆；若未達，測試不得以「全部安全等待」通過，應重做不含結果資訊的分層樣本。
6. 至少各 8 筆一致 WAIT 與一致 REMOVE（若監控窗確實含 REMOVE 事件），避免只測正例。
7. 合格 TRADE 案例的 `signal_event_ref`、`episode_stop_ref`、方向與部位角色三輪必須 100% 相同。
8. 不得以降低正例數、刪除不一致案例、放寬證據要求或查看績效後重抽樣來過關。

第 5、6 項的數量是協定草案參數，正式採用前可依路徑基準率調整一次；一旦 formal manifest 產生後即不得更動。

## 13. 模型更換時保持邏輯穩定

任何 AI 模型都不能被假設天然產生完全相同判讀。V2 能做到的是把差異限制在可觀測原子欄位，並在模型上線前攔截，不是宣稱跨模型永遠零差異。

模型指紋至少包含 provider、model id、snapshot／build、reasoning level、temperature（若可設）、prompt hash、schema hash、工具版本與候選生成器 hash。

換模型流程：

1. 舊模型繼續作正式來源；新模型只跑 shadow，不得直接接管。
2. 新舊模型各自在同一 frozen migration suite 獨立跑三次。
3. schema／證據合法率必須 100%；TRADE／REMOVE 權限、訊號 ref、stop ref、方向與部位角色須逐案等價。
4. critical atom 一致率、各情境 gate matrix、正例覆蓋與 UNKNOWN 比率都要揭露；只比較最終總交易數不算等價。
5. 任一會改變歷史進出日或防線的差異都列入 `BEHAVIORAL_DELTA`。超出預先凍結容許門檻時，不切換模型；不得用已知損益挑「績效較好」的模型答案。
6. 通過後建立新的 execution freeze manifest；舊 ledger、舊模型指紋與舊報表永久保留。

建議交易層採嚴格標準：已鎖定正例的進場 ref、stop ref 與部位角色 100% 等價；非政策說明文字不比較。這可確保模型換代不會悄悄改變操作邏輯。

## 14. 與舊純 AI 判讀的語意等價檢查

V2 一致性通過後，仍不能直接宣稱保留原本看盤邏輯。必須用「未看期後績效」的舊純 AI frozen ledger 做行為差異報告：

- 相同股票日是否審核。
- 主要情境、太極世代、左右 phase 是否相同；新協定須能指出差異來自哪個原子題或狀態機。
- 是否同日觸發、等待或移除。
- signal ref、episode stop、campaign stop 與 position role 是否相同。
- 原本純 AI 交易被新協定剔除、新增或延後的逐筆原因。

此步驟只檢查「是否把原本語意程式化錯了」，不以舊純 AI 或其績效作絕對真理。若發現系統性語意偏移，修改的是尚未凍結的協定／原子定義，修改後必須重新跑新的 holdout；不得直接把舊 AI 結論硬寫成例外名單。

## 15. Schema 與政策層的禁止欄位

正式 AI schema 應明確禁止下列欄位：

```text
primary_scenario
alternative_scenarios
left_right_phase
stage_location
taiji.phase
taiji.generation
scales.relationship
route
permission
triggered
trade_plan
position_role
buy / sell / add / exit
```

AI 只回覆 manifest 指定的 atoms。上述衍生欄位只能存在於程式產生的 `derived_semantic_ledger` 或 `policy_ledger`，並需標示產生器版本與 hash。

## 16. 建議的不可變產物鏈

```text
event_packet.json
-> required_question_manifest.json
-> raw_atomic_ai_response.json
-> validated_atomic_ledger.json
-> derived_structure_state.json
-> four_scenario_gate_matrix.json
-> hypothesis_conflict_report.json
-> v1_v2_v3_policy_decision.json
-> execution_ledger.json
```

每層都保存上游 SHA-256、程式版本、時間及模型指紋。回放時只能由上游不可變資料重建下游；不得直接手修 policy decision。

## 17. 實作前仍需凍結的項目

本文件是設計草案，下列內容若未先定案，不得寫正式 V2 schema 或進行績效：

1. 大小級候選樞紐與 anchor candidate 的確切生成參數。
2. 趨勢性、波動率、乾淨度、有肉、剩餘空間的量化欄位與比較窗；量化值是證據，不得偷偷成為未授權的新策略門檻。
3. 太極獨立作用腿的確認與 generation truth table。
4. stage／exhaustion truth table 的課程校準結果。
5. 成熟長多與第一次大級複製的互斥狀態機。
6. episode stop 候選選擇與多候選 material conflict 規則。
7. 正式監控採一次、兩次或三次 AI 判讀；以及 disagreement 的作業狀態與重審時機。
8. 96 筆 holdout 的實際分層數、seed、selector 與正例最低數。
9. 模型遷移允許的非政策 atom 差異與政策欄位零差異範圍。

以上應先用課程與 as-of 校準案例審查，再建立 `HYBRID_MONITORING_PROTOCOL_V2` 正式文件、schema、prompt、policy、測試與 freeze manifest。不得查看本輪期後損益來選擇定義。

## 18. 已知風險

1. 定錨乾淨度、父代／修正關係與剩餘空間仍含視覺語意，不可能只靠 schema 保證跨模型完全一致；原子化的價值是定位並攔截差異。
2. 三輪不一致降 UNKNOWN 會降低交易數；因此必須同時用正例覆蓋門檻防止協定退化成全 WAIT。
3. 若程式候選生成漏掉老師／人工會看到的長級定錨，AI 無權新增端點，可能造成假陰性；候選生成器需要獨立的 recall 校準。
4. 把象限、世代與左右階段程式化可能偏離原始純 AI 語意；必須用逐筆日期、signal ref 與 stop ref 做行為等價，而非只比總績效。
5. 單案例判讀與關鍵動作複核會增加成本與延遲，但可避免一個長尾案例拖累整批並讓重試可追溯。
6. 模型遷移驗收只能偵測差異，不能保證未來模型永遠相同；未通過 shadow suite 就不能切換。
7. 正向候選分層若間接使用未來漲幅，會造成嚴重結果洩漏；selector 只能使用截至日事實與 blinded 語意標籤。

## 19. 草案驗收結論

相較 V1，本草案的核心改變只有監控責任與輸出形式：

- AI 從「選情境、選 phase、決定要不要寫替代解讀」改為「逐候選回答固定原子題」。
- 程式從「驗證 AI 已選結果」改為「由完整原子事實重算四情境、歧義、V1/V2/V3 路徑與交易權限」。
- 驗收從「總體一致率」增加到「critical atom、正向路徑、訊號日、防線與部位角色」逐項驗收。
- 模型更換必須先跑 frozen shadow equivalence，不能直接替換。

這能大幅降低同一規則因模型重跑或換模型而漂移的風險，但在第 17 節的 truth tables、候選生成器與 holdout 驗收正式凍結前，仍不能宣稱已可長期正式監控，也不得開始 V3 全量績效回放。
