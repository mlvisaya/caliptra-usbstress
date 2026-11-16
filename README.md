# USB Stress

A standalone Python USB stress application with a command-line interface and
Tkinter desktop GUI. It supports configurable USB devices and an optional CST1
test-firmware profile. It does **not** import, build, or require the Caliptra MCU
repository, FPGA tools, or UART scripts.

The repository name is historical; generic mode is not Caliptra-specific.
This is a USB test utility, not a port of Linux `usbtest.ko`, Microsoft MUTT
USBStress, or a USB-IF certification suite.

## Install

Python 3.10 or newer:

```sh
python -m pip install -e .
```

Alternatively, install `requirements.txt` and run the scripts directly.
The CLI needs libusb; the Windows wheels include it. On Linux, install the
distribution's libusb runtime and grant access to **only the intended device**
using an appropriate udev rule. The application does not automatically detach
kernel drivers; unbind the intended test interface yourself when necessary.

The GUI needs Tcl/Tk:

- Windows: enable **Tcl/Tk and IDLE** in the official Python installer.
- Debian/Ubuntu: install `python3-tk`.
- A graphical desktop is required. WSL without a GUI display can still launch
  the application using the Windows Python executable.

On Windows, install **WinUSB** or **libusbK** for the intended test-device
interface with [Zadig](https://zadig.akeo.ie/). `port_cycle` additionally requires
Windows and libusbK, including its native DLL. Do not replace a hub, host
controller, keyboard/mouse, or UART driver.

## GUI

```sh
python usb_stress_gui.py
# or, after installation:
usbstress-gui
```

1. Choose **generic** or click **CST1 lab preset**.
   Selecting **cst1** from the Profile dropdown applies its required configuration,
   interface, alternate settings and endpoints while preserving the selected
   device, workload and compatible test selections.
2. Select a connected device from the discovered-device dropdown. Discovery runs
   at startup; **Refresh devices** rescans without blocking the GUI. The list shows
   product name, VID/PID, bus/address, and serial when available, and fills in the
   IDs automatically. No device is selected automatically and no test is started.
   Inaccessible strings are marked with a warning; driver/permission setup may
   still be required. Discovery only reads descriptors, never claims interfaces
   or changes configurations. Confirm configuration, interface, alternates, and
   bulk endpoint addresses yourself. Manual ID entry remains available.
   Numeric fields accept decimal or `0x` hexadecimal.
   The selected bus/address distinguishes devices sharing VID/PID even without
   serial numbers. Addresses are session-specific: after unplug/replug, refresh
   and reselect rather than reusing a stale saved location. Port cycling
   rediscovers the uniquely matching VID/PID and updates its address.
3. For generic data tests, declare the device's behavior and explicitly
   authorize data writes/state changes.
4. Choose individual tests and workload settings. Unavailable tests are disabled.
   **Select compatible tests** deliberately excludes port cycling; select that
   checkbox explicitly if needed.
5. Optionally save/load the configuration as JSON.
6. Run. The second tab updates each test to RUNNING, then PASS/FAIL immediately
   as events arrive, with an elapsed-time indicator during execution. Logs
   stream alongside the table. The final JSON report remains authoritative
   (including cleanup/report errors); live PASS events are per-test only.
   Expand a test row to inspect its key/value details, elapsed time and errors.
   Nested objects and lists expand into further child rows; the JSON report
   format is unchanged.
   The overall progress bar counts completed tests (PASS or FAIL), not bytes or
   predicted run time. The running test shows elapsed time in its table row.
   Aborted runs leave
   unexecuted tests uncounted rather than displaying 100%.
   Failed runs display an error dialog with the reason and report path after
   child-process cleanup, including discovery failures before tests start.

Each run uses an independent CLI child process and a snapshot of the settings,
so USB calls never block the GUI thread. Controls are disabled while running.
Stop requests graceful cancellation; if the child remains unresponsive it is
terminated after eight seconds. Forced termination is reported as an error:
USB cleanup and report completion cannot then be guaranteed. Only the owned
child process is stopped.

## CLI

No device is selected by default. Generic mode defaults to descriptor checks:

```sh
python usb_stress.py --vendor-id 0x1234 --product-id 0x5678
```

Use the exact configuration/interface/alternate for your device:

```sh
python usb_stress.py --profile generic --vendor-id 0x1234 --product-id 0x5678 \
  --interface 2 --alternate 0 --in-endpoint 0x83 --out-endpoint 0x03 \
  --behavior loopback --allow-data-tests --tests descriptors,loopback,soak \
  --iterations 4 --sizes 1,63,64,65,512 --duration 30 --report results/loopback.json
```

The CLI accepts the same JSON files as the GUI; explicit command-line values
override saved settings:

```sh
python usb_stress.py --config examples/cst1.json --iterations 4
```

On Windows use `py -3` instead of `python` if preferred. PowerShell commands
should be on one line or use PowerShell's backtick continuation, not `\`.

Reports are exclusively created, never overwritten. Default reports go into
`results/` beneath the current directory. Transfer/test failures return nonzero,
produce a FAIL report where possible, and leave remaining tests NOT_RUN.
Invalid configurations are rejected before USB discovery. A zero exit status
requires every selected test to pass.

## Device profiles and tests

### Generic

Only descriptor checks are enabled by default. Arbitrary USB endpoints are
**not** a promise that arbitrary data is safe to send: a production device
could interpret traffic as commands. Use cooperative test firmware.

| Test | Required device behavior |
|---|---|
| `descriptors` | Standard USB device/configuration descriptors |
| `bulk`, `duplex`, `queued_bulk` | OUT discards data, IN produces zeros |
| `halt` | Same source/sink behavior plus standard halt/clear-halt |
| `alternate` | Same source/sink behavior and both configured alternate settings |
| `loopback`, `soak` | Exact byte echo on the selected bulk pair |

Generic mode never sends CST1 vendor controls. It reports host byte accounting,
not device counters. IN and OUT must have equal bulk maximum packet sizes.
Loopback is packet-chunked to accommodate finite-buffer firmware. Source reads
are rounded to packet boundaries. Descriptors are read without changing the
alternate setting; data tests select the configured active alternate.

Generic mode does not automatically switch device behavior: source/sink and
loopback are mutually exclusive declarations. A combined test selection that
requires both is rejected. Use separate device configurations/runs instead.

### CST1

**CST1** is the cooperative protocol implemented by the original stress ROM,
not a USB standard. Any device implementing this contract can use the profile.
The lab preset selects VID/PID `0424:0007`; change those IDs for another device.
No MCU checkout is required, but compatible firmware must already be installed.

CST1 fixes configuration 1, interface 0, empty alternate 0 and bulk alternate 1,
OUT `01` and IN `81`. EP0 is 64 bytes; bulk packets are FS 64 or HS 512.
Capability response `CST1`, version 1, and required feature bits are checked
before data traffic; ordinary OCP recovery devices are rejected.

All generic test names plus:

| Test | Purpose |
|---|---|
| `control` | Seeded zero/short/multi-packet vendor control write/read-back |
| `chapter9` | CST1-specific configured-state descriptor/status/alternate subset and negative STALL recovery |
| `queued_control` | Multiple outstanding EP0 read-back requests |
| `cancel` | NAK-blocked single/queued IN and OUT cancellation, counters, and recovery after pipe reinitialization |
| `port_cycle` | Native Windows `UsbK_ResetDevice`, cleared firmware marker, and new control/loopback traffic |

Without `--tests`, CST1 runs the original seven groups:
`control,bulk,loopback,halt,alternate,duplex,soak`.

Run expanded coverage explicitly:

```sh
python usb_stress.py --profile cst1 --vendor-id 0x0424 --product-id 0x0007 \
  --tests control,bulk,loopback,halt,alternate,duplex,soak,chapter9,queued_bulk,queued_control,cancel,port_cycle \
  --iterations 2 --queue-depth 8 --duration 10
```

Omit `port_cycle` on Linux or with WinUSB. A serial selector disambiguates normal
tests, but the native port-cycle API selects by VID/PID only: this test requires
exactly one matching device even when a serial is supplied.

Protocol (device-recipient vendor requests):

| Request | Meaning |
|---|---|
| `40/5b` | Write up to 1024 control-test bytes |
| `c0/5c` | Read last completed control write |
| `c0/5d` | 16-byte capability block, `<4sBBBBHHI` |
| `40/5e`, value 0/1 | Source/sink or loopback; reset counters/queues |
| `c0/5f` | Four LE u32s: OUT/IN bytes and OUT/IN packets |

Counters wrap modulo 2^32. The capability block contains magic, version, mode,
alternate, speed (1 FS / 2 HS), packet size, control maximum, and feature bitmap.

## Coverage limits

- Real asynchronous transfers use `libusb1`/`usb1`, not simulated queueing with
  synchronous worker threads. `--queue-depth` is 2..64 per bulk direction/EP0.
- Whole-queue cancellation is supported; selective unlink from a continuing
  queue is not. Cancellation recovery includes alternate/mode pipe reset.
- Port cycling is not an isolated bus-reset test, MCU reboot, or verified VBUS
  power cycle. Standard PyUSB `reset()` is not used as a substitute.
- Linux kernel scatter-gather/DMA mapping, ISO/interrupt traffic, suspend and
  remote wakeup are not implemented.
- Windows may intercept/rewrite standard requests. `chapter9` is not USB3CV
  compliance coverage.
- Async batches include handle/alternate setup; throughput is not line-rate
  or a persistent-queue benchmark.
- USB devices are not reset/retried automatically after a test failure.

## Offline development checks

```sh
python -m unittest discover -s tests -v
```

Tests use fake USB transports and owned-process mocks; they do not access USB.
Tkinter widget tests need a desktop and are explicitly skipped when unavailable.
The extracted CST1 engine was previously exercised on HTG-940, but the new
standalone/generic application has **not** been hardware-qualified.

Licensed under Apache-2.0. The initial CST1 host engine was extracted from
[caliptra-mcu-sw](https://github.com/chipsalliance/caliptra-mcu-sw); the existing
MCU repository scripts remain independent and unchanged.
