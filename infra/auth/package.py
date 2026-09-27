"""Build a deterministic, dependency-free function artifact in the tools container."""
import hashlib
import json
import sys
import zipfile
from pathlib import Path

source, output = map(Path, sys.argv[1:3])
output.parent.mkdir(parents=True, exist_ok=True)
info = zipfile.ZipInfo("index.js", date_time=(2026, 1, 1, 0, 0, 0))
info.compress_type = zipfile.ZIP_DEFLATED
info.external_attr = 0o100644 << 16
with zipfile.ZipFile(output, "w") as archive:
    archive.writestr(info, (source / "index.js").read_bytes())

if len(sys.argv) == 4:
    inputs_path = Path(sys.argv[3])
    inputs = json.loads(inputs_path.read_text())
    if inputs.get("identity"):
        inputs["identity"]["code_sha256"] = hashlib.sha256(output.read_bytes()).hexdigest()
        inputs_path.write_text(json.dumps(inputs, indent=2) + "\n")
