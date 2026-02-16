# UPDATE_PLAN

## Goal
Align camera cycling with intended behavior: run **once per boot**, triggered only from `remla run`, while keeping `init()` idempotent and non-destructive on re-runs.

## Background (Current Behavior)
- Camera cycle is triggered in two places:
  - `remla/main.py:init()` checks `runMarker.exists()` and may cycle.
  - `remla/main.py:run()` calls `get_boot_status()` and may cycle.
- `runMarker` is a per-boot guard: `get_boot_status()` writes current boot time to `runMarker` and returns True only on a new boot.
- Because `init()` uses `runMarker.exists()` instead of `get_boot_status()`, a fresh install can cycle in `init()` and then cycle again in `run()` on the same boot.

## Target Behavior (Agreed)
- **Cycle only in `run()`**, never in `init()`.
- **Cycle exactly once per boot** using `get_boot_status()`.
- Cycling should only switch ArduCam channels and **restart `mediamtx`** per channel (no `remla.service` restart inside the cycle).
- Re-running `init()` should only create missing files/directories and not overwrite existing ones.
- If camera config changes, simplest policy is to require a reboot (no auto-recycle unless explicitly requested later).

## Planned Code Changes

### 1) Move camera cycling out of `init()`
- Remove the `runMarker.exists()` / `cycle_initialize_cameras()` block in `remla/main.py:init()`.
- `init()` should remain idempotent (create missing dirs/files, install services), but not perform boot-guarded runtime actions.

### 2) Keep boot-guarded cycling only in `run()`
- Keep `get_boot_status()` call in `remla/main.py:run()`.
- Ensure camera cycle is executed only when `get_boot_status()` returns True.
- Preserve existing logging to `camera_cycle.log` via `get_camera_logger()`.

### 3) Align documentation and behavior
- Update the docstring in `remla/systemHelpers.py:cycle_initialize_cameras()` to describe what it actually does:
  - select mux channel
  - restart `mediamtx`
  - wait `timeout_per_camera`
- Remove or clarify stale comments about starting/stopping `remla.service`.

### 4) Optional safety guard (decision needed)
- Consider restoring the guard that skips cycling when `remla.service` is already active.
- If you want this behavior, re-enable the block currently commented out in `cycle_initialize_cameras()`.
- If not, leave it as-is and rely solely on the per-boot guard.

## Related Fixes (Previously Recommended)
These are not strictly required for the boot-cycle change, but should be tracked:
- Fix `Experiment.setupSignalHandlers()` import usage (`signal.signal` vs `from signal import signal`).
- Fix `Experiment.setup()` socket call (`setTimeout` -> `settimeout`).
- Initialize missing `Experiment` attributes (`jsonFile`, `socketPath`, `clientQueue`, `connection`).
- Make `runDeviceMethod()` robust to `None`/string responses.
- Fix multiple inheritance initializers in controllers (`Plug`, `StepperSimple`, `DCMotorI2C`).
- Update `cycle_initialize_cameras()` comment to match mediamtx-only behavior.

## Testing / Verification
- Manual: reboot, then run `remla run -f` once and confirm cameras cycle only once (check `logsDirectory/camera_cycle.log`).
- Manual: run `remla run -f` again same boot and confirm no cycle (log shows skip).
- Manual: call `remla init` multiple times; confirm no camera cycle and existing files are not overwritten.
