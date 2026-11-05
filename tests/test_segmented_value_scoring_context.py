from datetime import date

import pytest

from research_center.models import CommandRequest
from research_center.prompt_registry import _mode_supplement, _scoring_rules_for_request, _template_for_request
from research_center.segmented_analysis_service import _build_final_prompt, _build_compact_final_retry_prompt, _final_scoring_context


@pytest.mark.parametrize('mode',['normal','deep'])
def test_value_final_and_retry_keep_same_canonical_rules_and_grades(mode):
    req=CommandRequest('value_scan','/value_scan',mode=mode,report_date=date(2026,11,4))
    for prompt in (_build_final_prompt(req,{},[],[]),
                   _build_compact_final_retry_prompt(req,{},[],[],RuntimeError('synthetic'))):
        assert _scoring_rules_for_request(req) in prompt
        assert _template_for_request(req) in prompt
        if mode=='deep':assert _mode_supplement(req) in prompt
        assert '不得自行創造新的正式評分權重' in prompt
        assert '85～100：高度重估候選' in prompt
        assert '0～49：暫不構成價值重估' in prompt
        assert '股票標籤重估模型規則' in prompt
        assert '題材新鮮度' in prompt and '資金確認' in prompt


def test_other_commands_do_not_receive_value_specific_scoring_context():
    for command in ('research','macro','theme','theme_flow','theme_radar','sector_strength'):
        assert _final_scoring_context(CommandRequest(command,'/'+command))==''
