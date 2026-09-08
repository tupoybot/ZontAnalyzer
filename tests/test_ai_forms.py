import re

from zont_analyzer.reports.ai_forms import render_ai_forms


def test_ai_form_is_lazy_and_uses_expected_api_contract() -> None:
    rendered = render_ai_forms({"ai_review": {"status": "due"}})
    assert 'data-ai-settings' in rendered
    assert 'data-ai-hint="due"' in rendered
    assert "fetch(apiBase + path" in rendered
    assert "api('/ai')" in rendered
    assert "api('/ai/review'" in rendered
    assert "expected_version" in rendered
    assert "review_interval_days" in rendered
    assert "credentials: 'same-origin'" in rendered
    assert "Открытие этого раздела не запускает генерацию" in rendered


def test_ai_form_does_not_interpolate_dynamic_review_values_as_markup() -> None:
    rendered = render_ai_forms({"ai_review": {"status": '</script><img src=x>'}})
    assert '</script><img' not in rendered
    assert '&lt;/script&gt;&lt;img src=x&gt;' in rendered
    assert 'textContent' in rendered
    assert 'innerHTML' not in rendered


def test_ai_form_has_settings_controls_and_bounded_polling() -> None:
    rendered = render_ai_forms()
    for marker in (
        'data-ai-enabled', 'data-ai-daily-model', 'data-ai-review-model',
        'data-ai-daily-effort', 'data-ai-review-effort', 'data-ai-review-enabled',
        'data-ai-review-interval', 'data-ai-reset', 'data-ai-history',
        'data-ai-accept', 'data-ai-reject', 'data-ai-defer',
    ):
        assert marker in rendered
    assert re.search(r'attempt < 8', rendered)
