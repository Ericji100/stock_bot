# enlightenment-integrated-v1.4-dow-analysis

- 新增一級 `n=2`、二級 `n=1` 樞紐生命週期：候選、局部成立、配對確認、失效與取代。
- 新增不顯示給使用者的 `market_structure_state`，以原子方式保存樞紐、正式大小防線、暫代邊界及首次確認時間。
- 收緊正式道氏防線資格；OR、箱頂底與一般高低點不會自動冒充防線。
- 整合 Type 1／2／3、級數升級及左左／左右／右左／右右成熟度，仍只從原四型態決定是否可進場。
- Type 1 維持警告用途，不單獨反手；Type 2 為一般有效翻向，Type 3 新方向必須等右左。
- 一口單不採課程的四份部位或加碼；無每日模擬進場硬性上限規則不變。
- 使用 `trade-monitor-analysis-v3` 與 contract v3；固定九欄、`constitution_event`、resume guard 與雙模式可見格式不變。
- `market_structure_state` 是內部欄位，不增加第十個通知章節；既有 v2 payload 仍由 contract 相容處理。
