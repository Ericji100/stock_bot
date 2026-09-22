# 專案目錄地圖

本文件是 `D:\code\stock_ai_bot` 的根目錄與主要套件目錄權威索引，集中記錄用途、Git 政策、清理條件及相關文件。新增、搬移或刪除根目錄時，應同步更新本文件。

## 同名目錄說明

```text
D:\code\stock_ai_bot\       Git 專案、工作區與執行根目錄
└─ stock_ai_bot\            可由 Python import 的正式股票 Bot 套件
```

外層名稱是本機資料夾名稱，可以包含 Git、文件、測試、設定和執行產物；內層有 `__init__.py`，提供 `stock_ai_bot.*` import namespace。這是一般 Python 專案結構，不是重複專案或備份。

目前不要重新命名外層或內層：批次檔、Windows 排程、文件、測試及大量 Python imports 都使用現有名稱。若未來改採 `src/stock_ai_bot/` layout，必須視為獨立架構遷移並執行完整測試。

## 狀態定義

- **追蹤**：正式原始碼或穩定文件，應提交 Git。
- **忽略**：本機資料、秘密、快取或執行產物，由 `.gitignore` 排除。
- **混合**：目錄本身有追蹤檔，也包含被忽略的本機檔。
- **可重建**：刪除後可由程式重新產生，但可能消耗時間、API 額度或網路流量。
- **條件式清理**：需先停止相關程序、確認資料已不再需要，或先備份。
- **不可直接清理**：正式原始碼、契約、歷史證據或重要使用者資料。

## 根目錄

| 目錄 | 用途 | Git | 清理政策 | 權威文件 |
|---|---|---|---|---|
| `.git/` | Git 版本、分支及物件資料庫 | Git 內部 | 不可手動整理或刪除 | Git 本身 |
| `.vscode/` | VS Code 顯示、搜尋及檔案監聽設定 | 追蹤 | 保留；變更後提交 | 本文件、`.vscode/settings.json` |
| `.venv/` | Python 虛擬環境與套件 | 忽略 | 可重建；刪除前停止 Bot，之後依 `requirements.txt` 重裝 | [維運手冊](operations.md) |
| `__pycache__/` | Python bytecode 快取 | 忽略 | 可安全清除，執行時會再產生 | `.gitignore` |
| `.cache/` | 選股、籌碼、財報、研究資料及回補快取 | 忽略 | 條件式清理；大量刪除會增加重抓時間與 API 用量 | [資料來源](data-sources.md)、[維運手冊](operations.md) |
| `.runtime/` | Bot heartbeat、watchdog PID、MiniMax 工具、監控及回放執行狀態 | 忽略 | 不可在程序運行時清除；只做有依據的局部維護 | [維運手冊](operations.md)、[監控 README](../trade_monitor/README.md) |
| `.workdata/` | 正式、開發及救援資料的本機集中存放區；各工作區以 profile 隔離 | 忽略 | 不進 Git；不可整批複製到 worktree | [工作資料配置](workdata-layout.md) |
| `.v7bridge/` | v7 bridge 隔離驗證的 control、run 與 attestation 產物 | 忽略 | 驗證完成且不需追溯時可封存或清除 | [工作區整理計畫](workspace-organization-plan.md) |
| `.v7s1/` | v7 stage-one 隔離研究的 control、run 與 attestation 產物 | 忽略 | 驗證完成且不需追溯時可封存或清除 | [工作區整理計畫](workspace-organization-plan.md) |
| `archive/` | 已退出正式路徑的舊版程式及歷史檔案 | 追蹤 | 不可直接刪除；先確認還原與稽核需求 | 本文件 |
| `backtests/` | 可重現的獨立回測程式與穩定結果 | 追蹤 | 保留；臨時大型結果改放 `reports/` 或 `outputs/` | [Backtests README](../backtests/README.md) |
| `config/` | 公開規則、schema、評分、知識庫及服務設定 | 混合 | 追蹤檔不可任意清理；`secrets.json` 僅留本機且不得提交 | [維運手冊](operations.md)、[資料來源](data-sources.md) |
| `data/` | 題材與 topic 系統的本機資料 | 忽略 | 條件式清理；先確認題材庫與執行狀態是否可重建 | [題材系統](topic-system.md) |
| `database/` | SQLite 投研、新聞、事件、來源快照與報告索引 | 忽略 | 重要資料；停止寫入並備份後才能維護 | [維運手冊](operations.md)、[系統架構](architecture.md) |
| `docs/` | 架構、維運、測試、策略與研究文件 | 追蹤 | 保留；過期文件應標記或移入 legacy，不直接丟棄 | 本文件、[README 文件索引](../README.md#文件索引) |
| `experiments/` | 尚未提升為正式測試或回測的本機 scratch 實驗 | 忽略 | 確認沒有需保存的結論後可清理 | [工作區整理計畫](workspace-organization-plan.md) |
| `local_data/` | 課程知識庫及保留的歷史備份 | 忽略 | 不可視為快取；需人工確認後才能封存或刪除 | [工作區整理計畫](workspace-organization-plan.md) |
| `logs/` | 排程、AI、prompt、題材、健檢及 watchdog 日誌 | 忽略 | 可依保留期輪替；問題調查期間保留相關區段 | [維運手冊](operations.md) |
| `memories/` | 本機 session／工作階段狀態 | 忽略 | 停止相關程序並確認不需續跑後才能清理 | 本文件 |
| `outputs/` | 回測、探針與研究的中間輸出 | 忽略 | 結論已提升到文件或正式 artifact 後可清理 | [工作區整理計畫](workspace-organization-plan.md) |
| `prompt/` | 報告、新聞、題材、評分與工作流 Prompt | 追蹤 | 正式輸入資產，不可當產物清除 | `prompt/manifest.json`、[AI 投研](ai-research.md) |
| `reports/` | Markdown、HTML、JSON 投研與回測報告 | 忽略 | 使用者產物；依日期封存，不假設一定可重建 | [維運手冊](operations.md)、[測試文件](testing.md) |
| `research_center/` | AI 投研、新聞、題材、資料整合、報告及 API | 追蹤 | 正式原始碼，不可直接清理 | [系統架構](architecture.md)、[AI 投研](ai-research.md) |
| `scripts/` | 回測、資料製作、驗證、遷移及研究腳本 | 追蹤 | 不可批次刪除；未來按研究家族分批整理 | [研究檔案分流](research-file-triage.md)、[工作區整理計畫](workspace-organization-plan.md) |
| `stock_ai_bot/` | 正式股票 Bot Python 套件 | 追蹤 | 核心原始碼，不可直接清理或改名 | [README 功能模組](../README.md#功能模組)、[系統架構](architecture.md) |
| `tests/` | pytest 測試、fixture、監控及回放測試 | 追蹤 | 保留；只清除其中的 cache／臨時輸出 | [測試文件](testing.md) |
| `tools/` | watchdog、健檢、驗證及維護工具 | 追蹤 | 保留；確認無入口、測試或文件引用後才可移除單一工具 | [維運手冊](operations.md)、[測試文件](testing.md) |
| `trade_monitor/` | 台指期即時監控、狀態契約、規則版本及 scheduler | 追蹤 | 正式系統；不可批次搬移或刪除 | [監控 README](../trade_monitor/README.md)、[版本索引](../trade_monitor/rules/VERSION_INDEX.md) |
| `trade_monitor_replay/` | 台指期歷史回放、確定性／AI 比較及重現契約 | 追蹤 | 保留；修正契約前不可重寫舊 manifest 或雜湊 | [回放文件](trade-monitor-replay.md)、[版本索引](../trade_monitor/rules/VERSION_INDEX.md) |

## `stock_ai_bot/` 套件

| 子目錄 | 職責 | 清理政策 |
|---|---|---|
| `backfill/` | 全市場資料回補、缺口修復、快取暖機與排程準備 | 正式原始碼，保留 |
| `charts/` | 台股與台指期圖表資料及 HTML 產生 | 正式原始碼，保留 |
| `common/` | 共用進度紀錄與多工輔助 | 正式原始碼，保留 |
| `data_sources/` | Yahoo、Fugle、歷史價格等資料來源 | 正式原始碼，保留 |
| `exports/` | 個股 Excel 匯出 | 正式原始碼，保留 |
| `market/` | 晨報、午報及市場摘要 | 正式原始碼，保留 |
| `monitoring/` | Radar、監控掃描及 Bot runtime health | 正式原始碼，保留 |
| `portfolio/` | 個人持股與股票名稱解析 | 正式原始碼，保留 |
| `scanning/` | 財報、營收與技術面掃描 | 正式原始碼，保留 |
| `scoring/` | 財務與選股評分 | 正式原始碼，保留 |
| `selection/` | 精選選股及老蕭策略流程 | 正式原始碼，保留 |
| `strategies/` | 籌碼、法人、投信、大戶與 TDCC 策略 | 正式原始碼，保留 |
| `telegram/` | Telegram 訊息格式、分段與傳送 | 正式原始碼，保留 |
| `__pycache__/` | Python bytecode | 可安全清除並自動重建 |

## 根目錄重要檔案

| 檔案 | 用途 | Git／清理政策 |
|---|---|---|
| `main.py` | Telegram Bot 啟動、指令註冊及排程入口 | 追蹤；應逐步縮小，但不可直接搬移 |
| `啟動機器人.bat`、`停止機器人.bat` | Windows 日常啟停入口 | 追蹤；保留 |
| `啟動機器人_runner.bat` | watchdog 使用的內部可見 runner | 追蹤；不可手動當成主要入口 |
| `config.json`、`portfolio.json` | 本機 Telegram／掃描設定與個人持股 | 忽略；重要私有資料，不得提交 |
| `config.example.json`、`portfolio.example.json` | 可提交的設定範例 | 追蹤；設定欄位變更時同步更新 |
| `stock_list.json` | 股票代號、名稱及市場清單 | 追蹤；目前多個模組依賴根目錄路徑 |
| `requirements.txt`、`pytest.ini` | Python 依賴及 pytest 收集設定 | 追蹤；保留 |
| 根目錄 `trade_monitor_*.py` 與舊 schema | 舊 Automation／舊文件相容入口 | 暫時保留；依[版本索引](../trade_monitor/rules/VERSION_INDEX.md#延後整理)處理 |

## 清理順序

1. 先執行 `git status --short`，不得覆蓋或刪除未辨識的使用者變更。
2. 先停止會寫入目標目錄的 Bot、watchdog、scheduler 或回放程序。
3. 先清理可重建的 `__pycache__/`、`.pytest_cache/`、`.tmp/` 類暫存。
4. `.cache/`、`logs/`、`outputs/` 依保留期或已完成的研究批次局部清理。
5. `reports/`、`database/`、`local_data/`、`.runtime/` 必須先確認用途與備份，不做整批刪除。
6. 任何 tracked 目錄搬移都要同步更新 imports、文件、測試、Windows 腳本及排程路徑。

根目錄中既有的 `.runtime/`、`.cache/`、`data/`、`database/`、`local_data/`、`logs/`、`outputs/` 與 `reports/` 可在本機遷移後保留為相容 junction；實體資料統一放在 `.workdata/<profile>/`。這些 junction 仍沿用原本的 Git ignore 規則。

## 文件權責

- 本文件：目前目錄用途、Git 政策與清理條件。
- [README](../README.md)：使用入口、主要功能與文件索引。
- [系統架構](architecture.md)：服務層及資料流，不重複維護完整檔案樹。
- [工作區整理計畫](workspace-organization-plan.md)：尚未完成的搬移方向與相容策略。
- [維運手冊](operations.md)：啟動、設定、排程、報告位置與日常維護。
- [工作資料配置](workdata-layout.md)：`.workdata` profile、worktree 共用原則與相容目錄。
- [監控版本索引](../trade_monitor/rules/VERSION_INDEX.md)：正式監控、回測、歷史版本與延後相容清理。

新增根目錄、變更 Git 追蹤政策或完成延後搬移時，必須同步更新本文件；若內容與實際 `.gitignore`、manifest 或程式路徑衝突，以實際契約為準並立即修正文檔。
