import time
import logging
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple
import hid
from .models import DeviceBattery
from .power_guard import get_refresh_generation, sample_mouse_activity

RAZER_VID = 0x1532
TRANSACTION_IDS = [0x1F, 0x3F, 0xFF]


@dataclass
class _RazerDeviceState:
    percent: Optional[int] = None
    is_charging: bool = False
    last_poll_mono: float = 0.0
    last_refresh_gen: int = 0


class RazerHIDCollector:
    def __init__(self):
        # Cache successful (path, tid) for each PID to ensure lightning-fast subsequent polls (~15ms)
        # and avoid multi-interface probe storms when a wireless mouse/headset enters sleep mode.
        self._successful_path_tid: Dict[int, Tuple[bytes, int]] = {}
        self._states: Dict[int, _RazerDeviceState] = {}

    @staticmethod
    def _create_razer_report(transaction_id: int, command_class: int, command_id: int, data_size: int) -> bytes:
        buf = bytearray(91)
        buf[0] = 0x00
        buf[1] = 0x00
        buf[2] = transaction_id
        buf[3] = 0x00
        buf[4] = 0x00
        buf[5] = 0x00
        buf[6] = data_size
        buf[7] = command_class
        buf[8] = command_id

        crc = 0
        for i in range(3, 89):
            crc ^= buf[i]
        buf[89] = crc
        buf[90] = 0x00
        return bytes(buf)

    def _query_interface(self, path: bytes, tid: int, cmd_id: int) -> int:
        dev = hid.device()
        try:
            dev.open_path(path)
            report = self._create_razer_report(tid, command_class=0x07, command_id=cmd_id, data_size=0x02)
            dev.send_feature_report(report)
            time.sleep(0.015)
            resp = dev.get_feature_report(0x00, 91)

            if resp and len(resp) >= 11:
                offset = 1 if len(resp) == 91 else 0
                status = resp[0 + offset]
                if status == 0x02:
                    return resp[9 + offset]
        except Exception:
            pass
        finally:
            try:
                dev.close()
            except Exception:
                pass
        return -1

    def _should_poll(self, pid: int, state: _RazerDeviceState, has_cached_endpoint: bool) -> bool:
        now = time.monotonic()
        cur_gen = get_refresh_generation()
        idle_sec, last_active_mono = sample_mouse_activity()

        if state.last_refresh_gen != cur_gen or state.last_poll_mono <= 0.0:
            return True

        elapsed = now - state.last_poll_mono

        if state.percent is not None:
            # Awake: allow one settle check right after cursor stops (catches dock charging quickly),
            # otherwise poll dongle gently every 15s instead of every 2s.
            if 1.5 <= idle_sec < 6.0 and state.last_poll_mono < last_active_mono:
                return True
            return elapsed >= 15.0

        # Currently sleeping / disconnected
        if has_cached_endpoint:
            # Known endpoint costs only 1 Feature Report: wake immediately on mouse move, or check every 15s for headsets
            if last_active_mono > state.last_poll_mono:
                return True
            return elapsed >= 15.0

        # Unknown endpoint (e.g. powered-off headset dongle at boot or wired non-battery Razer peripheral):
        # throttle full multi-interface discovery to avoid hammering USB every 2s
        if last_active_mono > state.last_poll_mono and elapsed >= 10.0:
            return True
        return elapsed >= 30.0

    def collect(self) -> List[DeviceBattery]:
        results: List[DeviceBattery] = []
        try:
            all_devs = hid.enumerate(RAZER_VID, 0)
        except Exception as e:
            logging.error(f"Razer HID enumerate error: {e}")
            return results

        grouped_by_pid: Dict[int, list] = {}
        for d in all_devs:
            pid = d["product_id"]
            grouped_by_pid.setdefault(pid, []).append(d)

        # Clean up unplugged Razer devices
        for stale_pid in list(self._states.keys()):
            if stale_pid not in grouped_by_pid:
                self._states.pop(stale_pid, None)
                self._successful_path_tid.pop(stale_pid, None)

        now = time.monotonic()
        cur_gen = get_refresh_generation()

        for pid, interfaces in grouped_by_pid.items():
            product_name = interfaces[0].get("product_string") or f"Razer Device (0x{pid:04X})"
            state = self._states.setdefault(pid, _RazerDeviceState())

            cached_entry = self._successful_path_tid.get(pid)
            cached_valid = (
                cached_entry is not None
                and any(iface.get("path") == cached_entry[0] for iface in interfaces)
            )
            if cached_entry is not None and not cached_valid:
                self._successful_path_tid.pop(pid, None)

            if self._should_poll(pid, state, cached_valid):
                battery_raw = -1
                charging_raw = 0

                if cached_valid and cached_entry is not None:
                    # 1. Fast path: query ONLY the verified (path, tid).
                    #    When val <= 0, the wireless device is sleeping — do NOT fall through
                    #    to blast all other HID interfaces and transaction IDs!
                    cached_path, cached_tid = cached_entry
                    val = self._query_interface(cached_path, cached_tid, cmd_id=0x80)
                    if val > 0:
                        battery_raw = val
                        charging_raw = self._query_interface(cached_path, cached_tid, cmd_id=0x84)
                else:
                    # 2. Initial discovery scan across interfaces & transaction IDs
                    for iface in interfaces:
                        path = iface["path"]
                        for tid in TRANSACTION_IDS:
                            val = self._query_interface(path, tid, cmd_id=0x80)
                            if val > 0:
                                battery_raw = val
                                charging_raw = self._query_interface(path, tid, cmd_id=0x84)
                                self._successful_path_tid[pid] = (path, tid)
                                break
                        if battery_raw > 0:
                            break

                state.last_poll_mono = now
                state.last_refresh_gen = cur_gen
                if battery_raw > 0:
                    state.percent = max(0, min(100, round((battery_raw / 255.0) * 100)))
                    state.is_charging = (charging_raw == 1)
                else:
                    state.percent = None
                    state.is_charging = False

            if state.percent is not None:
                results.append(
                    DeviceBattery(
                        id=f"razer_{pid:04x}",
                        name=product_name,
                        protocol="Razer 2.4G",
                        battery_percent=state.percent,
                        is_charging=state.is_charging,
                        status_text="Connected",
                    )
                )
            else:
                results.append(
                    DeviceBattery(
                        id=f"razer_{pid:04x}",
                        name=product_name,
                        protocol="Razer 2.4G",
                        battery_percent=None,
                        status_text="Sleeping / Disconnected",
                    )
                )

        return results
