import io
import logging


def test_configure_logging_routes_non_tty_to_file(tmp_path, monkeypatch):
    from app import main

    class NonTtyStderr(io.StringIO):
        def isatty(self):
            return False

    fake_stderr = NonTtyStderr()
    monkeypatch.setattr(main.sys, "stderr", fake_stderr)
    monkeypatch.setattr(main.settings, "debug_console", False)
    monkeypatch.setattr(main.settings, "debug_log_dir", str(tmp_path))
    monkeypatch.setattr(main.settings, "log_level", "INFO")

    try:
        main._configure_logging()
        logging.getLogger("unit.logging").info("non-tty log route check")
        for handler in logging.getLogger().handlers:
            handler.flush()

        assert fake_stderr.getvalue() == ""
        files = list(tmp_path.glob("app_*.log"))
        assert len(files) == 1
        assert "non-tty log route check" in files[0].read_text(encoding="utf-8")
    finally:
        for handler in logging.getLogger().handlers[:]:
            logging.getLogger().removeHandler(handler)
            handler.close()
        logging.basicConfig(level=logging.WARNING, force=True)


def test_configure_logging_routes_tty_to_file_unless_enabled(tmp_path, monkeypatch):
    from app import main

    class TtyStderr(io.StringIO):
        def isatty(self):
            return True

    fake_stderr = TtyStderr()
    monkeypatch.setattr(main.sys, "stderr", fake_stderr)
    monkeypatch.setattr(main.settings, "debug_console", False)
    monkeypatch.setattr(main.settings, "debug_log_dir", str(tmp_path))
    monkeypatch.setattr(main.settings, "log_level", "INFO")

    try:
        main._configure_logging()
        logging.getLogger("unit.logging").info("tty still routes to file")
        for handler in logging.getLogger().handlers:
            handler.flush()

        assert fake_stderr.getvalue() == ""
        files = list(tmp_path.glob("app_*.log"))
        assert len(files) == 1
        assert "tty still routes to file" in files[0].read_text(encoding="utf-8")
    finally:
        for handler in logging.getLogger().handlers[:]:
            logging.getLogger().removeHandler(handler)
            handler.close()
        logging.basicConfig(level=logging.WARNING, force=True)
