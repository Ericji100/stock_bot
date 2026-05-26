# 資料來源、快取與回補

系統以本地快取為優先，外部 API 與搜尋作為補強。大量掃描與 Radar 應盡量讀 `.cache/`，AI 投研再根據資料缺口補外部來源。

## 資料來源

| 資料 | 主要來源 | 位置 | 備註 |
|---|---|---|---|
| 股票清單 | TWSE、TPEx、`stock_list.json` | 專案根目錄 | 股票名稱與代號解析 |
| 股價 | TWSE、TPEx、Yahoo、Fugle | `.cache/` | 技術指標與掃描使用 |
| 月營收 | MOPS / 公開來源 | `.cache/` | 財報選股與 AI 底稿 |
| 財報 | MOPS / 公開來源 | `.cache/` | 毛利率、估值、財務細項 |
| 法人籌碼 | TWSE、TPEx、FinMind、Fugle | `.cache/chip_daily` | 缺口可用 FinMind 備援 |
| 大戶分布 | TDCC | `.cache/tdcc` | 價值重估與籌碼集中度 |
| 新聞 | MiniMax、Tavily、Gemini、手動 URL | `database/stock_research.db` | 本地新聞庫與題材雷達 |
| 題材資料 | TPEx、UDN、人工與 AI 變更包 | `config/`、`data/theme/` | 需要 review/confirm |
| AI 來源快照 | 外部搜尋與本地資料 | `database/stock_research.db` | 支援報告追溯 |

## 快取原則

- `/scan`、`/radar`、`/radar_more` 主要依本地快取與近期掃描結果。
- `/research`、`/value_scan` 會先讀本地 structured cache，再視資料缺口補外部來源。
- `/theme_radar` 主要使用本地新聞庫、近期掃描、題材庫與市場資料。
- 歷史日期模式只能使用該日期以前資料或保存過的來源快照。
- 回補 marker 用於避免重複打外部來源。

## 常用位置

| 路徑 | 用途 |
|---|---|
| `.cache/backfill/` | 回補完成 marker 與健康狀態 |
| `.cache/chip_daily/` | 法人日資料快取 |
| `.cache/tdcc/` | TDCC 集保資料 |
| `.cache/research_structured/` | 個股研究結構化快取 |
| `.cache/recent_scan_results.json` | 最近掃描結果 |
| `.cache/curated_scan_summary.json` | 精選掃描摘要 |
| `.cache/revenue_growth_candidates.json` | 營收成長候選 |
| `.cache/chip_hot_candidates.json` | 籌碼強勢候選 |
| `.cache/rerating_candidates.json` | 價值重估候選 |
| `database/stock_research.db` | 報告、新聞、來源與事件 metadata |

## 回補指令

```text
/backfill
/backfill 2026-05-22
/backfill 2026-05-22 force
/backfill_status
/data_status 2330
/news_status 2330
```

`force` 會忽略部分已有快取判斷，適合資料異常或需要重建時使用。

## 自動排程

| 時間 | 任務 |
|---|---|
| 16:30 | 籌碼快取今日回補 |
| 18:30 | 籌碼快取今日回補 |
| 21:00 | 籌碼快取完整回補 |
| 每 2 小時 | 完整資料定時回補健康檢查 |

## 資料缺口處理

AI 投研遇到資料不足時，應清楚標示：

- 缺少哪一類資料。
- 是否使用 fallback。
- fallback 來源與限制。
- 該缺口對評分或結論的影響。

例如 TDCC 缺漏時，不應把籌碼集中度當成強訊號；官方估值資料缺漏時，估值維度應偏中性或降低信心。
