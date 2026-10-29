import time
import logging
from typing import Dict, List, Optional, Set, Tuple
import hid
from .models import DeviceBattery

LOGITECH_VID = 0x046D


class LogitechHIDCollector:
    def __init__(self):
        # Cache PIDs confirmed to be wired devices without battery features (e.g., G512 keyboard)
        self._no_battery_pids: Set[int] = set()
        # Cache working interface path per PID for fast subsequent polls
        self._cached_paths: Dict[int, bytes] = {}

    @staticmethod
    def _transact(dev, cmd: bytes, expected_rep_id: int, timeout: float = 0.05) -> Optional[List[int]]:
        while dev.read(64):
            pass
        dev.write(cmd)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            resp = dev.read(64)
            if resp:
                if resp[0] == expected_rep_id:
                    return resp
            else:
                time.sleep(0.004)
        return None

    def _probe_hidpp20(self, dev, dev_idx: int) -> Tuple[int, bool, bool]:
        """
        Query Logitech HID++ 2.0 Feature 0x1004 (Unified Battery) or 0x1000 (Battery Status).
        Returns: (percent, is_charging, confirmed_no_battery)
        """
        answered_root_without_battery = False

        for feature_id_hi, feature_id_lo in ((0x10, 0x04), (0x10, 0x00)):
            try:
                # Root Feature (0x0000) function 0x00: IRoot.GetFeature(feature_id)
                get_feat_cmd = bytes([0x11, dev_idx, 0x00, 0x00, feature_id_hi, feature_id_lo] + [0x00] * 14)
                resp = self._transact(dev, get_feat_cmd, 0x11, timeout=0.05)

                if not resp or len(resp) < 6 or resp[2] in (0x8F, 0xFF):
                    continue

                feat_idx = resp[4]
                if feat_idx == 0x00:
                    # Device actively answered Root Feature and confirmed this battery feature is absent
                    answered_root_without_battery = True
                    continue

                answered_root_without_battery = False
                # Feature found: query battery state (function 0x00 or 0x10 depending on feature)
                fn_code = 0x10 if feature_id_lo == 0x04 else 0x00
                for fn in (fn_code, 0x00):
                    query_cmd = bytes([0x11, dev_idx, feat_idx, fn] + [0x00] * 16)
                    batt_resp = self._transact(dev, query_cmd, 0x11, timeout=0.05)
                    if not batt_resp or len(batt_resp) < 6 or batt_resp[2] != feat_idx:
                        continue

                    pct = batt_resp[4]
                    chg_status = batt_resp[5] if len(batt_resp) > 5 else 0
                    if 1 <= pct <= 100:
                        is_charging = chg_status in (0x01, 0x02, 0x03)
                        return pct, is_charging, False

                    pct2 = batt_resp[5] if len(batt_resp) > 5 else 0
                    if 1 <= pct2 <= 100:
                        is_charging = batt_resp[4] in (0x01, 0x02, 0x03)
                        return pct2, is_charging, False
            except Exception:
                continue

        return -1, False, answered_root_without_battery

    def _probe_hidpp10(self, dev, dev_idx: int) -> Tuple[int, bool]:
        """Query Logitech HID++ 1.0 Battery status (for older Unifying / G304 / G305 receivers)."""
        try:
            req = bytes([0x10, dev_idx, 0x81, 0x0D, 0x00, 0x00, 0x00])
            resp = self._transact(dev, req, 0x10, timeout=0.04)
            if resp and len(resp) >= 6 and resp[2] == 0x81 and resp[3] == 0x0D:
                level = resp[4]
                status = resp[5]
                if 1 <= level <= 100:
                    is_charging = status in (0x02, 0x03, 0x04)
                    return level, is_charging
        except Exception:
            pass
        return -1, False

    def _probe_path(self, path: bytes) -> Tuple[int, bool, bool]:
        """Returns: (percent, is_charging, confirmed_wired_no_battery)"""
        dev = hid.device()
        confirmed_wired_no_battery = False
        try:
            dev.open_path(path)
            dev.set_nonblocking(1)

            # 0x01 = first paired wireless device on receiver; 0xFF = direct USB device
            pct, chg, _ = self._probe_hidpp20(dev, 0x01)
            if 1 <= pct <= 100:
                return pct, chg, False

            pct10, chg10 = self._probe_hidpp10(dev, 0x01)
            if 1 <= pct10 <= 100:
                return pct10, chg10, False

            pct_dir, chg_dir, no_batt_dir = self._probe_hidpp20(dev, 0xFF)
            if 1 <= pct_dir <= 100:
                return pct_dir, chg_dir, False

            pct10_dir, chg10_dir = self._probe_hidpp10(dev, 0xFF)
            if 1 <= pct10_dir <= 100:
                return pct10_dir, chg10_dir, False

            # Only mark confirmed_wired_no_battery if direct device (0xFF) explicitly answered
            # Root Feature 0x0000 with feat_idx == 0x00 for both battery features
            if no_batt_dir:
                confirmed_wired_no_battery = True

        except Exception as e:
            logging.debug(f"Logitech probe error: {e}")
        finally:
            try:
                dev.close()
            except Exception:
                pass

        return -1, False, confirmed_wired_no_battery

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

        for pid, ifaces in grouped.items():
            prod_name = ifaces[0].get("product_string") or f"Logitech Device (0x{pid:04X})"

            # Fast path: try cached path first
            cached_path = self._cached_paths.get(pid)
            if cached_path and any(d.get("path") == cached_path for d in ifaces):
                pct, is_charging, _ = self._probe_path(cached_path)
                if 1 <= pct <= 100:
                    results.append(
                        DeviceBattery(
                            id=f"logitech_{pid:04x}",
                            name=prod_name,
                            protocol="Logitech 2.4G",
                            battery_percent=pct,
                            is_charging=is_charging,
                            status_text="Connected",
                        )
                    )
                    continue

            # Prioritize 20-byte HID++ 2.0 long report collections (usage *02) first
            ifaces.sort(key=lambda d: 0 if (d.get("usage", 0) & 0xFF) == 0x02 else 1)

            confirmed_no_batt = False
            found = False
            for d in ifaces:
                path = d.get("path")
                if path == cached_path:
                    continue
                pct, is_charging, no_batt = self._probe_path(path)
                if 1 <= pct <= 100:
                    self._cached_paths[pid] = path
                    results.append(
                        DeviceBattery(
                            id=f"logitech_{pid:04x}",
                            name=prod_name,
                            protocol="Logitech 2.4G",
                            battery_percent=pct,
                            is_charging=is_charging,
                            status_text="Connected",
                        )
                    )
                    found = True
                    break
                if no_batt:
                    confirmed_no_batt = True

            if not found and confirmed_no_batt:
                # Confirmed wired device without battery (e.g., G512 wired keyboard)
                self._no_battery_pids.add(pid)

        return results
