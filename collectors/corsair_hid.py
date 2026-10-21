import time
import logging
from typing import List, Tuple
import hid
from .models import DeviceBattery

CORSAIR_VID = 0x1B1C

# Corsair Slipstream / Wireless battery query probe
CORSAIR_BATTERY_PROBE = bytes([0x08, 0x02, 0x00, 0x00] + [0x00] * 60)

class CorsairHIDCollector:
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

        seen_pids = set()
        for d in devices:
            usage_page = d.get("usage_page", 0)
            pid = d.get("product_id", 0)
            path = d.get("path")

            if usage_page < 0xFF00 or pid in seen_pids:
                continue

            percent, is_charging = self._probe_device(path)
            prod_name = d.get("product_string") or f"Corsair Device (0x{pid:04X})"

            if 1 <= percent <= 100:
                seen_pids.add(pid)
                results.append(
                    DeviceBattery(
                        id=f"corsair_{pid:04x}",
                        name=prod_name,
                        protocol="Corsair 2.4G",
                        battery_percent=percent,
                        is_charging=is_charging,
                        status_text="Connected"
                    )
                )

        return results
