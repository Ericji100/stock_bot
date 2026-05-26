# AI 投研中心

AI 投研中心位於 `research_center/`，負責個股研究、宏觀研究、題材研究、題材雷達、價值重估、新聞整理、報告輸出與來源保存。

## 模型

| 模型 | 設定 | 用途 |
|---|---|---|
| Gemini | `gemini_api_key` 或 `google_api_key` | 預設投研、grounding、fallback |
| DeepSeek V4 Pro | `opencode_api_key` | 推理、摘要、指定 `--model deepseek` |
| MiniMax M2.7 | `minimax_api_key` | 長文整理、搜尋、比較報告 |
| Tavily | `tavily_api_key` | Search / Extract 外部搜尋與正文抓取 |

公開設定在 `config/research_center.json`；金鑰放 `config/secrets.json`。

## 常用 AI 指令

```text
/research 2330 --deep --model gemini
/research 2330 --score --model deepseek
/value_scan 精選選股 --top 30 --model minimax
/theme AI伺服器 --deep --model minimax
/theme_radar --days 7 --model deepseek
/news refresh --model deepseek
```

## 搜尋順序

投研任務會先組裝本地資料，再依資料缺口與設定決定是否補外部來源。

```text
本地快取 / 資料庫 / 公司知識庫
→ MiniMax MCP Search
→ Tavily Search
→ Gemini Search fallback
→ Tavily Extract / WebFetch
→ AI 報告
```

實際是否執行搜尋受 `config/research_center.json` 控制，例如：

```json
{
  "enable_minimax_search": true,
  "enable_tavily_search": true,
  "enable_tavily_extract": true,
  "gemini_search_mode": "fallback",
  "tavily_monthly_credit_limit": 1000,
  "tavily_credit_reserve": 20
}
```

## 成本與外部呼叫

| 指令 | AI | 外部搜尋 | 注意事項 |
|---|---|---|---|
| `/scan` | 否 | 否 | 本地量化掃描 |
| `/radar` | 可選 | 通常否 | AI 短評可用 `--no-ai-comment` 關閉 |
| `/news latest` | 否 | 否 | 讀本地新聞庫 |
| `/news refresh` | 是 | 是 | 會消耗模型與搜尋額度 |
| `/research` | 是 | 通常是 | 深度與評分模式來源需求較高 |
| `/value_scan` | 是 | 通常是 | 需要候選股來源、反證與資料缺口 |
| `/theme_radar` | 是 | 視情況 | 主要依本地新聞與掃描資料，可補模型摘要 |
| `/topic_maintain` | 是 | 視模型與資料 | 產生變更包，正式寫入仍需確認 |
| `/topic_import` | 否 | 否 | 本地匯入外部 JSON |
| `/report latest` | 否 | 否 | 讀本地報告 |

## Prompt 結構

| 目錄 | 內容 |
|---|---|
| `prompt/base/` | 通用基礎 Prompt |
| `prompt/report/` | 個股、宏觀、題材、價值重估、Telegram 摘要 |
| `prompt/news/` | 新聞摘要 |
| `prompt/topic/` | 題材庫維護與外部來源萃取 |
| `prompt/discovery/` | 搜尋任務生成 |
| `prompt/rules/` | 量化、籌碼、技術、來源、風險、歷史日期等規則 |
| `prompt/scoring/` | 股票量化評分與標籤重估模型 |

## 必守規則

- AI 最終評分不得直接照抄本地量化底稿。
- 本地分數是材料，不是結論；AI 要說明來源、反證與資料缺口。
- `/value_scan` 不得只因熱門題材給高分，必須結合營收、財報、估值、籌碼、TDCC 與催化驗證。
- 歷史日期模式必須過濾未來資料；可使用已保存 snapshot，但不得用現在網路搜尋改寫過去判斷。
- `/source-only` 類型任務應只整理來源，不應額外跑 AI 分析。
- 題材報告必須區分已驗證代表股、推論型代表股、待驗證候選股、疑似蹭題材。

## 報告輸出

預設輸出：

```text
reports/{report_type}/{target}/{report_id}.md
reports/{report_type}/{target}/{report_id}.html
reports/{report_type}/{target}/{report_id}.json
```

metadata 與來源快照保存在：

```text
database/stock_research.db
```

報告查詢可用：

```text
/report
/report latest
/report 2330 latest
/report theme AI伺服器 latest
```
