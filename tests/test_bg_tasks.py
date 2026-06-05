import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


async def _short_task():
    await asyncio.sleep(0)


async def _failing_task():
    await asyncio.sleep(0)
    raise RuntimeError("boom")


async def test_bg_task_schedule_holds_task_until_completion():
    from app.core import bg_tasks

    task = bg_tasks.schedule(_short_task(), name="test.bg.short")
    snapshot = bg_tasks.stats()
    assert "test.bg.short" in snapshot["by_name"]

    await task
    await asyncio.sleep(0)
    assert "test.bg.short" not in bg_tasks.stats()["by_name"]


async def test_bg_task_schedule_removes_failed_tasks_after_completion():
    from app.core import bg_tasks

    task = bg_tasks.schedule(_failing_task(), name="test.bg.fail")
    assert "test.bg.fail" in bg_tasks.stats()["by_name"]

    try:
        await task
    except RuntimeError:
        pass
    await asyncio.sleep(0)

    assert task.done()
    assert "test.bg.fail" not in bg_tasks.stats()["by_name"]


async def test_bridge_lifespan_uses_scheduler():
    import napcat_bridge

    names = []

    async def fake_cleanup():
        return None

    original_cleanup = napcat_bridge._periodic_cleanup
    napcat_bridge._periodic_cleanup = fake_cleanup
    try:
        real_schedule = __import__("app.core.bg_tasks", fromlist=["schedule"]).schedule

        def recording_schedule(coro, *, name=None):
            names.append(name)
            coro.close()
            loop = asyncio.get_running_loop()
            done = loop.create_future()
            done.set_result(None)
            return done

        import app.core.bg_tasks as bg_tasks
        bg_tasks.schedule = recording_schedule
        try:
            async with napcat_bridge.lifespan(napcat_bridge.app):
                pass
        finally:
            bg_tasks.schedule = real_schedule
    finally:
        napcat_bridge._periodic_cleanup = original_cleanup

    assert "bridge.cleanup" in names
