# 0050 與共用 ETF 回測定版配置

## 保留政策

- `.workdata/reports/dual-ma/etf/0050` 下所有歷史報告、附件、manifest、readback 與 QA 重播結果均原位保留。
- 報告清冊工具只讀取並計算 SHA，不移動、覆寫或刪除報告。
- 正式閱讀入口仍由 `.workdata/reports/dual-ma/README.md` 指定。

## 共用資料位置

ETF 資料與報告、執行輸出分離：

```text
.workdata/market-data/
  etf/
    registry.json
    0050/
      current.json
      snapshots/
        <snapshot-id>/
          snapshot.json
          package/
            artifact_manifest.json
            data/
            events/
            factors/
            fixtures/
            sources/
```

`registry.json` 只記錄相對路徑與正式 package manifest SHA。程式可用
`STOCK_AI_BOT_MARKET_DATA_ROOT` 指向另一部電腦上的資料根目錄；若只設定
`STOCK_AI_BOT_WORKDATA_ROOT`，則自動使用其下的 `market-data`。2006～2026
0050 正式重播消費者會優先解析這份 registry，尚未安裝 registry 的舊環境才
回退到原本的 `dev/dual-ma` 封存路徑，而且兩者都仍受 package manifest SHA
檢查。

目前 canonical snapshot 是
`2005-01-03_2026-09-30_86d91332d197`，正式區間為 2006-01-02 至
2026-09-30，2005 年只作暖機；package manifest SHA-256 為
`86D91332D1974CE54B0ED76F623EE253919DF3A7C5AA9CCB454A7516560F6CBB`。

## 驗證資料

```powershell
python scripts/etf_data_registry.py 0050
```

完整驗證會逐一檢查 package manifest 內所有檔案的 bytes 與 SHA-256。

## 產生歷史報告清冊

```powershell
python scripts/etf_report_inventory.py `
  .workdata/reports/dual-ma/etf/0050 `
  .workdata/market-data/etf/0050/report-inventory.json
```

## Git 邊界

Git 保存程式、測試、資料契約、文件與小型 fixture。完整行情、來源回執、日帳與歷史報告繼續由 `.workdata` 管理，不加入一般 Git commit。
