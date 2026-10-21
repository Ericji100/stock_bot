# 股票共用資料版本指標

大型正式資料仍位於專案根目錄的 `stock_data/`，不直接放入 Git。本頁保存可追溯版本資訊，避免清理工作再次誤刪正式資料。

- 狀態：`ACTIVE`
- release：`2026-09-30-v3`
- 選股日期：2006-01-02～2026-09-30，共 5,098 日
- ETF：0050、00635U 均為 `ACCEPTED_CANONICAL`
- release manifest SHA-256：`d50a2f8435a6c8e9288fe1ef6711646ce5e997aad0199fdfe976b3d62724bc32`
- tree fingerprint：`ad9ea37774c63f65cf9663692556eb6e3568f076e55ebf3ff6c6543cba3aa856`
- isolated rebuild：109,175 檔、0 mismatch
- 專案 D 槽複本：109,175 檔、0 mismatch
- consumer smoke：PASS

追蹤用快照：

- [current.json](current.json)
- [QA_SUMMARY.json](QA_SUMMARY.json)
- [壓縮後逐檔 release manifest](release_manifest.json.gz)（解壓後 SHA-256 必須為 `d50a2f8435a6c8e9288fe1ef6711646ce5e997aad0199fdfe976b3d62724bc32`）
- [隔離重建 exact audit](isolated-rebuild-exact-audit.json)
- [E→D exact audit](e-to-d-exact-audit.json)
- [consumer smoke](consumer-smoke.json)

完整 12 GiB 資料保留在 `stock_data/`；Git 保存版本指標、壓縮 manifest 與 QA 證據，不能用它取代資料本體備份。
