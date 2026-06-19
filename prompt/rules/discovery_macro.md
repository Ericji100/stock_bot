Gemini / MiniMax Search 任務：為 `/macro` 補找總經、市場風險溫度、台股衍生品、法人資金與反證資料。搜尋整理不是總經結論，不得捏造正式指標。

## 搜尋目的

`/macro` 的搜尋重點是「市場風險溫度」與「台股資金結構」，不是一般新聞摘要。搜尋結果需覆蓋：

1. 國際風險溫度：VIX、美債殖利率、美元指數 DXY、油價、黃金、Fed、CPI、PCE、升降息預期。
2. 台股衍生品：台指期、夜盤、外資期貨淨多空、台指選擇權 Put/Call ratio、未平倉、波動率。
3. 台股資金流：三大法人、外資、投信、自營商、融資融券、成交量、類股資金輪動。
4. 恐慌 / 貪婪 proxy：CNN Fear & Greed、VIX、台股融資、台指期、選擇權、成交量與市場寬度。
5. 區域與政策風險：中國、歐洲、日本、美國、關稅、戰爭、原物料、供應鏈、匯率、央行政策。
6. 反證與壓力測試：流動性收縮、信用壓力、美元走強、油價急升、台幣急貶、外資撤出。

## 重要限制

- VIX 是美股波動率，不得直接稱為台股恐慌指數。
- 若沒有正式台股 Fear & Greed index，必須標示為「本地 proxy」或「情緒 proxy」，不得假裝是正式指標。
- 台指選擇權 IV、Put/Call、未平倉、外資期貨部位若沒有官方或可靠來源，不得硬填數字。
- TAIFEX、TWSE、TPEx、央行、FRED、CME、官方統計與主流財經媒體優先。
- 社群與 YouTube 只能作情緒參考，不得作為宏觀結論核心證據。

## finding 要求

- 每個 finding 必須標示屬於 `global_risk_temperature`、`taiwan_derivatives_risk`、`taiwan_market_flow`、`fear_greed_proxy`、`geo_policy_macro_risk` 或 `macro_counter_evidence`。
- 若資料不足，請明確寫入 `missing_data`，例如「缺正式台指選擇權 IV」、「缺外資期貨最新淨部位」。
- 需要同時保留 risk-on 與 risk-off 的證據，不得只挑單邊敘事。
