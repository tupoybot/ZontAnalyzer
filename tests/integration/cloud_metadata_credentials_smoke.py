"""Check installed YDB metadata authentication without contacting a metadata server."""
import json
import os
from unittest.mock import Mock, patch

import ydb  # type: ignore[import-untyped]

os.environ["YDB_METADATA_CREDENTIALS"] = "1"
response = Mock()
response.text = json.dumps({"access_token": "synthetic-metadata-token", "expires_in": 3600})
with patch("requests.get", return_value=response) as request:
    credentials = ydb.credentials_from_env_variables()
    assert credentials.auth_metadata() == [("x-ydb-auth-ticket", "synthetic-metadata-token")]
    request.assert_called_once()
    assert request.call_args.kwargs["headers"] == {"Metadata-Flavor": "Google"}
    assert request.call_args.kwargs["timeout"] == 3
    response.raise_for_status.assert_called_once_with()
print("Cloud artifact: metadata credentials initialized and produced authentication metadata")
