import time
import logging
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple
import hid
from .models import DeviceBattery
from .power_guard import get_refresh_generation

SONY_VID = 0x054C

SONY_PIDS = {
    0x0CE6: "DualSense Wireless Controller",
    0x0DF2: "DualSense Edge Wireless Controller",
    0x05C4: "DualShock 4 Wireless Controller",
    0x09CC: "DualShock 4 Wireless Controller",
    0x0BA0: "DualShock 4 USB Wireless Adapter",
}


@dataclass
class _SonyControllerState:
    percent: Optional[int] = None
    is_charging: bool = False
    last_read_mono: float = 0.0
    last_success_mono: float = 0.0
    last_refresh_gen: int = 0


class SonyHIDCollector:
    def __init__(self):
        self._cached_paths: Dict[int, bytes] = {}
        self._states: Dict[int, _SonyControllerState] = {}

    def _read_controller_state(self, path: bytes, pid: int) -> Tuple[int, bool]:
        """100% passive read of standard PlayStation HID input reports (zero TX packets sent)."""
        dev = hid.device()
        try:
            dev.open_path(path)
            dev.set_nonblocking(1)

            # Try to read standard input reports (read up to 128 bytes)
            for _ in range(3):
                data = dev.read(128)
                if not data:
                    time.sleep(0.01)
                    continue

                rep_id = data[0] if data else 0

                # --- DualSense (PS5) ---
                if pid in (0x0CE6, 0x0DF2):
                    # USB report (ID 0x01, len >= 54)
                    if rep_id == 0x01 and len(data) >= 54:
                        raw_byte = data[53]
                        level = raw_byte & 0x0F
                        chg_state = (raw_byte >> 4) & 0x0F
                        percent = min(100, level * 10)
                        is_charging = (chg_state in (0x01, 0x02))
                        return percent, is_charging

                    # Bluetooth report (ID 0x31, len >= 55)
                    if rep_id == 0x31 and len(data) >= 55:
                        raw_byte = data[54]
                        level = raw_byte & 0x0F
                        chg_state = (raw_byte >> 4) & 0x0F
                        percent = min(100, level * 10)
                        is_charging = (chg_state in (0x01, 0x02))
                        return percent, is_charging

                # --- DualShock 4 (PS4) ---
                elif pid in (0x05C4, 0x09CC, 0x0BA0):
                    # USB report (ID 0x01, len >= 31)
                    if rep_id == 0x01 and len(data) >= 31:
                        raw_byte = data[30]
                        level = raw_byte & 0x0F
                        is_charging = bool(raw_byte & 0x10)
                        percent = min(100, round((level / 8.0) * 100))
                        return percent, is_charging

                    # Bluetooth report (ID 0x11, len >= 33)
                    if rep_id == 0x11 and len(data) >= 33:
                        raw_byte = data[32]
                        level = raw_byte & 0x0F
                        is_charging = bool(raw_byte & 0x10)
                        percent = min(100, round((level / 8.0) * 100))
                        return percent, is_charging

        except Exception as e:
            logging.debug(f"Sony HID read error: {e}")
        finally:
            try:
                dev.close()
            except Exception:
                pass
        return -1, False

    def collect(self) -> List[DeviceBattery]:
        results: List[DeviceBattery] = []
        try:
            devices = hid.enumerate(SONY_VID, 0)
        except Exception:
            return results

        grouped: Dict[int, List[dict]] = {}
        for d in devices:
            pid = d.get("product_id", 0)
            if pid in SONY_PIDS:
                grouped.setdefault(pid, []).append(d)

        for stale_pid in list(self._states.keys()):
            if stale_pid not in grouped:
                self._states.pop(stale_pid, None)
                self._cached_paths.pop(stale_pid, None)

        now = time.monotonic()
        cur_gen = get_refresh_generation()

        for pid, ifaces in grouped.items():
            prod_name = SONY_PIDS.get(pid, ifaces[0].get("product_string") or "PlayStation Controller")
            state = self._states.setdefault(pid, _SonyControllerState())

            cached_path = self._cached_paths.get(pid)
            if cached_path and not any(d.get("path") == cached_path for d in ifaces):
                self._cached_paths.pop(pid, None)
                cached_path = None

            should_read = (
                state.last_refresh_gen != cur_gen
                or state.last_read_mono <= 0.0
                or (now - state.last_read_mono) >= 15.0
            )

            if should_read:
                state.last_read_mono = now
                state.last_refresh_gen = cur_gen
                percent = -1
                is_charging = False

                if cached_path is not None:
                    percent, is_charging = self._read_controller_state(cached_path, pid)
                else:
                    for d in ifaces:
                        path = d.get("path")
                        percent, is_charging = self._read_controller_state(path, pid)
                        if 0 <= percent <= 100:
                            self._cached_paths[pid] = path
                            break

                if 0 <= percent <= 100:
                    state.percent = percent
                    state.is_charging = is_charging
                    state.last_success_mono = now
                elif pid == 0x0BA0 and (now - state.last_success_mono) >= 30.0:
                    # DualShock 4 USB Wireless Adapter stays plugged in when controller powers off
                    state.percent = None
                    state.is_charging = False

            if state.percent is not None:
                results.append(
                    DeviceBattery(
                        id=f"sony_{pid:04x}",
                        name=prod_name,
                        protocol="PlayStation HID",
                        battery_percent=state.percent,
                        is_charging=state.is_charging,
                        status_text="Connected",
                    )
                )
            elif pid == 0x0BA0 and state.last_success_mono > 0.0:
                results.append(
                    DeviceBattery(
                        id=f"sony_{pid:04x}",
                        name=prod_name,
                        protocol="PlayStation HID",
                        battery_percent=None,
                        status_text="Sleeping / Inactive",
                    )
                )

        return results
