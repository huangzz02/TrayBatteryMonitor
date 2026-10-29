import time
import logging
from dataclasses import dataclass
from typing import Dict, List, Optional, Set, Tuple
import hid
from .models import DeviceBattery
from .power_guard import (
    WirelessMouseState,
    decide_mouse_rf_action,
    get_refresh_generation,
    sample_mouse_activity,
)

LOGITECH_VID = 0x046D

# Non-mouse Logitech wireless peripherals (headsets / keyboards) that should poll on a
# fixed gentle timer rather than tracking mouse cursor movement
_NON_MOUSE_KEYWORDS = (
    "headset",
    "keyboard",
    "g733",
    "g533",
    "g535",
    "g933",
    "g935",
    "astro",
    "g915",
    "g715",
    "g515",
    "mx keys",
)


@dataclass
class _LogitechEndpoint:
    path: bytes
    dev_idx: int      # 0x01..0x02 = wireless paired device over receiver; 0xFF = direct USB cable
    proto_ver: int    # 20 = HID++ 2.0, 10 = HID++ 1.0
    feat_idx: int = 0
    fn_code: int = 0


class LogitechHIDCollector:
    def __init__(self):
        # Cache PIDs confirmed to be wired devices without battery features (e.g., G512 keyboard)
        self._no_battery_pids: Set[int] = set()
        # Cache verified HID++ battery endpoint & feature index per PID (1 single packet per poll!)
        self._cached_endpoints: Dict[int, _LogitechEndpoint] = {}
        self._states: Dict[int, WirelessMouseState] = {}
        self._last_discovery_mono: Dict[int, Tuple[float, int]] = {}

    @staticmethod
    def _transact(dev, cmd: bytes, expected_rep_id: int, timeout: float = 0.05) -> Optional[List[int]]:
        while dev.read(64):
            pass
        dev.write(cmd)
        target_dev = cmd[1]
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            resp = dev.read(64)
            if resp:
                # Abort immediately if receiver returns HID++ 1.0/2.0 error (0x8F / 0xFF) for sleeping/absent device
                if len(resp) >= 3 and resp[0] in (0x10, 0x11) and resp[1] == target_dev and resp[2] in (0x8F, 0xFF):
                    return resp
                if resp[0] == expected_rep_id and len(resp) >= 2 and resp[1] == target_dev:
                    return resp
            else:
                time.sleep(0.004)
        return None

    @staticmethod
    def _parse_hidpp20_battery(batt_resp: List[int], feat_idx: int) -> Tuple[int, bool]:
        if not batt_resp or len(batt_resp) < 6 or batt_resp[0] != 0x11 or batt_resp[2] != feat_idx:
            return -1, False
        pct = batt_resp[4]
        chg_status = batt_resp[5] if len(batt_resp) > 5 else 0
        if 1 <= pct <= 100:
            return pct, chg_status in (0x01, 0x02, 0x03)
        pct2 = batt_resp[5] if len(batt_resp) > 5 else 0
        if 1 <= pct2 <= 100:
            return pct2, batt_resp[4] in (0x01, 0x02, 0x03)
        return -1, False

    def _probe_hidpp20(self, dev, path: bytes, dev_idx: int) -> Tuple[int, bool, bool, Optional[_LogitechEndpoint]]:
        """
        Query Logitech HID++ 2.0 Feature 0x1004 (Unified Battery) or 0x1000 (Battery Status).
        Returns: (percent, is_charging, confirmed_no_battery, endpoint_meta)
        """
        answered_root_without_battery = False

        for feature_id_hi, feature_id_lo in ((0x10, 0x04), (0x10, 0x00)):
            try:
                # Root Feature (0x0000) function 0x00: IRoot.GetFeature(feature_id)
                get_feat_cmd = bytes([0x11, dev_idx, 0x00, 0x00, feature_id_hi, feature_id_lo] + [0x00] * 14)
                resp = self._transact(dev, get_feat_cmd, 0x11, timeout=0.05)

                if not resp or len(resp) < 6 or resp[0] != 0x11 or resp[2] in (0x8F, 0xFF):
                    continue

                feat_idx = resp[4]
                if feat_idx == 0x00:
                    # Device actively answered Root Feature and confirmed this battery feature is absent
                    answered_root_without_battery = True
                    continue

                answered_root_without_battery = False
                fn_candidates = (0x10, 0x00) if feature_id_lo == 0x04 else (0x00,)
                for fn in fn_candidates:
                    query_cmd = bytes([0x11, dev_idx, feat_idx, fn] + [0x00] * 16)
                    batt_resp = self._transact(dev, query_cmd, 0x11, timeout=0.05)
                    pct, is_charging = self._parse_hidpp20_battery(batt_resp, feat_idx)
                    if 1 <= pct <= 100:
                        ep = _LogitechEndpoint(
                            path=path,
                            dev_idx=dev_idx,
                            proto_ver=20,
                            feat_idx=feat_idx,
                            fn_code=fn,
                        )
                        return pct, is_charging, False, ep
            except Exception:
                continue

        return -1, False, answered_root_without_battery, None

    def _probe_hidpp10(self, dev, path: bytes, dev_idx: int) -> Tuple[int, bool, Optional[_LogitechEndpoint]]:
        """Query Logitech HID++ 1.0 Battery status (for older Unifying / G304 / G305 receivers)."""
        try:
            req = bytes([0x10, dev_idx, 0x81, 0x0D, 0x00, 0x00, 0x00])
            resp = self._transact(dev, req, 0x10, timeout=0.04)
            if resp and len(resp) >= 6 and resp[0] == 0x10 and resp[2] == 0x81 and resp[3] == 0x0D:
                level = resp[4]
                status = resp[5]
                if 1 <= level <= 100:
                    is_charging = status in (0x02, 0x03, 0x04)
                    ep = _LogitechEndpoint(path=path, dev_idx=dev_idx, proto_ver=10)
                    return level, is_charging, ep
        except Exception:
            pass
        return -1, False, None

    def _query_cached_endpoint(self, ep: _LogitechEndpoint) -> Tuple[int, bool]:
        """Fast 1-packet battery read on an already-discovered HID++ endpoint (zero discovery overhead)."""
        dev = hid.device()
        try:
            dev.open_path(ep.path)
            dev.set_nonblocking(1)
            if ep.proto_ver == 20:
                query_cmd = bytes([0x11, ep.dev_idx, ep.feat_idx, ep.fn_code] + [0x00] * 16)
                batt_resp = self._transact(dev, query_cmd, 0x11, timeout=0.05)
                return self._parse_hidpp20_battery(batt_resp, ep.feat_idx)
            else:
                req = bytes([0x10, ep.dev_idx, 0x81, 0x0D, 0x00, 0x00, 0x00])
                resp = self._transact(dev, req, 0x10, timeout=0.04)
                if resp and len(resp) >= 6 and resp[0] == 0x10 and resp[2] == 0x81 and resp[3] == 0x0D:
                    level = resp[4]
                    if 1 <= level <= 100:
                        return level, resp[5] in (0x02, 0x03, 0x04)
        except Exception as e:
            logging.debug(f"Logitech cached query error: {e}")
        finally:
            try:
                dev.close()
            except Exception:
                pass
        return -1, False

    def _discover_path(self, path: bytes) -> Tuple[int, bool, bool, Optional[_LogitechEndpoint]]:
        """Full one-time HID++ discovery on a candidate HID path."""
        dev = hid.device()
        confirmed_wired_no_battery = False
        try:
            dev.open_path(path)
            dev.set_nonblocking(1)

            # 0x01 / 0x02 = paired wireless devices on Lightspeed/Unifying/Bolt receiver; 0xFF = direct USB device
            for wireless_idx in (0x01, 0x02):
                pct, chg, _, ep = self._probe_hidpp20(dev, path, wireless_idx)
                if 1 <= pct <= 100 and ep is not None:
                    return pct, chg, False, ep

                pct10, chg10, ep10 = self._probe_hidpp10(dev, path, wireless_idx)
                if 1 <= pct10 <= 100 and ep10 is not None:
                    return pct10, chg10, False, ep10

            pct_dir, chg_dir, no_batt_dir, ep_dir = self._probe_hidpp20(dev, path, 0xFF)
            if 1 <= pct_dir <= 100 and ep_dir is not None:
                return pct_dir, chg_dir, False, ep_dir

            pct10_dir, chg10_dir, ep10_dir = self._probe_hidpp10(dev, path, 0xFF)
            if 1 <= pct10_dir <= 100 and ep10_dir is not None:
                return pct10_dir, chg10_dir, False, ep10_dir

            if no_batt_dir:
                confirmed_wired_no_battery = True

        except Exception as e:
            logging.debug(f"Logitech probe error: {e}")
        finally:
            try:
                dev.close()
            except Exception:
                pass

        return -1, False, confirmed_wired_no_battery, None

    def collect(self) -> List[DeviceBattery]:
        results: List[DeviceBattery] = []
        try:
            devices = hid.enumerate(LOGITECH_VID, 0)
        except Exception:
            return results

        grouped: Dict[int, List[dict]] = {}
        for d in devices:
            pid = d.get("product_id", 0)
            if pid in self._no_battery_pids:
                continue
            if d.get("usage_page", 0) in (0xFF00, 0xFF43):
                grouped.setdefault(pid, []).append(d)

        # Clean up unplugged receivers / devices
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
            prod_name = ifaces[0].get("product_string") or f"Logitech Device (0x{pid:04X})"
            lower_name = prod_name.lower()
            is_non_mouse = any(kw in lower_name for kw in _NON_MOUSE_KEYWORDS)

            ep = self._cached_endpoints.get(pid)
            ep_valid = ep is not None and any(d.get("path") == ep.path for d in ifaces)
            if ep is not None and not ep_valid:
                self._cached_endpoints.pop(pid, None)
                ep = None

            # 1. Verified endpoint fast path (sleep-safe & zero probe storms)
            if ep is not None:
                state = self._states.setdefault(
                    pid,
                    WirelessMouseState(sleep_timeout_sec=300.0, name=prod_name),
                )
                state.name = prod_name

                if ep.dev_idx == 0xFF:
                    # Direct USB cable connection: never sleeps over RF; poll gently every 30s
                    state.is_wired = True
                    state.is_sleeping = False
                    should_query = (
                        state.last_refresh_gen != cur_gen
                        or state.percent is None
                        or (now - state.last_rf_poll_mono) >= 30.0
                    )
                elif is_non_mouse:
                    # Wireless headset / keyboard: fixed gentle timer without mouse-cursor tie
                    state.is_wired = False
                    interval = 30.0 if state.is_sleeping else 60.0
                    should_query = (
                        state.last_refresh_gen != cur_gen
                        or state.last_rf_poll_mono <= 0.0
                        or (now - max(state.last_rf_poll_mono, state.last_wake_check_mono)) >= interval
                    )
                else:
                    # Wireless Lightspeed / Unifying / Bolt mouse: suppress RF while stationary so mouse sleeps!
                    state.is_wired = False
                    action = decide_mouse_rf_action(state, active_interval=30.0, charging_interval=30.0, sleep_buffer_sec=5.0)
                    should_query = action in ("poll_full", "check_sleep_only")

                if should_query:
                    state.last_wake_check_mono = now
                    state.last_refresh_gen = cur_gen
                    pct, is_charging = self._query_cached_endpoint(ep)
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

                proto_label = "Logitech Wired" if state.is_wired else "Logitech 2.4G"
                if not state.is_sleeping and state.percent is not None:
                    results.append(
                        DeviceBattery(
                            id=f"logitech_{pid:04x}",
                            name=prod_name,
                            protocol=proto_label,
                            battery_percent=state.percent,
                            is_charging=state.is_charging,
                            status_text="Connected",
                        )
                    )
                else:
                    results.append(
                        DeviceBattery(
                            id=f"logitech_{pid:04x}",
                            name=prod_name,
                            protocol=proto_label,
                            battery_percent=None,
                            status_text="Sleeping / Inactive",
                        )
                    )
                continue

            # 2. Unverified PID discovery (throttled if device was asleep at startup so receiver isn't spammed every 2s)
            last_disc_mono, last_disc_gen = self._last_discovery_mono.get(pid, (-1e9, 0))
            if last_disc_gen == cur_gen:
                elapsed_disc = now - last_disc_mono
                mouse_moved_since = (not is_non_mouse) and (last_active_mono > last_disc_mono) and (elapsed_disc >= 5.0)
                if not mouse_moved_since and elapsed_disc < 30.0:
                    continue

            self._last_discovery_mono[pid] = (now, cur_gen)

            # Prioritize 20-byte HID++ 2.0 long report collections (usage *02) first
            ifaces.sort(key=lambda d: 0 if (d.get("usage", 0) & 0xFF) == 0x02 else 1)

            confirmed_no_batt = False
            for d in ifaces:
                path = d.get("path")
                pct, is_charging, no_batt, new_ep = self._discover_path(path)
                if 1 <= pct <= 100 and new_ep is not None:
                    self._cached_endpoints[pid] = new_ep
                    is_wired = (new_ep.dev_idx == 0xFF)
                    state = WirelessMouseState(
                        percent=pct,
                        is_charging=is_charging,
                        is_wired=is_wired,
                        is_sleeping=False,
                        sleep_timeout_sec=300.0,
                        last_rf_poll_mono=now,
                        last_wake_check_mono=now,
                        last_refresh_gen=cur_gen,
                        name=prod_name,
                    )
                    self._states[pid] = state
                    results.append(
                        DeviceBattery(
                            id=f"logitech_{pid:04x}",
                            name=prod_name,
                            protocol="Logitech Wired" if is_wired else "Logitech 2.4G",
                            battery_percent=pct,
                            is_charging=is_charging,
                            status_text="Connected",
                        )
                    )
                    confirmed_no_batt = False
                    break
                if no_batt:
                    confirmed_no_batt = True

            if confirmed_no_batt and pid not in self._cached_endpoints:
                # Confirmed wired device without battery (e.g., G512 wired keyboard)
                self._no_battery_pids.add(pid)
                self._last_discovery_mono.pop(pid, None)

        return results
