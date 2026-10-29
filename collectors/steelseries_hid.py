import time
import logging
from typing import Dict, List, Optional, Tuple
import hid
from .models import DeviceBattery
from .power_guard import (
    WirelessMouseState,
    decide_mouse_rf_action,
    get_refresh_generation,
    sample_mouse_activity,
)

STEELSERIES_VID = 0x1038

# Probes from rivalcfg and HeadsetControl
# Mouse battery query: [0x00, 0x92] + 62 zeros
STEELSERIES_MOUSE_PROBE = bytes([0x00, 0x92] + [0x00] * 62)
# Headset battery query: [0x00, 0xB0] + 62 zeros
STEELSERIES_HEADSET_PROBE = bytes([0x00, 0xB0] + [0x00] * 62)


class SteelSeriesHIDCollector:
    def __init__(self):
        # Cache (path, working_probe_payload, is_headset) per PID
        self._cached_endpoints: Dict[int, Tuple[bytes, bytes, bool]] = {}
        self._states: Dict[int, WirelessMouseState] = {}
        self._last_discovery_mono: Dict[int, Tuple[float, int]] = {}

    def _probe_device(
        self,
        path: bytes,
        specific_probe: Optional[bytes] = None,
    ) -> Tuple[int, bool, Optional[bytes]]:
        dev = hid.device()
        probes = (specific_probe,) if specific_probe is not None else (STEELSERIES_MOUSE_PROBE, STEELSERIES_HEADSET_PROBE)
        try:
            dev.open_path(path)
            dev.set_nonblocking(1)

            for probe_payload in probes:
                try:
                    dev.send_feature_report(probe_payload)
                    time.sleep(0.025)
                    resp = dev.get_feature_report(0x00, 64)
                    if resp and len(resp) >= 4:
                        # Most SteelSeries devices report battery in byte 2 or byte 3
                        pct = resp[2]
                        if 1 <= pct <= 100:
                            is_charging = (resp[3] == 0x01 if len(resp) > 3 else False)
                            return pct, is_charging, probe_payload
                        pct_alt = resp[3]
                        if 1 <= pct_alt <= 100:
                            is_charging = (resp[4] == 0x01 if len(resp) > 4 else False)
                            return pct_alt, is_charging, probe_payload
                except Exception:
                    continue

        except Exception as e:
            logging.debug(f"SteelSeries probe error: {e}")
        finally:
            try:
                dev.close()
            except Exception:
                pass
        return -1, False, None

    def collect(self) -> List[DeviceBattery]:
        results: List[DeviceBattery] = []
        try:
            devices = hid.enumerate(STEELSERIES_VID, 0)
        except Exception:
            return results

        grouped: Dict[int, List[dict]] = {}
        for d in devices:
            if d.get("usage_page", 0) >= 0xFF00:
                grouped.setdefault(d.get("product_id", 0), []).append(d)

        for stale_pid in list(self._states.keys()):
            if stale_pid not in grouped:
                self._states.pop(stale_pid, None)
                self._cached_endpoints.pop(stale_pid, None)
                self._last_discovery_mono.pop(stale_pid, None)
        for stale_pid in list(self._last_discovery_mono.keys()):
            if stale_pid not in grouped:
                self._last_discovery_mono.pop(stale_pid, None)

        now = time.monotonic()
        cur_gen = get_refresh_generation()
        _, last_active_mono = sample_mouse_activity()

        for pid, ifaces in grouped.items():
            prod_name = (ifaces[0].get("product_string") or "").strip() or f"SteelSeries Device (0x{pid:04X})"

            cached = self._cached_endpoints.get(pid)
            if cached is not None and not any(d.get("path") == cached[0] for d in ifaces):
                self._cached_endpoints.pop(pid, None)
                cached = None

            # 1. Verified endpoint fast path (sends only the 1 matched probe, respects wireless mouse sleep)
            if cached is not None:
                c_path, c_probe, is_headset = cached
                state = self._states.setdefault(
                    pid,
                    WirelessMouseState(sleep_timeout_sec=300.0, name=prod_name),
                )
                state.name = prod_name

                if is_headset:
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
                    pct, is_charging, _ = self._probe_device(c_path, specific_probe=c_probe)
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
                            id=f"steelseries_{pid:04x}",
                            name=prod_name,
                            protocol="SteelSeries 2.4G",
                            battery_percent=state.percent,
                            is_charging=state.is_charging,
                            status_text="Connected",
                        )
                    )
                else:
                    results.append(
                        DeviceBattery(
                            id=f"steelseries_{pid:04x}",
                            name=prod_name,
                            protocol="SteelSeries 2.4G",
                            battery_percent=None,
                            status_text="Sleeping / Inactive",
                        )
                    )
                continue

            # 2. Throttled initial discovery
            is_headset_hint = any(kw in prod_name.lower() for kw in ("arctis", "nova", "headset"))
            last_disc_mono, last_disc_gen = self._last_discovery_mono.get(pid, (-1e9, 0))
            if last_disc_gen == cur_gen:
                elapsed_disc = now - last_disc_mono
                mouse_moved_since = (not is_headset_hint) and (last_active_mono > last_disc_mono) and (elapsed_disc >= 5.0)
                if not mouse_moved_since and elapsed_disc < 30.0:
                    continue

            self._last_discovery_mono[pid] = (now, cur_gen)

            for d in ifaces:
                path = d.get("path")
                percent, is_charging, matched_probe = self._probe_device(path)
                if 1 <= percent <= 100 and matched_probe is not None:
                    is_headset = (matched_probe == STEELSERIES_HEADSET_PROBE) or is_headset_hint
                    self._cached_endpoints[pid] = (path, matched_probe, is_headset)
                    self._states[pid] = WirelessMouseState(
                        percent=percent,
                        is_charging=is_charging,
                        is_sleeping=False,
                        sleep_timeout_sec=300.0,
                        last_rf_poll_mono=now,
                        last_wake_check_mono=now,
                        last_refresh_gen=cur_gen,
                        name=prod_name,
                    )
                    results.append(
                        DeviceBattery(
                            id=f"steelseries_{pid:04x}",
                            name=prod_name,
                            protocol="SteelSeries 2.4G",
                            battery_percent=percent,
                            is_charging=is_charging,
                            status_text="Connected",
                        )
                    )
                    break

        return results
