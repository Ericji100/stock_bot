# 啟蒙層 AI 綜合判讀規格 V3

版本：`enlightenment-ai-judgement-v3`
建立日期：2026-09-06
用途：在 V2 完整核心之外，建立可稽核、可分組回測的受控試單層
狀態：`FROZEN_FOR_RESEARCH（研究規則已凍結）`

## 1. 版本定位

V3 的直接父版本是 [`啟蒙層 AI 綜合判讀規格 V2`](enlightenment-ai-judgement-v2.md)。V1 與 V2 均保持唯讀，不被 V3 覆寫或放寬。

```text
V3
= V2_CORE（V2 完整合格核心，逐字沿用 V2）
+ NEAR_PASS（只限兩種順勢情境、最多一個指定 soft gate 為 UNKNOWN）
+ BEAR_REVERSAL_PROBE（空頭末段左右反轉的受控試單）
```

V3 解決的問題不是讓更多股票自動通過，而是把「V2 因一項尚不能完全證明而不交易」與「結構明確失敗」分開。任何 `FAIL`、Q3、末代耗竭、無因果防線或單一指標訊號，均不得因 V3 而取得進場權限。

## 2. 不變項與非目標

V3 完整沿用 V2 的下列規範：

- 課程來源、資料充分性與 MACD 21/55/55 的輔助角色。
- 定錨、太極世代、動態四象限、大小級道氏、防線與左右戰法。
- 四個主要情境的完整定義、必要 gate 與互斥路由。
- 只能使用判讀日收盤以前資料；日 K 收盤觸發後於下一交易日開盤執行。
- 母單、加碼、同日去重、episode、campaign、停損後再進與移出監控。
- 公司行動、還原權值技術座標、現金損益與交易成本口徑。

V3 不做下列事情：

- 不修改 V1 或 V2 文件、schema、機器規則或測試。
- 不把 V1 訊號本身當成 V3 的必要或充分條件。
- 不因任何已知贏家、代號、名稱、期後 MFE 或損益調整 gate。
- 不同時最佳化進場、部位與出場。
- 不把 `UNKNOWN` 解讀成 `PASS`；它只能取得明確標記的試單權限。
- 不允許 `NEAR_PASS` 成為其他情境都不成立時的殘餘分類。

## 3. 三層權限與優先順序

同股、同日、同方向只能產生一個決策層級，依序判斷：

1. V2 主要情境全部必要 gate 為 `PASS`，且 V2 可 `TRIGGERED`：歸入 `V2_CORE`。
2. V2 未完整通過，但符合第 6 或第 7 節全部條件：歸入 `NEAR_PASS`。
3. 大級仍為空頭，符合第 8 節全部條件：歸入 `BEAR_REVERSAL_PROBE`。
4. 其餘維持 `WATCHING／ARMED／REJECTED／DATA_INSUFFICIENT`，不交易。

優先權固定為：

```text
V2_CORE > NEAR_PASS_MACRO_COPY > NEAR_PASS_FRESH_Q1 > BEAR_REVERSAL_PROBE > NO_TRADE
```

不得為了同一天增加部位而同時套用兩層。若 V2 已通過，V3 不得另建試單。

## 4. `V2_CORE（V2完整合格核心）`

`V2_CORE` 沒有任何新判斷：

- 主要情境仍為 V2 四情境之一。
- 該情境全部必要 gate 為 `PASS`。
- 沒有 blocking disqualifier。
- 觸發、因果防線、監控資格與同日去重均符合 V2。
- V3 僅增加 `CORE_TRIGGERED（核心已觸發）` 標籤，方便與試單分帳。

V3 不得將原本 V2 的 `FAIL／UNKNOWN` 改寫為 `PASS`，也不得更換 V2 的主要情境來取得核心資格。

## 5. 所有試單共同硬條件

`NEAR_PASS` 與 `BEAR_REVERSAL_PROBE` 必須全部通過：

1. `ACTIVE_WATCHLIST（監控資格有效）`：上游選股 campaign 有效，未處於移出監控狀態。
2. `DATA_SUFFICIENT_FOR_ROUTE（資料足以判本路徑）`：不得為 `INSUFFICIENT`；大定錨複製與空頭反轉至少達 V2 的結構最低資料量。
3. `TRIGGER_COMPLETED（觸發事件已完成）`：不是只有計畫或等待；收盤已完成獨立的小級控制權轉換。
4. `CAUSAL_EPISODE_STOP（部位防線可因果確認）`：防線來源、確認日、級數與價位完整，且不是用訊號後資料回填。
5. `NOT_Q3（不是Q3）`：大、小級不得同時落入低趨勢低波動且沒有策略方向的 Q3。
6. `NOT_LATE_OR_EXHAUSTED（非末代耗竭）`：不得為明確第五段弱化、第三次攻擊衰退、末端爆量或剩餘空間不足。
7. `NO_SCALE_DIRECTION_CONFLICT（大小級無方向衝突）`：允許策略共振，不允許下一可交易方向相反。
8. `NOT_SINGLE_INDICATOR_SIGNAL（不是單一指標）`：均線、MACD、量能、新高或單根紅 K 不能單獨觸發。
9. `RISK_EXECUTABLE（風險可執行）`：訊號與防線的價格、百分比及 ATR 距離都要記錄，並依回測前凍結的成交風險參數判定。
10. `NO_V2_FAIL（V2無明確失敗）`：該主要情境所有必要 gate 中不得出現 `FAIL`。

任一共同硬條件不是 `PASS`，V3只能等待或拒絕。

## 6. `NEAR_PASS_MACRO_COPY（大定錨複製近合格試單）`

### 6.1 適用邊界

主要解讀必須是 `MACRO_COPY_RESONANCE（大定錨複製共振）`。小級修正結束與向上再發動已完成，但 V2 對父代完成、世代映射或大小級共振中的一項仍只能標 `UNKNOWN`。

### 6.2 必須 PASS 的 hard gates

- `CORRECTION_INTACT`
- `CORRECTION_BEAR_DOW_LINE_CAUSAL`
- `SMALL_UP_REANCHOR_BREAK`
- `NOT_Q3_OR_EXHAUSTED`
- `EPISODE_STOP_CAUSAL`

### 6.3 唯一允許 UNKNOWN 的 soft gates

以下三項中最多且恰好一項為 `UNKNOWN`，其他兩項必須 `PASS`：

- `COMPLETED_PARENT_ANCHOR`
- `TAIJI_GENERATION_MAPPED`
- `DUAL_SCALE_LONG_ALIGNMENT`

不得出現 `FAIL`。AI 必須說明為何是「證據尚不完整」而不是反證成立，並列出升級成 V2 核心所等待的事件。

### 6.4 觸發

- 小級向上反向定錨已成立。
- 收盤突破修正段已因果確認的空頭道氏防線，或完成等價的低不破低、高過高。
- 使用本 episode 鄰近小級防線，不得放寬到遠端父代起點。
- 第一次成交標記 `PROBE_MOTHER（試單母單）`。

## 7. `NEAR_PASS_FRESH_Q1（新生定錨近合格試單）`

### 7.1 適用邊界

主要解讀必須是 `FRESH_Q1_EXPANSION（新生定錨直接擴張）`。已有可追溯的新生向上作用段與因果失效，但 V2 對較高級破壞、Q1完整擴張或早期太極世代中的一項仍只能標 `UNKNOWN`。

### 7.2 必須 PASS 的 hard gates

- `FRESH_UP_ANCHOR`
- `MACD_SUPPORT_ONLY`
- `EARLY_LOCATION_WITH_SPACE`
- `FRESH_ANCHOR_STOP_CAUSAL`

### 7.3 唯一允許 UNKNOWN 的 soft gates

以下三項中最多且恰好一項為 `UNKNOWN`，其他兩項必須 `PASS`：

- `CLEAN_MEATY_DESTRUCTIVE_TRACEABLE`
- `DYNAMIC_Q1_EXPANSION`
- `EARLY_TAIJI_GENERATION`

不得出現 `FAIL`，並另做下列殘餘拆解：

- 若 `CLEAN_MEATY_DESTRUCTIVE_TRACEABLE` 為 UNKNOWN，`CLEAN／MEATY／TRACEABLE` 必須各自 PASS；只允許 `HIGHER_SCALE_DESTRUCTION` 尚待確認。
- 若 `DYNAMIC_Q1_EXPANSION` 為 UNKNOWN，小級向上趨勢擴張必須 PASS；只允許較高級波動率／趨勢同步證據尚待確認。
- 若 `EARLY_TAIJI_GENERATION` 為 UNKNOWN，位階仍須為 `EARLY`、同向攻擊序號不得超過 2，且不得已有 `COPY_LEG_5／LATER_GENERATION` 證據。

### 7.4 觸發

- 優先第一段破壞或第一次淺修正再發動。
- 收盤完成已確認小級防線／樞紐突破。
- 失效使用新錨起點或第一次有效修正低，不能只以長均線當停損。
- 第一次成交標記 `PROBE_MOTHER（試單母單）`。

## 8. `BEAR_REVERSAL_PROBE（空頭末端左右反轉試單）`

### 8.1 適用邊界

大級仍為下降定錨或空頭朝代，小級開始由空轉多。這一層不是宣告大級多頭成立，而是用可控失效測試左右反轉。

### 8.2 必須 PASS 的 hard gates

- `ACTIVE_LARGE_BEAR_ANCHOR`
- `LARGE_BEAR_DOW_DEFENSE_CAUSAL`
- `LEFT_RIGHT_PHASE_MAPPED`
- `DUAL_SCALE_SEPARATED`
- `PHASE_STOP_CAUSAL`

### 8.3 唯一允許 UNKNOWN 的 gate

- 只允許 `BEAR_LATE_STAGE_EVIDENCE` 為 `UNKNOWN`。
- 不得有任何 `FAIL`。
- 至少要有一項可量化但尚不足以完成 V2 gate 的末段線索，例如攻擊縮短、斜率衰退、量價背離、末跌加速後無法續低或時間／空間耗損。

### 8.4 左右階段權限

- `LL`：不得試單，只能 `WATCHING`。
- `LR`：小級已低不破低且高過高、失效鄰近時可試單；仍標示大級空頭壓力。
- `RL`：突破大級空頭防線後第一次有效拉回守住，可試單。
- `RR`：RL後再突破小樞紐，可試單；若已有獲利部位則依升級規則處理。
- `DIRECT_TO_RIGHT`：乾淨推進穿越大小級防線且仍在初期，可試單。

第一次成交一律是 `PROBE_MOTHER`，不得回補不存在的 LL／LR 部位。

## 9. 明確禁止條件

下列任一成立即不得由 V3 試單：

- V2任一必要 gate 為 `FAIL`。
- 同一路徑有兩項以上 `UNKNOWN`。
- UNKNOWN 不在該路徑允許清單內。
- 定錨、太極世代或大小級方向無法建立主要解讀。
- Q3、末代耗竭、複製失敗、父代／大級防線已失效。
- 訊號只來自均線、MACD、成交量、新高或單根K棒。
- 找不到鄰近且因果成立的 episode 防線。
- 防線距離超過回測前凍結的風險上限。
- 主要與替代解讀會改變方向、失效點或部位角色。
- 監控資格已失效，且尚未被上游重新選入。

## 10. 決策狀態與中文說明

V2原狀態全部保留，V3另增加研究層標籤：

| 狀態 | 中文說明 |
|---|---|
| `CORE_TRIGGERED（核心已觸發）` | V2主要情境全部必要gate通過，取得核心母單／核心加碼權限 |
| `NEAR_PASS_ARMED（近合格試單已建立計畫）` | 近合格路徑、等待事件及失效已知，但收盤觸發未完成 |
| `NEAR_PASS_TRIGGERED（近合格試單已觸發）` | 順勢近合格路徑已完成收盤觸發，下一交易日開盤執行試單 |
| `BEAR_PROBE_ARMED（空頭反轉試單已建立計畫）` | 左右階段、等待事件與防線已知，尚未完成收盤觸發 |
| `BEAR_PROBE_TRIGGERED（空頭反轉試單已觸發）` | 空頭末段左右試單已完成收盤觸發，下一交易日開盤執行 |
| `PROMOTION_ARMED（核心升級已建立計畫）` | 試單仍有效，等待完整V2核心條件與獲利加碼資格 |
| `PROMOTED_TO_CORE（已升級為核心）` | 後續獨立事件使V2全部gate通過；是否增加部位另依加碼規則 |
| `PROBE_STOPPED（試單已停損）` | 試單episode失效；不代表大級campaign必然失效 |
| `V3_NO_TRADE（V3不交易）` | 不符合核心或任一試單層的完整權限 |

對使用者顯示時，英文狀態後必須附中文括號。

## 11. 試單升級、同日去重與加碼

### 11.1 升級不等於自動買進

試單持有後若 V2 全部必要 gate 通過：

1. 將結構標為 `PROMOTED_TO_CORE`。
2. 原 `PROBE_MOTHER` 不改名、不重算成本、不抹除原始試單理由。
3. 同一天不得再建立第二個母單。
4. 只有原持倉於訊號日收盤已有淨浮盈，且出現不同日期的獨立新結構證明，才可新增核心加碼。
5. 若原持倉仍虧損，只升級結構狀態，不加碼。

### 11.2 失敗與再進

- 試單防線失效：結束該 episode，標 `PROBE_STOPPED`。
- 大級仍有效：回 `REENTRY_WATCHING`，等待新的獨立小結構；不能沿用舊觸發。
- 大級／父代失效：依 V2 轉 `CAMPAIGN_INVALIDATED` 並移出監控，直到上游重新選入。

## 12. 出場規則

V3第一階段不得改變V2使用的出場與成交規則。核心、近合格與空頭試單使用相同出場，以隔離進場品質：

- +2R以前使用該部位／episode因果防線。
- 曾達+2R後，成本防線只升不降。
- 小級樞紐失守先進入警戒；波段期連續兩日收盤跌破21MA才全數出場。
- 大級失效與監控移除依V2處理。

若日後比較其他停利方式，必須在V3進場判讀及全部訊號先凍結後另建實驗版本。

## 13. 部位研究分階段

### 13.1 第一階段：進場品質隔離

`BACKTEST_PARAMETER（回測參數）`

- `V3_EQUAL_UNIT_MOTHER_ONLY`：三層每個首次成交都固定約10,000元。
- 不加碼、不因層級調整部位。
- 同一成交、出場、成本與公司行動口徑。
- 分別統計核心、兩種近合格與空頭試單，不只看合併總損益。

### 13.2 第二階段：分層部位

只有第一階段訊號完全凍結後才測：

- `V2_CORE`：1.0單位。
- `NEAR_PASS`：0.5單位。
- `BEAR_REVERSAL_PROBE`：0.25單位。
- 升級核心時，只在獲利且有獨立新訊號時補到下一部位層級。

0.5與0.25是待驗證回測參數，不是課程唯一指定比例，也不得反過來改變是否觸發。

## 14. 回測矩陣與歸因

至少比較：

1. `V1_MOTHER_ONLY_10K`：舊版廣泛觸發基準。
2. `V2_MOTHER_ONLY_10K`：完整核心基準。
3. `V3_CORE_ONLY`：必須與相同資料下的V2完全一致。
4. `V3_CORE_PLUS_NEAR_PASS_EQUAL_UNIT`。
5. `V3_ALL_LAYERS_EQUAL_UNIT`。
6. `V3_ALL_LAYERS_TIERED_POSITION`。

並做消融：

- 只加入 `NEAR_PASS_MACRO_COPY`。
- 只加入 `NEAR_PASS_FRESH_Q1`。
- 只加入 `BEAR_REVERSAL_PROBE`。

每組至少輸出交易數、已實現／未實現、平均／中位報酬、勝率、PF、MFE／MAE、最大回撤、尖峰資金、最大同時持倉、單筆及尖峰初始風險、選股來源×層級×情境。來源歸因若重疊，必須明示不可加總。

## 15. 防止結果回填

V3建立與判讀必須遵守：

- 不在提示、規則、schema或逐筆證據中提供本輪已知贏家、輸家、MFE、損益或排名。
- 所有當期 AI 決策先寫入不可變 ledger 並計算 SHA-256，再解除績效封存。
- 規則若因語意矛盾需要修正，必須保留舊版並增加版本號；不得靜默覆寫。
- 已看過績效後提出的新條件只能標 `RESEARCH_HYPOTHESIS`，必須在其他凍結期間及未來影子監控驗證。
- 不得用「這檔後來大漲」證明當時的 UNKNOWN 應改為 PASS。

## 16. 每日 AI 輸出

輸出必須符合 [`enlightenment_ai_judgement_v3.schema.json`](../config/enlightenment_ai_judgement_v3.schema.json)，並包含：

1. 完整、合法的 V2 基礎判讀。
2. V3唯一決策層與唯一試單路徑。
3. V2全部必要gate結果及唯一UNKNOWN gate。
4. 十項共同硬條件逐項PASS／FAIL與日期價位證據。
5. 近合格路徑的殘餘拆解，或空頭末段的部分證據。
6. 訊號日、收盤價、episode防線、風險百分比與ATR距離。
7. 等待升級的明確事件。
8. 母單、試單、升級、加碼與同日去重狀態。
9. 未使用未來資料、績效對AI不可見、未引用已知贏家的聲明。

## 17. 採用門檻

V3在正式監控前必須：

- 至少在三個彼此分離的歷史選股期間，以相同凍結規則重播。
- V3 core 必須逐筆等同 V2，不得漂移。
- 各試單層必須獨立揭露；不得用核心績效掩蓋試單虧損。
- 在回測績效解封前另行凍結接受門檻，包括資金效率、PF、中位數、最大回撤及尖峰資金。
- 完成一段只通知、不交易的前向影子監控，檢查相同輸入能否穩定重現相同結構欄位。

V3通過規則完整性檢查，不代表已證明獲利；它目前是待回測的研究假說。
