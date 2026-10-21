# 0050 2006～2026 回測｜閱讀順序

本報告比較基本版－長線、現行 MA21＋移動道氏防線＋MACD、本次 MA21＋固定初始防守＋MACD，以及 0050 全程持有的兩種股利口徑。

1. [共享帳戶總覽](shared-account-report.md)
2. [大盤與風險比較](benchmark-risk-report.md)
3. [逐筆交易](trade-details.md)
4. [MA21 突破機會稽核](signal-opportunity-audit.csv)
5. [固定初始防守與 MACD 事件稽核](fixed-initial-stop-macd-event-audit.csv)
6. [完整逐筆交易 CSV](trade-details.csv)
7. [全部成交事件](transactions.csv)
8. [每日資產曲線資料](daily-equity.csv)
9. [逐年摘要](calendar-year-summary.csv)
10. [全期摘要](scenario-summary.csv)
11. [公司行動](corporate-actions.csv)
12. [大盤基準](benchmark.csv)
13. [資產曲線圖](equity-curves.svg)
14. [回落曲線圖](drawdown-curves.svg)
15. [輸入與口徑 manifest](input-manifest.json)
16. [產物 manifest](artifact-manifest.json)
17. [讀回驗證](readback.json)
18. [基準報告 parity](report-parity.json)
19. [QA checkpoint](checkpoint.json)


## 2005～2009 資料來源限制

2005～2009 每日未還原 OHLCV 來自 FinMind 公開端點；TWSE STOCK_DAY 不提供該期間日線，因此第一方驗證限於 FMSRFK 的 60 個月最高、最低、成交量與成交額。這不等於逐日第一方交叉驗證。本研究未使用付費 token，未用還原價成交，也未用未驗證資料補值。
