# AI混合回放的權責與盤後分歧審核

本文件只適用於隔離的`trade_monitor_replay`實驗，不接入正式監控、Telegram Bot指令或排程。

## 當下因果判讀

- 程式權威：已收盤OHLCV、指標數值、時間、可引用樞紐／波段／防線／結構事件候選、成交、硬停損、成本及事件生命週期。
- AI權威：候選是否具有課程上的控制意義、背景／工作錨、推進或修正、大小級控制、道氏解讀、Q1～Q4、太極／一之／左右／X流程、情境權重與交易取捨。
- 驗證器只檢查因果、引用、價格與AI自身結論的一致性；不得在同一根K有多個候選時替AI選主事件，也不得以未被AI接受的程式候選改寫AI趨勢。
- 每個AI分析點將比較紀錄追加到`semantic-comparisons.jsonl`。其中程式欄位明確標示為`EVIDENCE_CANDIDATES_ONLY`，分歧的初始`verdict`及`winner`均為空白／未解決。

## 盤後使用未來資料

只有原始AI判讀已驗證並保存，而且run不是`running`狀態時，才能執行：

```powershell
python -m trade_monitor_replay.posthoc_adjudication --run-id <RUN_ID>
```

預設附加判讀後3、5、10及20根K的收盤變化、MFE、MAE與區間，輸出至：

```text
.runtime/trade_monitor_replay/runs/<RUN_ID>/posthoc-semantic-adjudication.json
```

盤後工具不修改原始分析、memory、持倉、交易統計或TG訊息，也不依單次漲跌自動選出程式或AI為贏家。課程判讀仍需核對當時可見證據、級數與戰法應有行為；未來走勢只是結果證據。

若分歧顯示通用缺陷，應修改規則或程式並提升執行版本，再從頭做因果回放。不得直接把事後較漂亮的答案回填到原run。
