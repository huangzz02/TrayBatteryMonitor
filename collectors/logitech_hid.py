import time
import logging
from typing import List, Tuple
import hid
from .models import DeviceBattery

LOGITECH_VID = 0x046D

class LogitechHIDCollector:
    def _probe_hidpp20(self, dev, dev_idx: int) -> Tuple[int, bool]:
        """
        Query Logitech HID++ 2.0 Feature 0x1000 (Battery) or 0x1004 (Unified Battery).
        First queries Root Feature (0x0000) to find the FeatureIndex.
        If the device has no battery (e.g., wired G512 keyboard), FeatureIndex will be 0x00.
        """
        for feature_id_hi, feature_id_lo in ((0x10, 0x04), (0x10, 0x00)):
            try:
                # Root Feature (0x0000) function 0x00: IRoot.GetFeature(feature_id)
                get_feat_cmd = bytes([0x11, dev_idx, 0x00, 0x00, feature_id_hi, feature_id_lo] + [0x00] * 14)
                dev.write(get_feat_cmd)
                time.sleep(0.02)
                resp = dev.read(32)

                # resp: [0x11, dev_idx, feature_index=0x00, func=0x00, feature_idx_found, type, ...]
                if not resp or len(resp) < 6 or resp[0] != 0x11 or resp[2] == 0xFF:
                    continue

                feat_idx = resp[4]
                if feat_idx == 0x00:
                    # Feature not supported by device
                    continue

                # Feature found! Query battery level using this feat_idx, function 0x00 (getBatteryLevel/Status)
                query_cmd = bytes([0x11, dev_idx, feat_idx, 0x00] + [0x00] * 16)
                dev.write(query_cmd)
                time.sleep(0.02)
                batt_resp = dev.read(32)

                if batt_resp and len(batt_resp) >= 6 and batt_resp[0] == 0x11 and batt_resp[2] == feat_idx:
                    # For 0x1004 Unified Battery: byte 4 is battery percentage, byte 5 is status
                    pct = batt_resp[4]
                    chg_status = batt_resp[5] if len(batt_resp) > 5 else 0
                    is_charging = (chg_status in (0x01, 0x02, 0x03))
                    if 1 <= pct <= 100:
                        return pct, is_charging

                    # For 0x1000 Battery: byte 4 is level or byte 5 is level
                    pct2 = batt_resp[5] if len(batt_resp) > 5 else 0
                    if 1 <= pct2 <= 100:
                        is_charging = (batt_resp[4] in (0x01, 0x02, 0x03))
                        return pct2, is_charging
            except Exception:
                continue

        return -1, False

    def _probe_hidpp10(self, dev, dev_idx: int) -> Tuple[int, bool]:
        """
        Query Logitech HID++ 1.0 Battery status (for G304, G305, etc.)
        """
        try:
            req = bytes([0x10, dev_idx, 0x81, 0x0D, 0x00, 0x00, 0x00])
            dev.write(req)
            time.sleep(0.02)
            resp = dev.read(32)
            # Response: 0x10, dev_idx, 0x81, 0x0D, level, status, ... (0x8F is error)
            if resp and len(resp) >= 6 and resp[0] == 0x10 and resp[2] == 0x81 and resp[3] == 0x0D:
                level = resp[4]
                status = resp[5]
                is_charging = (status in (0x02, 0x03, 0x04))
                if 1 <= level <= 100:
                    return level, is_charging
        except Exception:
            pass
        return -1, False

    def _probe_interface(self, path: bytes) -> Tuple[int, bool]:
        dev = hid.device()
        try:
            dev.open_path(path)
            dev.set_nonblocking(1)

            # Check wireless device indexes: 0x01 (first paired wireless device) or 0xFF (direct device)
            for dev_idx in (0x01, 0xFF):
                pct, is_charging = self._probe_hidpp20(dev, dev_idx)
                if 1 <= pct <= 100:
                    return pct, is_charging

                pct10, is_charging10 = self._probe_hidpp10(dev, dev_idx)
                if 1 <= pct10 <= 100:
                    return pct10, is_charging10

        except Exception as e:
            logging.debug(f"Logitech probe error: {e}")
        finally:
            try:
                dev.close()
            except Exception:
                pass
        return -1, False

    def collect(self) -> List[DeviceBattery]:
        results: List[DeviceBattery] = []
        try:
            devices = hid.enumerate(LOGITECH_VID, 0)
        except Exception:
            return results

        seen_pids = set()
        for d in devices:
            usage_page = d.get("usage_page", 0)
            pid = d.get("product_id", 0)
            path = d.get("path")
            prod_name = d.get("product_string") or f"Logitech Device (0x{pid:04X})"

            # Skip explicitly wired or non-HID++ endpoints
            if usage_page not in (0xFF00, 0xFF43):
                continue

            if pid in seen_pids:
                continue

            percent, is_charging = self._probe_interface(path)
            if 1 <= percent <= 100:
                seen_pids.add(pid)
                results.append(
                    DeviceBattery(
                        id=f"logitech_{pid:04x}",
                        name=prod_name,
                        protocol="Logitech 2.4G",
                        battery_percent=percent,
                        is_charging=is_charging,
                        status_text="Connected"
                    )
                )

        return results
