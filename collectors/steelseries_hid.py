import time
import logging
from typing import List, Tuple
import hid
from .models import DeviceBattery

STEELSERIES_VID = 0x1038

# Probes from rivalcfg and HeadsetControl
# Mouse battery query: [0x00, 0x92] + 62 zeros
STEELSERIES_MOUSE_PROBE = bytes([0x00, 0x92] + [0x00] * 62)
# Headset battery query: [0x00, 0xB0] + 62 zeros
STEELSERIES_HEADSET_PROBE = bytes([0x00, 0xB0] + [0x00] * 62)

class SteelSeriesHIDCollector:
    def _probe_device(self, path: bytes) -> Tuple[int, bool]:
        dev = hid.device()
        try:
            dev.open_path(path)
            dev.set_nonblocking(1)

            for probe_payload in (STEELSERIES_MOUSE_PROBE, STEELSERIES_HEADSET_PROBE):
                try:
                    dev.send_feature_report(probe_payload)
                    time.sleep(0.025)
                    resp = dev.get_feature_report(0x00, 64)
                    if resp and len(resp) >= 4:
                        # Most SteelSeries devices report battery in byte 2 or byte 3
                        pct = resp[2]
                        if 1 <= pct <= 100:
                            is_charging = (resp[3] == 0x01 if len(resp) > 3 else False)
                            return pct, is_charging
                        pct_alt = resp[3]
                        if 1 <= pct_alt <= 100:
                            is_charging = (resp[4] == 0x01 if len(resp) > 4 else False)
                            return pct_alt, is_charging
                except Exception:
                    continue

        except Exception as e:
            logging.debug(f"SteelSeries probe error: {e}")
        finally:
            try:
                dev.close()
            except Exception:
                pass
        return -1, False

    def collect(self) -> List[DeviceBattery]:
        results: List[DeviceBattery] = []
        try:
            devices = hid.enumerate(STEELSERIES_VID, 0)
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
            prod_name = d.get("product_string") or f"SteelSeries Device (0x{pid:04X})"

            if 1 <= percent <= 100:
                seen_pids.add(pid)
                results.append(
                    DeviceBattery(
                        id=f"steelseries_{pid:04x}",
                        name=prod_name,
                        protocol="SteelSeries 2.4G",
                        battery_percent=percent,
                        is_charging=is_charging,
                        status_text="Connected"
                    )
                )

        return results
