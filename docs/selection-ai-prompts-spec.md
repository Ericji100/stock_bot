# 選股相關 AI Prompt 規格與優化建議

本文件盤點目前選股相關流程實際會使用的 AI prompt，並整理可優化方向。本文只描述規格與建議，不代表已修改 prompt 或程式邏輯。

## 範圍定義

本地選股本身不直接呼叫 AI prompt：

- `/scan`
- 技術面選股
- 籌碼選股
- 本地分數、Radar 分數、技術策略、籌碼策略計算

會使用 AI prompt 的，是選股後續的 AI 報告或短評流程：

- `/radar --model ...` 的 AI 短評
- `/value_scan` 與 `/value_scan --deep`
- `/research --score` 與 `/research --deep`
- 搜尋整理代理 prompt
- 低階模型資料整理 prompt

## Prompt 組合總覽

一般 AI 投研報告由 `research_center/prompt_registry.py::build_prompt_from_request()` 組合。順序如下：

```text
prompt/base/base.md
→ 指令主模板
→ 模式補充模板
→ 歷史日期規則
→ 搜尋規則
→ 評分與重估底稿
→ prompt/rules/report_context.md
→ prompt/rules/local_scoring_and_ai_final_scoring.md
→ 指令與模式專用 rules
→ 最終 Markdown 輸出要求
```

## 流程一：Radar AI 短評

### 觸發方式

- `/radar --model gemini`
- `/radar --model deepseek`
- `/radar --model minimax`
- 排程 Radar 推播若指定模型，也會走此流程

### Prompt 位置

- 程式內硬寫：`radar_service.py::_build_ai_comment_prompt()`

目前不是 md 檔。

### 送入資料

- Radar 候選股清單
- `ai_compact_pack`
- `data_coverage`
- 技術分、營收分、籌碼分、題材分、族群分、Radar 總分
- 外部來源摘要
- Radar 輕量 research pack 或既有 research structured cache

### AI 任務

只輸出 JSON 短評，不新增候選股、不改本地分數。

輸出欄位：

```json
{
  "comments": [
    {
      "code": "2330",
      "priority": "高/中/低",
      "confidence": "高/中/低",
      "reason": "短評",
      "risk": "風險",
      "watch": "觀察"
    }
  ]
}
```

### 主要限制

- 只能根據輸入資料判斷。
- 不得新增股票。
- 不得改變本地分數。
- `reason`、`risk`、`watch` 必須使用繁體中文。
- 資料不足時需降低 `priority` 與 `confidence`。

### 目前問題

1. `_build_ai_comment_prompt()` 在 `radar_service.py` 中重複定義；Python 只會保留後面的定義，前一版較嚴格的規則會失效。
2. prompt 寫在程式裡，不利人工維護與版本控管。
3. `priority` 與 `confidence` 缺少硬性上限規則，例如：
   - 只有技術訊號、沒有來源，不得給高信心。
   - 缺法人或營收資料時，信心上限應降低。
   - 只有新聞或題材情緒時，不得給高優先度。
4. JSON schema 有要求，但目前主要靠解析容錯；可補 contract test。

### 優化建議

1. 將 prompt 移到 `prompt/radar/radar_ai_comment.md`。
2. 保留單一 `_build_ai_comment_prompt()`，改成讀 md。
3. 增加分級規則：
   - `priority=高` 必須同時具備：本地分數高、至少一項基本面或籌碼確認、資料覆蓋不低、主要風險可控。
   - `confidence=高` 必須有官方或主流來源支撐，且 `data_coverage` 不可為 insufficient。
   - 只有技術訊號時，`confidence` 最高為中。
   - 只有題材或新聞熱度時，`priority` 最高為中。
4. 增加測試：
   - prompt 必須包含 `ai_compact_pack`
   - prompt 必須禁止新增股票與改分
   - prompt 必須要求嚴格 JSON
   - prompt 必須包含資料不足時降級規則

## 流程二：Value Scan 一般模式

### 觸發方式

- `/value_scan`
- `/value_scan 精選選股`
- `/value_scan 選股雷達`
- `/value_scan 監控清單`

### Prompt 位置

- 主模板：`prompt/report/value_scan.md`
- 共用規則：
  - `prompt/base/base.md`
  - `prompt/rules/report_context.md`
  - `prompt/rules/local_scoring_and_ai_final_scoring.md`
  - `prompt/rules/rerating_snapshot_rules.md`
  - `prompt/rules/source_quality_rules.md`
  - `prompt/rules/risk_and_counter_evidence_rules.md`
- 評分底稿：
  - `prompt/scoring/股票標籤重估模型.md`
  - `prompt/scoring/股票量化評分標準.md`

### 送入資料

優先使用 `ai_candidate_evidence_pack`，包含：

- 候選股基本資料
- 本地重估分數
- 技術、營收、籌碼、題材、Radar 等底稿
- 題材庫背景
- 來源與資料缺口
- `value_scan_sort_policy`
- `early_signal_priority`

### AI 任務

產出價值重估掃描報告，核心是判斷候選股是否發生「舊標籤 → 新標籤」的市場重估。

### 主要限制

- 不得只因本地分數高就給高 AI 分數。
- 不得只因新聞熱門、低股價、論壇討論或短線情緒給高分。
- 必須區分已驗證加分、推論型加分、情緒型參考。
- 必須說明 AI 排名與本地排序差異。
- 技術面只能作為確認，不得單獨支撐高價值重估分。

### 目前問題

1. `value_scan.md` 的章節編號有重複：
   - `## 十一、指定章節`
   - `## 十一、各章節要求`
2. 「前 N 名摘要」格式附近出現 code fence 風險，後面的「早期異動保留規則」可能被模型誤判成範例的一部分。
3. 一般模式 prompt 很長，且多處與共用規則重複，可能稀釋輸出格式要求。
4. 對 `early_signal_priority` 的規則已存在，但位置偏後，容易被忽略。

### 優化建議

1. 修正章節編號與 code fence。
2. 將「早期異動保留規則」提前到輸出範圍之前。
3. 在排名規則中明確要求分成兩類：
   - 交叉確認型
   - 早期異動保留型
4. 將重複的「不得照抄本地分數」保留在共用規則，主模板只寫本流程特有要求。
5. 增加 prompt contract test，檢查：
   - 無未閉合 code fence
   - 指定章節唯一且編號不重複
   - 包含 `early_signal_priority`
   - 包含「交叉確認型」與「早期異動保留型」

## 流程三：Value Scan Deep 模式

### 觸發方式

- `/value_scan --deep`

### Prompt 位置

- 主模板：`prompt/report/value_scan.md`
- deep 補充：`prompt/report/value_scan_deep.md`
- 共用與評分規則同一般模式

### AI 任務

產出深度價值重估分析，逐檔分析最多前 N 名，檢查重估證據、財報營收、籌碼法人、技術面、反證與未來觀察指標。

### 主要限制

- 不得只分析第一名。
- 不得用「以某檔為例」取代完整分析。
- 深度分析最多前 30 名。
- 若資料不足，必須降低確定性。

### 目前問題

1. `value_scan_deep.md` 結構完整，但內容與 `value_scan.md` 和共用 rules 有大量重複。
2. 深度模式是補充模板，但實際 prompt 中會同時包含一般模板與 deep 模板，可能造成章節要求互相重疊。
3. deep 模板對「後續研究優先度」的分類可更明確，避免輸出近似買賣建議。

### 優化建議

1. 將 `value_scan.md` 設為共用基礎，`value_scan_deep.md` 只保留 deep 差異。
2. 或改成 deep 模式直接使用 `value_scan_deep.md` 作主模板，不再同時載入一般模板。
3. 明確定義研究優先度：
   - A：可優先做單股深度研究
   - B：等待營收/法說/公告驗證
   - C：僅保留觀察
   - D：資料不足或蹭題材風險高
4. 禁止使用「買進」、「加碼」、「追價」等語句維持不變。

## 流程四：Research Score / Deep

### 觸發方式

- `/research 股票 --score`
- `/research 股票 --deep`

### Prompt 位置

- `prompt/report/research_score.md`
- `prompt/report/research_deep.md`
- `prompt/base/base.md`
- `prompt/rules/local_scoring_and_ai_final_scoring.md`
- `prompt/rules/quantitative_score_rules.md`
- `prompt/rules/rerating_snapshot_rules.md`
- `prompt/rules/chip_score_rules.md`
- `prompt/rules/technical_score_rules.md`
- `prompt/rules/source_quality_rules.md`
- `prompt/rules/risk_and_counter_evidence_rules.md`

### 評分底稿

- `prompt/scoring/股票量化評分標準.md`
- `prompt/scoring/股票標籤重估模型.md`

### AI 任務

單股評分或深度研究，輸出：

- AI 最終財務與題材評分
- AI 最終飆股基因評分
- AI 最終價值重估評分
- AI 最終研究用買入評分 1～5 分
- 本地量化底稿與 AI 最終評分差異
- 風險與反證
- 後續觀察指標

### 目前問題

1. `research_score.md` 已明確要求不得創造新權重，但底稿仍是兩份長文件合併後截斷。
2. 評分標準檔案過長時，即使字元上限提高，仍可能出現前段規則比後段規則更容易被模型遵守的情況。
3. 「研究用買入評分」容易被模型寫成交易建議，雖然目前已有禁止語句，仍可加強語義。

### 優化建議

1. 將 `股票量化評分標準.md` 拆成：
   - `financial_hard_metrics.md`
   - `theme_soft_metrics.md`
   - `high_growth_gene.md`
   - `final_research_score.md`
2. `research --score` 載入完整評分規則。
3. `value_scan` 只載入與重估和候選排序直接相關的規則。
4. 將「AI 最終研究用買入評分」改名或補充為「研究優先度與風險報酬評估」，降低交易建議歧義。

## 流程五：搜尋整理 Prompt

### Prompt 位置

- `prompt/discovery/discovery_task.md`
- `prompt/rules/discovery_value_scan.md`

### AI 任務

只做搜尋結果整理、正文摘要、來源分級、資料缺口標記，不產出最終投資結論。

### 主要限制

- 不得只靠模型記憶。
- 不得產出買賣建議。
- snippet 不能作為強證據。
- Level 1 / Level 2 來源優先。
- 需輸出 JSON。

### 目前問題

1. `discovery_value_scan.md` 很短，只寫泛用目標，對選股品質幫助有限。
2. 搜尋任務可以更精準區分：
   - 官方公告與月營收
   - 法說會與公司展望
   - 新產品/新客戶
   - 反證與風險
   - 同族群比較

### 優化建議

為 `/value_scan` 增加專用 discovery query groups：

- `公司代號 公司名稱 月營收 YoY 毛利 EPS`
- `公司名稱 法說會 展望 新產品 客戶`
- `公司名稱 公告 MOPS 重大訊息`
- `公司名稱 產業趨勢 供應鏈 產品`
- `公司名稱 風險 衰退 庫存 毛利 下滑`
- `公司名稱 法人 投信 外資 籌碼`

並要求每組來源回傳時標示用途：

- 支持重估
- 支持反證
- 只作情緒
- 資料不足

## 流程六：低階模型資料整理 Prompt

### Prompt 位置

- 程式內硬寫：`research_center/ai_workflow_service.py::build_low_model_digest_prompt()`

### AI 任務

MiniMax M2.7 等低階模型只做資料整理：

- facts
- events
- risk_evidence
- counter_evidence
- missing_data
- source_map
- warnings

### 主要限制

- 嚴禁產出最終投資結論。
- 不得輸出買入評分、目標價、買賣建議。
- 只輸出 JSON。

### 目前問題

1. prompt 寫在程式裡，不易維護。
2. 與報告主 prompt 的資料角色分工清楚，但可補一個 md 模板，讓規則集中。

### 優化建議

1. 移到 `prompt/workflow/low_model_digest.md`。
2. 增加 JSON schema contract test。
3. 保留現有壓縮資料邏輯，不改資料流程。

## 評分底稿拆分建議

目前 `_scoring_rules_for_request()` 會讀取：

- `prompt/scoring/股票量化評分標準.md`
- `prompt/scoring/股票標籤重估模型.md`

並以 `SCORING_RULES_CHAR_LIMIT = 36000` 截斷。

建議拆分：

| 新檔案 | 用途 | 適用流程 |
|---|---|---|
| `financial_hard_metrics.md` | 財務、營收、毛利、EPS、現金流硬指標 | `/research --score`、`/research --deep`、`/value_scan --deep` |
| `theme_soft_metrics.md` | 題材、產品、客戶、供應鏈、新聞可信度 | `/research`、`/value_scan` |
| `high_growth_gene.md` | 飆股基因、量價、籌碼、成長轉折 | `/research --score`、`/value_scan --deep` |
| `final_research_score.md` | 1～5 研究優先度與風險報酬評估 | `/research --score`、`/research --deep` |
| `rerating_model.md` | 舊標籤、新標籤、重估證據、蹭題材風險 | `/value_scan`、`/research --score` |

載入策略：

- `/value_scan normal`：載入 `rerating_model.md`、`theme_soft_metrics.md`、簡版 `financial_hard_metrics.md`
- `/value_scan deep`：載入 `rerating_model.md`、`financial_hard_metrics.md`、`theme_soft_metrics.md`、`high_growth_gene.md`
- `/research --score`：全部載入
- `/research --deep`：全部載入，但可用章節摘要版降低 prompt 長度

## 優先級建議

### P0：先修正會直接影響輸出穩定性的問題

1. 移除或合併 `radar_service.py` 重複 `_build_ai_comment_prompt()`。
2. 修正 `value_scan.md` 章節編號與 code fence 風險。
3. 新增 prompt contract test。

### P1：提升維護性

1. 將 Radar AI 短評 prompt 移到 md。
2. 將低階模型資料整理 prompt 移到 md。
3. 將評分底稿拆成多個 md。

### P2：提升報告品質

1. 強化 Radar `priority` / `confidence` 分級規則。
2. 強化 `/value_scan` 早期異動與交叉確認分類。
3. 強化搜尋 query group，讓反證與官方資料更穩定進入 prompt。

## 建議實作順序

1. 新增 prompt contract tests，不先改功能。
2. 整理 `value_scan.md` 格式與章節。
3. 整理 Radar AI 短評 prompt，先合併重複函式，再移到 md。
4. 拆分評分底稿，修改 `_scoring_rules_for_request()` 精準載入。
5. 補強 `/value_scan` discovery query groups。
6. 跑完整測試。

## 驗收標準

1. `/radar --model ...` AI 短評仍能解析 JSON。
2. Radar prompt 不允許新增候選股、不允許改本地分數。
3. `/value_scan` prompt 無未閉合 code fence。
4. `/value_scan` 輸出章節唯一且順序穩定。
5. `/value_scan` 能區分：
   - 交叉確認型
   - 早期異動保留型
   - 蹭題材風險偏高
6. `/research --score` 仍能取得完整評分規則。
7. 完整 `unittest discover` 通過。
