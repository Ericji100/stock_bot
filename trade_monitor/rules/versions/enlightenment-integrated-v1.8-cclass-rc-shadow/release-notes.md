# v1.8 戰法 C 班 RC release notes

狀態：`RC shadow，未部署`。正式 Automation `1-k`、正式 scheduler config、Telegram、active-version 與正式 runtime 仍維持 v1.7／PAUSED。

## 完整整合內容

- 以單一 `engine_mode` 合併有序太極、異常一之、無序及重置；不產生第二份盤勢答案。
- 太極保留定錨朝代、前世今生、1～5／POST_5 段序、父子關係及幅度、時間、斜率、乾淨度、破壞性五項比較。
- 一之保留離心力形成／確認、一條龍早中末段與品質、生死門形成／待命、日內外位置、左右力量、耗竭與失效。
- 新增唯一工作看法及維持、降級、中立化、正式翻向條件；複製失敗或動能失效不自動反手。
- 所有 C 班狀態只映射回原有四型態、交易憲法、結構停損、一口單管理及固定九欄。

## 明確不納入

- 不新增第五種進場型態或第十個通知欄位。
- 不採用無停損、核爆滿倉、多口母單／牡丹、固定歷史點數或舊年月參數。
- 不用後來完成的 Day High／Day Low、波段或動能結果回填當時判斷。

## 技術變更

- schema `trade-monitor-analysis-v6`
- `market_structure_state.version=4`
- `cclass_context.version=1`
- contract version 6
- RC runtime 與 dual-scale state 使用隔離路徑，Telegram 必須 dry-run。

正式提升前仍須完成完整測試、依時間順序的回放比較與隔離 Shadow；失敗時停在 v1.7，不得勉強部署。
