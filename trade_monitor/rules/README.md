# 監控規則版本庫

每個 `versions/<version_id>/prompt.md` 都是可直接套用的完整提示詞，不依賴疊加增補檔。`current-pre-enlightenment-20260902` 是部署前不可變還原點；`enlightenment-integrated-v1` 是正式整合版。

目前正式版、回測版、歷史封存版與已知契約待修項目，先查閱 [`VERSION_INDEX.md`](VERSION_INDEX.md)。實際正式版本仍只以 `active-version.json` 為準。

## 列出與驗證

```powershell
python -m trade_monitor.versioning list
python -m trade_monitor.versioning verify --version current-pre-enlightenment-20260902
python -m trade_monitor.versioning verify --version enlightenment-integrated-v1
python -m trade_monitor.versioning verify --version enlightenment-integrated-v1.2-local-clock
python -m trade_monitor.versioning verify --version enlightenment-integrated-v1.3-dual-mode
python -m trade_monitor.versioning verify --version enlightenment-integrated-v1.4-dow-rc-shadow
python -m trade_monitor.versioning verify --version enlightenment-integrated-v1.4-dow-analysis
python -m trade_monitor.versioning verify --version enlightenment-integrated-v1.4-dow-dual-mode
python -m trade_monitor.versioning verify --version enlightenment-integrated-v1.5-scenario-analysis
python -m trade_monitor.versioning verify --version enlightenment-integrated-v1.5-scenario-dual-mode
python -m trade_monitor.versioning verify --version pre-dual-scale-v1.5-20260902
python -m trade_monitor.versioning verify --version enlightenment-integrated-v1.6-dual-scale-rc
python -m trade_monitor.versioning verify --version enlightenment-integrated-v1.6-dual-scale
python -m trade_monitor.versioning verify --version enlightenment-integrated-v1.7-anchor-rc-shadow
python -m trade_monitor.versioning verify --version pre-anchor-v1.6-20260902
python -m trade_monitor.versioning verify --version enlightenment-integrated-v1.7-anchor
python -m trade_monitor.versioning verify --version pre-cclass-v1.7-20260902
python -m trade_monitor.versioning verify --version enlightenment-integrated-v1.8-cclass-rc-shadow
python -m trade_monitor.versioning verify --version enlightenment-integrated-v1.8-cclass
python -m trade_monitor.versioning verify --version pre-xprocess-v1.8-20260902
python -m trade_monitor.versioning verify --version enlightenment-integrated-v1.9-xprocess-rc-shadow
python -m trade_monitor.versioning verify --version enlightenment-integrated-v1.9-xprocess
python -m trade_monitor.versioning verify --version pre-unified-v1.9-20260902
python -m trade_monitor.versioning verify --version enlightenment-integrated-v2.0-unified-rc-shadow
python -m trade_monitor.versioning verify --version enlightenment-integrated-v2.0-unified
python -m trade_monitor.versioning verify --version enlightenment-integrated-v2.1.4-same-grade-quadrant
python -m trade_monitor.versioning compare --from current-pre-enlightenment-20260902 --to enlightenment-integrated-v1
```

`verify` 只讀取版本檔並比對 manifest SHA-256；`compare` 只顯示安全的行數、位元組與雜湊差異，不輸出完整提示詞。

## 正式切換與回復

1. 重新讀取 Codex Automation `1-k` 的實際設定與本機 TOML，確認 Automation ID、名稱、RRULE、target task、通知政策及狀態。
2. 以本目錄中目標版本的完整 `prompt.md` 呼叫 Codex Automation 更新功能；不得直接編輯 `automation.toml`，不得建立第二個排程。
3. 保留切換前的實際 Automation 狀態；部署交易期間可以暫停，兩側驗證成功後才恢復原狀態。
4. 重新讀取實際設定，計算實際 prompt UTF-8 SHA-256，與 manifest 比對。
5. 驗證成功後才更新 `active-version.json`，並在 `activation-history.jsonl` 追加一筆完整 JSON 記錄。
6. 若驗證失敗，立即用 `current-pre-enlightenment-20260902` 的完整提示詞經同一正式更新功能還原，再次驗證；不要修改 TOML 作為捷徑。

`activation-history.jsonl` 每行是一筆切換結果，應包含操作 ID、起訖時間、來源／目標版本、更新前後雜湊、Automation ID、RRULE、target task、保留狀態與結果。

`enlightenment-integrated-v1.2-local-clock` 的 Automation prompt 是自足的完整版本；其中交易分析仍由本機程序鎖定並逐字使用 `enlightenment-integrated-v1.1`，新增內容只負責本機第 05 秒觸發與 heartbeat relay。

`enlightenment-integrated-v1.3-dual-mode` 延續相同本機讀秒與 v1.1 交易規則；唯一新增行為是由 finalize 先選定一致訊息：`NOTIFY` 使用完整九欄，`DONT_NOTIFY` 使用固定短版，Telegram 與 Codex 任務不得再各自改寫。

## 執行版本、分析版本與 scheduler config

三者是不同責任，正式切換必須成組完成：

- **Analysis rules version**：本機 Codex CLI 真正讀取的完整交易分析規則與 schema。
- **Automation execution version**：heartbeat 的本機讀秒／outbox relay／雙模式執行提示詞。
- **Local scheduler config**：把本機程序鎖定到指定分析 prompt、SHA-256、schema 與內部狀態開關。

正式組合：

| 組合 | Automation execution | Local analysis | Schema／狀態 |
|---|---|---|---|
| v1.3 | `enlightenment-integrated-v1.3-dual-mode` | `enlightenment-integrated-v1.1` | v2，無 `market_structure_state` |
| v1.4 | `enlightenment-integrated-v1.4-dow-dual-mode` | `enlightenment-integrated-v1.4-dow-analysis` | v3，原子保存內部 `market_structure_state` |
| v1.5 | `enlightenment-integrated-v1.5-scenario-dual-mode` | `enlightenment-integrated-v1.5-scenario-analysis` | v4／state v2，四向防線、情境與候選階段 |
| v1.6 | `enlightenment-integrated-v1.6-dual-scale` | `enlightenment-integrated-v1.6-dual-scale-analysis` | v4／state v2＋Chrome雙尺度state v1 |
| v1.7 | `enlightenment-integrated-v1.7-anchor` | `enlightenment-integrated-v1.7-anchor-analysis` | v5／state v3＋因果定錨與動態象限；已接受 65～76 秒延遲限制 |
| v1.8 RC | `enlightenment-integrated-v1.8-cclass-rc-shadow` | `enlightenment-integrated-v1.8-cclass-rc-shadow-analysis` | v6／state v4＋完整太極 1～5 段、一之動能階段與工作看法；未部署 |
| v1.8 | `enlightenment-integrated-v1.8-cclass` | `enlightenment-integrated-v1.8-cclass-analysis` | v6／state v4＋完整戰法 C 班單一系統整合；上一個正式版 |
| v1.9 RC | `enlightenment-integrated-v1.9-xprocess-rc-shadow` | `enlightenment-integrated-v1.9-xprocess-rc-shadow-analysis` | v7／state v5＋X 開盤證據、第一次 DH／DL、主鏡頭、家族 DNA／共振、A-B-C 與時間失效；未部署 |
| v1.9 | `enlightenment-integrated-v1.9-xprocess` | `enlightenment-integrated-v1.9-xprocess-analysis` | v7／state v5＋X 決策鏈正式單一系統整合；上一個正式版 |
| v2.0 RC | `enlightenment-integrated-v2.0-unified-rc-shadow` | `enlightenment-integrated-v2.0-unified-rc-shadow-analysis` | v7／state v5 不變＋兩腳後依清晰度選主判讀、定錨後四象限、中文顯示層及可選唯讀結構化一分 K；未部署 |
| v2.0 | `enlightenment-integrated-v2.0-unified` | `enlightenment-integrated-v2.0-unified-analysis` | v7／state v5 不變＋完整課程統一正式版；上一個正式版 |
| v2.1 | `enlightenment-integrated-v2.1-prospective` | `enlightenment-integrated-v2.1-prospective-analysis` | v8／state v6＋全戰法前瞻情境、雙向預案及主控戰法專屬進出場；上一個正式版 |
| v2.1.1 | `enlightenment-integrated-v2.1.1-course-terms` | `enlightenment-integrated-v2.1.1-course-terms-analysis` | v8／state v6 不變；使用者可見名稱保留 ATR14 與 A／B／C 級點；上一個正式版 |
| v2.1.2 | `enlightenment-integrated-v2.1.2-pivot-terms` | `enlightenment-integrated-v2.1.2-pivot-terms-analysis` | v8／state v6 不變；使用者可見 Dow 結構統一使用次高點／次低點；上一個正式版 |
| v2.1.3 | `enlightenment-integrated-v2.1.3-type-terms` | `enlightenment-integrated-v2.1.3-type-terms-analysis` | v8／state v6 不變；Type1／Type2／Type3 保留原名並於首次出現附簡短說明；上一個正式版本 |
| v2.1.4 | `enlightenment-integrated-v2.1.4-same-grade-quadrant` | `enlightenment-integrated-v2.1.4-same-grade-quadrant-analysis` | v8／state v6 不變；工作象限的趨勢與波動必須採同一控制級數，禁止跨級拼接；歷史正式版 |
| v2.1.5 | `enlightenment-integrated-v2.1.5-defense-consistency` | `enlightenment-integrated-v2.1.5-defense-consistency-analysis` | v8／state v6；可見歷史樞紐與同級道氏防線一致性；歷史正式版 |
| v2.1.6 | `enlightenment-integrated-v2.1.6-anchor-hierarchy` | `enlightenment-integrated-v2.1.6-anchor-hierarchy-analysis` | v8／state v6；大小錨父子階層、跨級與時間價格一致性；歷史正式版 |
| v2.1.7 | `enlightenment-integrated-v2.1.7-context-consistency` | `enlightenment-integrated-v2.1.7-context-consistency-analysis` | v8／state v6；太極歷史、控制級數象限、雙向情境與具體位置一致；上一正式版 |
| v2.1.8 | `enlightenment-integrated-v2.1.8-anchor-origin` | `enlightenment-integrated-v2.1.8-anchor-origin-analysis` | v8／state v6；跨級大錨與太極起點同源、發布前生命週期驗證；現行正式版 |

`enlightenment-integrated-v1.6-dual-scale-rc` 是未部署的影子候選：交易規則沿用 v1.5，只增加 Chrome 控制的約 180 根 K DETAIL、每 15 分鐘約 296 根 K OVERVIEW、共享租約、恢復驗證與 20 分鐘全局摘要時效。正式 Automation、正式 scheduler config、Telegram 與 `active-version.json` 均不得在 RC 驗證完成前切換。

`enlightenment-integrated-v1.6-dual-scale` 是由上述 RC 驗證後提升的正式版本；Automation prompt 與同版本 `local_scheduler_config.json` 必須成對部署或成對回復。

`enlightenment-integrated-v1.7-anchor-rc-shadow` 是未部署的定錨整合候選：以 v5 schema／state v3 保存定錨生命週期、大小級控制權與動態象限候選；不增加第五種型態或第十欄。正式 v1.6、Automation、Telegram、scheduler config、active-version 與正式 runtime 均不得因 RC 測試而切換。

`enlightenment-integrated-v1.7-anchor` 是上一個正式版本。它曾因必要回放／Shadow 門檻尚未先完成而回復 v1.6；完成 n=2／n=3 因果回放、修正 schema／契約問題，且使用者明確接受完整分析約需 65～76 秒、可能略過中間已收盤 K 的限制後，再次成組部署。execution prompt、analysis prompt、schema v5 與 state v3 scheduler config 必須成組切換，`pre-anchor-v1.6-20260902` 是不可變還原點。

`enlightenment-integrated-v1.8-cclass-rc-shadow` 是戰法 C 班完整整合候選。它以唯一主模式保存有序太極或異常一之：太極完整保留 1～5／POST_5 段、父子關係與幅度／時間／斜率／乾淨度／破壞性，一之完整保留離心力、一條龍、生死門、位置、品質與耗竭；最後仍只能映射原四型態、九欄與一口單管理。其 config、state 與 Telegram 都必須使用隔離 dry-run；在完整測試、依序回放及 Shadow 通過前，正式 v1.7、Automation、scheduler config、active-version 與正式 runtime 不得切換。`pre-cclass-v1.7-20260902` 是本次不可變還原點。

`enlightenment-integrated-v1.8-cclass` 是上一個正式版本。它由上述 RC 完成 2026/8 全月依序 n=2／n=3 回放與三輪隔離 Shadow 後提升；execution prompt、analysis prompt、schema v6、state v4 scheduler config 已成組保存。`pre-cclass-v1.7-20260902` 是可驗證還原點。

`enlightenment-integrated-v1.9-xprocess-rc-shadow` 是保留不變的 X 戰法班流程整合候選。它不建立第五種型態，而以 state v5 `decision_chain_context` 保存可靠開盤資料來源、第一次 DH／DL 樣本、因果腳數與唯一主鏡頭、家族 DNA、三項共振、原四型態映射、質性 A／B／C、觸發後應有行為及時間／動機失效。2026/8 的 20 個日盤已完成逐時 n=2／n=3 回放，三輪隔離 Shadow 已通過 v7／v5。原建議的五個完整交易日前向 Shadow 尚未完成，使用者明確接受此限制並要求正式升級。

`enlightenment-integrated-v1.9-xprocess` 是保留的歷史正式版本。它由上述 RC 升級，execution prompt、analysis prompt、schema v7、state v5 scheduler config 已成組保存；`pre-xprocess-v1.8-20260902` 是不可變還原點。

`pre-unified-v1.9-20260902` 是統一交易系統候選版修改前的不可變 v1.9 還原點。`enlightenment-integrated-v2.0-unified-rc-shadow` 修正原本以腳數機械分流主判讀工具的落差：少於兩腳只用開盤證據，兩腳以上依當時結構清晰度選太極或四象限；四象限以定錨後結構為主、均線與 ATR14 只交叉驗證。它也新增固定中文顯示層與預設關閉的唯讀結構化行情介面。候選已通過測試與 2026 年 8 月因果回放，但尚未完成多日即時前向 Shadow，因此不得更新 `active-version.json` 或 Automation。

`enlightenment-integrated-v2.0-unified` 是由上述 RC 提升的正式版本。使用者明確接受多日即時前向 Shadow 尚未完成的限制並要求正式升級；execution prompt、analysis prompt、schema v7、state v5 scheduler config 必須成組部署與回復。結構化行情介面預設關閉，未配置可靠來源時仍採圖面估計。`pre-unified-v1.9-20260902` 是本次不可變還原點。

`enlightenment-integrated-v2.1-prospective` 將所有正式戰法納入單一前瞻決策鏈，保存主要／備用情境、雙向預案與太極複製修正演化；主控戰法使用自己的進場、停損、應有行為與出場規則。

`enlightenment-integrated-v2.1.1-course-terms` 完整承接 v2.1，只將使用者可見名稱改回課程慣用的 `ATR14` 與 `A級點／B級點／C級點`；交易邏輯、schema v8、state v6、九欄格式、RRULE 與 target task 均不變，Automation 保持 `PAUSED`。

`enlightenment-integrated-v2.1.2-pivot-terms` 完整承接 v2.1.1，只把使用者可見的「較低高點／較低的高點」統一為「次高點」，把「較高低點／較高的低點」統一為「次低點」；內部 Dow 判斷、交易條件、schema v8、state v6、九欄、RRULE 與 target task 均不變，Automation 保持 `PAUSED`。

`enlightenment-integrated-v2.1.3-type-terms` 完整承接 v2.1.2，將使用者可見的三類反轉改回 `Type1／Type2／Type3`；同一則通知各類第一次出現附固定簡短說明，後續只保留代號。內部反轉判斷、交易條件、schema v8、state v6、九欄、RRULE 與 target task 均不變，Automation 保持 `PAUSED`。

`enlightenment-integrated-v2.1.4-same-grade-quadrant` 完整承接 v2.1.3 schema hotfix。工作象限改由相同控制級數、相同作用中錨後的趨勢與波動判斷；大級背景只留在大趨勢。契約會拒絕趨勢增強卻排序第二／第三象限、或波動收斂卻排序第一／第二象限等跨軸矛盾；進出場、停損、交易憲法、九欄與 Telegram 格式不變。

`enlightenment-integrated-v2.1.5-defense-consistency` 會由可見歷史重建因果樞紐，並要求每個已確認道氏方向同時具有同級、同方向且來源正確的作用中防線；點位精度不足不再等同結構不存在。

`enlightenment-integrated-v2.1.6-anchor-hierarchy` 將大小錨保存為父子階層，禁止把跨越時段極值與主要結構的完整長推進降級成唯一小錨，並要求每個錨的時間與估計價格指向同一根 K。

`enlightenment-integrated-v2.1.7-context-consistency` 由同一控制級數判斷象限，重建可見的太極複製／修正段，大小錨衝突時同時保留原大錨修正與反向小錨延續兩條預案，位置說明使用現價、上方壓力、下方支撐與可否執行的具體句型。

`enlightenment-integrated-v2.1.8-anchor-origin` 要求跨級大錨承接前一反向錨的最後極值，太極第一段與所屬大錨使用同一起點。分析輸出會在發布前同時通過內容語意與跨分鐘生命週期驗證；錯誤大錨修復與後續反向接管只能走受限制的顯式接力，不會放寬一般作用中狀態的保護。

不能只切 Automation prompt，也不能只切 `trade_monitor/local_scheduler_config.json`。完整切換順序是：

1. 暫停 Automation，等待正在執行的本機分析結束。
2. 停止 `StockAiBot-TradeMonitorLocalClock`，確認只停止對應的 `trade_monitor.local_scheduler` daemon。
3. 切換 local scheduler config，驗證分析 prompt SHA-256 與 schema。
4. 重啟 scheduler；Automation 仍 PAUSED 時先以隔離 runtime／dry-run 驗證。
5. 使用 Codex Automation update 切換完整 execution prompt，讀回驗證其 SHA-256。
6. 確認 execution／analysis 配對後，恢復切換前狀態，更新 `active-version.json` 並追加 `activation-history.jsonl`。

## 完整回復 v1.3＋v1.1

1. 將 Automation `1-k` 暫停。
2. 停止 `StockAiBot-TradeMonitorLocalClock`。
3. 從 `pre-dow-three-brothers-v1.3-20260902/local_scheduler_config.json` 回復正式 scheduler config；確認重新指向 `enlightenment-integrated-v1.1` 與其 SHA-256。
4. 重啟 Windows 工作排程並做隔離 dry-run。
5. 使用 Codex Automation update 回復 `enlightenment-integrated-v1.3-dual-mode/prompt.md`，不得直接改 `automation.toml`。
6. 讀回驗證 execution prompt SHA-256 為 `3d27217cf020ec955b29f55bb4cac06de6b5c340be6e6db0fb07f53502558e71`，analysis SHA-256 為 `207ee754f677920e256eeb737fc7f0c8fc21cd2a4a009931c304bbdd86501ea3`。
7. 恢復部署前 Automation 狀態，再更新 active version 與 activation history。
