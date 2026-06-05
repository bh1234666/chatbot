"""Quick test of shell double-wrapping bug."""
import asyncio, sys

async def test():
    # Test 1: create_subprocess_shell with cmd /c dir /b
    print("Test 1: create_subprocess_shell('cmd /c dir /b')")
    proc = await asyncio.create_subprocess_shell(
        "cmd /c dir /b",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=".",
    )
    stdout, stderr = await proc.communicate()
    print(f"  rc={proc.returncode}")
    print(f"  stdout={stdout[:200]!r}")
    print(f"  stderr={stderr[:200]!r}")

    # Test 2: Just dir /b (no cmd /c prefix)
    print("Test 2: create_subprocess_shell('dir /b') — raw command")
    proc = await asyncio.create_subprocess_shell(
        "dir /b",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=".",
    )
    stdout, stderr = await proc.communicate()
    print(f"  rc={proc.returncode}")
    print(f"  stdout={stdout[:200]!r}")
    print(f"  stderr={stderr[:200]!r}")

    # Test 3: create_subprocess_exec with cmd.exe /c dir /b
    print("Test 3: create_subprocess_exec('cmd.exe', '/c', 'dir /b')")
    proc = await asyncio.create_subprocess_exec(
        "cmd.exe", "/c", "dir /b",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=".",
    )
    stdout, stderr = await proc.communicate()
    print(f"  rc={proc.returncode}")
    print(f"  stdout={stdout[:200]!r}")
    print(f"  stderr={stderr[:200]!r}")

    # Test 4: create_subprocess_shell with just 'dir'
    print("Test 4: create_subprocess_shell('dir') — raw command")
    proc = await asyncio.create_subprocess_shell(
        "dir",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=".",
    )
    stdout, stderr = await proc.communicate()
    print(f"  rc={proc.returncode}")
    print(f"  stdout={stdout[:200]!r}")
    print(f"  stderr={stderr[:200]!r}")

    # Test 5: subprocess.run with shell=True to compare
    import subprocess
    print("Test 5: subprocess.run('cmd /c dir /b', shell=True)")
    r = subprocess.run("cmd /c dir /b", shell=True, capture_output=True, cwd=".")
    print(f"  rc={r.returncode}")
    print(f"  stdout={r.stdout[:200]!r}")
    print(f"  stderr={r.stderr[:200]!r}")

asyncio.run(test())
