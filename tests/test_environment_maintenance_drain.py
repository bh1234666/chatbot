from stress_tools.run_environment_maintenance import unfinished_tasks


def test_unfinished_tasks_filters_done_tasks():
    class DummyTask:
        def __init__(self, done):
            self._done = done

        def done(self):
            return self._done

    pending = DummyTask(False)
    assert unfinished_tasks([DummyTask(True), pending]) == [pending]
