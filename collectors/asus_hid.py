import time
import logging
from typing import List, Tuple
import hid
from .models import DeviceBattery

ASUS_VID = 0x0B05

# ASUS ROG Wireless battery query probe
ASUS_BATTERY_PROBE = bytes([0x51, 0x12, 0x00, 0x00] + [0x00] * 60)

class ASUSHIDCollector:
    def _probe_device(self, path: bytes) -> Tuple[int, bool]:
        dev = hid.device()
        try:
            dev.open_path(path)
            dev.set_nonblocking(1)

            try:
                dev.send_feature_report(ASUS_BATTERY_PROBE)
                time.sleep(0.025)
                resp = dev.get_feature_report(0x51, 64)
                if resp and len(resp) >= 6:
                    pct = resp[4]
                    if 1 <= pct <= 100:
                        is_charging = (resp[5] == 0x01 if len(resp) > 5 else False)
                        return pct, is_charging
                    pct2 = resp[5]
                    if 1 <= pct2 <= 100:
                        is_charging = (resp[6] == 0x01 if len(resp) > 6 else False)
                        return pct2, is_charging
            except Exception:
                pass

        except Exception as e:
            logging.debug(f"ASUS probe error: {e}")
        finally:
            try:
                dev.close()
            except Exception:
                pass
        return -1, False

    def collect(self) -> List[DeviceBattery]:
        results: List[DeviceBattery] = []
        try:
            devices = hid.enumerate(ASUS_VID, 0)
        except Exception:
            return results

        seen_pids = set()
        for d in devices:
            usage_page = d.get("usage_page", 0)
            pid = d.get("product_id", 0)
            path = d.get("path")

            if usage_page < 0xFF00 or pid in seen_pids:
                continue

            percent, is_charging = self._probe_device(path)
            prod_name = d.get("product_string") or f"ASUS ROG Device (0x{pid:04X})"

            if 1 <= percent <= 100:
                seen_pids.add(pid)
                results.append(
                    DeviceBattery(
                        id=f"asus_{pid:04x}",
                        name=prod_name,
                        protocol="ASUS ROG 2.4G",
                        battery_percent=percent,
                        is_charging=is_charging,
                        status_text="Connected"
                    )
                )

        return results
