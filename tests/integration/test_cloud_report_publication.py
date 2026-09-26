"""An upload interruption resumes the saved report without repeating source or AI work."""
from unittest.mock import Mock

import pytest

from tests.integration.test_cloud_report_jobs import _runner, _seed_archive
from zont_analyzer.cloud.report_jobs import ReportRequest


@pytest.mark.ydb
def test_report_job_retries_publication_after_canonical_save(tmp_path, monkeypatch):
    db, runner, client = _runner(tmp_path)
    payload = {'kind': 'daily', 'date': '2026-09-23', 'max_requests': 2, 'use_ai': False}
    _seed_archive(db, runner, payload)
    monkeypatch.setenv('CLOUD_PUBLICATION_BUCKET', 'test-bucket')
    publish = Mock(side_effect=[RuntimeError('upload interrupted'), {'reports': 1}, {'reports': 1}])
    monkeypatch.setattr('zont_analyzer.application.publication.publish_reports', publish)
    assert runner.run(payload)['status'] == 'pending'
    assert runner.run(payload)['phase'] == 'analyze'
    with pytest.raises(RuntimeError, match='upload interrupted'):
        runner.run(payload)
    period = runner.period(ReportRequest.model_validate(payload))
    assert db.jobs.get(period.job_key).state != 'done'
    assert len(db.storage.execute('SELECT id FROM reports;')[0].rows) == 1
    calls = client.history_calls, client.event_calls
    # A new analysis would replace the stored report; retry must take its saved checkpoint.
    monkeypatch.setattr('zont_analyzer.application.analysis.AnalysisService.analyze_daily',
                        Mock(side_effect=AssertionError('analysis repeated')))
    result = runner.run(payload)
    assert result['status'] == 'done' and result['publication'] == {'reports': 1}
    assert db.jobs.get(period.job_key).state == 'done'
    assert runner.run(payload)['reused'] is True
    assert (client.history_calls, client.event_calls) == calls
    assert publish.call_count == 3
