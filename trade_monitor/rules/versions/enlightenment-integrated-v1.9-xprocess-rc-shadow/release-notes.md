# v1.9 X 流程整合 RC Shadow

本版完整承接 v1.8，不另建一套 X 策略，而把 X 課程改寫為同一市場狀態中的盤中決策順序。

## 新增

- `decision_chain_context` state v1：原子保存流程階段、現貨／期貨開盤資料來源、第一次 DH／DL 樣本、已確認腳數、唯一主鏡頭、家族 DNA、三項共振、原四型態映射、質性 A／B／C、預期行為及時間／動機失效。
- schema v7、contract v7、market structure state v5 與 v4→v5 空狀態因果遷移。
- X 流程歷史逐時回放：日盤 20 sessions、5,991 根 K，保留 n=2／n=3 比較與第一次端點首次可知時間。
- 隔離 Shadow：三輪合法 v5 相鄰狀態、Telegram dry-run、正式環境不變。

## 保留

- 原四型態、原九欄、一口單、收盤確認、結構停損、交易憲法、canonical message、Chrome DETAIL／OVERVIEW 與 resume guard。
- 多空鏡像；沒有每日三次硬上限。
- Automation 1-k、正式 v1.8 scheduler、TG、active-version 與正式 runtime 均未切換。

## 明確不納入

- 第五種型態、獨立 X 投票答案、第十個通知欄位。
- 固定點數跳空門檻、沒有資料來源的現貨價、精確課程勝率、五檔委託簿推測。
- 巨量集中風險、無停損、攤平、核爆滿倉、多口母子單與未完成反擊規則。

## 尚未完成的正式門檻

- 至少五個完整交易日的前向 Shadow，需涵蓋非空開盤證據、主鏡頭切換、原四型態映射、A／B／C 與時間／動機失效案例。
- 在上述門檻完成並由使用者另行要求正式升級前，本 RC 不得部署。
