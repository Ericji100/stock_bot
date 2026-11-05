# 維運手冊

本文件整理啟動、設定、排程、報告位置與常見維護動作。

## 啟動

```bash
python main.py
```

Windows 可執行：

```text
啟動機器人.bat
```

日常只使用 `啟動機器人.bat`，沿用單一 watchdog 與 runner，不從開發 worktree 啟動第二個 Bot。目前交付版本為 OpenAI Luna／Terra xhigh 混合路由：新聞、題材庫及Radar先用Luna搜尋，重要缺口用Terra補查；背景新聞分類與題材整理用Luna，Radar短評與手動最終分析用Terra。第二帳戶優先；明確贈額／分額不足才按原條件使用DeepSeek，費用未知不重送。不啟動舊MiniMax MCP或使用舊Gemini／Tavily金鑰。公開行情與非AI選股照原流程。

## 設定檔

| 檔案 | 用途 |
|---|---|
| `config.json` | Telegram token、chat id、Fugle key、掃描設定、監控清單 |
| `portfolio.json` | 個人持股 |
| `config/research_center.json` | AI 投研公開設定、報告位置、搜尋開關 |
| `config/secrets.json` | AI、搜尋與 API 金鑰 |
| `stock_list.json` | 股票代號與名稱 |

`config/secrets.json` 不應提交到 Git。

OpenAI已核對贈額及有效期登記於 `config/research_center.json` 的 `openai_credit_grants`，不是自動每月續發承諾；兩帳戶金鑰放在私密設定的 `openai_api_keys`。正式帳務使用 `.workdata/ai_budget/openai/openai-credits.sqlite3`，完整保留歷史開發支出與未知預留。不得帶入測試root或建立空帳本規避已消耗贈額。

## 排程

| 時間 | 任務 |
|---|---|
| 啟動後 | 視條件補發晨報 |
| 12:30 | 監控掃描 |
| 13:50 | 台股午報 |
| 17:45 | 持股籌碼推播 |
| 18:00 | 每日新聞背景整理；正常新聞不發 Telegram，異常通知另行去重 |
| 20:30 | 交易日全部選股 |
| 21:30 | 交易日 Radar 推播，15 檔 Terra xhigh 短評；重要搜尋缺口由Terra補查 |
| 週二、五 22:30 | AI 題材庫維護，產生待審核更新包，不自動套用 |
| 16:30、18:30、21:00 | 籌碼快取回補 |
| 每 2 小時 | 完整資料回補健康檢查 |

## 報告位置

| 位置 | 內容 |
|---|---|
| `reports/` | Markdown、HTML、JSON 報告 |
| `database/stock_research.db` | 報告 metadata、新聞、事件、來源快照 |
| `.cache/research_structured/` | AI 投研結構化快取 |

報告查詢：

```text
/report
/report latest
/report 2330 latest
/report theme AI伺服器 latest
```

## 日常檢查

OpenAI每筆有效贈額按背景70%、手動20%、緩衝10%分配，仍受實際剩餘、有效期及單工作US$5限制；背景不能借用手動與緩衝。開發測試獨立記帳、不占正式分額，但真實支出仍消耗贈額。只在明確預檢或額度拒絕時走DeepSeek；未知費用停止該工作，保存成果及原預留並去重通知，不跨模型重送。

DeepSeek備援常態月總額US$10：背景7、手動2、緩衝1；保留2026年11月既有授權的手動追加US$2，之後恢復常態。測試不再混入正式手動分額。混合排程約US$58.45～64.55／月僅為含搜尋工具費的條件試算，不是實際月均值，舊DeepSeek背景6.63美元估算不可套用到OpenAI版本。行情與本地選股不因AI額度不足停用。當前結果與限制統一見 [唯一上線驗收表](hybrid-release-acceptance.md)。

```text
/backfill_status
/data_status 2330
/news_status 2330
/radar_more
/report latest
```

## 常見維護動作

| 情境 | 動作 |
|---|---|
| 掃描資料疑似過期 | `/backfill` |
| 指定日期資料缺漏 | `/backfill 2026-05-22 force` |
| AI 報告來源不足 | 檢查 `config/secrets.json` 與 `config/research_center.json` 搜尋開關 |
| 題材代表股誤判 | 建立或審核 change pack，使用 `/topic_review`、`/topic_confirm` |
| 新聞庫沒有新資料 | `/news refresh --model deepseek` |
| 長任務卡住 | `/stop` |

## 編碼

專案以 UTF-8 儲存中文文件與設定。Windows 終端若出現亂碼，先確認：

```text
chcp 65001
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
```

`啟動機器人.bat` 已包含這些設定。

檢查檔案內容是否混入亂碼 marker：

```text
python -B tools/encoding_health_check.py
```

這個工具只做健檢，不會批次修檔。若終端顯示中文亂碼，先確認上述環境設定；若健檢工具回報非 allowlist 檔案有 UTF-8 decode error 或 mojibake marker，再針對該檔案查來源與入口清理邏輯，不要直接做全專案轉碼或批次移除 BOM。
