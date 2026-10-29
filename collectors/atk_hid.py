import time
import logging
from typing import Callable, Dict, List, Optional, Tuple
import hid
from .models import DeviceBattery
from .power_guard import (
    WirelessMouseState,
    decide_mouse_rf_action,
    get_refresh_generation,
)

# Vendor IDs for Compx / Nordic / NearLink MCUs (ATK, VXE, MADLIONS, VGN, Zaopin, Scyrox, Dareu, G-Wolves)
ATK_VIDS = (
    0x373B,  # ATK, MADLIONS, VXE (HiSilicon NearLink, Nordic 54L15 & Nordic 52840)
    0x3710,  # ATK WebHID variant
    0x3554,  # VXE, VGN, Zaopin, Scyrox (Compx / Nordic 52840)
    0x248A,  # Maxxter / Compx
    0x260D,  # Dareu
    0x33E4,  # G-Wolves
)

# Known PID -> Display Name table for receivers & wired endpoints that report generic internal strings
KNOWN_PID_NAMES: Dict[int, str] = {
    0x1216: "ATK A9 SE Wireless Mouse",
    0x1222: "ATK A9 SE Wireless Mouse",
    0x1223: "ATK A9 Wireless Mouse",
    0x1224: "ATK A9 Plus Wireless Mouse",
    0x1211: "ATK A9 Ultimate",
    0x11D9: "ATK A9 Ultimate",
    0x11B6: "ATK A9 Ultimate",
    0x11DC: "ATK NearLink 8K Wireless Mouse",
    0x1031: "ATK F1 Ultimate",
    0x102E: "ATK F1 Ultimate",
    0x104D: "VXE MAD R Wireless Mouse",
    0x103F: "VXE MAD R Wireless Mouse",
    0x1040: "VXE MAD R Major Plus",
    0x104C: "VXE MAD R Major Plus",
    0x1155: "ATK Zero Wireless Mouse",
    0x124F: "ATK Zero Wireless Mouse",
    0x1154: "ATK Zero Wireless Mouse",
    0xF58A: "VXE R1 Pro Max",
    0xF58C: "VXE R1 Pro Max",
    0xF58E: "VXE R1 SE+",
    0xF58F: "VXE R1 SE+",
    0xF503: "VGN F1 Pro",
    0xF502: "VGN F1 Pro",
    0xFB3E: "VGN F2 Pro Max",
    0xFB3D: "VGN F2 Pro Max",
    0xF524: "Zaopin Z2 Mini",
    0xF526: "Zaopin Z2 Mini",
    0xF5F7: "Scyrox V8",
    0xF5F6: "Scyrox V8",
}

ATK_FALLBACK_PROBES = (
    (0x05, bytes([0x05, 0x15] + [0x00] * 30), 8, 9),
    (0x06, bytes([0x06, 0x01] + [0x00] * 30), 8, 9),
)

# BITMOUSE GetDeviceType (0x1A) values that indicate direct USB-C wired mode
_BITMOUSE_WIRED_TYPES = (2, 3, 6, 7)


def _clean_device_name(prod: str, mfr: str, pid: int) -> str:
    """Format a clean, recognizable device name without raw internal dongle strings."""
    if pid in KNOWN_PID_NAMES:
        return KNOWN_PID_NAMES[pid]

    if not prod:
        return f"ATK Mouse (0x{pid:04X})"

    lower_prod = prod.lower()
    lower_mfr = mfr.lower()

    if "dongle" in lower_prod or "receiver" in lower_prod:
        if "a9" in lower_prod:
            return "ATK A9 Wireless Mouse"
        if "mad" in lower_prod or "mad" in lower_mfr:
            return "VXE MAD R Wireless Mouse"
        if "f1" in lower_prod:
            return "ATK F1 Wireless Mouse"
        if "x1" in lower_prod:
            return "ATK X1 Wireless Mouse"
        if "zero" in lower_prod:
            return "ATK Zero Wireless Mouse"
        if "r1" in lower_prod:
            return "VXE R1 Wireless Mouse"
        if "vgn" in lower_prod or "vgn" in lower_mfr:
            return "VGN Wireless Mouse"
        return "ATK Wireless Mouse"

    return prod


class ATKHIDCollector:
    """
    Sleep-safe collector for ATK, VXE, MADLIONS, and VGN wireless and wired mice.
    Supports:
      - BITMOUSE Protocol (Usage Page 0xFF05: HiSilicon NearLink & Nordic 54L15, 64-byte 0x72)
        Uses local dongle check (0x81, target=0), non-waking sleep check (0x23, target=1),
        firmware sleep-timer sync (0x09), and stationary RF silence so mice enter hardware sleep.
      - CompX Protocol (Usage Page 0xFF02 / 0xFF04: Nordic 52840, 17-byte 0x08)
        Uses GetWirelessMouseOnline (0x03) before GetBatteryLevel (0x04) + stationary RF silence.
      - Legacy Compx feature/output report protocols.
    """

    def __init__(self):
        # Cache verified vendor endpoint (path, usage_page, proto_kind) per (vid, pid)
        self._cached_endpoints: Dict[Tuple[int, int], Tuple[bytes, int, str]] = {}
        self._states: Dict[Tuple[int, int], WirelessMouseState] = {}
        self._sleep_cfg_loaded: Dict[Tuple[int, int], bool] = {}

    @staticmethod
    def _build_bitmouse_cmd(cmd_id: int, param_len: int, cmd_len: int, target: int) -> bytes:
        buf = bytearray(64)
        buf[0] = 0x08
        buf[2] = 0x72
        buf[3] = param_len
        buf[4] = 0x00
        buf[5] = target
        buf[6] = cmd_id
        buf[7] = cmd_len
        buf[1] = sum(buf[2:]) & 0xFF
        return bytes(buf)

    @staticmethod
    def _build_compx_cmd(cmd_id: int) -> bytes:
        req = bytearray(17)
        req[0] = 0x08
        req[1] = cmd_id
        req[16] = (0x55 - (0x08 + cmd_id)) & 0xFF
        return bytes(req)

    @staticmethod
    def _transact(
        dev,
        payload: bytes,
        match_fn: Callable[[List[int]], bool],
        timeout: float = 0.12,
    ) -> Optional[List[int]]:
        """Drain stale reports, write request, and wait up to `timeout` seconds for a matching reply."""
        while dev.read(64):
            pass
        dev.write(payload)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            res = dev.read(64)
            if res:
                if match_fn(res):
                    return res
            else:
                time.sleep(0.005)
        return None

    def _bitmouse_cmd(self, dev, cmd_id: int, param_len: int, cmd_len: int, target: int, timeout: float = 0.12) -> Optional[List[int]]:
        payload = self._build_bitmouse_cmd(cmd_id, param_len, cmd_len, target)
        return self._transact(
            dev,
            payload,
            lambda r: len(r) >= 8 and r[0] == 0x08 and r[1] == 0x72 and r[5] == cmd_id,
            timeout=timeout,
        )

    def _read_bitmouse_sleep_timeout(self, dev, key: Tuple[int, int], state: WirelessMouseState) -> None:
        """Read configured sensorSleepTime (seconds) from firmware via GetCurrentMouseConfig (0x09)."""
        try:
            cfg = self._bitmouse_cmd(dev, 0x09, 20, 18, target=1, timeout=0.12)
            if cfg and len(cfg) >= 18 and cfg[2] == 0:
                sleep_sec = cfg[16] | (cfg[17] << 8)
                if 15 <= sleep_sec <= 3600:
                    state.sleep_timeout_sec = float(sleep_sec)
                    self._sleep_cfg_loaded[key] = True
        except Exception:
            pass

    def _poll_bitmouse_endpoint(
        self,
        dev,
        key: Tuple[int, int],
        state: WirelessMouseState,
        action: str,
    ) -> bool:
        """
        Poll a BITMOUSE (0xFF05) device while respecting wireless sleep protection.
        Returns True if the endpoint speaks the BITMOUSE 0x72 protocol.
        """
        now = time.monotonic()
        cur_gen = get_refresh_generation()

        # 1. Local USB query (target=0, ZERO wireless RF packets!) to distinguish wired vs dongle
        is_dongle = True
        type_res = self._bitmouse_cmd(dev, 0x1A, 2, 1, target=0, timeout=0.08)
        protocol_matched = type_res is not None

        if type_res and type_res[2] == 0:
            is_dongle = type_res[7] not in _BITMOUSE_WIRED_TYPES

        if not is_dongle:
            # Direct USB-C wired mode: target=0 queries mouse over USB cable directly (never sleeps)
            state.is_wired = True
            state.is_sleeping = False
            if state.percent is not None and state.last_refresh_gen == cur_gen and (now - state.last_rf_poll_mono) < 15.0:
                return True

            batt_res = self._bitmouse_cmd(dev, 0x07, 2, 1, target=0, timeout=0.10)
            if batt_res and batt_res[2] == 0 and 1 <= batt_res[7] <= 100:
                state.percent = batt_res[7]
                is_chg = True
                chg_res = self._bitmouse_cmd(dev, 0x0F, 2, 1, target=0, timeout=0.08)
                if chg_res and chg_res[2] == 0:
                    is_chg = chg_res[7] > 0
                state.is_charging = is_chg
                state.last_rf_poll_mono = now
                state.last_refresh_gen = cur_gen
                return True
            return protocol_matched

        # 2. Wireless Dongle mode: check local dongle link status (0x81, target=0, ZERO RF packets!)
        state.is_wired = False
        dongle_res = self._bitmouse_cmd(dev, 0x81, 3, 1, target=0, timeout=0.08)
        if dongle_res is not None:
            protocol_matched = True
            if dongle_res[2] != 0 or dongle_res[7] == 0:
                # Mouse is powered OFF, out of range, or switched to wired/BT mode
                if not state.is_sleeping:
                    state.sleep_confirmed_mono = now
                state.is_sleeping = True
                state.is_charging = False
                state.last_refresh_gen = cur_gen
                return True

        if not protocol_matched and action in ("skip_sleeping", "skip_use_cache"):
            return False

        # 3. Respect RF silence decisions so stationary mice count down to sleep undisturbed
        if action in ("skip_sleeping", "skip_use_cache"):
            return True

        if action == "check_sleep_only":
            # Single non-waking online status check (0x23) after hardware sleep timeout has elapsed
            online_res = self._bitmouse_cmd(dev, 0x23, 3, 1, target=1, timeout=0.10)
            if not online_res or online_res[2] != 0 or online_res[7] != 0:
                state.is_sleeping = True
                state.sleep_confirmed_mono = now
                state.is_charging = False
            else:
                # Still awake (e.g. placed on wall charger / dock or sleepTime increased in ATK HUB)
                self._read_bitmouse_sleep_timeout(dev, key, state)
                batt_res = self._bitmouse_cmd(dev, 0x07, 2, 1, target=1, timeout=0.12)
                if batt_res and batt_res[2] == 0 and 1 <= batt_res[7] <= 100:
                    state.percent = batt_res[7]
                chg_res = self._bitmouse_cmd(dev, 0x0F, 2, 1, target=1, timeout=0.10)
                if chg_res and chg_res[2] == 0:
                    state.is_charging = bool(chg_res[7] > 0)
                state.last_rf_poll_mono = now
            return True

        # 4. action == "poll_full": check GetMouseOnlineStatus (0x23) BEFORE battery commands
        state.last_wake_check_mono = now
        state.last_refresh_gen = cur_gen

        online_res = self._bitmouse_cmd(dev, 0x23, 3, 1, target=1, timeout=0.10)
        if online_res is None:
            return protocol_matched

        protocol_matched = True
        # online_res[7] == 0 -> Awake / Online; online_res[7] != 0 -> Sleeping!
        if online_res[2] != 0 or online_res[7] != 0:
            if not state.is_sleeping:
                state.sleep_confirmed_mono = now
            state.is_sleeping = True
            state.is_charging = False
            return True

        # Mouse is confirmed Awake! Read firmware sleep timer if needed, then battery & charging
        was_sleeping = state.is_sleeping
        state.is_sleeping = False
        if was_sleeping or not self._sleep_cfg_loaded.get(key, False):
            self._read_bitmouse_sleep_timeout(dev, key, state)

        batt_res = self._bitmouse_cmd(dev, 0x07, 2, 1, target=1, timeout=0.12)
        if batt_res and batt_res[2] == 0 and 1 <= batt_res[7] <= 100:
            state.percent = batt_res[7]
            chg_res = self._bitmouse_cmd(dev, 0x0F, 2, 1, target=1, timeout=0.10)
            if chg_res and chg_res[2] == 0:
                state.is_charging = bool(chg_res[7] > 0)
            else:
                state.is_charging = False
            state.last_rf_poll_mono = now
        return True

    def _poll_compx_endpoint(
        self,
        dev,
        state: WirelessMouseState,
        action: str,
        is_dongle_hint: bool,
    ) -> bool:
        """
        Poll a CompX / Nordic 52840 (0xFF02 / 0xFF04) device.
        Checks GetWirelessMouseOnline (0x03) before GetBatteryLevel (0x04) so sleeping mice
        are accurately detected without unnecessary RF battery reads.
        """
        now = time.monotonic()
        cur_gen = get_refresh_generation()

        if action in ("skip_sleeping", "skip_use_cache") and state.last_rf_poll_mono > 0:
            return True

        if action == "check_sleep_only":
            online_res = self._transact(
                dev,
                self._build_compx_cmd(0x03),
                lambda r: len(r) >= 7 and r[0] == 0x08 and r[1] == 0x03,
                timeout=0.08,
            )
            if online_res and online_res[6] != 1:
                state.is_sleeping = True
                state.sleep_confirmed_mono = now
                state.is_charging = False
            else:
                state.last_rf_poll_mono = now
            return True

        state.last_wake_check_mono = now
        state.last_refresh_gen = cur_gen

        # Check CompX GetWirelessMouseOnline (0x03): byte 6 == 1 means awake/online
        online_res = self._transact(
            dev,
            self._build_compx_cmd(0x03),
            lambda r: len(r) >= 7 and r[0] == 0x08 and r[1] == 0x03,
            timeout=0.08,
        )
        protocol_matched = online_res is not None
        if online_res is not None and online_res[6] != 1 and is_dongle_hint:
            if not state.is_sleeping:
                state.sleep_confirmed_mono = now
            state.is_sleeping = True
            state.is_charging = False
            return True

        batt_res = self._transact(
            dev,
            self._build_compx_cmd(0x04),
            lambda r: len(r) >= 8 and r[0] == 0x08 and r[1] == 0x04,
            timeout=0.10,
        )
        if batt_res and 1 <= batt_res[6] <= 100:
            state.percent = batt_res[6]
            state.is_charging = bool(batt_res[7])
            state.is_wired = not is_dongle_hint and bool(batt_res[7])
            state.is_sleeping = False
            state.last_rf_poll_mono = now
            return True

        if protocol_matched:
            if not state.is_sleeping:
                state.sleep_confirmed_mono = now
            state.is_sleeping = True
            state.is_charging = False
            return True

        return False

    def _poll_legacy_endpoint(self, dev, state: WirelessMouseState, action: str) -> bool:
        now = time.monotonic()
        cur_gen = get_refresh_generation()
        if action in ("skip_sleeping", "skip_use_cache") and state.last_rf_poll_mono > 0:
            return True

        state.last_wake_check_mono = now
        state.last_refresh_gen = cur_gen

        for rep_id, payload, batt_idx, chg_idx in ATK_FALLBACK_PROBES:
            try:
                res = self._transact(
                    dev,
                    payload,
                    lambda r, bi=batt_idx: len(r) > bi and 1 <= r[bi] <= 100,
                    timeout=0.05,
                )
                if res:
                    state.percent = res[batt_idx]
                    state.is_charging = bool(res[chg_idx]) if len(res) > chg_idx else False
                    state.is_sleeping = False
                    state.last_rf_poll_mono = now
                    return True

                dev.send_feature_report(payload)
                time.sleep(0.025)
                f_resp = dev.get_feature_report(rep_id, len(payload) + 1)
                if f_resp and len(f_resp) > batt_idx and 1 <= f_resp[batt_idx] <= 100:
                    state.percent = f_resp[batt_idx]
                    state.is_charging = bool(f_resp[chg_idx]) if len(f_resp) > chg_idx else False
                    state.is_sleeping = False
                    state.last_rf_poll_mono = now
                    return True
            except Exception:
                continue

        if state.last_rf_poll_mono > 0:
            if not state.is_sleeping:
                state.sleep_confirmed_mono = now
            state.is_sleeping = True
            state.is_charging = False
            return True
        return False

    def _access_endpoint(
        self,
        path: bytes,
        usage_page: int,
        proto_hint: Optional[str],
        key: Tuple[int, int],
        state: WirelessMouseState,
        action: str,
        is_dongle_hint: bool,
    ) -> Optional[str]:
        """Open HID path and run the appropriate protocol handler. Returns matched proto_kind or None."""
        dev = hid.device()
        try:
            dev.open_path(path)
            dev.set_nonblocking(1)

            if proto_hint == "bitmouse" or (proto_hint is None and usage_page in (0xFF05, 0)):
                if self._poll_bitmouse_endpoint(dev, key, state, action):
                    return "bitmouse"
                if proto_hint == "bitmouse" or usage_page == 0xFF05:
                    return None

            if proto_hint == "compx" or (proto_hint is None and usage_page in (0xFF02, 0xFF04, 0)):
                if self._poll_compx_endpoint(dev, state, action, is_dongle_hint):
                    return "compx"
                if proto_hint == "compx" or usage_page in (0xFF02, 0xFF04):
                    return None

            if proto_hint == "legacy" or (proto_hint is None and usage_page not in (0xFF05, 0xFF02, 0xFF04)):
                if self._poll_legacy_endpoint(dev, state, action):
                    return "legacy"

        except Exception as e:
            logging.debug(f"Failed to access ATK HID path {path}: {e}")
        finally:
            try:
                dev.close()
            except Exception:
                pass
        return None

    def collect(self) -> List[DeviceBattery]:
        results: List[DeviceBattery] = []

        # Group all vendor endpoints (>= 0xFF00) by (vid, pid)
        devs_by_pid: Dict[Tuple[int, int], List[dict]] = {}
        for vid in ATK_VIDS:
            try:
                for d in hid.enumerate(vid, 0):
                    if d.get("usage_page", 0) >= 0xFF00:
                        key = (vid, d.get("product_id", 0))
                        devs_by_pid.setdefault(key, []).append(d)
            except Exception as e:
                logging.debug(f"Error enumerating ATK VID 0x{vid:04X}: {e}")

        # Clean up unplugged device state so re-plugging immediately triggers a fresh read
        active_keys = set(devs_by_pid.keys())
        for stale_key in list(self._states.keys()):
            if stale_key not in active_keys:
                self._states.pop(stale_key, None)
                self._cached_endpoints.pop(stale_key, None)
                self._sleep_cfg_loaded.pop(stale_key, None)

        for key, dev_list in devs_by_pid.items():
            vid, pid = key
            mfr0 = (dev_list[0].get("manufacturer_string") or "").strip()
            prod0 = (dev_list[0].get("product_string") or "").strip()
            is_dongle_hint = (
                "dongle" in prod0.lower()
                or "receiver" in prod0.lower()
                or any(d.get("usage_page") == 0xFF04 for d in dev_list)
            )

            state = self._states.get(key)
            if state is None:
                default_sleep = 60.0 if any(d.get("usage_page") == 0xFF05 for d in dev_list) else 120.0
                state = WirelessMouseState(
                    sleep_timeout_sec=default_sleep,
                    name=_clean_device_name(prod0, mfr0, pid),
                )
                self._states[key] = state

            action = decide_mouse_rf_action(state)
            matched = False

            # 1. Fast path: use cached verified endpoint
            cached = self._cached_endpoints.get(key)
            if cached is not None:
                c_path, c_up, c_proto = cached
                if any(d.get("path") == c_path for d in dev_list):
                    # During sleep / cache skip, avoid opening USB handle every second.
                    # For BITMOUSE, throttle local dongle status check (0x81) to once every 5 seconds.
                    now = time.monotonic()
                    if action in ("skip_sleeping", "skip_use_cache"):
                        if c_proto != "bitmouse" or (now - state.last_dongle_check_mono) < 5.0:
                            matched = True
                        else:
                            res_proto = self._access_endpoint(
                                c_path, c_up, c_proto, key, state, action, is_dongle_hint
                            )
                            if res_proto is not None:
                                state.last_dongle_check_mono = now
                                matched = True
                    else:
                        res_proto = self._access_endpoint(
                            c_path, c_up, c_proto, key, state, action, is_dongle_hint
                        )
                        if res_proto is not None:
                            state.last_dongle_check_mono = now
                            matched = True

            # 2. Discovery scan across vendor usage pages: prioritize 0xFF05 (BITMOUSE), then 0xFF04/0xFF02 (CompX)
            if not matched:
                def _up_prio(d: dict) -> int:
                    up = d.get("usage_page", 0)
                    if up == 0xFF05:
                        return 0
                    if up in (0xFF04, 0xFF02):
                        return 1
                    return 2

                dev_list.sort(key=_up_prio)
                for d in dev_list:
                    path = d.get("path")
                    up = d.get("usage_page", 0)
                    mfr = (d.get("manufacturer_string") or "").strip()
                    prod = (d.get("product_string") or "").strip()
                    dongle_flag = is_dongle_hint or "dongle" in prod.lower() or "receiver" in prod.lower() or up == 0xFF04

                    res_proto = self._access_endpoint(
                        path, up, None, key, state, "poll_full", dongle_flag
                    )
                    if res_proto is not None:
                        self._cached_endpoints[key] = (path, up, res_proto)
                        state.name = _clean_device_name(prod, mfr, pid)
                        matched = True
                        break

            proto_str = "ATK Wired" if state.is_wired else "ATK 2.4G"
            dev_id = f"atk_{vid:04x}_{pid:04x}"

            if not state.is_sleeping and state.percent is not None:
                results.append(
                    DeviceBattery(
                        id=dev_id,
                        name=state.name,
                        protocol=proto_str,
                        battery_percent=state.percent,
                        is_charging=state.is_charging,
                        status_text="Connected",
                    )
                )
            else:
                results.append(
                    DeviceBattery(
                        id=dev_id,
                        name=state.name,
                        protocol=proto_str,
                        battery_percent=None,
                        status_text="Sleeping / Inactive",
                    )
                )

        return results
