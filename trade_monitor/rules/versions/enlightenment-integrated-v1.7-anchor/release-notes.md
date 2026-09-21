# v1.7 anchor integration

- 完成 n=2／n=3 因果回放、隔離 Shadow 與 149 項監控測試後，依使用者明確接受量測延遲的指示提升為正式版本。
- 新增內部 `anchor_context`，保存因果定錨生命週期、作用中大小錨、控制級數與動態象限候選。
- 定錨狀態固定為 `FORMING → CONFIRMED → INVALIDATED／REPLACED`，禁止回填首次辨識時間或改寫已終止歷史。
- Type 1／2／3、左右成熟度、道氏防線與號盤直接映射至反向定錨，不建立平行策略。
- 象限從作用中錨後的趨勢性與波動變化推導；象限只控制既有四型態准入，不單獨觸發進場。
- schema 正式升為 v5、`market_structure_state` 升為 v3；v1.6 的 v2 快照只在 prepare 記憶中升級，完整驗證成功後才原子保存。
- 保留 Chrome DETAIL／OVERVIEW、每分鐘第 05 秒本機觸發、resume guard、九欄 canonical message、Telegram 雙模式與一口單管理。
- 沒有新增第五型態、第十欄、多口加碼或每日最多三次進場限制。
- 隔離 Shadow 的完整分析約需 65～76 秒；使用者已接受可能略過中間已收盤 K 的已知限制，系統不得宣稱交易所等級逐根即時。
