# v1.7 anchor integration RC shadow

- 新增內部 `anchor_context`，保存最多兩個工作級數的錨歷史、作用中大小錨、控制級數與因果時間。
- 定錨狀態固定為 `FORMING → CONFIRMED → INVALIDATED／REPLACED`；錨識別、起點與首次看見時間不可重寫。
- Type 1／2／3、左右成熟度、道氏防線與號盤直接映射至反向定錨，不建立平行策略。
- 象限改為從作用中錨之後的波段動態比較，保存趨勢增減、波動擴縮、工作象限、主要／次要候選及刪除理由。
- 大小級衝突只決定背景與進場時機的控制權，使用者仍只收到一套操作情境。
- 第二象限較慢的外圍擴張失敗，以原有「假突破／假跌破反轉」右左分支處理，不增加第五種型態。
- schema 升為 v5、`market_structure_state` 升為 v3；v2 只在 RC 記憶中升級，分析驗證成功後才原子寫入。
- 正式 v1.6 prompt、scheduler config、Automation、Telegram、active-version 與正式 runtime 均不切換。
