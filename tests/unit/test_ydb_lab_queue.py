"""Queue waiting measurements use acknowledged monotonic cycles, never fixture dates."""

import pytest

from tools import ydb_lab_queue


def clock(monkeypatch):
    current = [100.0]
    monkeypatch.setattr(ydb_lab_queue.time, "monotonic", lambda: current[0])
    return current, ydb_lab_queue.QueueClock()


def item(dirty, stamp=-123456789):
    return {"dirty": dirty, "queued_at": stamp}


def test_age_uses_monotonic_acknowledgement_not_fixture_wall_clock(monkeypatch):
    current, queue = clock(monkeypatch)
    monkeypatch.setattr(ydb_lab_queue.time, "time", lambda: 9999999999)
    queue.committed({"private-href": item(1)}, {})
    current[0] += 2.5
    assert queue.age(1) == 2.5
    assert queue.age(0) == 0


def test_pending_flag_merge_preserves_first_acknowledged_cycle_age(monkeypatch):
    current, queue = clock(monkeypatch)
    queue.committed({"a": item(1)}, {})
    current[0] += 10
    queue.committed({"a": item(7, 9999999999)}, {"a": item(1)})
    current[0] += 3
    assert queue.age(1) == 13


def test_clean_and_deleted_items_leave_queue_and_requeue_gets_fresh_age(monkeypatch):
    current, queue = clock(monkeypatch)
    queue.committed({"a": item(1), "b": item(1)}, {})
    current[0] += 10
    queue.committed({"a": item(0)}, {"a": item(1), "b": item(1)})
    assert queue.age(0) == 0
    assert queue.age(1) is None
    current[0] += 10
    queue.committed({"a": item(1)}, {"a": item(0)})
    current[0] += 4
    assert queue.age(1) == 4


def test_preexisting_dirty_cycle_stays_unknown_until_clean_requeue(monkeypatch):
    current, queue = clock(monkeypatch)
    queue.committed({"old": item(1)}, {"old": item(1)})
    assert queue.age(1) is None
    current[0] += 50
    queue.committed({"old": item(7)}, {"old": item(1)})
    assert queue.age(1) is None
    queue.committed({"old": item(0)}, {"old": item(7)})
    current[0] += 5
    queue.committed({"old": item(1)}, {"old": item(0)})
    current[0] += 3
    assert queue.age(1) == 3


def test_partial_commits_preserve_other_cycles_and_require_exact_count(monkeypatch):
    current, queue = clock(monkeypatch)
    queue.committed({"a": item(1)}, {})
    current[0] += 10
    queue.committed({"b": item(1)}, {})
    current[0] += 2
    assert queue.age(2) == 12
    assert queue.age(1) is None
    assert queue.age(3) is None
    queue.committed({}, {"a": item(1)})
    assert queue.age(1) == 2


def test_no_acknowledged_commit_cannot_create_known_age(monkeypatch):
    _, queue = clock(monkeypatch)
    assert queue.age(0) == 0
    # A failed save has no committed() notification; observed count cannot be
    # assigned an age until an acknowledged transition establishes its origin.
    assert queue.age(1) is None


@pytest.mark.parametrize("count", [-1, None, True, 1.5])
def test_invalid_pending_count_is_unknown(monkeypatch, count):
    _, queue = clock(monkeypatch)
    assert queue.age(count) is None


def test_save_hook_tracks_only_true_acknowledgements_and_restores(monkeypatch):
    from zont_analyzer.adapters.ydb.publication import PublicationRepository

    current, queue = clock(monkeypatch)
    response = [False]
    calls = []

    def original(repo, items, previous, *args, **kwargs):
        calls.append((repo, items, previous, args, kwargs))
        if isinstance(response[0], Exception):
            raise response[0]
        return response[0]

    monkeypatch.setattr(PublicationRepository, "save", original)
    repo, items, previous = object(), {"a": item(1)}, {}
    with ydb_lab_queue.observe_publication_queue(queue):
        assert PublicationRepository.save(repo, items, previous, "meta", expected_checkpoint=2) is False
        assert queue.age(1) is None
        response[0] = RuntimeError("save failed")
        with pytest.raises(RuntimeError, match="save failed"):
            PublicationRepository.save(repo, items, previous, "meta", expected_checkpoint=2)
        assert queue.age(1) is None
        # Truthy values do not substitute for the repository's actual True ack.
        response[0] = 1
        assert PublicationRepository.save(repo, items, previous, "meta", expected_checkpoint=2) == 1
        assert queue.age(1) is None
        response[0] = True
        assert PublicationRepository.save(repo, items, previous, "meta", expected_checkpoint=2) is True
        current[0] += 4
        assert queue.age(1) == 4
    assert PublicationRepository.save is original
    assert len(calls) == 4
    assert all(call == (repo, items, previous, ("meta",), {"expected_checkpoint": 2}) for call in calls)


def test_save_hook_restores_when_context_exits_with_exception(monkeypatch):
    from zont_analyzer.adapters.ydb.publication import PublicationRepository

    _, queue = clock(monkeypatch)
    original = PublicationRepository.save
    with pytest.raises(RuntimeError, match="outside save"), ydb_lab_queue.observe_publication_queue(queue):
        raise RuntimeError("outside save")
    assert PublicationRepository.save is original
