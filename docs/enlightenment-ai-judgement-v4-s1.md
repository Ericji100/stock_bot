# 啟蒙層 AI 綜合判讀規格 V4-S1

版本：`enlightenment-ai-judgement-v4-s1`
建立日期：2026-09-08
用途：只修正 `MATURE_TREND_PULLBACK / V2_CORE（長多慣性拉回再發動完整合格）` 的朝代與局部進場位階混用問題
狀態：`DRAFT_FOR_OUTCOME_BLIND_RESEARCH（結果盲化研究草案）`

## 1. 版本定位

V4-S1 是 V3 之外的獨立研究版本，不覆寫 V1、V2 或 V3。它不增加新選股戰法，也不調整其他六條 V3 路徑、成交、部位、停損或出場規則。

```text
V4-S1
= V3 的原子問題、AI 證據格式、四情境邊界與交易規則
+ 只針對 MATURE_TREND_PULLBACK 拆開：
  CAMPAIGN_MATURITY（多頭朝代成熟度）
  LOCAL_ENTRY_STAGE（本次局部進場位階）
```

V2 已明定：「第五段、第三次攻擊或更後代不自動禁止，但必須降級並揭露剩餘空間、追價與複製失敗風險。」現行 V3 程式卻把 `COPY_LEG_5／LATER_GENERATION` 直接推成 `LATE／EXHAUSTED`，使長多情境在規則上永遠不能進場。V4-S1 修正的是這個實作矛盾，不把後代自動視為安全，也不把 `UNKNOWN` 改成 `PASS`。

## 2. 不變項

- V1、V2、V3 文件、設定、提示詞、schema、程式與既有輸出維持唯讀。
- AI 仍只回答凍結的原子問題並附當日以前證據，不直接輸出買賣決策。
- 原有四情境優先序、V2 各情境必要 gate、因果防線、Q3、大小級方向、單一指標禁令與風險可執行條件不變。
- 訊號日收盤成立、下一交易日開盤成交、同股同日去重、母單／加碼、`+2R` 狀態切換與波段出場不變。
- `MACRO_COPY_RESONANCE`、`BEAR_REVERSAL_LEFT_RIGHT`、`FRESH_Q1_EXPANSION` 與三條 V3 增量路徑不因本版取得新權限；本階段只驗證情境1。

## 3. `CAMPAIGN_MATURITY（多頭朝代成熟度）`

這個維度只回答「大結構是否已有長多慣性」，不回答「今天是否適合進場」。

| 狀態 | 定義 |
|---|---|
| `MATURE_CONFIRMED（成熟已確認）` | `LONG_CAMPAIGN_REPEATED_SUCCESS=PASS`；依該凍結原子定義，可由至少一次成功推進＋修正／複製，或跨越足夠時間的同級 HH＋HL 證明 |
| `MATURE_UNRESOLVED（成熟度未定）` | 長多反覆成功或足夠時間 HH＋HL 證據為 `UNKNOWN` |
| `NOT_MATURE（尚未成熟）` | 長多反覆成功或足夠時間 HH＋HL 證據為 `FAIL` |

`completed_prior_copy_count` 仍須記錄作世代稽核，但不得排除 V2 明定的「足夠時間 HH＋HL」成熟路徑。`COPY_LEG_5`、`LATER_GENERATION` 或同方向第三次以上攻擊，是朝代描述與風險降級標籤，不直接決定本次進場是早期或末段。

## 4. `LOCAL_ENTRY_STAGE（本次局部進場位階）`

這個維度只評估「目前這一次朝代內修正後的小級再發動」，不得使用太極世代或累計攻擊次數直接決定結果。它使用既有原子證據：

- `EXH_ATTACK_SHORTENING（攻擊縮短）`
- `EXH_SLOPE_DECAY（斜率衰退）`
- `EXH_PRICE_VOLUME_DIVERGENCE（量價背離）`
- `EXH_FAILED_CONTINUATION（延續失敗）`
- `EXH_TIME_SPACE_EXHAUSTION（時間／空間耗竭）`
- `LOCATION_REMAINING_SPACE_ADEQUATE（剩餘空間足夠）`
- `LOCATION_NOT_EXTENDED_FROM_ORIGIN（未過度遠離本次發動起點）`

判定順序固定如下：

1. `EXHAUSTED（耗竭）`
   - `EXH_TIME_SPACE_EXHAUSTION=PASS`，或
   - `LOCATION_REMAINING_SPACE_ADEQUATE=FAIL`。
2. `LATE（局部末段）`
   - `EXH_FAILED_CONTINUATION=PASS`，或
   - 四項弱化證據至少兩項為 `PASS`，或
   - `LOCATION_NOT_EXTENDED_FROM_ORIGIN=FAIL`。
3. `EARLY（局部初期）`
   - 時間／空間耗竭為 `FAIL`；
   - 剩餘空間足夠與未過度延伸均為 `PASS`；
   - 四項弱化證據全部為 `FAIL`。
4. `MIDDLE（局部中段）`
   - 時間／空間耗竭為 `FAIL`；
   - 剩餘空間足夠與未過度延伸均為 `PASS`；
   - 四項弱化證據恰有一項為 `PASS`，其餘為 `FAIL`。
5. 其他組合一律 `UNRESOLVED（未定義）`，不得交易。

優先順序必須先判耗竭、再判末段，不能因其他證據良好蓋過明確的空間不足或過度延伸。

## 5. 情境1交易條件

`MATURE_TREND_PULLBACK / V2_CORE` 只有在下列條件全部成立時才可 `TRADE（交易）`：

1. `CAMPAIGN_MATURITY=MATURE_CONFIRMED`。
2. `LOCAL_ENTRY_STAGE` 為 `EARLY` 或 `MIDDLE`。
3. V2 的八項成熟多頭必要 gate 全部為 `PASS`。
4. 監控有效、資料充分、收盤觸發完成。
5. 大級多頭防線與父代 campaign 防線均因果有效。
6. 大、小級下一可交易方向均向上，且兩級都不是 Q3。
7. 訊號具有獨立結構，不是均線、MACD、成交量、新高或單根紅 K 單獨觸發。
8. episode 防線鄰近且風險可執行。
9. 主要與替代假說不造成方向、失效或部位角色衝突。

第五代、後代或同方向第三次以上攻擊，若本次局部再發動仍是 `EARLY／MIDDLE`，可以取得核心資格，但必須輸出 `DEGRADED_LATE_GENERATION_OR_ATTACK（後代／第三次攻擊降級）`，並逐項揭露剩餘空間、追價及複製失敗風險。此研究軌的 `FIXED` 名目單位維持約 10,000 元，因此降級先反映在設定等級、報告分層及不得追價；不暗中改變凍結部位。若 `EXH_FAILED_CONTINUATION=PASS`、出現多項弱化、過度延伸或空間耗竭，必須等待或拒絕。

## 6. 可達性與執行順序

在再次呼叫 AI 前，必須先以結果盲化、匿名、只含判讀日以前資料的完整候選母體，證明：

- 新 reducer 確實存在 `MATURE_TREND_PULLBACK / V2_CORE / TRADE` 的合法輸出路徑。
- 正向控制不是由股票名稱、未來漲幅或已知績效挑選。
- 正向控制只代表規則上「可能合格」，不預填 AI 答案，也不宣稱課程判讀一定正確。
- 若可達案例不足以支撐至少3個三輪一致交易案例，必須在 AI 執行前中止。

後續順序固定為：可達性 → 三輪一致性 → 結果盲化課程符合度 → FIXED → ADD2 → 停止，不自動進入情境2。

## 7. 技術修正

V4-S1 reducer 必須使用與入口相同的 V3 validator，不得在 reducer 內再次改用 V2 validator。這只修正 `REVERSAL_PROBE` 合法例外被二次驗證誤判為 `INVALID_PACKET` 的技術問題；不授予情境1以外的新交易權限。

## 8. 研究限制

- 本版尚未通過一致性、課程符合度或績效驗證，不可用於實盤。
- 不得因可達性成立就宣稱策略有效；可達性只證明程式沒有把路徑封死。
- 不得使用期後 MFE、損益或贏家名單調整本版定義。
- 後續任何規則變更都必須另建版本，不能覆寫本版凍結成果。
