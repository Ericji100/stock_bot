# 工作資料配置

`.workdata/` 是本機正式執行、開發研究與分流救援資料的集中存放區。它位於 `D:\code\stock_ai_bot` 內，但由 Git 忽略，因此 `git clone` 與 `git worktree add` 不會自動複製其中資料。

## 配置結構

```text
D:\code\stock_ai_bot\.workdata\
├─ prod\
│  ├─ .runtime\
│  ├─ .cache\
│  ├─ data\
│  ├─ database\
│  ├─ local_data\
│  ├─ logs\
│  ├─ outputs\
│  └─ reports\
├─ dev\
│  ├─ stock-monitor\
│  │  └─ shared\       課程回測、知識庫與研究實驗資料
│  ├─ tmf-monitor\
│  │  └─ shared\       台指期回放、clock probe 與歷史驗證資料
│  └─ dual-ma\
└─ rescue\
   └─ split-20260922\
```

正式 Bot 使用 `prod`。每個開發 worktree 必須使用自己的 profile，不可寫入 `prod`。需要共用的大型歷史資料應以唯讀方式引用，不要複製整套資料，也不要讓研究程序覆寫正式狀態。

每個開發 profile 的 `shared/` 保存該研究線的完整歷史資料。開發 worktree 可透過 junction 連到自己的 `shared/`；正式 `prod` 不得連到任何 `dev` profile，避免未完成資料重新出現在正式工作區。

## 環境變數

| 變數 | 正式預設值 | 用途 |
|---|---|---|
| `STOCK_AI_BOT_WORKDATA_ROOT` | `<project>\.workdata` | 集中資料根目錄 |
| `STOCK_AI_BOT_WORKDATA_PROFILE` | `prod` | `prod` 或開發工作區名稱 |

Python 程式可使用 `stock_ai_bot.workdata` 取得正規路徑。新程式不得新增寫死到專案根目錄的 mutable data 路徑。

## 相容目錄

現有正式程式仍有許多根目錄相對路徑。完成本機遷移後，專案根目錄的 `.runtime/`、`.cache/`、`data/`、`database/`、`local_data/`、`logs/`、`outputs/` 與 `reports/` 暫時保留為 Windows directory junction，但只能指向 `.workdata/prod/` 的對應目錄。

相容 junction 的目的只是讓既有程式不中斷。新程式應改用 `stock_ai_bot.workdata`；等所有正式入口完成路徑遷移與回歸測試後，才能移除 junction。

## Worktree 規則

- Git 追蹤的程式、測試與文件要透過 commit、merge 或 cherry-pick 同步，不人工複製。
- `.workdata/`、`.runtime/`、`reports/` 等忽略資料不隨 worktree 建立而複製。
- 每個 worktree 的相容目錄只能指向 `.workdata/dev/<profile>/`。
- `D:\code\stock_ai_bot` 的正式相容目錄不得指向 `.workdata/dev/`。
- 正式設定與 Telegram token 不複製到開發 worktree。
- 分支完成時，只把經測試的正式程式提交回 `main`；研究輸出留在該 profile 或封存區。

## 救援資料

`rescue/` 只保存分流 manifest、必要快照與短期還原證據。驗證完成後應刪除可重建的壓縮檔和雜湊檢查展開目錄；保留項目也不得提交 Git。
