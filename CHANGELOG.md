# Changelog

本檔只保留高層級變更摘要。原始 README 中的大量日期型更新、Prompt 備忘、功能補強與修正脈絡，已完整保留於 [docs/legacy-readme-full.md](docs/legacy-readme-full.md)。

維護規則：未來任何功能、指令、資料流程、AI 行為、設定、排程或重要修正的改動，都必須同步檢查並補充 `README.md` 與本 `CHANGELOG.md`。

## 2026-10-21

- 新增 0050 final-v1.7 ETF 影子監控整合，沿用既有 Telegram `Application`、token、JobQueue、`main.py` 與單一 polling process。
- 新增獨立且預設關閉的 `config/etf_monitor.json`；ETF 路由固定 `-5527055845` 並以 `getChat` fail-closed 驗證，不變更任何舊 `chat_id` 或 `trade_monitor_chat_id`。
- 新增 12:45 預覽、13:55 `BUY_NEXT_OPEN` / `SELL_NEXT_OPEN` / `HOLD` 決策與 22:00 完整資料核對；三個工作使用獨立 timeout/lock，不進入舊定時任務序列。
- 監控核心直接轉接 accepted final-v1.7 engine，共用 hash 驗證的 ETF snapshot registry、原始未調整執行價與企業行動技術座標，不使用 Yahoo 或舊版 long-term monitor。
- 新增 SQLite 狀態、pending action、企業行動、事件/outbox、Telegram message ID、更正與重啟恢復；更正採 append-only，不覆寫歷史事件。
- 新增 accepted artifact replay、核心 parity、路由、日曆/陳舊資料、狀態、冪等、重送、重啟、migration、concurrency、單一啟動鏈與功能關閉回歸測試。

## 2026-05-26

- 新增專案維護規則：未來任何重要改動都必須同步更新 `README.md` 與 `CHANGELOG.md`。
- 重構 `README.md`：改為專案入口、快速啟動、常用指令、系統狀態、資料來源、AI 成本與文件索引。
- 新增 `docs/commands.md`：集中 Telegram 指令速查。
- 新增 `docs/architecture.md`：整理模組架構、資料流、設計約束與排程。
- 新增 `docs/ai-research.md`：整理 AI 模型、搜尋順序、Prompt 結構、成本與必守規則。
- 新增 `docs/data-sources.md`：整理資料來源、快取、回補與資料缺口處理。
- 新增 `docs/topic-system.md`：整理題材庫、新聞系統、代表股分類與維護流程。
- 新增 `docs/operations.md`：整理啟動、設定、排程、報告與日常維運。
- 新增 `docs/testing.md`：整理測試與驗收指令。
- 保留原始 README 全文於 `docs/legacy-readme-full.md`，避免知識流失。

## 歷史內容

完整歷史紀錄請先查：

```text
docs/legacy-readme-full.md
```

若後續要繼續精煉，可再把 legacy 文件中的日期型紀錄逐批搬入本檔。
