# 00635U 黃金期貨 ETF 資料契約 v1

本文件只凍結正式回測所需的資料介面。現階段 **00 尚未驗收 01 的資料快照與 manifest SHA-256**，因此不得執行或宣稱任何 00635U 正式績效；準備程式只能使用明確標示的合成夾具。

## 1. 期間與交易日

- 商品：00635U（期元大 S&P 黃金）。
- 上市日：2015-04-15。不得建立、補接或推估上市日前的 ETF 價格。
- 正式績效期間從上市日開始；技術訊號只能從該策略所需指標在 00635U 自有歷史中全部可用後開始。
- 策略 A 至少要求 MA21、MA144、MACD(21,55,55)；策略 B 另要求 KD(9,9,55)。暖機列可存在於正式期內，但不得產生訊號或成交。
- `NEXT_SESSION_OPEN` 的次一交易日必須來自封存的臺灣官方交易日曆，不得用自然日推算。

## 2. 必要資料與欄位

正式 snapshot 至少應包含下列不可變檔案，實際檔名由 manifest 宣告：

1. 00635U 官方交易日 OHLCV：`date, open, high, low, close, volume, official_session, tradable, tradability_reason`。停牌、零量或缺 open 必須明標；不得把不可成交列當成交價。正式 manifest 中此檔角色為 `etf_daily_causal`。
2. 公司行動與現金分配：生效日、除息權利鎖定日、付款日、事件 ID、每股現金、單位變更比例及來源。程式列以同一事件 ID 的 `ENTITLEMENT`／`PAYMENT` 兩階段表示，不得只按付款日當時持股推定權利；FORMAL 一律拒絕有金額但沒有 stage 的舊式股利列。
3. 臺灣交易日曆與停牌／無法成交註記。
4. 00635U 官方追蹤的 Gold ER 指數：來源日期、數值、來源時區、發布可用日期與來源版本；manifest 角色為 `gold_er_official`。
5. 臺灣加權股價報酬指數：可選 manifest 角色為 `taiex_total_return`，只供補充比較，不屬主七線；缺少時報告明標 `HOLD_MISSING_TAIEX_TOTAL_RETURN_ROLE`。
6. 若報告進行匯率歸因，需另附 USD/TWD 來源、定價時點與時區；不得把匯率序列暗中混入交易訊號。

每個價格欄位必須可追溯到來源檔、原始列與 SHA-256。缺值、重複交易日、非正價格或日期逆序一律中止，不得靜默補值。

## 3. 價格座標與公司行動

- 未還原 OHLC 是唯一成交、滑價、費稅、期末清算與買進持有帳務座標。
- 技術分析座標只供均線、MACD、KD、進出場事件與固定防守判定。
- 公司行動於當日訊號判斷前依因果順序套用；股數與技術防守價須以互相對應的比例重訂，重訂本身不是交易或停損。
- 現金分配的資格日與付款日分開保存；只有付款日才增加帳戶現金。
- 截止仍未付款的具名應收股息保留在 ledger；正式 equity／drawdown 沿用 0050 現金制，在付款前排除應收，另輸出 `equity_including_receivables_audit` 供稽核。
- 延遲到次一交易日開盤成交的訂單，若跨越公司行動，必須把訊號日防守價映射到成交日座標，不得使用成交後資訊改寫原訊號。

## 4. 時區與黃金期貨 ETF 特性

- 所有時間戳必須保存來源時區與轉換規則。臺灣收盤、美國期貨結算與 Gold ER 指數日值不是天然同一資訊集合。
- 正式報告必須揭露期貨轉倉、正逆價差、美元／新臺幣匯率、海外與臺灣交易時區錯位、兩地休市差異、追蹤誤差與追蹤缺口。
- Gold ER 指數是市場基準，不是假設可無摩擦交易的策略；TAIEX 報酬指數只在補充區呈現。
- Gold ER 對時必須明選 `STRICT_SAME_SESSION`、`COMMON_PUBLISHED_DAYS` 或 `LAST_KNOWN_PUBLISHED`。採 last-known 時每個臺灣 session 都要輸出來源日、可用日、來源時區與 stale 天數；不得無聲 forward-fill 或使用尚未發布的未來值。

## 5. Manifest 與 00 驗收閘門

正式 manifest 的最低根欄位如下：

```json
{
  "schema": "00635U_DATA_SNAPSHOT_V1",
  "symbol": "00635U",
  "listing_date": "2015-04-15",
  "immutable": true,
  "files": [
    {"path": "relative/path.csv", "sha256": "64-lowercase-hex"}
  ]
}
```

正式入口必須同時收到：

1. manifest 實體檔；
2. 00 明確接受的 manifest SHA-256；
3. `accepted_by=00`；
4. manifest 本身與每個列入檔案的雜湊完全一致。

任一條件不成立即回報 `STOP_WAIT_00_00635U_DATA`。正式 orchestrator 會在自己的 API 邊界再次驗證 manifest 實體 SHA、schema 與所有檔案雜湊；不能只依賴 CLI 外層檢查。準備版只產生明標合成的測試包，正式執行入口只有閘門完整通過後才可運行。

## 6. Git 與產物邊界

- Git 只保存程式、測試、規則、資料契約與小型人工夾具。
- 正式完整資料、圖表、報告、manifest/readback 與交易明細保存於 `.workdata`。
- 既有 0050 程式、資料與報告一律唯讀；共用程式若未來需要修改，必須先證明 0050 數值與位元組精確一致。
