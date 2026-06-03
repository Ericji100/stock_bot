# AI 投研中心

AI 投研中心位於 `research_center/`，負責個股研究、宏觀研究、題材研究、題材雷達、價值重估、新聞整理、報告輸出與來源保存。

## 模型

| 模型 | 設定 | 用途 |
|---|---|---|
| Gemini | `gemini_api_key` 或 `google_api_key` | 預設投研、grounding、fallback |
| DeepSeek V4 Pro | `opencode_api_key` | 推理、摘要、指定 `--model deepseek` |
| MiniMax M3 | `minimax_api_key` | 長文整理、搜尋、比較報告 |
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

### `/value_scan` 搜尋任務

`/value_scan` 會依 AI 候選股批次產生搜尋 query，每批最多 4 檔，避免一次塞入太多股票造成搜尋失焦。主要任務如下：

| 任務 | 用途 |
|---|---|
| 官方公告與月營收 | 查 MOPS、重大訊息、月營收、毛利率、EPS、法說會與 IR，確認重估是否有硬資料支撐。 |
| 產品客戶與供應鏈驗證 | 查新產品、新客戶、訂單、供應鏈位置與營收貢獻，避免只靠題材名稱推論。 |
| 舊標籤與新標籤重估 | 查價值重估、市場重估、轉型、新應用、法人報告與重新定價線索。 |
| 法人籌碼與資金確認 | 查外資、投信、自營商、TDCC、融資券與大戶持股，確認資金是否支持重估。 |
| 反證與重估失敗風險 | 查營收未達預期、庫存、毛利下滑、客戶集中、展望下修與訂單遞延。 |

每個任務會標示 `evidence_role`，例如「支持重估」、「支持反證」、「只作情緒」或「資料不足」，供後續搜尋整理與報告生成判斷來源用途。

搜尋整理 prompt 會要求每個 finding 盡量標示 `evidence_usage`：

| evidence_usage | 用途 |
|---|---|
| `supports_rerating` | 可支持價值重估，但需有官方、主流媒體、產業來源或本地結構化資料支撐。 |
| `supports_counter_evidence` | 可作降分或保守判斷的反證，例如營收未跟上、毛利下滑、庫存、客戶集中或展望下修。 |
| `sentiment_only` | 只能作市場情緒或待驗證線索，例如新聞熱度、論壇討論、股價波動或法人短評。 |
| `insufficient` | 資料不足、只有 snippet、日期不可驗證、缺少官方或正文來源。 |

若只有情緒或 snippet，`evidence_level` 不得標示為 `strong`；若缺少官方、財報、月營收、法說會、產品客戶或供應鏈資料，必須寫入 `missing_data`。

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
| `prompt/radar/` | Radar AI 短評 JSON prompt |
| `prompt/news/` | 新聞摘要 |
| `prompt/topic/` | 題材庫維護與外部來源萃取 |
| `prompt/discovery/` | 搜尋任務生成 |
| `prompt/workflow/` | AI 工作流內部 prompt，例如低階模型資料整理 |
| `prompt/rules/` | 量化、籌碼、技術、來源、風險、歷史日期等規則 |
| `prompt/scoring/` | 股票量化評分、標籤重估模型與拆分後評分規則 |

`prompt/scoring/` 目前保留舊版長檔，並新增拆分後規則：

- `financial_hard_metrics.md`：財務、營收、毛利、EPS、現金流、存貨等硬指標。
- `theme_soft_metrics.md`：題材、產品、客戶、供應鏈、新聞可信度。
- `high_growth_gene.md`：飆股基因、量價、籌碼、成長轉折。
- `final_research_score.md`：研究優先度與風險報酬評估。
- `rerating_model.md`：舊標籤、新標籤、重估證據與蹭題材風險。

載入策略由 `research_center/prompt_registry.py::_scoring_rules_for_request()` 控制：

- `/research --score`、`/research --deep` 載入完整拆分規則。
- `/value_scan` 一般模式載入標籤重估、題材軟指標與財務硬指標。
- `/value_scan --deep` 額外載入飆股基因規則。

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
## 嵌入式市場想像力

AI 投研不只整理公開資訊，還要在事實邊界內推演市場可能交易的故事。所有報告型 AI 指令會載入 `prompt/rules/embedded_market_imagination_rules.md`，但 `--source-only` 與 source-only 模式不載入。

適用指令：

- `/research`、`/research --score`、`/research --deep`
- `/value_scan`
- `/macro`
- `/theme`
- `/theme_radar`
- `/theme_flow`
- `/sector_strength`

獨立 AI prompt 也有相同精神：

- `prompt/radar/radar_ai_comment.md`：Radar AI 短評需包含市場可能買單故事、爆發前兆、待驗證訊號與失敗條件。
- `prompt/news/news_summary.md`：新聞分類需判斷哪些新聞可能發酵成題材，哪些只是資訊或情緒。
- `prompt/topic/topic_maintain.md`：題材維護可提出候選題材與候選公司，但必須維持 `verified`、`inferred`、`candidate`、`missing` 邊界。

寫法限制：

1. 市場想像必須嵌入原本章節，不取代原本報告結構。
2. 評分仍依原本財務硬指標、題材軟指標、飆股基因、價值重估與最終研究用買入評分規則。
3. 想像要標示待驗證，不能把推論寫成事實。
4. 需要同時寫爆發條件、驗證訊號與失敗條件。
5. 技術面、籌碼面、營收面、新聞面、產業面、趨勢面、題材面都應盡量掃描。
