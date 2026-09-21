# TMF歷史逐K監控回放

> 目前凍結驗證版是`course-state-v2.1.8-replay-adapter-v31`／`replay-execution-v99-isolated-no-trade-minute`。本頁保留部分舊版演進紀錄；目前架構、驗證結果與換模型規則以[確定性v31驗證報告](trade-monitor-deterministic-replay-v31.md)為準。

這個工具使用期交所歷史逐筆成交資料重建TMF一分K，並把證交所官方每5秒加權指數聚合為現貨一分K。v31由確定性程式逐根計算交易事實；AI可作文字解說，但不是定錨、道氏、象限、戰法、進出場或停損的決策來源。它和正式Bot、Automation `1-k`及`.runtime/trade_monitor/`完全隔離。

## 安全邊界

- 不import或啟動`main.py`、polling、Job Queue或正式Codex Automation。
- 不控制Chrome，不讀取正式監控截圖。
- 正式v2.1.8交易提示詞與schema皆唯讀引用並以SHA-256鎖定；沒有修改或複製另一套交易規則。
- 目前本機實驗設定使用v10回放資料契約，執行器已更新為`replay-execution-v60-comma-safe-bar-role-grounding`。它累積修正固定點數分級、硬性象限判斷、權重覆寫、檢查點誤作持有線、保護停損狀態、反向候選被延後到第4個樞紐、太極父代價位誤接舊時間／同類樞紐、結構升級後大級方向或已知象限無事件消失、升級趨勢未依大級趨勢軸恢復延續狀態、內部自由備註與程式控制級數互相矛盾、同一作用錨在較晚樞紐確認時順向極值倒退（含尚無正式大錨時的結構子錨路徑）、已有時間／高低角色與衍生保護價被格式器重複或誤註解、持倉中遇到結構升降級時事件卡與管理卡互相衝突，以及高級數端點稀疏時把高點→較高低點誤命名為多方推進（或低點→較低高點誤命名為空方修正）的問題。程式會逐端點核對相鄰時間、價位、高低角色與方向；角色和價格方向矛盾的配對不建立推進／修正段。大級太極在已有小錨時，會把該小錨起點併入可追溯端點，避免只刪除矛盾段後一併漏掉真正的修正低／高點。作用錨的順向極值保持單調延伸，卡片內的衍生保護價不因巧合等於歷史OHLC而被加上錯誤K棒來源；持倉中的升降級保留事件引用與級數變化，但一律輸出持倉管理卡，空手時才使用獨立升降級卡。持倉保護價的ATR緩衝在新防線第一次可知時鎖定；同一防線下後續ATR變化不會讓停損漂移，AI只能維持舊保護價或採用程式提供的新完成防線候選。同方向有多個有效防線時，多單採較高、空單採較低的保護候選，而既有鎖定停損本身永遠是合法觸發價。程式衍生的保護價可作觸發線，STOP卡與交易統計以既定停損價及實際觸發K時間，而非分析截止時間或該根收盤價計算。初始進場、第一次停損、合法再進及第二次停損的再進權由程式持倉唯一管理；AI的action、setup與memory三處必須一致，不能自行消耗、恢復或轉移額度。稀疏分析時若停損K與分析截止相隔已滿1根完整1分K，可直接標為`AVAILABLE`；一般失效、逾期與`NO_CHASE`仍永久封鎖。假跌破與假突破文字也分別使用「跌破後收復」與「向上突破後收回其下」，避免方向語意顛倒。控制級數、升級方向、相關內部備註及升級後的延續／修正標籤由程式和大級趨勢軸共同管理，AI負責課程品質與情境判讀。詳見[課程偏離稽核](trade-monitor-course-audit-20260905.md)。
- 守門器變嚴格、但規則prompt與schema雜湊完全相同時，可先用`python -m trade_monitor_replay --resume <RUN_ID> --rewind-to HH:MM`只回退到受影響分析點之前；它不呼叫AI。若舊分支的原始輸出仍是相同prompt/schema下逐點因果產生，可再用`python -m trade_monitor_replay --resume <RUN_ID> --reuse-saved-raw --batch-size N`依序重驗，不重新呼叫AI；任何一點未通過新守門器即停止。普通resume遇到執行版本不同仍會拒絕，避免靜默混用狀態規則。
- 防線從確認後逐根檢查收盤；第一次被破壞時由程式標為`BROKEN`並保存時間，之後不能再被AI當成作用中防線。
- 回放狀態只寫入`.runtime/trade_monitor_replay/`。
- 模擬Telegram群組沒有設定時直接失敗，不回退正式`chat_id`。
- AI輸出未通過schema、語意或跨K狀態驗證時最多修正一次；仍失敗就停止，且不傳Telegram。

## 交易日

`--date 2026-08-27`代表：

- 背景夜盤：2026-08-26 15:00至2026-08-27 05:00。
- 逐K日盤：2026-08-27 08:45至13:45。

日盤逐K只會把當時已收盤資料交給所選AI provider，不會提供後續K棒。

08:45至08:59只提供前一交易日現貨收盤；09:00起才逐分鐘揭露當日現貨K。官方整日OHLC只用於下載完整性驗證，不會提前放入AI context。

## 本機設定

預設設定檔是`.runtime/trade_monitor_replay/config.json`。它不保存Bot Token或MiniMax Key；兩者分別唯讀使用既有`config.json`及`config/secrets.json`。

```json
{
  "instrument": "TMF",
  "ai_provider": "minimax",
  "ai_model": "MiniMax-M3",
  "codex_reasoning_effort": "medium",
  "codex_executable": "codex",
  "ai_timeout_seconds": 300,
  "max_ai_attempts": 2,
  "max_output_tokens": 16384,
  "detail_bar_count": 180,
  "overview_minutes": 15,
  "analysis_every_bars": 2,
  "telegram_enabled": false,
  "telegram_chat_id": null,
  "send_quiet_status": true,
  "quiet_status_interval_minutes": 10,
  "message_profile": "course_event_cards",
  "rule_manifest_path": "trade_monitor_replay/rules/course-state-v2.1.8-replay-adapter-v10/rule-manifest.json"
}
```

Codex回放可改成：

```json
{
  "ai_provider": "codex",
  "ai_model": "gpt-5.6-sol",
  "codex_reasoning_effort": "medium"
}
```

Codex provider透過已登入的本機Codex CLI建立隔離、唯讀且不保存session的結構化呼叫；不會讀取專案`AGENTS.md`、啟動工具或修改正式監控。Codex帳戶用量百分比由Codex app管理，長區間實驗應分段執行並在每段之間檢查用量。

`course-state-v2.1.8-replay-adapter-v10`的manifest仍唯讀引用正式v2.1.8。不能因此宣稱回放與正式判斷完全等價：回放schema較簡化，執行提示曾加入不受課程支持的判斷覆蓋，v23已修正目前查明的核心項目。execution_version與執行提示雜湊改變後，既有run拒絕混用。

錨生命週期每個分析點都重新以相同因果規則更新：

- `背景大錨`：由因果樞紐階層或已確認的結構接管辨認，不使用120／30點門檻。開盤延伸極值確認為二級樞紐且已有小級延續證據時，可辨認包含內部小結構的父級推進；此工程轉譯另標`OPENING_EXTREME_HIGHER_PIVOT`及待AI品質審查，不自動等同完整大級道氏或大級防線。
- `子級小錨`：背景內已完成小級接管條件的作用中方向，不等於最新微小修正。
- `目前工作段`：仍在形成的最新推進或修正，端點可以隨每輪已收盤K更新，但不會因此冒充背景錨。
- `反向候選`：與背景相反且達到結構門檻的候選；只建立另一方向劇本或使背景降級。必須再完成反向修正及同方向延續，才可真正取代背景。
- Type2接管仍核對反向推進、修正、延續與大級防線破壞。Type3跨大小級防線急轉不是必須再等一組四樞紐的課程規定；目前自動接管涵蓋尚不完整，不能宣稱所有反轉均已程式化。
- 大級與小級各保存多方、空方兩個道氏槽位；TG會同時顯示作用中方向與另一方向已失守／仍有效的防線。
- 防線收盤失守後背景先標為降級；只有反向接管條件尚未完成時，下一輪收復才標為「降級後收復」。若Type 2已接管，收復舊價位只是新背景內的修正，不會復活舊錨。

結構生命週期以3個監控時段分開：日盤08:45、夜盤下午15:00，以及美股09:30 ET開盤（台灣時間隨美國夏令／冬令自動換算）。前一時段只作盤前快照與關鍵價參考；新時段的定錨、道氏、象限及太極重新累積。新時段前4根1分K只能稱開盤證據；OR5收完後，程式才可用時段開盤價至OR5方向極值建立「小級開盤錨」，不能直接稱大錨。開盤錨內的反向點必須先完成課程`n=2`樞紐條件，才能記為`defense_candidate`；只有它之後的同向攻擊確實突破先前錨極值，才依「先有結果、來源才取得資格」升格為正式道氏防線。反向行情也必須用同級`n=2`樞紐完成「高過高＋低不破低」或「低破低＋高不過高」，才可成為`reverse_candidate`；單純超過固定點數的反彈／回落只保存為未定級工作段。候選可以作觀察價或觸發參考，但不得顯示成作用中防線，也不得單獨決定道氏方向。每個run另存`anchor-lifecycle-state.json`，checkpoint亦保存同一快照，便於重跑與稽核。

四象限先選作用中錨和級數，再比較其後同級推進／修正的幅度、時間、斜率、破位與品質。`quadrant_context.authority=EVIDENCE_ONLY`：程式不強制Q1～Q4、不使用0.67回撤界線、不預設下一象限、不要求維持8分鐘。5／20根窗口僅供診斷，不定義大小級數；AI填寫兩軸、候選、依據後，契約核對欄位與兩軸對應。

v10錨確認的程式檢查：推進－修正－延續的A/B/C轉折必須已可確認，且後續K收盤越過B，才能建立作用中方向錨或反向方向候選。越過B的收盤K就是仍在形成的D端，不必再等待D成為未來確認的n=2樞紐。只有影線越過B、收盤剛好等於B，或在C尚未確認前曾越過B，都不算確認。事件與防線取得資格的時間使用首次合法收盤，而非回填到影線極值時間；既有錨可以記錄影線新極值，但不能僅憑該影線升格新的道氏防線。反向方向即使完成自己的A/B/C/D，只要原控制錨防線尚未被已收盤K破壞，就仍是修正或反向候選；控制權只在防線破壞後因果移交。沒有正式大錨時，作用中的子級錨仍可建立同向Q4／太極延續候選。2026-08-25 09:14高44,387、收44,330，未收過09:09高44,377，以及2026-08-26 09:10收盤完成多方HH＋HL候選，均已列為回歸案例。修復程式與既有契約不一致的缺口後，只有逐點重算確認先前檢查點完全一致，才可從該缺口以前接續；交易規則或提示詞變更仍須新run。

反向候選的工作段亦使用全時段已揭露資料中的同級確認樞紐，不能從候選「首次確認時間」截斷K棒再找局部高低。例：09:16才確認候選，不代表先前的09:15低點不可用；09:18已確認的工作段應由09:15低點起算，而非任選09:17低點。確認前仍保留舊工作段，候選與非候選分支採用相同因果判斷。

訊息中的裸價位若對應多個不同時間的結構點，且該句未指明工作段、錨或防線，顯示層只保留價位，不猜K棒時間。明確給出的時間保留；來源唯一時照常補時間。來源提示只限同一句，不讓前一句「目前工作段」誤套到後一句歷史太極比較。

太極的朝代錨和逐代父段不同。帳本保留同級段的幅度、時間、斜率、影線及形成狀態，提供同向比較（3對1、5對3、4對2）的量測；AI仍需判讀比例、乾淨度、破壞性及複製／修正品質。形成中幅度較短不等於複製失敗；複製失敗不自動翻向。

結構升級由程式先提供因果證據，而非要求AI憑文字猜測。以多方為例：末小級多方防線失守、父級低點仍守住，之後已收盤K重新突破中間前高時，程式產生`GRADE_UPGRADE`；事件時間是第一次可確認的收盤時間，不回填到先前低點。空方完全鏡像。

升級後若收盤破壞替代防線，程式產生`GRADE_DOWNGRADE`；若再破父級起點，產生`STRUCTURE_INVALIDATED`。已知樞紐、防線、OR5或OR15被收盤突破後，在最多2根內收回且幅度通過ATR過濾時，產生`FALSE_BREAK_RECLAIM`。這些只是客觀事件，不會自動翻向或下單。

程式帳本提供多空樞紐／掃價極值及0.2×ATR14緩衝參考，明標不是課程唯一停損。Q4應依所選同級完整修正、假突破依掃價極值設外側停損；驗證器不再強制等於最新樞紐±0.2ATR。現階段仍無法完整驗證AI所選修正是否真屬該戰法同級全段，不能把外側檢查當成完整策略驗證。

## 使用方式

v23接受進場時另保存`accepted_at`。每2根或稀疏抽樣時，最早模擬成交時間為「訊號後下一根」與「AI接受時點後下一根」的較晚者；不得用AI已看過的K棒開盤價回填成交。情境權重保留AI原值，只驗證總和與範圍，不依舊錨方向交換多空數字。顯示層也不再用0.8回撤比例或Q4標籤改寫盤勢摘要。

只驗證資料，不呼叫MiniMax或Telegram：

```powershell
python -m trade_monitor_replay --date 2026-08-27 --instrument TMF --dry-run
```

事件驅動模式會逐根由程式更新狀態，只在定錨、防線、結構生命週期、假突破收復及節流後的象限基線變化呼叫AI。先以dry-run查看預估呼叫數：

```powershell
python -m trade_monitor_replay --date 2026-08-27 --event-driven --dry-run
```

確認後執行回放（沒有`--telegram`就不推送）：

```powershell
python -m trade_monitor_replay --date 2026-08-27 --event-driven --mode auto
```

`--event-driven`可搭配`--from`、`--to`或`--max-bars`，不能和`--at`或`--analysis-every`混用。事件時間表建立後會鎖入run；resume時不能改排程，只能續跑或使用`--batch-size`分批。歷史排程雖預先列出事件時間，送給AI的內容仍只包含該時間以前的K棒。

只建立一次盤前夜盤快照：

```powershell
python -m trade_monitor_replay --date 2026-08-27 --instrument TMF --preopen-only
```

測試開盤前30分鐘：

```powershell
python -m trade_monitor_replay --date 2026-08-27 --instrument TMF --from 08:45 --to 09:15 --mode auto
```

只在數個事前選定的已收盤K時間進行因果判讀；每個`--at`都只看該時間以前的資料，狀態依時間順序連續更新：

```powershell
python -m trade_monitor_replay --date 2026-08-27 --at 09:07 --at 09:17 --at 09:38 --telegram
```

`--at`不能和`--from`、`--to`、`--max-bars`或`--analysis-every`混用，也不能在resume時更換，避免同一run的時間軸被重寫。

預設每兩根已收盤一分K分析一次，仍把兩根原始一分K依序交給MiniMax，不合成二分K。需要逐分鐘精查時使用：

```powershell
python -m trade_monitor_replay --date 2026-08-27 --analysis-every 1 --max-bars 5
```

加上`--telegram`才會推送到模擬群組：

```powershell
python -m trade_monitor_replay --date 2026-08-27 --instrument TMF --from 08:45 --to 09:15 --mode auto --telegram
```

只測試模擬群組連線、不呼叫AI；同一群組只會成功發送一次，重跑回傳`duplicate`：

```powershell
python -m trade_monitor_replay --telegram-test
```

接續暫停或失敗前最後一個已驗證狀態：

```powershell
python -m trade_monitor_replay --resume <run_id>
```

盤前快照已成功時，直接沿用該狀態續跑第一根日盤K，不重新呼叫盤前分析：

```powershell
python -m trade_monitor_replay --resume <run_id> --continue-day --max-bars 1
```

把該run最新一份已驗證的canonical message補送到模擬群組，不重新呼叫MiniMax，也不重寫判讀：

```powershell
python -m trade_monitor_replay --deliver-latest <run_id>
```

補送也使用run內去重狀態；同一event重複執行會回傳`duplicate`，不會再次發送。

補送某個已驗證抽樣時間（例如該點先前被安靜狀態節流），同樣不重跑AI：

```powershell
python -m trade_monitor_replay --deliver-at <run_id> --delivery-time 10:26
```

判讀規則或資料紀律修正後，可把最後一根日盤K還原到前一個已驗證checkpoint後重跑；舊prompt、raw output及錯誤紀錄都會保留：

```powershell
python -m trade_monitor_replay --resume <run_id> --rerun-last
```

Telegram每一分段固定以前綴`🔥`識別，canonical message以`🧪【歷史回放】`標示歷史日期與模擬K時間。v10依事件分成盤前快照、觀察、準備、進場資格、模擬進場、模擬再進場、持倉管理、停損、出場、結構升／降級／失效、候選失效及條件不變：

- 每一輪內部仍完整判讀大小結構、定錨、因果樞紐、道氏防線、號盤、四象限、太極、一之戰法、X流程、箱型／左右、主要與備用情境、雙向預案及交易憲法。
- 本次不執行進一步縮短TG訊息的建議，沿用既有事件卡內容。`cclass_mode`保留在內部稽核資料，但TG不顯示「C班」字樣，只顯示太極與一之摘要；X流程另列目前步驟，其他非主控方法仍依`focus_methods`決定是否展開。
- 訊息分別標示`大錨`、`小錨`、`目前工作段`與`反向候選`，並顯示起訖時間、價位、幅度、歷時與狀態。工作段可以逐輪更新，但不能冒充背景錨；反向候選完成接管前也不能取代大錨。
- 大級背景象限與小級工作象限分開，兩級各自保存趨勢／波動兩軸，且必須先從作用中錨判斷目前是推進或修正；可同時表達「大級空方Q4修正、小級反彈形成中」，並列出下一步較可能發展成Q1延續或Q2擴張的候選。`TRANSITION`不能取代主要象限候選。訊息使用`Q1`～`Q4`、`1分K`、`15分`、`21MA`、`105MA`、`ATR14`及`3～5根`等盤中簡稱；TG中的指標與價位小數會取整為盤中易讀點數。錨點、道氏防線、結構事件與可追溯的觸發／檢查點優先顯示為`09:25低44,957點`或`09:10高45,154點`；找不到確定來源時只顯示價位，不猜測K棒時間。
- 多方與空方setup可同時存在。情境權重代表當下條件分配，不是經回測的統計勝率；多方、盤整、空方合計固定100%。
- 障礙分為觸發線、順勢檢查點及硬目標。Q4／Q1沿交易方向的已知前高、前低、開盤區間及夜盤邊界先作檢查點，不會只因第一檢查點不足1.5R就機械否決進場；到達後再依突破、停滯或收回觸發區管理。
- 同一setup停損後最多再進場一次，至少等待1根完整1分K；`active_setup_key`、`last_stop_time`及`reentry_count`以程式持倉為唯一權威，準備、進場、停損與再進場使用不同事件卡，避免把`ARMED`誤認為持倉或把新初始單誤當成已用過再進權。
- 每個可執行setup保存客觀`trigger_level`、`trigger_operator`及`valid_bars`。程式在後續已收盤K完成條件時產生`ENTRY_ELIGIBLE`；AI只能接受`ENTER`，或用結構停損過寬、硬障礙太近、級數衝突、交易憲法禁止其中一項固定原因否決，不能再用「繼續等待」。
- 接受`ENTER`只會建立待成交事件：例如09:37收盤觸發，程式在09:38第一個可成交open建立模擬倉並另外發進場卡。超過有效根數或第一個可成交價已越過停損時取消，不追認成交。
- 客觀事件保存發生時間、有效根數、到期時間、是否已分析及消費時間。稀疏抽樣看到的舊事件若已過期，只作歷史狀態，不再冒充當下準備訊號。
- 空手setup進入`ARMED`或確認待成交狀態時固定改用準備卡；一般觀察卡不能承載已待觸發的交易條件。
- 不顯示未收盤即時K、逐根OHLC／成交量、補處理說明或`kind=`、`bar_time=`、`price=`等內部參數。
- `DONT_NOTIFY`安靜狀態預設最多每10分鐘發一則；任何真正的`NOTIFY`、進場、管理、停損或出場事件不受節流影響。

雙層契約固定使用事件卡顯示。若要逐字比較舊版正式九欄與完整state v6，需把`rule_manifest_path`改回`formal-v2.1.8-replay-adapter-v3`，並把`message_profile`設為`formal_nine_sections`；這只切換回放實驗模式，不變更正式規則。

每個run另外保存：

- `deterministic-evidence-ledger.json`：最新程式證據帳本。
- `anchor-lifecycle-state.json`：本輪背景大錨、子級小錨、目前工作段、反向候選及大錨防線狀態。
- `deterministic-evidence-events.jsonl`：樞紐、段、防線、結構升降級／失效與假突破收復的實際首次出現時間。
- `deterministic-event-state.json`：事件的`ACTIVE／ANALYZED／EXPIRED`、有效根數、到期及消費時間。
- `execution-events.jsonl`：進場資格後的下一根成交、過期或跳空越過停損等程式執行事件。
- `deterministic-timeline.json`：事件驅動run的完整因果事件時間表與實際選取的AI分析時間；固定頻率run不建立此檔。
- `replay-position-state.json`：程式管理的隔離模擬持倉。
- `replay-memory.json`：AI保存最多3套作用中setup、大小級控制狀態、象限與看法維持／降級／翻向條件。
- `analysis/*-state-checkpoint.json`：可安全回退的逐點雙層狀態。

這些檔案只存在`.runtime/trade_monitor_replay/runs/<run_id>/`，不會讀寫正式`.runtime/trade_monitor/`。

## 限制

- 第一版固定TMF近月；不宣稱等同TX。
- 1分K無法判定同一分鐘內高低點的先後；保護停損以該根OHLC是否穿越停損價判定，跳空越過時以可觀測開盤價成交。唯讀績效報告會計算程式進場、程式停損及AI收盤出場，但不模擬委託排隊、成交延遲或同根內目標與停損先後。
- 程式固定可客觀重現的價格事實、定錨父代、雙向道氏與象限起算關係；太極複製品質、一之、左右、唯一主控戰法、情境權重及最後的進出場裁量仍由AI判讀。
- v23當前課程主鏈不再使用120／30點分級。舊版重現分支仍以`LEGACY_*`保留，不能作課程依據。假突破2根／ATR篩選、1～10根等待窗等仍屬實驗限制，不是老師通用規則；全套課程的缺口與保留理由見稽核文件。
- Telegram網路逾時可能無法確認訊息是否已送達，去重是best effort。
- 規則修改後必須建立新版本並從前一夜重新跑，不能接續舊狀態。
