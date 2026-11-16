# Licensed under the Apache-2.0 license
"""Tkinter desktop interface for the independent USB stress application."""

from __future__ import annotations

from dataclasses import fields
from datetime import datetime
import json
from pathlib import Path
import queue
import sys
import threading
import time

from usb_stress_config import Settings, CST1_TESTS, GENERIC_TESTS, SOURCE_TESTS, LOOPBACK_TESTS
from usb_stress_runner import Runner
from usb_stress_discovery import discover_devices


class Application:
    def __init__(self, root):
        import tkinter as tk
        from tkinter import ttk

        self.tk = tk
        self.ttk = ttk
        self.root = root
        self.runner = Runner()
        self.report: Path | None = None
        self.closing = False
        self.variables = {}
        self.test_variables = {}
        self.editors = []
        self.discovered = []
        self.discovery_events = queue.Queue()
        self.scanning = False
        self.active_test: str | None = None
        self.active_subtest: str | None = None
        self.active_started = 0.0
        self.status = tk.StringVar(value="Ready - refresh devices to discover connected USB devices.")
        root.title("USB Stress - Generic USB / CST1")
        root.geometry("1040x830")
        root.minsize(900, 720)
        root.protocol("WM_DELETE_WINDOW", self.close)
        notebook = ttk.Notebook(root)
        notebook.pack(fill="both", expand=True, padx=10, pady=10)
        setup = ttk.Frame(notebook, padding=10)
        output = ttk.Frame(notebook, padding=10)
        notebook.add(setup, text="Configuration and tests")
        notebook.add(output, text="Live output and results")
        self.notebook = notebook

        device = ttk.LabelFrame(setup, text="Device selection", padding=8)
        device.pack(fill="x")
        selection = ttk.Frame(device)
        selection.grid(row=0, column=0, columnspan=6, sticky="ew", pady=(0, 8))
        self.device_choice = ttk.Combobox(selection, state="readonly", width=95)
        self.device_choice.pack(side="left", fill="x", expand=True)
        self.device_choice.bind("<<ComboboxSelected>>", self.select_device)
        self.refresh_button = ttk.Button(selection, text="Refresh devices", command=self.refresh_devices)
        self.refresh_button.pack(side="right", padx=4)
        self.editors.extend(((self.device_choice, "readonly"), (self.refresh_button, "normal")))
        labels = (
            ("profile", "Profile", ("generic", "cst1")),
            ("vendor_id", "Vendor ID (0x...)", None),
            ("product_id", "Product ID (0x...)", None),
            ("serial_number", "Serial (optional)", None),
            ("configuration", "Configuration", None),
            ("interface", "Interface", None),
            ("alternate", "Active alternate", None),
            ("inactive_alternate", "Inactive alternate", None),
            ("out_endpoint", "Bulk OUT (0x...)", None),
            ("in_endpoint", "Bulk IN (0x...)", None),
            ("behavior", "Generic behavior", ("source_sink", "loopback")),
        )
        for index, (name, label, choices) in enumerate(labels):
            row, column = divmod(index, 3)
            row += 1
            column *= 2
            ttk.Label(device, text=label).grid(row=row, column=column, sticky="w", padx=4, pady=4)
            variable = tk.StringVar()
            self.variables[name] = variable
            editor = (ttk.Combobox(device, textvariable=variable, values=choices, state="readonly", width=18)
                      if choices else ttk.Entry(device, textvariable=variable, width=20))
            editor.grid(row=row, column=column + 1, sticky="ew", padx=4)
            if choices:
                editor.bind("<<ComboboxSelected>>",
                            self.profile_changed if name == "profile"
                            else lambda _event: self.update_tests())
            self.editors.append((editor, "readonly" if choices else "normal"))
        preset = ttk.Button(device, text="CST1 lab preset", command=self.preset)
        preset.grid(row=5, column=4, columnspan=2, sticky="e", pady=4)
        self.editors.append((preset, "normal"))
        consent = tk.BooleanVar()
        self.variables["allow_data_tests"] = consent
        editor = ttk.Checkbutton(
            setup, text="I authorize generic data writes/state changes; this device supports the selected behavior",
            variable=consent,
        )
        editor.pack(anchor="w", pady=6)
        self.editors.append((editor, "normal"))
        ttk.Label(setup, text="Source/sink means OUT is discarded and IN returns zeros. Loopback must echo bytes.\n"
                  "CST1 is a cooperative test-firmware protocol, not a USB standard. Do not target production storage.",
                  foreground="#8b4513").pack(anchor="w")

        workload = ttk.LabelFrame(setup, text="Workload", padding=8)
        workload.pack(fill="x", pady=8)
        workload_fields = (("iterations", "Iterations"), ("queue_depth", "Queue depth (2-64)"),
                           ("duration", "Soak seconds"), ("timeout_ms", "Transfer timeout ms"),
                           ("discovery_timeout", "Discovery seconds"), ("seed", "Random seed"))
        for index, (name, label) in enumerate(workload_fields):
            row, column = divmod(index, 3)
            column *= 2
            ttk.Label(workload, text=label).grid(row=row, column=column, padx=4, pady=4, sticky="w")
            variable = tk.StringVar()
            self.variables[name] = variable
            editor = ttk.Entry(workload, textvariable=variable, width=18)
            editor.grid(row=row, column=column + 1, padx=4)
            self.editors.append((editor, "normal"))
        for row, name, label in ((2, "sizes", "Sizes, comma-separated bytes"), (3, "report", "Report JSON (blank = auto)")):
            variable = tk.StringVar()
            self.variables[name] = variable
            ttk.Label(workload, text=label).grid(row=row, column=0, sticky="w", padx=4, pady=4)
            editor = ttk.Entry(workload, textvariable=variable)
            editor.grid(row=row, column=1, columnspan=5, sticky="ew", padx=4)
            self.editors.append((editor, "normal"))

        tests = ttk.LabelFrame(setup, text="Tests - unavailable profile/behavior cases are disabled", padding=8)
        tests.pack(fill="x")
        self.test_buttons = {}
        for index, name in enumerate(CST1_TESTS):
            variable = tk.BooleanVar()
            self.test_variables[name] = variable
            editor = ttk.Checkbutton(tests, text=name, variable=variable)
            editor.grid(row=index // 4, column=index % 4, sticky="w", padx=15, pady=3)
            self.test_buttons[name] = editor
        ttk.Label(setup, text="port_cycle is Windows/libusbK only and deliberately disconnects/re-enumerates the device.\n"
                  "It does not establish isolated bus-reset, power-cycle, Linux DMA or full compliance coverage.",
                  foreground="#8b4513").pack(anchor="w", pady=6)
        buttons = ttk.Frame(setup)
        buttons.pack(fill="x", pady=6)
        for label, callback in (("Load configuration", self.load), ("Save configuration", self.save),
                                ("Select compatible tests", self.select_tests)):
            editor = ttk.Button(buttons, text=label, command=callback)
            editor.pack(side="left", padx=4)
            self.editors.append((editor, "normal"))
        self.run_button = ttk.Button(buttons, text="Run selected tests", command=self.start)
        self.run_button.pack(side="right", padx=4)
        self.stop_button = ttk.Button(buttons, text="Stop", command=self.stop, state="disabled")
        self.stop_button.pack(side="right", padx=4)
        ttk.Label(root, textvariable=self.status).pack(fill="x", padx=12, pady=(0, 8))

        self.results = ttk.Treeview(output, columns=("status", "details"), show="tree headings", height=13)
        self.results.heading("#0", text="Test / key")
        self.results.heading("status", text="Status")
        self.results.heading("details", text="Value / error")
        self.results.column("#0", width=240)
        self.results.column("status", width=85)
        self.results.column("details", width=650)
        self.results.pack(fill="x")
        progress = ttk.Frame(output)
        progress.pack(fill="x", pady=(8, 0))
        self.progress_text = tk.StringVar(value="No tests started")
        ttk.Label(progress, textvariable=self.progress_text).pack(anchor="w")
        self.overall_progress = ttk.Progressbar(progress, mode="determinate", maximum=1)
        self.overall_progress.pack(fill="x", pady=3)
        log_frame = ttk.Frame(output)
        log_frame.pack(fill="both", expand=True, pady=(8, 0))
        self.log = tk.Text(log_frame, wrap="word", state="disabled", height=20)
        scroll = ttk.Scrollbar(log_frame, command=self.log.yview)
        self.log.configure(yscrollcommand=scroll.set)
        self.log.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.apply(Settings())
        root.after(100, self.poll)
        root.after(200, self.refresh_devices)

    def refresh_devices(self) -> None:
        if self.scanning or self.runner.running:
            return
        self.scanning = True
        self.refresh_button.configure(state="disabled")
        self.run_button.configure(state="disabled")
        self.status.set("Discovering USB devices (descriptor reads only)...")

        def scan():
            try:
                self.discovery_events.put(("devices", discover_devices()))
            except (OSError, ValueError, RuntimeError, ImportError) as error:
                self.discovery_events.put(("error", str(error)))

        threading.Thread(target=scan, daemon=True).start()

    def select_device(self, _event=None) -> None:
        index = self.device_choice.current()
        if self.runner.running or not 0 <= index < len(self.discovered):
            return
        device = self.discovered[index]
        self.variables["vendor_id"].set(f"0x{device.vendor_id:04x}")
        self.variables["product_id"].set(f"0x{device.product_id:04x}")
        self.variables["serial_number"].set(device.serial)
        for name in ("bus", "address"):
            value = getattr(device, name)
            self.variables[name].set("" if value is None else str(value))
        self.status.set(device.warning or "Device selected. Check profile/interface/endpoints before running.")

    def apply(self, settings: Settings) -> None:
        self.device_choice.set("")
        for item in fields(settings):
            value = getattr(settings, item.name)
            if item.name == "tests":
                continue
            if item.name in ("bus", "address"):
                if item.name not in self.variables:
                    self.variables[item.name] = self.tk.StringVar()
                value = "" if value is None else str(value)
            if item.name == "sizes":
                value = ",".join(map(str, value))
            elif item.name in ("vendor_id", "product_id", "out_endpoint", "in_endpoint"):
                value = "" if value is None else f"0x{value:04x}" if "id" in item.name else f"0x{value:02x}"
            self.variables[item.name].set(value)
        for name, variable in self.test_variables.items():
            variable.set(name in settings.tests)
        self.update_tests()

    def compatible(self) -> set[str]:
        if self.variables["profile"].get() == "cst1":
            return set(CST1_TESTS) - ({"port_cycle"} if sys.platform != "win32" else set())
        excluded = (LOOPBACK_TESTS if self.variables["behavior"].get() == "source_sink" else SOURCE_TESTS)
        return set(GENERIC_TESTS) - excluded

    def profile_changed(self, _event=None) -> None:
        if self.variables["profile"].get() == "cst1":
            defaults = Settings.cst1()
            for name in ("configuration", "interface", "alternate", "inactive_alternate",
                         "out_endpoint", "in_endpoint"):
                self.variables[name].set(str(getattr(defaults, name)))
            self.status.set("CST1 protocol settings applied; selected device and workload preserved.")
        self.update_tests()

    def update_tests(self) -> None:
        compatible = self.compatible()
        for name, editor in self.test_buttons.items():
            available = name in compatible
            if not available:
                self.test_variables[name].set(False)
            editor.configure(state="normal" if available and not self.runner.running else "disabled")

    def select_tests(self) -> None:
        for name, variable in self.test_variables.items():
            variable.set(name in self.compatible() and name != "port_cycle")

    def collect(self) -> Settings:
        settings = Settings()
        for item in fields(settings):
            if item.name == "tests":
                value = [name for name, variable in self.test_variables.items() if variable.get()]
            elif item.name == "allow_data_tests":
                value = self.variables[item.name].get()
            else:
                value = self.variables[item.name].get().strip()
                if item.name == "sizes":
                    value = [int(part.strip(), 0) for part in value.split(",")]
                elif item.name in ("duration", "discovery_timeout"):
                    value = float(value)
                elif item.name not in ("profile", "serial_number", "behavior"):
                    value = None if item.name in ("bus", "address") and not value else int(value, 0)
            setattr(settings, item.name, value)
        settings.validate()
        return settings

    def preset(self) -> None:
        selected = self.device_choice.current()
        current = self.discovered[selected] if 0 <= selected < len(self.discovered) else None
        self.apply(Settings.cst1(vendor_id=0x0424, product_id=0x0007,
                                tests=["control", "bulk", "loopback", "halt", "alternate", "duplex", "soak"]))
        if current is not None:
            self.device_choice.current(selected)
            self.select_device()

    def load(self) -> None:
        from tkinter import filedialog, messagebox
        path = filedialog.askopenfilename(filetypes=[("USB Stress JSON", "*.json")])
        if path:
            try:
                self.apply(Settings.load(Path(path)))
            except (OSError, ValueError) as error:
                messagebox.showerror("Cannot load configuration", str(error))

    def save(self) -> None:
        from tkinter import filedialog, messagebox
        try:
            settings = self.collect()
            path = filedialog.asksaveasfilename(defaultextension=".json",
                                              filetypes=[("USB Stress JSON", "*.json")])
            if path:
                settings.save(Path(path))
        except (OSError, ValueError) as error:
            messagebox.showerror("Cannot save configuration", str(error))

    def start(self) -> None:
        from tkinter import messagebox
        if self.scanning:
            return
        try:
            settings = self.collect()
            if "port_cycle" in settings.tests and not messagebox.askyesno(
                    "Confirm port cycling", "This will cycle the selected device's USB port connection.\nContinue?"):
                return
            text = self.variables["report"].get().strip()
            self.report = Path(text).expanduser().resolve() if text else (
                Path.cwd() / "results" / f"gui-{datetime.now():%Y%m%d-%H%M%S-%f}.json"
            )
            self.results.delete(*self.results.get_children())
            self.active_test = None
            self.active_subtest = None
            self.overall_progress.configure(maximum=len(settings.tests), value=0)
            self.progress_text.set(f"0 / {len(settings.tests)} tests completed")
            for name in settings.tests:
                self.results.insert("", "end", iid=name, text=name, values=("NOT_RUN", ""))
                from usb_stress import subtest_names
                for child in subtest_names(name, settings):
                    self.render_subtest(name, dict(name=child, status="NOT_RUN"))
            self.runner.start(settings, self.report)
            self.set_running(True)
            self.status.set(f"Running - report: {self.report}")
            self.notebook.select(1)
        except (OSError, ValueError, RuntimeError) as error:
            messagebox.showerror("Cannot start tests", str(error))

    def set_running(self, running: bool) -> None:
        for editor, idle_state in self.editors:
            editor.configure(state="disabled" if running else idle_state)
        self.run_button.configure(state="disabled" if running else "normal")
        self.stop_button.configure(state="normal" if running else "disabled")
        self.update_tests()

    def append_log(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text)
        if int(self.log.index("end-1c").split(".")[0]) > 3000:
            self.log.delete("1.0", "501.0")
        self.log.see("end")
        self.log.configure(state="disabled")

    def live_event(self, line: str) -> bool:
        from usb_stress import PROGRESS_PREFIX
        if not line.startswith(PROGRESS_PREFIX):
            return False
        try:
            event = json.loads(line[len(PROGRESS_PREFIX):])
            name, status = event["name"], event["status"]
            if "subtest" in event:
                if status not in ("RUNNING", "PASS", "FAIL", "SKIP") or not self.results.exists(name):
                    raise ValueError("Unknown subtest parent or status")
                child = dict(event, name=event["subtest"])
                node = self.render_subtest(name, child)
                if status == "RUNNING":
                    self.active_subtest = node
                elif self.active_subtest == node:
                    self.active_subtest = None
                self.status.set(f"{name} / {child['name']}: {status}")
                return True
            if status not in ("RUNNING", "PASS", "FAIL") or not self.results.exists(name):
                raise ValueError("Unknown test or status in progress event")
            if status == "RUNNING":
                self.active_test = name
                self.active_started = time.monotonic()
                details = "Starting..."
            else:
                details = event.get("error") or ""
                if self.active_test == name:
                    self.active_test = None
                self.active_subtest = None
            self.results.item(name, values=(status, details))
            if status != "RUNNING":
                self.render_details(name, event)
            self.update_progress()
            self.results.see(name)
            self.status.set(f"{name}: {status} - report: {self.report}")
        except (ValueError, KeyError, TypeError) as error:
            self.append_log(f"Invalid progress event: {error}\n")
        return True

    def render_subtest(self, parent: str, event: dict) -> str:
        node = f"{parent}::subtest::{event['name']}"
        if not self.results.exists(node):
            self.results.insert(parent, "end", iid=node, text=event["name"], values=("NOT_RUN", ""))
        self.results.item(node, values=(event["status"], event.get("error") or event.get("reason") or ""))
        self.render_details(node, {key: value for key, value in event.items()
                                   if key in ("elapsed_seconds", "error", "details")})
        if "executions" in event:
            self.results.insert(node, "end", text="executions", values=("", event["executions"]))
        return node

    def render_details(self, name: str, event: dict) -> None:
        children = [child for child in self.results.get_children(name)
                    if not child.startswith(f"{name}::subtest::")]
        if children:
            self.results.delete(*children)

        def insert(parent: str, key: str, value) -> None:
            if isinstance(value, (dict, list)):
                node = self.results.insert(parent, "end", text=key, values=("", ""))
                entries = value.items() if isinstance(value, dict) else enumerate(value)
                for child_key, child_value in entries:
                    insert(node, str(child_key), child_value)
            else:
                text = ("null" if value is None else str(value).lower()
                        if isinstance(value, bool) else str(value))
                self.results.insert(parent, "end", text=key, values=("", text))

        for key in ("elapsed_seconds", "error"):
            if key in event:
                insert(name, key, event[key])
        details = event.get("details")
        if isinstance(details, dict):
            for key, value in details.items():
                insert(name, str(key), value)
        elif details is not None:
            insert(name, "details", details)
        for child in event.get("subtests", []):
            self.render_subtest(name, child)

    def update_progress(self) -> None:
        rows = self.results.get_children()
        completed = sum(self.results.item(row, "values")[0] in ("PASS", "FAIL") for row in rows)
        self.overall_progress.configure(maximum=max(1, len(rows)), value=completed)
        self.progress_text.set(f"{completed} / {len(rows)} tests completed")

    def completed(self, status: int) -> None:
        self.active_test = None
        self.active_subtest = None
        failure = None
        try:
            if self.runner.forced:
                raise ValueError("Forced stop; results and USB cleanup may be incomplete")
            if self.report is None:
                raise ValueError("No report path")
            result = json.loads(self.report.read_text(encoding="utf-8"))
            cases = result.get("tests")
            if result.get("schema_version") != 1 or not isinstance(cases, list) or not cases:
                raise ValueError("Missing/invalid test report")
            for case in cases:
                name = case["name"]
                if self.results.exists(name):
                    self.results.item(name, values=(case["status"], case.get("error") or ""))
                    self.render_details(name, case)
            overall = result.get("overall")
            if status != 0 or overall != "PASS" or any(case.get("status") != "PASS" for case in cases):
                failed = next((case for case in cases if case.get("status") == "FAIL"), None)
                failure = result.get("error") or result.get("cleanup_error")
                if not failure and failed:
                    failure = f"{failed['name']}: {failed.get('error', 'Test failed')}"
                failure = failure or f"Test run did not pass (exit {status}, status {overall})"
            if status == 0 and overall != "PASS":
                raise ValueError("Successful process did not produce a passing report")
            self.status.set(f"{overall} - exit {status} - {self.report}")
        except (OSError, ValueError, KeyError, TypeError) as error:
            failure = str(error)
            self.status.set(f"Run failed - exit {status}: {error}")
            self.append_log(f"\nReport error: {error}\n")
        finally:
            self.update_progress()
            try:
                self.runner.finish()
            except OSError as error:
                cleanup_error = f"Temporary configuration cleanup failed: {error}"
                failure = f"{failure}\n{cleanup_error}" if failure else cleanup_error
                self.append_log(f"\nTemporary configuration cleanup failed: {error}\n")
            self.set_running(False)
        if failure is not None:
            from tkinter import messagebox
            messagebox.showerror("USB stress run failed",
                                 f"{failure}\n\nReport: {self.report}\n"
                                 "See Live output and results for details.",
                                 parent=self.root)

    def poll(self) -> None:
        try:
            kind, value = self.discovery_events.get_nowait()
        except queue.Empty:
            pass
        else:
            self.scanning = False
            self.refresh_button.configure(state="normal")
            self.run_button.configure(state="normal")
            if kind == "devices":
                self.discovered = value
                self.device_choice.set("")
                self.device_choice.configure(values=[device.label for device in value])
                self.status.set(f"Found {len(value)} USB devices. Select one; none is selected automatically.")
            else:
                self.status.set(f"USB discovery failed: {value}")
                self.append_log(f"USB discovery failed: {value}\n")
        try:
            self.runner.tick()
        except OSError as error:
            self.status.set(f"Process termination failed: {error}")
        for _ in range(200):
            try:
                kind, value = self.runner.events.get_nowait()
            except queue.Empty:
                break
            if kind == "exit":
                self.completed(int(value))
            elif kind == "log" and self.live_event(str(value)):
                pass
            else:
                self.append_log(str(value))
                if kind == "error":
                    self.status.set(str(value))
        if self.active_test is not None:
            elapsed = time.monotonic() - self.active_started
            self.results.item(self.active_test, values=("RUNNING", f"Elapsed: {elapsed:.1f} s"))
            if self.runner.stop_deadline is None:
                child = (f" / {self.results.item(self.active_subtest, 'text')}"
                         if self.active_subtest and self.results.exists(self.active_subtest) else "")
                self.status.set(f"Running {self.active_test}{child} - {elapsed:.1f} s - report: {self.report}")
        if self.closing and not self.runner.running:
            self.root.destroy()
            return
        self.root.after(100, self.poll)

    def stop(self) -> None:
        self.runner.stop()
        self.status.set("Stopping - waiting for transfer cleanup; forced stop follows if unresponsive")
        self.stop_button.configure(state="disabled")

    def close(self) -> None:
        if self.runner.running:
            from tkinter import messagebox
            if not messagebox.askyesno("Tests are running", "Stop the owned test process and exit?"):
                return
            self.closing = True
            self.stop()
        else:
            self.root.destroy()


def main() -> int:
    try:
        import tkinter as tk
    except ImportError:
        print("Tkinter is missing. On Linux install python3-tk; on Windows enable Tcl/Tk in Python setup.",
              file=sys.stderr)
        return 1
    try:
        root = tk.Tk()
    except tk.TclError as error:
        print(f"Cannot open a desktop window: {error}", file=sys.stderr)
        return 1
    Application(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
