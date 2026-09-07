from zont_analyzer.reports.experiment_forms import experiment_form


def test_form_escapes_values_and_preserves_zero_and_home_time() -> None:
    markup = experiment_form({
        "category": "firmware_rollback", "parameter": '<script>alert("x")</script>',
        "before": 0, "after": "1.0", "performed_at": "2026-09-07T08:30:00+00:00",
    }, timezone="Europe/Samara")
    assert '<option value="firmware_rollback" selected>' in markup
    assert '<script>' not in markup
    assert 'value="0"' in markup
    assert 'value="2026-09-07T12:30:00"' in markup
    assert 'type="datetime-local"' in markup
    assert ' required' not in markup
