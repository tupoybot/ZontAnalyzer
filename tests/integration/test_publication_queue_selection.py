"""Native queue-index traversal visits present masks without starving older work."""
from __future__ import annotations

import pytest

from tests.ydb_support import make_database
from zont_analyzer.adapters.ydb.publication import PublicationRepository


@pytest.mark.ydb
@pytest.mark.parametrize(("ages_by_mask", "expected_queries"), [
    ({}, 1),
    ({17: [30, 10, 20, 40]}, 2),
    ({1: [120, 100, 110], 17: [3, 1, 2], 31: [30, 10, 20]}, 4),
    # The first page includes only part of mask 17; it must be revisited.
    ({1: [100], 17: [3, 1, 2], 31: [30, 10, 20]}, 4),
])
def test_pending_prefix_reads_scale_with_present_masks(
    tmp_path, monkeypatch, ages_by_mask, expected_queries,
):
    db = make_database(tmp_path)
    expected = set()
    for mask, ages in ages_by_mask.items():
        for age in ages:
            href = f"mask-{mask}-age-{age}"
            db.storage.execute(
                "DECLARE $href AS Utf8; DECLARE $mask AS Int64; DECLARE $age AS Int64; "
                "UPSERT INTO publication_items "
                "(href,report_id,kind,period_start,period_end,generated_at,comparisons,dirty,queued_at,entry) "
                "VALUES ($href,$href,'daily',0,1,2,false,$mask,$age,'published');",
                {"$href": href, "$mask": mask, "$age": age},
            )
        expected.update(f"mask-{mask}-age-{age}" for age in sorted(ages)[:2])
    # Completed items must not appear in the positive-prefix traversal.
    db.storage.execute(
        "UPSERT INTO publication_items(href,dirty,queued_at) VALUES ('clean',0,0);",
    )
    execute = db.storage.execute
    prefixes = []

    def capture(query, parameters=None, **kwargs):
        assert "publication_items VIEW by_queue" in query
        prefixes.append(parameters["$after"])
        return execute(query, parameters, **kwargs)

    monkeypatch.setattr(db.storage, "execute", capture)
    items = PublicationRepository(db.storage).pending_items(2, "", include_unpublished=False)
    assert set(items) == expected
    assert len(prefixes) == expected_queries
    assert prefixes == sorted(set(prefixes))
    assert prefixes[0] == 0
    assert len(items) <= 2 * len(ages_by_mask)
