# v34 第一階段：多方 Q2／Q4 決策追加規則

本檔只約束歷史回放第一階段的可成交範圍。正式
`enlightenment-integrated-v2.1.8-anchor-origin`規則及v32基礎rubric仍是課程語意來源；
本追加規則若與基礎rubric把Q2過度縮寫為單一假跌破型態的文字衝突，以本檔為準。

## 1. 市場判讀仍完整，成交範圍縮小

- 市場結構仍須雙向判讀定錨、大小結構、道氏、防線、四象限、太極、一之、左右、X流程及C班狀態。
- 交易執行固定`LONG_ONLY`，且只有多方Q2與多方Q4可以建立可成交setup。
- Q1／一之、開盤區間、Q3壓縮、箱型、左右及其他戰法只可作支持、衝突、風險、退出或禁止做多證據。
- 空方結構只能用來判斷多方修正、接管風險、禁止做多、退出，以及多方是否重新取得資格。

## 2. Q2市場狀態與Q2交易設置分離

Q2市場狀態仍依課程定義為同級趨勢性減弱、波動擴張。不得用固定點數、固定分鐘數、
單純n根樞紐或「1～2根內收回」取代這個象限定義。

本階段允許的多方Q2交易設置只有：

1. `Q2_FALSE_BREAK_RECLAIM`：既有合格邊界、作用中道氏防線或已成熟開盤邊界被跌破後收復，掃低極值可追溯，之後重新向上發動。
2. `Q2_FAILED_COUNTERTREND_REVERSAL`：父級多方控制仍保留，成熟空方反向候選沒有完成同級接管，並在可確認端點失敗後重新向多方發動。
3. `Q2_SLOW_OUTER_EXPANSION_FAILURE`：外圍向下破位後無法延續並收復，形成反向多方小錨、突破原空方同級防線、完成右左拉回後重新發動。這不是把快速假跌破任意延長。

假跌破收復只是Q2的其中一種可執行設置，不等於全部Q2。事件搜尋可以在事前固定的有限視窗內等待決定性收復；實際用了幾根必須保存，不能事後改寫。

Q2進場後使用掃低／反向失敗極值外的結構停損，並以事前凍結的2～3根快速行為窗檢查是否離開低點、守住收復線並產生有利推進。這是本回放版本的執行政策，不是Q2象限的課程定義。

## 3. Q4市場狀態與原生執行

Q4仍依課程定義為同級趨勢性增強、波動收縮。可成交設置為多方定錨有效時，完整同級修正受控，修正端點已因果確認並重新向上發動。

- `ANCHOR_LEG_SEQUENCE`及`CONFIRMED_PULLBACK_ENDPOINT_N2`屬Q4候選。
- `Q4_AGGRESSIVE_PULLBACK_REVERSAL`允許在已收盤止跌轉強K出現時，用截至該根的完整修正極值鎖停損，不必等待n=2端點右側再多兩根；其課程品質仍由AI判斷。
- 停損必須在完整同級修正低點外，不得借用Q1緊停損。
- 前高原則上是`CHECKPOINT`；只有確實使交易風險報酬不合理的價位才是`HARD_TARGET`。
- 進場後的行為窗只檢查原交易論點是否開始實現；達到有利檢查點後，後續改由結構、太極品質與AI持倉判讀管理，不得讓已消費的入場時間窗永久重啟。

## 4. AI接受進場的必要一致性

- Q4候選在形成與準備階段，AI須填寫`working_quadrant=Q4`，或`TRANSITION`且主候選為Q4、兩軸為趨勢增強／波動收縮；Q2候選同理，須填寫`working_quadrant=Q2`，或`TRANSITION`且主候選為Q2、兩軸為趨勢減弱／波動擴張。
- 到`ENTRY_ELIGIBLE`時，若最新已收盤觸發K已使重新發動呈現趨勢增強／波動擴張，`working_quadrant`應忠實改寫Q1；此時只有`main_strategy_family`仍逐字保存原Q2／Q4、`setup_key`與`facts_hash`符合本輪gate，且AI課程品質逐項全為`PASS`，才可`ENTER`。Q1只是觸發後的動態市場狀態，不是本階段新增的可交易來源。
- `ENTRY_ELIGIBLE`時AI只能接受`ENTER`，或使用既有固定否決原因；不得用「再等一根」規避決策。
- `STOP_TOO_WIDE`只有在runtime事前提供明確的數值風險上限，且程式要求的完整結構停損確實超過該上限時才能使用。若沒有事前數值上限，不得因固定1口、停損點數看起來很大或無法縮小部位而臨場否決。
- `HARD_OBSTACLE_TOO_CLOSE`必須有可追溯的`HARD_TARGET`；順勢前高、觸發線與`CHECKPOINT`不得冒充硬障礙。`GRADE_CONFLICT`只在本輪`grade_relation=CONFLICT`時使用；`CONSTITUTION_BLOCKED`只在程式交易憲法已明確鎖定時使用。四項都不成立時必須接受`ENTER`。
- Q2專門處理同級趨勢性減弱、向下波動擴張後的多方收復。若AI已選定仍有效的多方`SMALL→LARGE`升級結構為`LARGE`控制、大級替代防線未破，而且本輪是程式合格的多方Q2候選，較低級空方錨／空方段就是Q2的交易背景：可以保留`grade_relation=CONFLICT`描述大小級方向不同，但`grade_control`與`course_permission`必須為`PASS`，不得只因該較低級反向段或固定1口使用`GRADE_CONFLICT`否決。只有同級／更大級空方正式接管、父級防線失效或其他明確硬否決成立時，才是會禁止Q2的級數衝突。
- 相同候選、停止、行為窗及成交順序由程式鎖定；格式修正不得改變AI原本的`ENTER／HOLD／EXIT`。
- 新增的慢速Q2與積極Q4候選會帶`facts_hash`。AI須以`ai_course_assessment`逐字引用`setup_key`與`facts_hash`，並固定檢查`anchor_control`、`grade_control`、`dow_defense`、`quadrant_axes`、`taiji_relation`、`location_quality`、`trigger_quality`、`stop_integrity`、`expected_behavior`及`course_permission`，逐項輸出`PASS／FAIL／UNKNOWN`；沒有帶`facts_hash`的候選時填`null`。`facts_hash`不一致、任一必要項為`FAIL`或`UNKNOWN`時均不得進場。

## 5. 績效歸類

每個成交事件必須保留`candidate_source`與`entry_strategy`，以便分開統計：

- Q2：`Q2_FALSE_BREAK_RECLAIM`、`Q2_FAILED_COUNTERTREND_REVERSAL`、`Q2_SLOW_OUTER_EXPANSION_FAILURE`
- Q4：`Q4_PULLBACK_CONTINUATION`、`Q4_AGGRESSIVE_PULLBACK_REVERSAL`

其他來源不得混入本階段交易績效。

`course_reading.main_strategy_family`是v34必填的結構化歸因欄位，只允許
`NONE`、`Q2`、`Q4`。沒有作用中的可成交候選或持倉時填`NONE`；接受進場、
等待成交或持倉中則必須依`entry_gate.required_entry_strategy`（缺少時才依
`candidate_source`）填入對應的`Q2`或`Q4`。太極、道氏、X流程及其他課程方法
可以保留在`focus_methods`作為支持或風險證據，但不得改寫主戰法家族或績效歸因。

## 6. 進場後的動態象限與確認階段

- `main_strategy_family`永遠保存原始成交戰法的Q2或Q4歸因；`working_quadrant`則描述最新已收盤K完成後的當前市場狀態，兩者不得混為一談。
- 修正低點後的重新發動若已出現同向實體／波幅擴張並突破觸發線或前高，當前工作軸應寫`INCREASING／EXPANDING`、工作象限寫Q1；Q2或Q4仍保留為原交易歸因。只有重新發動後仍以收斂、平穩方式同向推進時，工作象限才繼續寫Q4。
- `AGGRESSIVE_CONFIRMED`只用於原候選明載`confirmation_style=AGGRESSIVE`的成交與持倉；其他已完成收盤觸發、再於下一根開盤成交的Q2／Q4使用`CONSERVATIVE_CONFIRMED`。不得依模型當下信心自行互換。
- 多方Q2假跌破收復後，在尚未完成一組新的多方推進—修正—再推進以前，C班／太極仍是`RESETTING`；不能只因第一段快速反彈越過檢查點就改稱`TAIJI_ORDERED`。Q4若本來已有可追溯父代、完整修正及重新發動，父級未失效時可維持`TAIJI_ORDERED`。
