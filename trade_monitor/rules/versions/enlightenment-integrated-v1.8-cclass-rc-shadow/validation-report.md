# v1.8 戰法 C 班 RC 回放與隔離 Shadow 驗證

驗證時間：2026-09-02（Asia/Taipei）

## 結論

- RC：`rc_shadow_validated_not_deployed`
- 正式 active version：`enlightenment-integrated-v1.7-anchor`
- Automation `1-k`：`PAUSED`
- 正式 scheduler config：v1.7／schema v5／state v3，SHA-256 `1511e1b171b25692d7666b93e6f9dd06d912eca386e9820790f216321e760c29`
- 實際 Automation TOML SHA-256：`69a395850189f7edd2471c423eb51fdaa5997960a93336bd5bbf90c3883d6684`
- RC transport：隔離 runtime、Telegram disabled、`--dry-run`
- 正式 Automation、正式 runtime、正式 outbox、Telegram 與 active-version：未修改

## 完整性驗證

- 太極：保留 `ANCHOR_1／CORRECTION_2／COPY_3／CORRECTION_4／COPY_5／POST_5`、父子 leg、前世今生、定錨時間效力，以及幅度、時間、斜率、乾淨度、破壞性五項比較。
- 一之：保留離心力形成／確認、一條龍早中末段與 GOLD／K_GOLD／EARTH／CROOKED、生死門形成／ARMED、日內外／邊界位置、左右力量、耗竭與失效。
- 工作看法：保留維持、降級、中立化及正式翻向條件；複製失敗或動能失效不自動反手。
- 單一系統：所有狀態只能映射原四型態、交易憲法、結構停損、一口單管理及固定九欄；沒有第五種型態、平行答案或第十欄。
- 拒絕移植：無停損、核爆滿倉、多口牡丹及 2022 固定點數／分鐘參數未納入正式規則。

## 2026/8 逐根因果回放

來源：`C:\Users\紀成達\Downloads\tmf_chart_b7d18f803ee54d838cc3194ef647a8bb.html`

- 22,791 根 1 分 K，40 個日／夜盤 session。
- 樞紐只在 confirmation index 才進入狀態；動能基準只使用之前 20 根；生死門只在形成 K 收盤後判斷；不使用最終 Day High／Low 回填。

| 指標 | n=2 | n=3 |
|---|---:|---:|
| 因果已知太極 legs | 2,864 | 2,349 |
| 完整五段序列 | 558 | 453 |
| COPY_3／COPY_5 五維比較 | 1,033 | 865 |
| 複製弱／失敗率 | 42.59% | 42.20% |
| leg 已知延遲中位數 | 2 根 | 3 根 |
| 離心力確認 proxy | 387 | 387 |
| 生死門形成 proxy | 327 | 327 |
| 生死門 armed proxy | 119 | 119 |
| 結構樞紐壓力 | 207 | 176 |

n=3 少 515 個可用 leg 並多一根中位確認延遲，但複製弱／失敗率只改善 0.39 個百分點；沒有證據足以取代現行 n=2。RC 繼續固定 n=2。離心力、生死門與品質門檻只是回放診斷 proxy，不會成為正式固定進場參數，也不代表勝率或獲利能力。

完整報告：

- `trade_monitor/reports/cclass-v18-n2-n3-2026-08.md`
- `trade_monitor/reports/cclass-v18-n2-n3-2026-08.json`

## 三次連續隔離 Shadow

| 權威已收盤 K | 分析秒數 | 總秒數 | schema／state | Telegram |
|---|---:|---:|---|---|
| 19:03 | 88.197 | 88.809 | v6／v4 accepted | dry_run |
| 19:05 | 79.558 | 80.123 | v6／v4 transition accepted | dry_run |
| 19:07 | 76.436 | 77.032 | v6／v4 transition accepted | dry_run |

- 平均 Codex 分析 81.397 秒；範圍 76.436～88.197 秒。
- 三輪都以相同隔離 state 因果延續並原子保存；沒有回填錨、太極 leg 或一之 episode。
- 畫面缺少可因果建立的有效錨與可靠雙價位校準，因此模型正確保持 `engine_mode=UNDEFINED`、`order_state=UNDEFINED`、`taiji legs=0`、`momentum stage=NONE`、`setup=NONE`，而非為了套課程硬造狀態。
- 三輪皆顯示 NOTIFY，原因是隔離 dry-run 不會把首次恢復通知標成已送達；下一輪 resume guard 會持續 force notify。這是 dry-run 的恢復保護語意，不代表正式 unchanged 輪會每次長文通知。
- OVERVIEW 為 UNAVAILABLE，沒有被當作有效大級證據；DETAIL 仍完成安全的 fail-safe 分析。

## 自動測試

- `python -m pytest tests/trade_monitor -q`：**172 passed**
- v6 schema、contract v6、state v3→v4／v2→v4 記憶中遷移、非法回填、作用中 leg／episode、一之無事件禁用、複製失敗不自動翻向：通過。
- 原四型態、固定九欄、canonical message、event id、bridge dry-run、resume guard、v1.7 formal compatibility：通過。
- 不可變 v1.7 還原點與 v1.8 RC prompt／schema／config 雜湊：通過。

## 限制與正式部署門檻

- 目前完整分析平均仍超過一分鐘，無法保證每根 1 分 K 都逐根完成；這比 v1.7 已接受的延遲略重，不應宣稱交易所等級即時。
- 回放不是獲利回測；尚未模擬四型態完整准入、成交、滑價、成本、停損、出場與 R。
- 本次 Shadow 沒有出現可因果建立的太極或一之事件，因此驗證了空狀態與相鄰轉移，但真實非空事件仍需要較長前向觀察。
- 未取得使用者另一次明確正式部署指示前，不更新 Automation `1-k`、正式 scheduler config、active-version 或 Telegram。
