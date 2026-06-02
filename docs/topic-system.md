# 題材庫與新聞系統

題材系統包含正式題材庫、外部來源同步、AI 變更包、新聞庫與題材雷達。核心原則是「AI 可提出草稿，但正式資料需人工確認」。

## 主要檔案

| 路徑 | 用途 |
|---|---|
| `config/theme_profiles.json` | 正式題材 profile |
| `config/company_theme_map.json` | 公司與題材對應 |
| `config/supply_chain_nodes.json` | 供應鏈節點 |
| `config/company_knowledge.json` | 公司知識庫 |
| `config/tpex_industry_chain.json` | TPEx 產業鏈同步快取 |
| `config/udn_industry_topics.json` | UDN 產業資料庫同步快取 |
| `data/theme/dynamic_theme_cache.json` | 動態題材快取 |
| `prompt/topic/` | 題材維護與來源萃取 Prompt |

## 題材維護流程

```text
/topic_maintain
→ AI 產生 change pack
→ /topic_review
→ 人工檢查
→ /topic_confirm change_xxx 或 /topic_reject change_xxx
```

外部高階 AI 也可先生成 JSON，再用 `/topic_import` 匯入成本地變更包。

## 常用指令

| 指令 | 用途 |
|---|---|
| `/topic_maintain` | 完整維護題材庫 |
| `/topic_maintain --bootstrap` | 補齊既有題材缺欄位 |
| `/topic_seed_prompt` | 產生外部 AI 題材庫提示詞 |
| `/topic_import` | 匯入外部 AI JSON |
| `/topic_source_sync --tpex` | 同步 TPEx 產業鏈 |
| `/topic_source_sync --udn` | 同步 UDN 產業資料庫 |
| `/topic_review` | 查看變更包 |
| `/topic_confirm change_xxx` | 套用變更包 |
| `/topic_reject change_xxx` | 拒絕變更包 |
| `/topic_profiles` | 查看正式題材庫 |
| `/topic_reset --confirm` | 備份後清空題材庫 |

## 題材命名與分類規則

報告不得把所有價格強勢股都寫成題材代表股。必須分成：

| 類別 | 定義 |
|---|---|
| 已驗證代表股 | 有官方、產品、營收、法說、供應鏈或可信來源支持 |
| 推論型代表股 | 有合理產業鏈關聯，但證據仍不足 |
| 待驗證候選股 | 價格、量能或新聞熱度強，但尚未驗證題材連結 |
| 疑似蹭題材 | 名稱、概念或市場傳言相近，但缺乏實質證據 |

`/theme_radar` 的 `representative_stocks` 只放 verified、inferred、direct_map；`candidate_stocks` 不能在報告中稱為代表股。

## 記憶體題材特別規則

記憶體題材不得只靠「半導體業」產業別命中。需要具備 DRAM、NAND、NOR、HBM、SSD、記憶體模組、記憶體控制晶片或儲存控制晶片等產品或證據。

## 新聞系統

| 指令 | 行為 |
|---|---|
| `/news latest` | 讀本地新聞庫，不外部搜尋 |
| `/news 7d` | 讀近 7 天本地新聞 |
| `/news refresh --model deepseek` | 外部搜尋、整理並寫入新聞庫 |
| `/news_save` | 保存使用者提供的新聞 URL |
| `/news_detail` | 查看新聞細節 |
| `/news_status 2330` | 查詢個股新聞保存狀態 |

`/news latest` 與 `/news 7d` 的時間判斷規則：

- 優先使用來源提供的 `published_at`。
- 若 `published_at` 有值但不在時間窗內，該新聞不顯示。
- 若 `published_at` 空白，才使用新聞庫 `created_at` 作為 fallback。
- `latest` 時間窗為最近 24 小時；`7d` 時間窗為最近 7 天。

新聞庫與題材雷達互相支援：新聞提供催化與風險，題材庫提供結構化分類與代表股邏輯。
## 新聞來源與推送規則

- 新聞庫以 `news_origin` 區分來源：`refresh` 為 `/news refresh` 與排程新聞整理；`manual` 為使用者貼上的新聞；`research` 為調研指令保存的搜尋來源。
- `/news latest`、`/news 7d` 與每日推送只顯示 `refresh` 來源。`manual` 與 `research` 仍會保存並供調研脈絡使用，但不主動推送。
- 顯示日期優先使用 `published_at`。只有 `refresh` 來源且 `published_at` 空白時，才可用 `created_at` 輔助判斷；其他來源不能只因今天入庫就變成今日新聞。
- 使用者偏好只在通過來源、日期、台股財經與非文章頁過濾後，對合格新聞做小幅排序加權。
- AI 分類逾時時會縮短 payload 重試；若仍逾時，剩餘新聞改用本地分類，避免整輪新聞整理卡住。
