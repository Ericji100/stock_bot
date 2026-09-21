# v1.8 戰法 C 班正式部署報告

狀態：正式 Automation 與 scheduler 已成組切換並讀回驗證；Automation 保持 `PAUSED`。

部署前基線：

- active-version：`enlightenment-integrated-v1.7-anchor`
- Automation `1-k`：`PAUSED`
- Automation TOML SHA-256：`69a395850189f7edd2471c423eb51fdaa5997960a93336bd5bbf90c3883d6684`
- 正式 scheduler config SHA-256：`1511e1b171b25692d7666b93e6f9dd06d912eca386e9820790f216321e760c29`
- 還原點：`pre-cclass-v1.7-20260902`

部署結果：

- active-version：`enlightenment-integrated-v1.8-cclass`
- Automation execution prompt SHA-256：`510eddf16b13f0820aba06c9ee59ea13cf89e25b50eff5dc24ce849da191248e`
- analysis prompt SHA-256：`653137176451710e8f549083be764083fc0a95989f08cc70ca2c05dc0aa56397`
- schema v6 SHA-256：`cce2a76baf06a119bd8f0c9914d59d045d2115e3e7697f9046a89e6f79ddbec8`
- 正式 scheduler config SHA-256：`c6f7925518ad13611694e62f12b96cd09f61cad6f0fbfae65359f3a6d8dfbcea`
- Automation TOML SHA-256：`882e6f1d514cf4caa7ff665fbe407c2d9ac3a06bd3926c54722d6106ce53b4fd`
- 保留欄位：Automation ID `1-k`、名稱、`FREQ=MINUTELY;INTERVAL=1`、target thread、通知政策與 `PAUSED` 狀態均未變。
- 狀態遷移：正式 runtime 仍保留既有 state v1；只有未來使用者明確啟動後，第一份通過 v6 驗證的分析才會在記憶中因果遷移並原子升級為 state v4。若驗證失敗則維持舊狀態並 fail-safe。
- 部署期間未擷取圖表、未執行正式分析、未傳送 Telegram。
- 還原點：`pre-cclass-v1.7-20260902`。
- 逐根回放：22,791 根、40 個 session；n=2 保留，n=3 未提供足夠改善證據。
- 隔離 Shadow：三輪通過，分析時間 88.197／79.558／76.436 秒，平均 81.397 秒；Telegram 全為 dry-run。
- 正式測試：`173 passed`；版本雜湊驗證通過。
