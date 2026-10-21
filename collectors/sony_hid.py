import time
import logging
from typing import List, Tuple
import hid
from .models import DeviceBattery

SONY_VID = 0x054C

SONY_PIDS = {
    0x0CE6: "DualSense Wireless Controller",
    0x0DF2: "DualSense Edge Wireless Controller",
    0x05C4: "DualShock 4 Wireless Controller",
    0x09CC: "DualShock 4 Wireless Controller",
    0x0BA0: "DualShock 4 USB Wireless Adapter",
}

class SonyHIDCollector:
    def _read_controller_state(self, path: bytes, pid: int) -> Tuple[int, bool]:
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

        seen_pids = set()
        for d in devices:
            pid = d.get("product_id", 0)
            if pid not in SONY_PIDS or pid in seen_pids:
                continue

            path = d.get("path")
            percent, is_charging = self._read_controller_state(path, pid)

            if 0 <= percent <= 100:
                seen_pids.add(pid)
                prod_name = SONY_PIDS.get(pid, d.get("product_string") or "PlayStation Controller")
                results.append(
                    DeviceBattery(
                        id=f"sony_{pid:04x}",
                        name=prod_name,
                        protocol="PlayStation HID",
                        battery_percent=percent,
                        is_charging=is_charging,
                        status_text="Connected"
                    )
                )

        return results
