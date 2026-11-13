# 0050 證交所開休市 API 換年修正驗收（2026-11-13）

11/13 12:45 盤中預覽及 13:55 收盤後正式判讀都在行情查詢前中止。稽核記錄的原因是 `official TWSE holiday receipt does not cover target year`。證交所未指定年度的 OpenAPI 已回傳民國 116 年日曆，當日作業仍需要民國 115 年資料。原資料異常通知的 Telegram 訊息編號為 10639、10640。[證交所 OpenAPI 開休市資料](https://openapi.twse.com.tw/v1/holidaySchedule/holidaySchedule)

交易日檢查與日資料擷取改向證交所指定目標年度的 1 月 1 日（`date=YYYY0101`）。驗證會核對官方網址與查詢參數、回應的年度、欄位及逐列日期。跨年補資料時，每日帳本引用其所屬年度的原始回應，並以 SHA-256 驗證。舊版 OpenAPI 日曆收據仍依原規格讀取和驗證。臨時休市仍由獨立的證交所公告檢查判定。[證交所 2026 年開休市資料](https://www.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date=20260101&response=json)

## 測試與正式驗收

- 0050 監控回歸共 324 項，323 項通過、1 項跳過。跳過的是需要在此 checkout 安裝正式凍結 ETF registry 的完整歷史回放。回歸涵蓋換年後仍按指定年份取數、拒絕年份不符或參數重複的回應、跨年日資料各用所屬年度的收據，以及舊收據讀取和臨時休市判定。
- 以證交所實際連線驗證 2026、2027 年日曆，確認 11/13 開市與 12/25 休市。隔離驗證中，11/13 13:30 官方收盤成交價和同日完整日行情都為 102.50 元。此輪隔離驗證未發送通知。
- 14:21:58 由原 watchdog 啟動新版 Bot，14:22:04 監控就緒。原恢復流程補跑當天的正式判讀。22:00 作業依原定排程啟動，擷取並驗證完整官方日資料，新增日資料帳本採用年度查詢收據，原件雜湊核對一致。

| 作業 | 執行結果（臺北時間） | Telegram 送達回執 |
| --- | --- | --- |
| 13:55 收盤後正式判讀 | 14:22:07 補跑，14:22:24 完成。收盤 102.50 元，空手等待（HOLD）。 | `10642`，14:22:24 送達，1 次發送 |
| 22:00 晚間完整資料確認 | 22:00:00 啟動，22:00:14 完成。完整日資料確認，與下午判讀一致。 | `10709`，22:00:14 送達，1 次發送 |

22:00:43 讀取正式資料庫複查，兩次作業均為 `SUCCEEDED`，當晚無資料失敗，SQLite `quick_check=ok`。部署前後的歷史事件與其 outbox 證據雜湊一致，設定檔雜湊一致，監控以影子模式執行。12:45 時點的報價未保存，盤中預覽無法還原，原失敗紀錄保留。

## 原始驗收證據

原件保存在本機 `.workdata/qa/etf-calendar-20261113/`，下表為該次留存檔案的 SHA-256。正式 SQLite、日行情原始回應及測試暫存不納入 Git。正式日資料帳本所用年度日曆原件 SHA-256 為 `7fefe785ea7155a5004a2eb74486ad865ea5c4b5f02ee0cffbbdacb1ca2ea390`。

| 檔案 | SHA-256 |
| --- | --- |
| `tests-final.xml` | `af252e5923e8acc564dfad59a7e86dcb5df1b024cb265dceacc8783abb10f5d4` |
| `official-data-verification.json` | `1ee594cf1214c18d734de23cea996024f860c3169062cf2cad5515135ac9d780` |
| `production-before.json` | `54f4caefae08c194182718194fae17c607165dc542fb2435cc11dc25e9cfcee1` |
| `production-afternoon.json` | `7233f55b2d6d3f978f0c1d83a07c9e6315b2eae6ea59d8ec1c27e4e55dca9b53` |
| `production-evening.json` | `3e50d76268cbee104646a6de29c9fd0840a10565bc769b2029c7c49237343c17` |
