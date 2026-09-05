"""Run only the real feedback HTTP server for archive browser acceptance."""
from pathlib import Path

from zont_analyzer.application.feedback import build_feedback_server
from zont_analyzer.runtime import build_runtime

runtime = build_runtime(Path("/config/config.yaml"), Path("/data"))
server = build_feedback_server(runtime)
server.serve_forever()
