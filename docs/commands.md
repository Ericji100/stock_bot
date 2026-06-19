# Telegram 指令速查

本文件整理目前 `main.py` 與 `research_center/telegram_handlers.py` 註冊的 Telegram 指令。若新增或移除指令，請同步更新本文件與 `/help` 文字。

## 模型參數

AI 指令通常支援：

```text
--model gemini
--model deepseek
--model minimax
```

- `gemini`：預設投研模型，可搭配 grounding。
- `deepseek`：透過 OpenCode Go，適合推理與摘要。
- `minimax`：適合長文搜尋整理與比較報告。

## 選股與雷達

| 指令 | 用途 |
|---|---|
| `/scan` | 開啟選股掃描選單 |
| `/scan 2026-05-22` | 指定日期掃描 |
| `/radar` | 今日選股雷達 |
| `/radar --model deepseek` | 指定模型產生 Radar 短評 |
| `/radar --no-ai-comment` | 不產生 AI 短評 |
| `/radar_more` | 查看最近一次 Radar 完整名單 |
| `/radar_more 2026-05-22` | 查看指定日期 Radar |

## 個股與價值分析

| 指令 | 用途 |
|---|---|
| `/research` | 互動式個股研究 |
| `/research 2330` | 直接深度研究個股，等同 `/research 2330 --deep` |
| `/research 2330 --score` | 個股評分研究 |
| `/research 2330 --deep` | 深度個股研究 |
| `/research 2330 --date 2026-05-22` | 指定日期研究 |
| `/research 2330 --model minimax` | 指定模型 |
| `/value_scan` | 開啟價值重估掃描選單 |
| `/value_scan 精選選股` | 掃描精選選股 |
| `/value_scan 我的持股` | 掃描持股 |
| `/value_scan 2330` | 單股價值重估 |
| `/value_scan 精選選股 --top 30 --model deepseek` | 指定數量與模型 |

## 市場與新聞

| 指令 | 用途 |
|---|---|
| `/news` | 新聞選單 |
| `/news latest` | 最新新聞 |
| `/news 7d` | 近 7 天新聞 |
| `/news refresh --model deepseek` | 更新新聞庫 |
| `/news_detail` | 查看新聞細節 |
| `/news_save` | 保存新聞 URL |
| `/macro` | 宏觀研究選單 |
| `/macro 台股` | 台股宏觀研究 |
| `/macro 全球 AI --deep --model minimax` | 指定主題、模式與模型 |
| `/morning` | 晨報 |
| `/noon` | 午報 |
| `/tw_market` | 台股午報 |

## 題材與族群

| 指令 | 用途 |
|---|---|
| `/theme` | 題材研究選單 |
| `/theme AI伺服器` | 題材研究 |
| `/theme AI伺服器 --deep --top 20` | 深度題材研究 |
| `/theme_radar` | 市場題材雷達，互動選日期與模型 |
| `/theme_radar --days 7` | 近 7 天題材統計 |
| `/theme_radar --date 2026-05-22 --model minimax` | 指定日期與模型 |
| `/theme_flow AI伺服器` | 題材擴散路徑 |
| `/theme_flow AI伺服器 --date 2026-05-22 --model deepseek` | 指定日期與模型 |
| `/sector_strength` | 族群強弱排行 |
| `/sector_strength --date 2026-05-22 --model deepseek` | 指定日期與模型 |

## 題材庫維護

| 指令 | 用途 |
|---|---|
| `/topic_maintain` | 完整維護題材庫，互動選模型 |
| `/topic_maintain --bootstrap` | 補足既有題材庫缺欄位 |
| `/topic_maintain --bootstrap --model minimax` | 指定模型補題材庫 |
| `/topic_seed_prompt` | 產生外部高階 AI 題材庫提示詞 |
| `/topic_import` | 匯入外部 AI JSON，本地轉成變更包，不呼叫 AI |
| `/topic_source_sync` | 同步 TPEx/UDN 外部來源並套用正式題材庫 |
| `/topic_source_sync --tpex` | 只同步 TPEx 產業鏈 |
| `/topic_source_sync --udn` | 只同步 UDN 產業資料庫 |
| `/topic_review` | 查看所有變更包 |
| `/topic_review change_xxx` | 查看指定變更包 |
| `/topic_confirm change_xxx` | 套用變更包 |
| `/topic_reject change_xxx` | 拒絕變更包 |
| `/topic_profiles` | 查看正式題材庫 |
| `/topic_reset --confirm` | 備份後清空題材庫 |

## 持股與監控

| 指令 | 用途 |
|---|---|
| `/my` | 查看我的持股 |
| `/in 2330` | 加入持股 |
| `/out 2330` | 移除持股 |
| `/list_m` | 查看監控清單 |
| `/add_m 2330` | 加入監控 |
| `/del_m 2330` | 移除監控 |
| `/check` | 執行監控掃描 |

## 資料回補與匯出

| 指令 | 用途 |
|---|---|
| `/backfill` | 回補本地資料 |
| `/backfill 2026-05-22` | 指定日期回補 |
| `/backfill 2026-05-22 force` | 強制回補 |
| `/data_status 2330` | 查詢個股 Feature Pack / 資料覆蓋狀態 |
| `/backfill_status` | 查詢最近回補 marker 與快取健康度 |
| `/news_status 2330` | 查詢新聞庫保存狀態 |
| `/export 2330` | 匯出股票資料 |
| `/stock_chart 2330 2026-01-01 2026-05-01 1d` | 匯出個股圖表 |
| `/tmf_chart 2026-05-01 2026-05-05 1m` | 匯出 TMF 圖表 |

## 報告與系統

| 指令 | 用途 |
|---|---|
| `/report` | 查看最近報告清單 |
| `/report latest` | 查看最近一份報告 |
| `/report 2330 latest` | 查看個股最近報告 |
| `/report theme AI伺服器 latest` | 查看題材最近報告 |
| `/help` | 完整指令說明 |
| `/ai_help` | `/help` 的相容別名 |
| `/start` | 常用入口 |
| `/stop` | 停止目前任務 |
