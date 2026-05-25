"""Build copyable prompts for external high-end AI topic-library seeding."""
from __future__ import annotations

from .topic_maintain_service import _load_prompt


_FALLBACK_TOPIC_SEED_PROMPT = """你是熟悉台股、半導體、AI 供應鏈、電子零組件、傳產循環、金融、政策題材與產業新聞驗證的高階投研分析 AI。

請只輸出 JSON object。

請建立或回填台股題材庫，內容必須包含 summary、confidence、actions、warnings、sources。
actions 至少 12 筆；每筆 action 需包含 theme_id、affected_companies、company_relations、supply_chain_nodes。
不得捏造 revenue_exposure 或營收占比；無法確認時使用 unknown 並寫入 missing_data。
"""


def build_topic_seed_prompt() -> str:
    """Return a copy-paste prompt for external deep research AI tools."""
    prompt = _load_prompt("topic_seed_prompt")
    return prompt or _FALLBACK_TOPIC_SEED_PROMPT
