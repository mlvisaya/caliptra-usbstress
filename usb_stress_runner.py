# Licensed under the Apache-2.0 license
"""Own a CLI child process; keep USB calls and cancellation off the GUI thread."""

from __future__ import annotations

import os
from pathlib import Path
import queue
import signal
import subprocess
import sys
import tempfile
import threading
import time

from usb_stress_config import Settings


class Runner:
    def __init__(self):
        self.events: queue.Queue[tuple[str, str | int]] = queue.Queue()
        self.process: subprocess.Popen[str] | None = None
        self.config_path: Path | None = None
        self.stop_deadline: float | None = None
        self.forced = False

    @property
    def running(self) -> bool:
        return self.process is not None

    def start(self, settings: Settings, report: Path) -> None:
        if self.running:
            raise RuntimeError("A test process is already running")
        settings.validate()
        if report.exists():
            raise ValueError(f"Report already exists: {report}")
        descriptor, name = tempfile.mkstemp(prefix="usbstress-", suffix=".json")
        os.close(descriptor)
        self.config_path = Path(name)
        try:
            settings.save(self.config_path)
            self.process = subprocess.Popen(
                [sys.executable, "-u", str(Path(__file__).with_name("usb_stress.py")),
                 "--config", str(self.config_path), "--report", str(report), "--progress-events"],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                errors="replace", env=dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8"),
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0,
                start_new_session=sys.platform != "win32",
            )
        except (OSError, ValueError):
            self.config_path.unlink()
            self.config_path = None
            raise
        self.stop_deadline = None
        self.forced = False
        threading.Thread(target=self._read, args=(self.process,), daemon=True).start()

    def _read(self, process: subprocess.Popen[str]) -> None:
        if process.stdout is None:
            self.events.put(("error", "Child output pipe is unavailable"))
            return
        try:
            for line in process.stdout:
                self.events.put(("log", line))
        except OSError as error:
            self.events.put(("error", f"Cannot read test output: {error}"))
        finally:
            process.stdout.close()
            self.events.put(("exit", process.wait()))

    def stop(self, grace_seconds: float = 8) -> None:
        if self.process is None or self.stop_deadline is not None:
            return
        self.stop_deadline = time.monotonic() + grace_seconds
        if self.process.poll() is None:
            try:
                if sys.platform == "win32":
                    self.process.send_signal(signal.CTRL_BREAK_EVENT)
                else:
                    self.process.send_signal(signal.SIGINT)
            except OSError as error:
                self.events.put(("error", f"Graceful stop failed: {error}; bounded termination will follow"))

    def tick(self) -> None:
        if (self.process is not None and self.stop_deadline is not None
                and time.monotonic() >= self.stop_deadline and self.process.poll() is None):
            self.process.kill()
            self.forced = True
            self.events.put(("error", "Forced termination: USB cleanup/report completion is not guaranteed"))
            self.stop_deadline = None

    def finish(self) -> None:
        if self.process is not None and self.process.poll() is None:
            raise RuntimeError("Cannot release a running test process")
        self.process = None
        self.stop_deadline = None
        if self.config_path is not None:
            self.config_path.unlink()
            self.config_path = None
