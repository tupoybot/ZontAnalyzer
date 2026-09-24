import pytest

from tools.assert_ydb_memory import verify


def test_memory_check_reads_actual_pdisk_configuration():
    config = {"blob_storage_config": {"service_set": {"pdisks": [
        {"path": "SectorMap:1:64"}, {"path": "SectorMap:2:64"},
    ]}}}
    assert verify(config) == {"in_memory": True, "pdisk_count": 2}


@pytest.mark.parametrize("disks", [[], [{"path": "/ydb_data/pdisks/1"}], [
    {"path": "SectorMap:1:64"}, {"path": "/ydb_data/pdisks/2"},
]])
def test_memory_check_rejects_missing_or_disk_backed_storage(disks):
    with pytest.raises(ValueError, match="in-memory PDisks"):
        verify({"blob_storage_config": {"service_set": {"pdisks": disks}}})
