"""Test shell double-wrapping fix in handle_run."""
import asyncio
import sys, os, tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.llm.tools.workspace import handle_run

async def main():
    with tempfile.TemporaryDirectory() as tmpdir:
        print("Test 1: 'dir /b' (raw cmd built-in)")
        r = await handle_run(tmpdir, "dir /b", timeout_sec=5)
        ok = r.get("ok")
        print(f"  ok={ok}, rc={r.get('returncode')}, stderr={r.get('stderr','')[:100]!r}")
        assert ok, f"dir /b should succeed, got: {r}"

        print("Test 2: 'dir' (raw cmd built-in)")
        r = await handle_run(tmpdir, "dir", timeout_sec=5)
        ok = r.get("ok")
        print(f"  ok={ok}, rc={r.get('returncode')}, stderr={r.get('stderr','')[:100]!r}")
        assert ok, f"dir should succeed, got: {r}"

        print("Test 3: 'echo hello' (raw cmd built-in)")
        r = await handle_run(tmpdir, "echo hello", timeout_sec=5)
        ok = r.get("ok")
        print(f"  ok={ok}, rc={r.get('returncode')}, stdout={r.get('stdout','')[:100]!r}")
        assert ok, f"echo should succeed, got: {r}"

        print("Test 4: 'type nul' (raw cmd built-in)")
        r = await handle_run(tmpdir, "type nul", timeout_sec=5)
        ok = r.get("ok")
        print(f"  ok={ok}, rc={r.get('returncode')}, stderr={r.get('stderr','')[:100]!r}")
        # type nul should work
        assert ok, f"type nul should succeed, got: {r}"

        # Create a test file for copy test
        with open(os.path.join(tmpdir, "a.txt"), "w") as f:
            f.write("test")

        print("Test 5: 'copy a.txt b.txt' (raw cmd built-in with args)")
        r = await handle_run(tmpdir, "copy a.txt b.txt", timeout_sec=5)
        ok = r.get("ok")
        print(f"  ok={ok}, rc={r.get('returncode')}, stderr={r.get('stderr','')[:100]!r}")
        assert ok, f"copy should succeed, got: {r}"
        assert os.path.isfile(os.path.join(tmpdir, "b.txt")), "b.txt should exist"

        print("Test 6: LLM pre-wrapped 'cmd /c dir /b'")
        r = await handle_run(tmpdir, "cmd /c dir /b", timeout_sec=5)
        ok = r.get("ok")
        print(f"  ok={ok}, rc={r.get('returncode')}, stderr={r.get('stderr','')[:100]!r}")
        assert ok, f"cmd /c dir /b should succeed, got: {r}"

    print()
    print("All tests passed. Shell double-wrapping is fixed.")

asyncio.run(main())
