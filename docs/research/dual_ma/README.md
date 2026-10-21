# 雙均線與 ETF 正式研究索引

本目錄是可由 Git 長期保存的精選研究入口。它不是 `.workdata` 的完整鏡像；只收錄正式定版、方法基準、決策依據及必要的 manifest／readback／機器表。

## 0050

| 類型 | 狀態 | 入口 |
|---|---|---|
| 2006～2026 最終策略 v1.7 | **正式採納／ACCEPT** | [主報告](0050/final-v1.7/README.md) · [完整研究報告](0050/final-v1.7/report.md) · [逐筆明細](0050/final-v1.7/trade-details.md) |
| v1.7 的直接來源 v1.6 | 重現鏈，數值與 v1.7 相同 | [來源報告](0050/source-v1.6/README.md) |
| 2017～2026 基準 | 已採納歷史基準 | [報告](0050/accepted-history/2017-2026-base-v1/report.md) |
| `base-v4` 相容性基準 | 已採納歷史基準 | [報告](0050/accepted-history/base-v4/report.md) · [QA 接受證據](0050/accepted-history/base-v4/QA_ACCEPTED.json) |
| 2006～2026 固定方法比較 | 已採納歷史基準 | [入口](0050/accepted-history/2006-2026-fixed-methods-v1/README.md) |
| 2017～2026 MA21／固定停損／MACD | 已採納歷史基準 | [入口](0050/accepted-history/2017-2026-ma21-fixed-stop-macd-v1-1/README.md) |

v1.7 `artifact-manifest.json` SHA-256：`fc7181a77496827996650708df97ee3ba9bb8d58132b035f6455c5c67d1e8b7f`。直接來源 v1.6 的 manifest SHA-256：`d782fb18d6766c96dd20d5166b776d27275bc66032a28a676119a3c736ada0e7`。

完整重現鏈也已保存：[v1.6 run](0050/run-chain/v1.6/) 與 [v1.7 run](0050/run-chain/v1.7/)。v1.6 run manifest SHA-256 為 `3453215d77a11cf02f395a04ee64c5725e65d35f0d9fa0efa5c9da905b4415d4`；v1.7 產生器仍以 v1.6 report/run 的固定 SHA 為輸入。這兩個 run 目錄不可當成另一份績效解讀。

另見 [0050 v1.7 正式監控接入提案](../../strategies/dual_ma/0050-v1-7-live-monitoring-proposal.md)。該文件仍是提案，尚未啟用正式通知或自動下單。

## 選股來源 × 策略

| 研究 | 用途 | 入口 |
|---|---|---|
| 舊 7 來源 × 5 策略（35 組） | 舊方法、欄位與候選比較基準 | [五年總覽](source-strategy/kd-2r-death-cross-source-strategy-v2/2022-2026-source-strategy-overview.md) · [候選排名](source-strategy/kd-2r-death-cross-source-strategy-v2/2022-2026-candidate-ranking-v1.md) |
| 舊五組候選 | 保存當時為何選出五組的決策鏈 | [候選分析](source-strategy/legacy-five-candidates/report.md) · [前十贏家](source-strategy/legacy-five-candidates-top10/report.md) |
| v23：7 來源 × 14 策略 | 98 組 V1／V4 正式比較、機器表與稽核 | [導覽](source-strategy/v23/REPORT_zh.md) · [V1](source-strategy/v23/V1_來源策略績效報告.md) · [V4](source-strategy/v23/V4_來源策略績效報告.md) · [總覽](source-strategy/v23/V1_V4_總覽與最終建議.md) |
| v25：新九策略 MA21 高／低結構 | 比較 `MA21 ≥ MA144` 與 `< MA144` | [導覽](source-strategy/regime-v25/REPORT_zh.md) · [總覽](source-strategy/regime-v25/V1_新九策略_高低結構總覽.md) |

v23 保留 canonical 去重後的總計／風險欄及 selection-year cohort 年度欄；年度 cohort 可能重疊，不可直接相加。v23 manifest SHA-256：`8546b41190c191bbc4da59161fc326b41027a0cd6e2de2055c0a0dc49b33dbaa`。v25 manifest SHA-256：`64b4c52866d40caa501dfd597234888839d4859bbaebaa54531f12704485e26e`。

## 00635U

- [KD(9,9,55) 循環進場＋T-1 Pivot Stop 研究](00635u/kd-cycle-pivot-stop/README.md)
- 這是完整正式格式的隔離研究包，但 `publication_eligible=false`，**不是已採納交易策略**。
- manifest SHA-256：`23e1a85f93aee1975c895e25d03bfaa7c69ca870cecf66f09df41abfb72a0cbf`。

## 來源與完整性

- 原始封存：`E:\stock_ai_cleanup_archive_20261020.zip`
- 精選恢復清單、每檔來源路徑、CRC、bytes 與 SHA-256：[RESTORE_MANIFEST.json](RESTORE_MANIFEST.json)
- 恢復工具：[scripts/restore_curated_research_archive.py](../../../scripts/restore_curated_research_archive.py)
- 本次只拉回 428 個精選檔案（含 0050 v1.6→v1.7 完整 run 重現鏈）。失敗版、superseded 版本、smoke、`qa00` 重播、重複 B/C、chunks、cache、log、P10/P11 與 A～D 原始工作包仍只保留在 ZIP。

## 閱讀規則

1. 先讀人類主報告，再視需要查 CSV／JSON／逐筆證據。
2. 報告是否「正式採納」以本索引與報告內狀態共同判定；有完整報告不代表策略已採納。
3. 精選封存檔保持唯讀。新研究必須建立新版本，不覆寫舊 manifest 或報告。
4. 大型行情與 20 年股票清單由 `stock_data/current.json` 指向的版本提供，不複製進本研究目錄。
