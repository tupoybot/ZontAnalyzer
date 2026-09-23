from zont_analyzer.adapters.ydb.database import YdbDatabase
from zont_analyzer.adapters.ydb.revisions import RevisionRepository
from zont_analyzer.adapters.ydb.telemetry import bump_revision


def test_coalescing_keeps_concurrent_change_after_ack(ydb_database: YdbDatabase) -> None:
    repo = RevisionRepository(ydb_database)
    ydb_database.transaction(lambda tx: bump_revision(tx, "gas:fixture"))
    observed = repo.changes_since(0)
    assert len(observed) == 1
    old_revision = observed[0]["revision"]
    ydb_database.transaction(lambda tx: bump_revision(tx, "gas:fixture"))
    assert repo.acknowledge(expected=0, through=old_revision)
    assert not repo.acknowledge(expected=0, through=old_revision)
    remaining = repo.changes_since(repo.progress())
    assert len(remaining) == 1
    assert remaining[0]["revision"] > old_revision
