# AI-HYBRID v34 LONG_ONLY Q2＋Q4

此版本由v33 long-only adapter延伸，仍引用正式v2.1.8 prompt、schema及v32基礎rubric，並以獨立追加規則縮小第一階段可成交範圍。

- 市場診斷仍完整且雙向。
- 交易方向仍只做多。
- 可成交setup只允許多方Q2與多方Q4。
- Q2市場分類與Q2假跌破／反向候選失敗設置分開。
- Q2、Q4成交事件保留不同`entry_strategy`，供後續分組績效使用。
- v34使用專屬輸出schema；`main_strategy_family`固定為`NONE/Q2/Q4`，並與進場策略家族分離保存 supporting methods。
- `ai_course_assessment`為必填但可為`null`；帶`facts_hash`的新Q2／Q4候選必須完成固定10項`PASS／FAIL／UNKNOWN`課程檢查，任一必要項不明或失敗均不得進場。
- 正式監控、Telegram Bot及既有排程均不在本版本範圍內。

v33及更早版本保持唯讀；任何v34結果不得與v171或其他執行版本混成同一份績效。
