import time
import logging
from typing import Dict, List, Set, Tuple
import hid
from .models import DeviceBattery
from .power_guard import (
    WirelessMouseState,
    decide_mouse_rf_action,
    get_refresh_generation,
    sample_mouse_activity,
)

CORSAIR_VID = 0x1B1C

# Corsair Slipstream / Wireless battery query probe
CORSAIR_BATTERY_PROBE = bytes([0x08, 0x02, 0x00, 0x00] + [0x00] * 60)

# Internal Corsair iCUE cooling / fan / lighting hubs that never have batteries
IGNORED_CORSAIR_KEYWORDS = (
    "commander",
    "lighting node",
    "icue link",
    "hydro",
    "h100",
    "h115",
    "h150",
    "h170",
)

_NON_MOUSE_KEYWORDS = (
    "void",
    "virtuoso",
    "hs80",
    "hs65",
    "hs55",
    "headset",
    "k65",
    "k70",
    "k100",
    "keyboard",
)


class CorsairHIDCollector:
    def __init__(self):
        self._ignored_pids: Set[int] = set()
        self._cached_paths: Dict[int, bytes] = {}
        self._states: Dict[int, WirelessMouseState] = {}
        self._last_discovery_mono: Dict[int, Tuple[float, int]] = {}

    def _probe_device(self, path: bytes) -> Tuple[int, bool]:
        dev = hid.device()
        try:
            dev.open_path(path)
            dev.set_nonblocking(1)

            try:
                dev.send_feature_report(CORSAIR_BATTERY_PROBE)
                time.sleep(0.025)
                resp = dev.get_feature_report(0x08, 64)
                if resp and len(resp) >= 6:
                    # ckb-next mapping: byte 4 is battery level (or byte 4/5 * 1000)
                    pct = resp[4]
                    chg_code = resp[3]
                    is_charging = (chg_code in (0x02, 0x03))
                    if 1 <= pct <= 100:
                        return pct, is_charging
                    # Millivolt or permille representation
                    if len(resp) >= 6:
                        permille = (resp[4] << 8) | resp[5]
                        if 10 <= permille <= 1000:
                            return round(permille / 10.0), is_charging
            except Exception:
                pass

        except Exception as e:
            logging.debug(f"Corsair probe error: {e}")
        finally:
            try:
                dev.close()
            except Exception:
                pass
        return -1, False

    def collect(self) -> List[DeviceBattery]:
        results: List[DeviceBattery] = []
        try:
            devices = hid.enumerate(CORSAIR_VID, 0)
        except Exception:
            return results

        grouped: Dict[int, List[dict]] = {}
        for d in devices:
            usage_page = d.get("usage_page", 0)
            pid = d.get("product_id", 0)
            if usage_page < 0xFF00 or pid in self._ignored_pids:
                continue

            raw_prod = (d.get("product_string") or "").strip()
            if any(kw in raw_prod.lower() for kw in IGNORED_CORSAIR_KEYWORDS):
                self._ignored_pids.add(pid)
                continue

            grouped.setdefault(pid, []).append(d)

        for stale_pid in list(self._states.keys()):
            if stale_pid not in grouped:
                self._states.pop(stale_pid, None)
                self._cached_paths.pop(stale_pid, None)
                self._last_discovery_mono.pop(stale_pid, None)
        for stale_pid in list(self._last_discovery_mono.keys()):
            if stale_pid not in grouped:
                self._last_discovery_mono.pop(stale_pid, None)

        now = time.monotonic()
        cur_gen = get_refresh_generation()
        _, last_active_mono = sample_mouse_activity()

        for pid, ifaces in grouped.items():
            prod_name = (ifaces[0].get("product_string") or "").strip() or f"Corsair Device (0x{pid:04X})"
            is_non_mouse = any(kw in prod_name.lower() for kw in _NON_MOUSE_KEYWORDS)

            cached_path = self._cached_paths.get(pid)
            if cached_path and not any(d.get("path") == cached_path for d in ifaces):
                self._cached_paths.pop(pid, None)
                cached_path = None

            # 1. Verified wireless device fast path (sleep-safe)
            if cached_path is not None:
                state = self._states.setdefault(
                    pid,
                    WirelessMouseState(sleep_timeout_sec=180.0, name=prod_name),
                )
                state.name = prod_name

                if is_non_mouse:
                    interval = 30.0 if state.is_sleeping else 60.0
                    should_query = (
                        state.last_refresh_gen != cur_gen
                        or state.last_rf_poll_mono <= 0.0
                        or (now - max(state.last_rf_poll_mono, state.last_wake_check_mono)) >= interval
                    )
                else:
                    action = decide_mouse_rf_action(state, active_interval=30.0, charging_interval=30.0, sleep_buffer_sec=5.0)
                    should_query = action in ("poll_full", "check_sleep_only")

                if should_query:
                    state.last_wake_check_mono = now
                    state.last_refresh_gen = cur_gen
                    pct, is_charging = self._probe_device(cached_path)
                    if 1 <= pct <= 100:
                        state.percent = pct
                        state.is_charging = is_charging
                        state.is_sleeping = False
                        state.last_rf_poll_mono = now
                    else:
                        if not state.is_sleeping:
                            state.sleep_confirmed_mono = now
                        state.is_sleeping = True
                        state.is_charging = False

                if not state.is_sleeping and state.percent is not None:
                    results.append(
                        DeviceBattery(
                            id=f"corsair_{pid:04x}",
                            name=prod_name,
                            protocol="Corsair 2.4G",
                            battery_percent=state.percent,
                            is_charging=state.is_charging,
                            status_text="Connected",
                        )
                    )
                else:
                    results.append(
                        DeviceBattery(
                            id=f"corsair_{pid:04x}",
                            name=prod_name,
                            protocol="Corsair 2.4G",
                            battery_percent=None,
                            status_text="Sleeping / Inactive",
                        )
                    )
                continue

            # 2. Throttled initial discovery
            last_disc_mono, last_disc_gen = self._last_discovery_mono.get(pid, (-1e9, 0))
            if last_disc_gen == cur_gen:
                elapsed_disc = now - last_disc_mono
                mouse_moved_since = (not is_non_mouse) and (last_active_mono > last_disc_mono) and (elapsed_disc >= 5.0)
                if not mouse_moved_since and elapsed_disc < 30.0:
                    continue

            self._last_discovery_mono[pid] = (now, cur_gen)

            for d in ifaces:
                path = d.get("path")
                percent, is_charging = self._probe_device(path)
                if 1 <= percent <= 100:
                    self._cached_paths[pid] = path
                    self._states[pid] = WirelessMouseState(
                        percent=percent,
                        is_charging=is_charging,
                        is_sleeping=False,
                        sleep_timeout_sec=180.0,
                        last_rf_poll_mono=now,
                        last_wake_check_mono=now,
                        last_refresh_gen=cur_gen,
                        name=prod_name,
                    )
                    results.append(
                        DeviceBattery(
                            id=f"corsair_{pid:04x}",
                            name=prod_name,
                            protocol="Corsair 2.4G",
                            battery_percent=percent,
                            is_charging=is_charging,
                            status_text="Connected",
                        )
                    )
                    break

        return results
