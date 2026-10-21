import time
import logging
from typing import List
import hid
from .models import DeviceBattery

ATK_VIDS = [0x3554, 0x373B, 0x248A]

ATK_PROBES = [
    (0x08, [0x08, 0x04, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x49], 6, 7),
    (0x05, [0x05, 0x15] + [0x00] * 30, 8, 9),
    (0x06, [0x06, 0x01] + [0x00] * 30, 8, 9),
]

class ATKHIDCollector:
    def _probe_device(self, path: bytes) -> tuple[int, bool]:
        dev = hid.device()
        try:
            dev.open_path(path)
            dev.set_nonblocking(1)

            for rep_id, payload, batt_idx, chg_idx in ATK_PROBES:
                try:
                    dev.write(bytes(payload))
                    time.sleep(0.03)
                    resp = dev.read(64)
                    if resp and len(resp) > batt_idx and 1 <= resp[batt_idx] <= 100:
                        return resp[batt_idx], (resp[chg_idx] == 1 if len(resp) > chg_idx else False)

                    dev.send_feature_report(bytes(payload))
                    time.sleep(0.03)
                    f_resp = dev.get_feature_report(rep_id, 33)
                    if f_resp and len(f_resp) > batt_idx and 1 <= f_resp[batt_idx] <= 100:
                        return f_resp[batt_idx], (f_resp[chg_idx] == 1 if len(f_resp) > chg_idx else False)
                except Exception:
                    continue
        except Exception as e:
            logging.debug(f"Failed to access ATK HID: {e}")
        finally:
            try:
                dev.close()
            except Exception:
                pass
        return -1, False

    def collect(self) -> List[DeviceBattery]:
        results: List[DeviceBattery] = []
        seen_pids = set()

        for vid in ATK_VIDS:
            try:
                devices = hid.enumerate(vid, 0)
            except Exception:
                continue

            for d in devices:
                usage_page = d.get("usage_page", 0)
                pid = d.get("product_id", 0)

                if usage_page < 0xFF00 or pid in seen_pids:
                    continue

                percent, is_charging = self._probe_device(d["path"])
                name = d.get("product_string") or f"ATK Mouse (0x{pid:04X})"

                if 1 <= percent <= 100:
                    seen_pids.add(pid)
                    results.append(
                        DeviceBattery(
                            id=f"atk_{vid:04x}_{pid:04x}",
                            name=name,
                            protocol="ATK 2.4G",
                            battery_percent=percent,
                            is_charging=is_charging,
                            status_text="Connected"
                        )
                    )
                else:
                    results.append(
                        DeviceBattery(
                            id=f"atk_{vid:04x}_{pid:04x}",
                            name=name,
                            protocol="ATK 2.4G",
                            battery_percent=None,
                            status_text="Sleeping / Inactive"
                        )
                    )
                    seen_pids.add(pid)

        return results
