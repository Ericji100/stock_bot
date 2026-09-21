# 唯讀結構化一分 K 資料契約

此介面是監控的可選數值來源，不是下單介面。預設關閉；只有使用者另行提供可靠、合法且唯讀的即時資料來源後，才可在候選版設定中啟用。

## 檔案格式

預設路徑：`.runtime/trade_monitor/structured-market-data.json`

```json
{
  "version": 1,
  "source": "read-only-provider-name",
  "instrument": "TMF",
  "timezone": "Asia/Taipei",
  "bars": [
    {
      "time": "2026-09-02T10:39:00+08:00",
      "open": 46248,
      "high": 46261,
      "low": 46242,
      "close": 46250,
      "volume": 1234
    }
  ]
}
```

## 驗證與權威邊界

- 商品只接受 `TX`、`MTX` 或 `TMF`，但監控仍不得據此推測契約乘數、口數或金額。
- 所有時間必須含時區、對齊整分鐘、嚴格遞增且不可重複。
- 每根開高低收必須是有限數字且符合高低關係；成交量可省略，但若存在不得為負數。
- 來源最後一根必須恰好等於 scheduler 的最新已收盤 K。過期、未來、無效或不存在都不提供精確數值。
- 通過後可計算精確最新開高低收、二十一／一百零五期簡單移動平均、Wilder ATR14、開盤五／十五分鐘區間及因果 `n=2` 樞紐。
- 結構化數值與 Chrome 圖面明顯衝突時，撤回點位並回報資料不一致，不任選一方。
- 本介面不讀取或保存 Token、Chat ID、帳號、餘額、保證金、實際持倉或損益，也不能送單。

## 啟用方式

候選版本的 `local_scheduler_config.json` 內有：

```json
"structured_market_data": {
  "enabled": false,
  "source_path": ".runtime/trade_monitor/structured-market-data.json",
  "max_age_seconds": 90
}
```

正式啟用前必須先驗證來源延遲、商品、時區、斷線、跨日夜盤及錯誤資料的安全降級；不得只把 `enabled` 改成 `true` 就直接部署。
