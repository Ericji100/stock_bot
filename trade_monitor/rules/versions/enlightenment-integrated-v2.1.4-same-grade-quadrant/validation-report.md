# v2.1.4 工作象限同級證據修正驗證

- 監控測試：256 passed。
- 版本雜湊：execution、analysis、schema 與 scheduler config 均由版本庫鎖定。
- 回歸條件：`trend_dynamics=INCREASING` 時，工作象限及主要候選不得為第二／第三象限；波動軸採鏡像約束。
- 隔離 Shadow：使用正式監控保留的細節圖與先前因果狀態重判，結果為 `SMALL／ONLY_SMALL`、趨勢增加、波動收斂、工作象限第四象限、主要候選第四象限、次要候選第一象限。
- 傳輸隔離：Shadow 未寫入正式狀態、未建立模擬成交、未發送 Telegram。
- Automation：同一 ID `1-k`、同一每分鐘 RRULE、同一 target task，更新後維持 `ACTIVE`。
- 限制：本修正只處理象限證據級數一致性，不代表獲利保證，也未補上結構化 OHLCV。
