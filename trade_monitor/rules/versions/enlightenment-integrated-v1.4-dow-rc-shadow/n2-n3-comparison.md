# 道氏樞紐參數 n=2／n=3 回放比較

- 來源：`C:\Users\紀成達\Downloads\tmf_chart_b7d18f803ee54d838cc3194ef647a8bb.html`
- 資料標題：TMF 交互式分析圖表 2026-08-01 ~ 2026-08-30
- 1 分 K 根數：22791
- 納入交易時段根數：22791

| 指標 | n=2 | n=3 |
|---|---:|---:|
| 局部成立樞紐 | 5101 | 3680 |
| 配對確認樞紐 | 3018 | 2456 |
| 二級樞紐 | 722 | 587 |
| 每千根局部樞紐 | 223.816 | 161.467 |
| 確認延遲中位數（根） | 2.0 | 3.0 |
| 同向更極端點取代 | 452 | 334 |
| Type 1 proxy 警告 | 173 | 129 |
| Type 1 proxy 假警告率 | 0.9557 | 0.9646 |

## 重疊度

- 共同局部樞紐：3680
- 僅 n=2：1421
- 僅 n=3：0
- Jaccard：0.7214

## 日／夜盤與時段密度

| 時段 | 根數 | n=2 局部樞紐／千根 | n=3 局部樞紐／千根 |
|---|---:|---:|---:|
| DAY_FIRST_HOUR | 1200 | 225.0 | 164.167 |
| DAY_GENERAL | 4791 | 223.544 | 159.674 |
| NIGHT_FIRST_HOUR | 1200 | 212.5 | 153.333 |
| NIGHT_GENERAL | 15600 | 224.679 | 162.436 |

## 限制

- This compares causal pivot mechanics only; it is not a profitability backtest.
- Type 1 false-warning rate is an explicitly defined structural proxy, not a course-claimed win rate.
- Costs, slippage, four-pattern admission, quadrants and exits are not included.
