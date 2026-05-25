# MiniMax Token Plan MCP web_search 驗證腳本

## 用途

驗證 MiniMax Token Plan MCP 的 `web_search` 是否能在本機正常呼叫。

此腳本**不會接入正式投研流程**，僅供一次性驗證使用。

## 兩種模式

### tools_only 模式（不消耗 credits）

```powershell
python tools/minimax_mcp_verify.py
```

- 啟動 MCP server
- 呼叫 `list_tools()` 確認 `web_search` 存在
- **不**呼叫 `web_search`，不消耗搜尋 credits

### web_search 模式（消耗 credits）

```powershell
python tools/minimax_mcp_verify.py --search
python tools/minimax_mcp_verify.py --search --query "台積電 2026 法說會"
```

- 啟動 MCP server
- 呼叫 `list_tools()` 確認 `web_search` 存在
- **會**呼叫 `web_search` 一次，消耗 MiniMax Token Plan credits
- 預設 query：`台積電 2026 法說會`

## 執行需求

- Python 3.10+
- `mcp` Python 套件：`pip install mcp`
- `uv` 套件（提供 `uvx`）：`pip install uv`
- MiniMax Token Plan API Key（具備 web_search credit 權限）

## API Host

國際站 MiniMax API：`https://api.minimax.io`

## 環境變數（可選）

若 `uvx` / `uv` 不在系統 PATH，可指定絕對路徑：

```powershell
$env:UVX_EXE_PATH="C:\Path\To\uvx.exe"
$env:UV_EXE_PATH="C:\Path\To\uv.exe"
```

## 輸出

- CMD 顯示工具列表與搜尋結果摘要
- `logs/minimax_mcp_verify/latest_result.json` 包含完整結果（所有模式）
- `logs/minimax_mcp_verify/search_YYYYMMDD_HHMMSS.json` 為 web_search 模式的時間戳存檔

### 檔案保存規則

| 模式 | latest_result.json | search_*.json |
|------|-------------------|---------------|
| tools_only | ✅ 更新 | ❌ 不產生 |
| web_search | ✅ 更新 | ✅ 產生（避免被 tools_only 覆蓋）|

web_search 模式會同時寫入 `latest_result.json` 和 `search_YYYYMMDD_HHMMSS.json`。`latest_result.json` 內含 `archived_result_path` 欄位指向時間戳檔。

JSON 額外欄位：
- `latest_result_path`: latest_result.json 的完整路徑
- `archived_result_path`: 時間戳存檔路徑，tools_only 為 null，web_search 為 `search_YYYYMMDD_HHMMSS.json` 路徑

JSON 欄位：
- `ok`: 整體是否成功
- `mode`: `tools_only` 或 `web_search`
- `query`: 查詢字串
- `tool_found`: web_search 是否存在
- `tools`: 工具列表
- `source_count`: 回傳來源數量
- `url_count`: 有 URL 的來源數量
- `has_snippets`: 是否有摘要（檢查頂層 snippets/summaries，也檢查每筆 source 的 snippet/summary）
- `has_related_queries`: 是否有相關建議
- `normalized_sources`: 前 10 筆標準化來源（title, url, snippet, published_date）
- `related_queries`: 相關建議列表
- `raw_text_preview`: 原始回傳（最多 2000 字）
- `raw_result`: 完整原始回傳
- `latest_result_path`: 最新結果路徑
- `archived_result_path`: 時間戳存檔路徑，tools_only 為 null
- `error`: 錯誤訊息

### 離線驗證

可用 `--normalize-file` 對時間戳存檔重新執行 normalize，不需啟動 MCP server：

```powershell
python tools/minimax_mcp_verify.py --normalize-file logs/minimax_mcp_verify/search_20260516_123456.json
```

## 可刪除檔案

驗證完成後可刪除以下檔案：

- `tools/minimax_mcp_verify.py`
- `tools/README_minimax_mcp_verify.md`
- `logs/minimax_mcp_verify/`（整個目錄）

## 注意

- 不要將 API Key 寫入任何檔案
- 此腳本不會接入 `research_center/` 正式流程
- `--search` 模式**會消耗 MiniMax Token Plan credits**，請確認額度充足
- 不會自動重試，避免重複消耗 credits
- 若 `uvx not found`，請先安裝 `uv`：`pip install uv`