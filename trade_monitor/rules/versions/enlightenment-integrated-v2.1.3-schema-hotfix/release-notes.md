# v2.1.3 Structured Outputs schema hotfix

本版本完整承接 `enlightenment-integrated-v2.1.3-type-terms`，只修正 Codex API 的結構化輸出相容性：

- 移除 `supporting_methods` 陣列上的 `uniqueItems`。目前 Codex Structured Outputs 不接受這個 JSON Schema 關鍵字。
- 相同的去重與生命週期限制仍由 Python contract／adapter 驗證，不降低交易規則約束。
- 本機純圖表擷取器增加 Edge 遠端桌面視窗相容辨識；不控制商品、週期、指標、下單或帳務區。

本版沒有修改分析提示詞、趨勢、定錨、樞紐、防線、太極、四象限、進出場、交易憲法、九欄格式或 Telegram canonical message。
