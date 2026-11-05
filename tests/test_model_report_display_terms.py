import pytest

from research_center.report_display_normalizer import load_report_display_terms,normalize_report_text


@pytest.fixture(autouse=True)
def clear_terms_cache():
    load_report_display_terms.cache_clear()
    yield
    load_report_display_terms.cache_clear()


def test_actual_model_evidence_labels_are_readable_without_duplicate_parentheses():
    raw='可核實報導（verified_fact）|合理推論（reasoned_inference）|市場假說（market_hypothesis）|情緒訊號（sentiment_signal）|資料不足（insufficient）'
    rendered=normalize_report_text(raw)
    assert rendered=='已取得的報導|合理推論|市場假說|情緒訊號|資料不足'
    assert normalize_report_text(rendered)==rendered


def test_standalone_evidence_codes_and_sector_names_translate_without_changing_urls_or_numbers():
    raw='reasoned_inference | PCB／CCL | 光通訊／CPO | -2.71% [S027](https://example.com/reasoned_inference?topic=PCB)'
    result=normalize_report_text(raw)
    assert result=='合理推論 | 印刷電路板／銅箔基板 | 光通訊／共同封裝光學 | -2.71% [S027](https://example.com/reasoned_inference?topic=PCB)'
    assert normalize_report_text(result)==result
