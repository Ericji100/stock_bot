"""Build copyable prompts for external high-end AI topic-library seeding."""
from __future__ import annotations

from .topic_maintain_service import _load_prompt


_FALLBACK_TOPIC_SEED_PROMPT = """你是熟悉台股、產業鏈、題材輪動與公司基本面的高階投研分析 AI。

請只輸出 JSON object，不要輸出 Markdown、code fence 或 JSON 以外的說明。

這份 JSON 會由 `/topic_import` 匯入，轉成 change pack，經 `/topic_confirm` 後寫入四個正式檔案：
1. `config/theme_profiles.json`：來自 `actions[].theme_id`、`theme_name`、`keywords`、`industries`、`supply_chain_role`、`risk_notes`、`missing_data`。
2. `config/company_theme_map.json`：來自 `actions[].company_relations`。
3. `config/supply_chain_nodes.json`：來自 `actions[].supply_chain_nodes`。
4. `config/company_knowledge.json`：來自 `company_knowledge_updates.companies`。

頂層必須包含：
`mode`、`summary`、`confidence`、`actions`、`company_knowledge_updates`、`warnings`、`sources`。

`mode` 請使用 `initial` 或 `update`。
若 `actions` 全部都是 `update_theme`，不得輸出 `"mode": "initial"`，必須輸出 `"mode": "update"`。
`actions` 至少 12 筆，每筆必須包含：
`action_type`、`theme_id`、`theme_name`、`keywords`、`industries`、`supply_chain_role`、`confidence`、`reason`、`evidence`、`company_relations`、`affected_companies`、`supply_chain_nodes`、`risk_notes`、`missing_data`、`counter_evidence`。

每個正式代表公司至少要同時出現在：
`company_relations`、`affected_companies`、`supply_chain_nodes`、`company_knowledge_updates.companies`。
每一筆 `company_relations` 中，只要 `verification_status` 是 `verified` 或 `inferred`，就必須在 `company_knowledge_updates.companies` 補同一個 `company_code`。
`company_knowledge_updates.companies` 每家公司至少要補：`company_name`、`product_lines`、`customers`、`revenue_exposure`、`supply_chain_roles`、`evidence_sources`、`risk_notes`、`missing_data`。

`company_relations` 必須包含公司代號、公司名稱、題材 ID、角色、關聯強度、關聯類型、驗證狀態、產品、客戶、營收曝險、受惠邏輯、證據、反證與資料缺口。
`supply_chain_nodes` 必須包含 node_id、theme_id、company_code、company_name、layer、role、upstream、downstream、product_keywords、customers、revenue_exposure、benefit_logic、confidence、source_level、evidence、risk_notes、missing_data。
`company_knowledge_updates.companies` 必須包含 company_name、product_lines、customers、revenue_exposure、supply_chain_roles、evidence_sources、risk_notes、missing_data。

每個 evidence 必須包含 `source`、`source_level`、`content`，若可取得請補 `url` 與 `publish_date`。
每個 action 都必須嘗試補 `counter_evidence`；如果真的查不到反證，不能只留空陣列，請在該 action 的 `missing_data` 寫入「尚未找到明確反證，需後續追蹤」。
請使用即時外部網路資料搜尋；證據不足請標示 `candidate`、`missing` 或寫入 `missing_data`，不要捏造。"""


def build_topic_seed_prompt() -> str:
    """Return a copy-paste prompt for external deep research AI tools."""
    prompt = _load_prompt("topic_seed_prompt")
    return prompt or _FALLBACK_TOPIC_SEED_PROMPT
