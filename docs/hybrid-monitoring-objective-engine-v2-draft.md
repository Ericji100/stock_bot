# HYBRID_MONITORING_PROTOCOL_V2｜客觀結構／因果狀態引擎草案

狀態：`DRAFT_ONLY（尚未凍結、不得用於正式績效）`
目的：將可由截至當日資料唯一推導的結構交給程式，縮小 AI 的自由分類空間；不修改既有策略 V1／V2／V3，也不修改任何 `HYBRID_MONITORING_PROTOCOL_V1` 凍結檔。

## 1. 結論

V1 一致性不足的核心不是 AI 不懂課程，而是責任邊界過寬：AI 同時自由選主要情境、左右階段、大小級道氏、象限、太極世代與替代情境；其中部分其實可由已確認樞紐及突破事件因果推導。尤其 V1 的 `alternative_scenarios` 是否被模型主動列出，會直接改變 `交易／等待`，同一價格事實便可能產生不同權限。

V2 應採「程式建立不可改寫的骨架、AI 只回答固定原子問題、程式再組合情境與權限」：

1. 程式逐日重建因果樞紐、攻擊、道氏防線、左右分界與尺度關係。
2. 程式列出固定數量的候選定錨假設與每個假設的客觀統計，不讓 AI 自造起訖點。
3. AI 不再輸出 `primary_scenario`、`left_right_phase`、自由格式替代情境或最終 gate；只對每個候選假設回答相同的原子問題。
4. 程式由同一份原子答案完整評估四個情境，按既有 V3 優先順序決定路徑；多個解讀若會改變方向、失效點或部位角色，固定轉為等待。
5. 所有狀態 append-only（只新增版本、不覆寫歷史），確保同一輸入、同一規格永遠得到同一客觀 ledger。

這能消除「程式本來就能算卻交給 AI 重猜」的漂移；但定錨品質、父子關係、動態象限與位階仍有課程明確保留的裁量性，不能假裝全部已有唯一公式。

## 2. 課程與既有 V1 的依據

本草案直接對照：

- `course_knowledge_base/啟蒙交易班/整理成果/啟蒙交易班_課程整理.md`
- `course_knowledge_base/啟蒙交易班/整理成果/四象限戰法強化_完整課程整理.md`
- `course_knowledge_base/道氏三兄弟/整理成果/道氏三兄弟_完整課程整理.md`
- `course_knowledge_base/戰法C班/整理成果/戰法C班_完整課程整理.md`
- `docs/hybrid-monitoring-protocol-v1.md`
- `config/hybrid_monitoring_protocol_v1.json`
- `config/hybrid_common_structure_v1.schema.json`
- `config/hybrid_semantic_prompt_v1.md`
- `scripts/hybrid_v3_policy.py`
- `scripts/hybrid_v3_event_packets.py`

課程提供的硬語意如下：

- 道氏多頭為高過高＋低不破低；空頭為低破低＋高不過高。
- 防線必須先有實際創高／創低結果，才可回指該次攻擊的發動樞紐；不能提前把未證明的低／高點升為防線。
- 左右分界是原方向道氏防線被突破的事件，不是圖形上的幾何左右。
- 小級反轉不等於大級反轉；大小級必須分開。
- Q1～Q4 的軸是趨勢性與波動率，不是多空方向；象限是動態比較，不是固定貼標籤。
- 太極必須先有定錨，再談修正、複製、世代與衰退；MACD 21/55/55 只能輔助切段，不能單獨成立定錨。
- 課程未提供「乾淨、有肉、象限、比例原則、耗竭」的唯一量化公式，這些不可被偷換成未揭露的機械門檻。

## 3. V1 現況不能直接升格為客觀真值的地方

現有封包仍可作相容性基線，但下列欄位不可直接宣稱等於課程正式定義：

1. `add_causal_pivots` 目前使用日 K 半徑 3／10 的置中最高低點；只等待右側半徑完成，未加入課程所要求「高點候選的最低點之後被跌破／低點候選的最高點之後被突破」。同價並列也可能被接受。
2. `LARGE` 目前是半徑 10 的獨立極值，不是課程所述由一級樞紐再取參數 1 的二級樞紐。
3. `MACRO_DEFENSE_ALERT` 現在比較的是「最近一個大級低點」，不是「曾造成創高的最近攻擊發動低點」，兩者不能畫上等號。
4. `candidate_stop_refs` 只是取收盤下方最近三個小級低點，未證明哪一個屬於本 episode 的因果失效點。
5. V1 `_objective.trigger_completed` 只要同日出現 `SMALL_CONTROL_BREAK` 或 `LARGE_CONTROL_BREAK` 就成立，尚未綁定情境要求的特定控制高與特定防線。
6. 現有事件只完整處理向上突破；若要建立對稱道氏與反轉狀態，必須同時產生向下跌破、攻擊來源及防線版本。

因此 V2 應保留 `LEGACY_RADIUS_3_10` 作行為對照，另建 `COURSE_CAUSAL_L1_L2` 流；兩者不可混用。正式採用哪一流，須先做純 AI 歷史訊號的行為等價比較，再凍結，不能在回測途中切換。

## 4. 欄位責任矩陣

| 領域 | 程式可唯一推導 | AI 固定原子判讀 | 最後由程式組合 |
|---|---|---|---|
| 日 K／指標 | 還原權值 OHLCV、MA、ATR、MACD、完成週期、資料品質 | 無 | 事件與可用性 |
| 樞紐 | 候選、局部確認、交替配對、取代、有效日、級數、版本 | 比例是否足以代表工作級數 | 接受／保留比例疑慮 |
| 道氏 | HH／HL／LL／LH、BULL／BEAR／TRANSITION／UNDEFINED | 無權覆寫 | 結構方向 |
| 因果防線 | 防線來源、建立日、突破日、是否仍有效 | 無權自選價位 | episode 可選防線集合 |
| 左右 | 大防線突破前後、LR／RL／RR／直接進右的事件順序 | 左左的末段品質與背離線索 | 權限可用 phase；LL 永不由 AI 單獨開權限 |
| 尺度 | 大小級客觀方向一致／衝突／不足 | 象限策略方向是否同向 | 最終 resonance／conflict |
| 定錨 | 候選段、端點、幅度、時間、斜率、效率、破壞事實 | 乾淨、有肉、是否為控制父代、修正歸屬 | 定錨假設集合 |
| 太極 | 選定父代後的腿序、攻擊次數、幅度／時間／斜率比 | 父子血緣、複製品質邊界、是否已時間失效 | phase、generation、衰退旗標 |
| 四象限 | 原始趨勢／波動比較向量 | 趨勢增減、波動擴縮的固定二軸答案 | Q1／Q2／Q3／Q4／UNRESOLVED |
| 位階／耗竭 | 累積 ATR、同向次數、距錨時間、量價與斜率數值 | 剩餘空間、邊界型衰退、較高級壓力 | EARLY／MIDDLE／LATE／EXHAUSTED |
| 情境與 gate | 無自由猜測 | 對所有固定原子問題給 PASS／FAIL／UNKNOWN | 四情境全矩陣、路徑、交易／等待／移除 |

### 4.1 V1 欄位的 V2 歸屬

- 移出 AI：`scales.*.dow_state`、`scales.*.dow_defense_ref`、可決策的 `left_right_phase`、方向型 `scales.relationship`。
- 拆成「程式候選＋AI 選擇」：`anchors`、`taiji.parent_anchor_id`。
- 拆成原子答案後由程式映射：`primary_scenario`、`taiji.phase`、`taiji.generation`、`stage_location`、`semantic_flags.q3/exhausted/scale_conflict/single_indicator_only`。
- 完全刪除自由輸入：`alternative_scenarios`。V2 改為程式固定評估所有情境假設。
- 保留給 AI 但改成完整固定矩陣：定錨品質、象限二軸、父子／修正歸屬、空頭末段線索、剩餘空間等原子判讀。

## 5. 因果資料時序

每個交易日固定按下列順序處理，任何實作不得自行調序：

1. 載入前一交易日收盤後已鎖定的狀態。
2. 套用公司行動，技術結構留在還原權值座標，現金帳留在原始座標。
3. 加入當日完整日 K，計算只用 `<= as_of` 的指標與原始事件。
4. 用當日資料更新樞紐候選、局部確認與取代紀錄。
5. **控制突破一律比較前一日已有效的控制線**；當日才完成確認的樞紐，不得同時被宣稱為當日已突破，以消除日 K 內高低先後不明造成的循環。
6. 更新攻擊、防線、道氏與左右狀態，寫入 append-only 客觀 ledger。
7. 建立匿名 AI 原子問卷；AI 只看截至當日證據與固定候選 ID。
8. 驗證所有答案與證據後，程式映射象限、太極、情境、gate 與權限。
9. 收盤訊號下一交易日開盤才執行；成交閘門與交易模擬仍沿用獨立凍結規格。

`confirmation_date = as_of` 的樞紐可以在第 7 步作語意證據，但其 `effective_for_control_break` 必須是下一交易日。這是日 K 資料粒度下的保守操作規則，不是課程原句，必須在 V2 manifest 明示。

## 6. 因果樞紐狀態機

### 6.1 一級樞紐（L1）

每一尺度必須凍結 `n`；若採課程流，日 K 初版可測 `n=1/2/3`，但正式協定只能選一組。不得依個股或事後績效自由切換。

高點候選 `H(i)` 需同時滿足：

- 左右各 `n` 根 K 的 high 嚴格小於 `high[i]`；同價不算通過。
- `i` 之後至少一根 K 的 low 嚴格小於 `low[i]`。
- `confirmation_date = max(右側第 n 根日期, 首次跌破候選 low 的日期)`。

低點完全鏡像：左右 low 嚴格較高，且之後 high 嚴格突破候選 high。

狀態：

```text
RAW_CANDIDATE
  ├─ 右側或離開條件未完成 → PENDING
  ├─ 條件完成             → LOCAL_CONFIRMED
  └─ 被更極端同側候選取代 → REVOKED（保留版本）

LOCAL_CONFIRMED
  ├─ 下一個相反側局部點被接受 → PAIRED_CONFIRMED
  └─ 配對前同側更極端點成立   → SUPERSEDED（保留版本）
```

交替序列規則：

- 只有 high／low 交替才加入工作序列。
- 連續同側點在相反點尚未接受前，保留更極端者；同價選較早 source date，另記後續同價測試，不重寫舊輸出。
- `LOCAL_CONFIRMED` 與 `PAIRED_CONFIRMED` 必須分欄保存。道氏排列只使用已配對點；即時控制突破可使用前一日已有效的局部點，但須標 `LOCAL_CONTROL`，不得冒稱已完成道氏排列。
- 比例過小不由程式靜默刪除；程式計算比例向量並標 `PROPORTION_REVIEW_REQUIRED`，AI 只回答固定的「是否足以代表本工作級數」。

### 6.2 二級樞紐（L2）

課程流從 L1 已接受序列再取一次：

- L2 高：某 L1 高左右各一個 L1 高更低。
- L2 低：某 L1 低左右各一個 L1 低更高。
- 參數固定為 1，同樣保留候選、確認、配對、取代與 effective date。

若因相容性暫時沿用半徑 10，欄位必須命名 `LEGACY_LARGE`，不得與 `COURSE_L2` 共用 `LARGE` 名稱。

## 7. 道氏與因果防線狀態機

### 7.1 同級關係

以最後兩個已配對高點、最後兩個已配對低點計算：

- `HH／LH／EH`：後高大於／小於／等於前高。
- `HL／LL／EL`：後低大於／小於／等於前低。
- `BULL`：`HH` 且低點為 `HL` 或 `EL`。
- `BEAR`：`LL` 且高點為 `LH` 或 `EH`。
- `TRANSITION`：資料足夠，但只完成一半改變或高低關係矛盾。
- `UNDEFINED`：各側不足兩個可用點或比例審核尚未完成。

價格先正規化至凍結精度再比較；浮點 epsilon 只處理儲存誤差，不能被用來創造可調式突破緩衝。

### 7.2 攻擊與防線

多頭攻擊：前一日有效的控制高 `H`，以及 `H` 之後、突破日前最後一個有效低點 `L`。當日收盤首次嚴格高於 `H.price` 時，建立 `UP_ATTACK_CONFIRMED`，`L` 才取得多頭防線資格。

空頭完全鏡像：收盤首次嚴格低於控制低 `L` 時，回指其後最後有效高點 `H` 為空頭防線。

每個防線版本至少保存：

```text
defense_id, campaign_id, scale, side,
source_pivot_id, control_pivot_id,
established_on, effective_on, price,
breached_on, status, supersedes_defense_id
```

硬限制：

- 防線來源樞紐必須在突破日前已有效。
- 未造成新控制高／低的樞紐不得升格防線。
- 同一 campaign 內，多頭防線只升不降；空頭防線只降不升。若候選會反向放寬，拒絕更新並記 `NON_MONOTONIC_DEFENSE_CANDIDATE`。
- 小級防線失效只結束 episode；大級防線失效才可成為 campaign 移除候選。
- 「最近大級低點」和「大級多頭防線」是兩個欄位，永不互相代用。

### 7.3 控制狀態

每尺度分開保存：

- `UP_CONTROL`：最近有效事件為向上控制突破，且其多頭防線未破。
- `DOWN_CONTROL`：最近有效事件為向下控制突破，且其空頭防線未破。
- `CONTESTED`：局部與配對／較高級結果相反，或同日順序無法由日 K 確認。
- `UNRESOLVED`：資料不足。

AI 可解釋控制狀態的結構意義，但不得覆寫事件、價位或日期。

## 8. 左右反轉狀態機

左右狀態只在「大級客觀為 BEAR／DOWN_CONTROL 且存在有效大級空頭防線」時啟動；一般多頭拉回不可硬套左右四部位。

```text
BEAR_CAMPAIGN
  ├─ 未有小級翻多                    → LEFT_PRECONFIRM
  ├─ 小級 UP_CONTROL、大空防未破      → LR
  ├─ 同一未中斷上攻穿越小、大空防      → DIRECT_TO_RIGHT
  └─ 大空防被突破                    → RIGHT_AWAIT_PULLBACK

RIGHT_AWAIT_PULLBACK
  ├─ 第一個因果小級拉回低點成立且守住   → RL
  └─ 新大級空頭控制重新成立             → RESET_TO_NEW_BEAR_CAMPAIGN

RL
  ├─ 收盤突破 RL 後形成的有效小級控制高 → RR
  ├─ 收盤失守 RL 低點                  → RIGHT_FAILED
  └─ 新大級空頭控制重新成立             → RESET_TO_NEW_BEAR_CAMPAIGN
```

定義補充：

- `LL` 的「末跌加速、次低不破、跨市場背離」含裁量。程式只輸出 `LEFT_PRECONFIRM` 與客觀末段向量；AI 可提交固定末段線索，但 LL 不得單靠該判讀取得 V3 交易權限。
- `LR` 必須有小級因果翻多，不能只因紅 K、均線收復或 MACD 翻紅成立。
- `DIRECT_TO_RIGHT` 要求同一 `attack_id` 在中間沒有新確認反向小級樞紐的情況下，依序穿越小、大空頭防線。
- 防線一旦被因果突破，不能因價格跌回線下就把歷史改回左側；除非新空頭 campaign 成立，否則標 `RIGHT_FAILED／UNRESOLVED`。
- V2 最終相容欄位可映射為 `NONE／LL／LR／RL／RR／DIRECT_TO_RIGHT／UNRESOLVED`，其中 `LL` 僅為說明標籤，`LR/RL/RR/DIRECT_TO_RIGHT` 由程式事件決定。

## 9. 大小級關係

程式先只判斷價格結構關係：

| 大級控制 | 小級控制 | 客觀關係 |
|---|---|---|
| UP | UP | `DOW_DIRECTION_RESONANCE` |
| DOWN | DOWN | `DOW_DIRECTION_RESONANCE` |
| UP | DOWN | `SMALL_COUNTER_LARGE_UP` |
| DOWN | UP | `SMALL_COUNTER_LARGE_DOWN` |
| 任一 CONTESTED／UNRESOLVED | 任意 | `UNRESOLVED` |

V1 的 `STRATEGY_RESONANCE` 不可由道氏方向直接推出。例如大 Q4、小 Q2 的名稱不同，策略方向仍可能一致；這部分改由 AI 回答兩尺度各自「下一段策略要求方向」，程式再映射：

- 方向同向：`STRATEGY_RESONANCE`。
- 方向相反且兩者都確定：`STRATEGY_CONFLICT`。
- 任一不確定：`MIXED／UNRESOLVED`。

最終 `scale_conflict` 由程式依客觀道氏衝突與 AI 策略方向共同計算，不再讓 AI直接填布林值。

## 10. 定錨與太極：候選圖而非自由畫線

### 10.1 程式列舉候選定錨

每個 `as_of` 只提供固定、有限候選集合：

1. `PIVOT_LEG`：兩個相反側、已有效樞紐間的方向段。
2. `MACD_SKELETON`：已完成 MACD 21/55/55 區段內的低至高／高至低骨架；只作候選，不自動合格。
3. `DEFENSE_BREAK_IMPULSE`：造成某級因果防線／重要控制線突破的作用段。
4. `COMPOSITE_SPAN`：由連續同向攻擊與中間未破父防線修正構成的較大段；合併規則必須固定且只能用當時已知端點。

每個候選提供不可改寫的：起訖 refs、方向、尺度、幅度／ATR、K 數、效率比、內部樞紐數、反向 K 比例、影線比例、斜率、突破哪些控制線、相對 105/144MA 區間、完成／形成中狀態。

AI 對每個候選逐一回答：`CLEAN`、`MEATY`、`CONTROLLING_PARENT`、`SAME_CAMPAIGN` 等固定問題，不得另填日期或價格。

### 10.2 太極圖

對每個候選父代，程式預先產生後續交替腿圖：

```text
LEG_1 = 父代／第一作用段
LEG_2 = 其後第一逆向段
LEG_3 = 其後第一同向段
LEG_4 = 其後第二逆向段
LEG_5 = 其後第二同向段
```

程式可唯一計算：腿序、方向、幅度、時間、斜率、效率、回撤率、是否創高／低、同向攻擊次數及各比值。AI 只回答：

- 逆向段是否仍屬該父代的修正，或已是反向新錨。
- 後代與父代的綜合複製品質是支持／反證／不足。
- 是否存在時間失效、級數升級或複製轉修正。

一旦 AI 選定 `parent_candidate_id`，`taiji.phase`、`generation`、`same_direction_attack_number` 由程式依腿圖映射，不再由 AI 自由命名。若不同候選父代會導致不同交易權限，固定等待；若所有合格父代得到相同方向、同一 stop 與同一部位角色，可依凍結優先序選最長或最具破壞性的候選。

## 11. 四象限與位階的原子化

課程沒有唯一象限公式，因此 V2 不應直接用任意數值門檻取代老師的動態判讀。程式先輸出兩組原始比較：

- 趨勢向量：連續創高／創低率、方向效率、同向腿相對幅度、控制線推進、斜率變化。
- 波動向量：ATR 比、腿幅比、真實波幅比、修正幅度變化、K 棒擴縮。

AI 只能回答兩個固定軸：

- `TREND_AXIS = INCREASING／DECREASING／UNRESOLVED`
- `VOLATILITY_AXIS = EXPANDING／CONTRACTING／UNRESOLVED`

程式映射：

- increasing＋expanding → Q1
- decreasing＋expanding → Q2
- decreasing＋contracting → Q3
- increasing＋contracting → Q4
- 其他 → UNRESOLVED

方向另由道氏／定錨決定，絕不從象限名稱推論多空。

位階不再只問 AI 一個 `EARLY/MIDDLE/LATE`。程式提供同向攻擊次數、腿世代、累積 ATR、距定錨天數、後代／父代幅度、時間與斜率比；AI逐項回答「剩餘空間足夠」「較高級壓力明確」「時間失效」「複製衰退」。建議的操作映射為：

- `EARLY`：LEG_1 或 LEG_3／同向攻擊不超過 2，且無耗竭反證。
- `LATE`：LEG_5、後代世代或第三次以上同向攻擊，但尚未達耗竭。
- `EXHAUSTED`：末代條件再加至少一項可核對衰退（攻擊縮短、斜率衰退、量價背離、延續失敗、時間空間耗盡）。
- 其餘 `MIDDLE／UNRESOLVED`。

這個映射是為穩定執行所加的操作定義，不是課程提供的唯一數學公式；必須在 V2 正式凍結前以不看績效的標註案例校準。

## 12. AI 原子問卷與四情境組合

每個候選假設都提交同樣的固定欄位，禁止只提交「自己選中的情境」所需 gate。最少應包含：

- 候選定錨：乾淨、有肉、是否控制父代、修正是否隸屬、父代是否仍有效。
- 太極：複製品質、修正品質、時間失效、級數升級、複製轉修正。
- 象限：趨勢軸、波動軸、策略方向。
- 位階：剩餘空間、較高級壓力、五代／後代耗竭線索。
- 空頭末段：攻擊縮短、斜率衰退、量價背離、續低失敗、時間空間耗竭；每項固定作答。
- 證據：每個答案必須引用程式給定 refs；不足填 UNKNOWN，反證才填 FAIL。

程式再用既有策略 V3 的優先順序完整計算：

1. `V2_CORE`
2. `NEAR_PASS_MACRO_COPY`
3. `NEAR_PASS_FRESH_Q1`
4. `BEAR_REVERSAL_PROBE`
5. `NO_TRADE`

不得由 AI輸出路徑或交易權限。四情境都由同一份答案被評估，因而不存在「模型忘了列替代情境，反而取得交易」的漏洞。

## 13. 歧義與衝突處理

固定決策規則：

1. **程式事實優先**：AI 的日期、價位、道氏、防線、phase 若與客觀 ledger 不同，輸出無效，不採多數決修補。
2. **完整假設集**：程式列出的每個候選父代都必須回答；漏答者為 UNKNOWN，不可因少列替代解讀而提高權限。
3. **多路徑同質**：若多個合格情境的方向、episode stop、部位角色完全相同，按既有 V3 優先序選一路並保留其餘審計紀錄。
4. **多路徑異質**：任一合格假設會改變方向、stop 或母單／試單／加碼角色，固定 `WAIT_AMBIGUOUS_MATERIAL_HYPOTHESIS`。
5. **Q3**：由象限兩軸映射，不能由 AI 額外關閉或開啟。
6. **耗竭**：由世代＋固定衰退原子答案映射，不能由一個自由布林決定。
7. **單一指標**：程式依 gate 的證據家族計數；只有 MA、MACD、成交量或單根 K 一個家族支持時，固定 blocking。
8. **當日多事件**：合併成一個 after-close 狀態；任何需判斷日內先後的結論標 `INTRABAR_ORDER_UNKNOWN`，不猜順序。
9. **資料不足**：預熱不足、樞紐不足或座標異常一律降級 UNKNOWN；不得以均線排列補成大結構。
10. **重分類**：只新增 `SUPERSEDED_BY` 版本；歷史當日的原判斷、當時可見證據與權限永不回寫。

## 14. 必須成立的不變量

- 任一 evidence、樞紐、攻擊、防線、定錨端點皆不得晚於 `as_of`。
- `source_date <= confirmation_date < effective_for_control_break_date`；非交易日以實際下一交易日計。
- 防線的 `established_on` 不得早於造成創高／創低的突破日。
- 多頭防線來源必為低樞紐，空頭防線來源必為高樞紐。
- `RR` 必須有同一 reversal campaign 的 `RL`；`RL` 必須晚於大空防突破。
- `DIRECT_TO_RIGHT` 必須由同一 attack_id 穿越兩級防線，且中途無反向有效小級樞紐。
- 大小級共振不可由同一組樞紐重複充當兩個尺度。
- 同一輸入 SHA、同一引擎版本與同一設定，客觀 ledger 位元級一致。
- AI 回答順序、批次大小、子程序或模型重試不得改變程式客觀狀態與候選集合。
- 任何移除都必須引用大級 campaign 的因果防線失效；小級停損只能結束 episode。

## 15. 最低測試組

### 15.1 樞紐因果

1. 未滿右側 `n` 根時，不得提前出現樞紐。
2. 右側完成但高點候選 low 尚未跌破，維持 PENDING；離開條件完成日才 LOCAL_CONFIRMED。
3. 同價並列高／低不符合「左右嚴格較低／較高」。
4. 配對前連續同側點只保留更極端工作點，舊版本標 SUPERSEDED，不刪歷史。
5. 當日新確認樞紐不可同日再被當成已有效控制線突破。
6. L2 只從已有效 L1 序列產生，且左右各一個同側 L1 點。

### 15.2 道氏與防線

7. HH＋HL → BULL；LL＋LH → BEAR；只完成一半 → TRANSITION；點不足 → UNDEFINED。
8. 出現較高低點但後續未破控制高，不得上移多頭防線。
9. 收盤破控制高後，攻擊發動低點才於該日建立防線。
10. 同 campaign 的較低多頭防線候選被拒絕，不得放寬風險。
11. 空頭防線做完全鏡像測試。
12. 小級防線失守只結束 episode；大級防線失守才建立移除候選。

### 15.3 左右與尺度

13. 小級翻多但大空防未破 → LR，不可宣稱成熟大多頭。
14. 大空防突破後第一個因果拉回低點 → RL；再破其後控制高 → RR。
15. 同一攻擊連續穿越小、大空防且無中間反向樞紐 → DIRECT_TO_RIGHT。
16. 穿越兩級防線中間已有反向樞紐，不得標 DIRECT_TO_RIGHT。
17. 大 UP、小 DOWN → SMALL_COUNTER_LARGE_UP，不得被 AI 改成方向共振。
18. 防線突破後跌回線下只能 RIGHT_FAILED／重建新 campaign，不得回寫成未突破。

### 15.4 AI 邊界與策略組合

19. AI 漏答任一候選假設，該答案 UNKNOWN，不能取得較寬鬆路徑。
20. AI 自造 ref、日期、價位或 phase，整筆輸出驗證失敗。
21. 兩個情境都合格且同方向／同 stop／同角色，依凍結優先序決定。
22. 兩個情境都合格但 stop 或角色不同，固定 WAIT。
23. 相同原子答案不論 JSON 欄位順序、批次大小或重試次數，產生完全相同路徑。
24. 只引用 MACD 翻紅、MA 收復或單根紅 K，不得通過 `single_indicator_only`。
25. Q3 必須由 trend decreasing＋volatility contracting 映射；AI 不能同時聲稱 Q1。

### 15.5 資料與重播

26. 除權息日前後以還原權值座標維持結構連續，現金損益仍用原始成交座標。
27. 750 根不足時長級品質為 UNKNOWN，不得用短歷史假裝完整大定錨。
28. 同一事件重播兩次不產生重複攻擊、防線或 phase 轉換。
29. 隨機打亂不同股票處理順序，逐股 ledger 與合併 SHA 不變。
30. 在任一 `as_of` 截斷資料重建，結果必須與完整資料於該日保存的快照一致。

## 16. 建議輸出物與版本隔離

實作時全部另建 V2 檔，不覆寫 V1：

- `config/hybrid_objective_engine_v2.json`
- `config/hybrid_atomic_semantics_v2.schema.json`
- `scripts/hybrid_objective_engine_v2.py`
- `scripts/hybrid_atomic_ai_review_v2.py`
- `scripts/hybrid_v3_policy_v2.py`
- `tests/test_hybrid_objective_engine_v2.py`

正式 manifest 必須分別鎖定：價格來源、公司行動、pivot 流、比較精度、事件順序、候選定錨列舉規則、AI schema／prompt／model、策略版本及執行版本。報表標題同時顯示：

```text
Strategy rule: ENLIGHTENMENT_AI_JUDGEMENT_V3
Monitoring protocol: HYBRID_MONITORING_PROTOCOL_V2
Objective engine: COURSE_CAUSAL_L1_L2_V1（或明示 LEGACY_RADIUS_3_10）
```

## 17. 採用前的驗證順序

1. 先用人工可核對的合成圖與已標註案例通過上述客觀引擎測試。
2. 以 V1 已完成的 60 例作開發校準，只看語意差異，不看未來績效。
3. 另抽與 V1 不重疊的匿名 holdout；必須分層包含可交易路徑候選、等待、移除與大防線事件，避免全部 WAIT 也能虛假通過。
4. 同輸入獨立三次 AI 原子判讀；客觀欄位應為 100%，交易權限至少 95%，重要原子語意／情境至少 90%，且每個正式路徑都要有最低正例覆蓋。
5. 通過後，才與舊純 AI 逐筆比較：訊號日、情境、phase、stop、交易／等待；差異須逐類解釋，不能只看總績效接近。
6. 行為等價可接受後，才鎖定 V3 全量語意 ledger 並解封績效；V1／V2 若要公平比較，必須重用同一客觀 ledger 與同一 AI 原子答案。

## 18. 主要風險

1. **樞紐定義遷移風險**：由半徑 3／10 改成課程 L1／L2，訊號日期與數量很可能明顯改變；這是語意修正，不是單純重構。
2. **日 K 內順序不可見**：同根 K 同時確認與突破時無法知道先後；必須接受隔日生效的保守延遲，或另取得更細資料，不能猜。
3. **主觀詞仍存在**：乾淨、有肉、父子血緣、象限動態與剩餘空間沒有課程唯一公式；V2 能縮小選項，但不能把裁量偽裝成客觀真值。
4. **過度保守風險**：多假設異質即等待會降低觸發數；一致性驗收必須同時檢查正例路徑覆蓋，避免「全部不交易」取得漂亮一致率。
5. **相容性與課程純度衝突**：若保留 legacy pivots，較容易對齊既有績效；若採 course L1/L2，較接近課程但需要重新建立基準。兩者須明示選擇，不可混稱。
6. **AI 證據選擇仍可能漂移**：即使答案原子化，不同模型仍可能在邊界例不同；完整候選集、固定問卷、固定證據向量與保守合併不可省略。

## 19. 建議決策

先不要直接撰寫 V2 最終 policy。第一個可實作里程碑應是只建立 `COURSE_CAUSAL_L1_L2` 客觀 ledger，並對 20～30 個合成與真實截斷案例逐點驗證樞紐、道氏防線及 LR→RL→RR。這一層若不可靠，後面的 AI 提示詞再穩定，也只會穩定地解讀錯誤骨架。

客觀引擎驗證後，再凍結 AI 原子問卷與情境映射；最後才做一致性與績效。這樣未來調整 V3 策略 gate 時，可以重用同一份客觀結構 ledger，不必重新計算所有價格因果狀態；只有真的改變 pivot／防線定義時，才需要重建底層。
