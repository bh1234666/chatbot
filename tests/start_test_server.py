"""Temporary test server — verifies bot starts cleanly with all fixes."""
import sys, os, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

print("Starting test server...")
import uvicorn
uvicorn.run(
    "app.main:app",
    host="127.0.0.1",
    port=18999,
    log_level="warning",
    timeout_keep_alive=5,
)
