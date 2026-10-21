# 研究產物保存政策

## 三層資料

### 1. Git 正式層

放入 Git：

- 已採納或會影響後續決策的主報告。
- 舊方法／欄位基準與候選選擇依據。
- 必要機器表、圖表、manifest、readback、契約與 exact-rebuild 稽核。
- 可以說明「為何採納／未採納」的完整證據。

不得原地覆寫；修正時新增版本並保留舊版狀態。

### 2. 本機正式資料層

`stock_data/current.json` 指向的版本保存 20 年選股清單、行情、ETF、公司行動與正式 SHA manifest。資料量大，不放入 Git；它必須保持唯讀並另有已驗證備份。

### 3. 可清理工作層

`.workdata`、工作包與隔離重播根可包含 chunks、raw、cache、log、partial、失敗版與重複 B/C。只有在正式產物已進 Git、manifest 已驗證、ZIP 備份可讀且使用者確認後才能清理。

## 清理前 allowlist

清理工具不得再把整個 `.workdata/reports` 當成可刪目標。每次清理前必須建立 allowlist，至少包含：

1. `docs/research/**` 已追蹤的精選封存。
2. `stock_data/**` 現行版本與 QA。
3. 目前仍在執行的 checkpoint／input manifest／未完成 root。
4. 尚未推送的 Git commit 與未提交的人類規則文件。

不在 allowlist 的項目也不能直接刪除；先做可讀 ZIP、檔案數／bytes／SHA 稽核，再取得明確刪除授權。

## 重現邊界

- Git 精選層支援閱讀、數值稽核與大部分報告重現。
- 需要完整 raw／run 重播時，依 `RESTORE_MANIFEST.json` 回到原 ZIP 或正式資料備份。
- 不把重複 QA 根一起放進 Git；exact audit 與唯一正式根已足以證明 byte parity。
