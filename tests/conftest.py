from collections.abc import Iterator

import pytest


@pytest.fixture(autouse=True)
def legacy_gas_regressions(monkeypatch: pytest.MonkeyPatch) -> None:
    # Preserve coverage of the suspended implementation. Suspension tests remove
    # this explicit opt-in and exercise the production default.
    monkeypatch.setenv("ZONT_GAS_ANALYSIS_ENABLED", "1")


@pytest.fixture(scope="session", autouse=True)
def cleanup_ydb_session() -> Iterator[None]:
    yield
    from tests.ydb_support import cleanup_session

    cleanup_session()


@pytest.fixture(autouse=True)
def cleanup_application_databases(
    request: pytest.FixtureRequest, cleanup_ydb_session: None,
) -> Iterator[None]:
    from tests.ydb_support import begin_test, end_test

    with pytest.MonkeyPatch.context() as tracking_patch:
        begin_test(request.node.nodeid, ydb_allowed=request.node.get_closest_marker("ydb") is not None,
                   monkeypatch=tracking_patch)
        try:
            yield
        finally:
            end_test()
