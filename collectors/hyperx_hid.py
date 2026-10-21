import time
import logging
from typing import List, Tuple
import hid
from .models import DeviceBattery

HYPERX_VIDS = [0x03F0, 0x0951]

# Probe commands validated by HeadsetControl
# Probe 1 (Cloud II Wireless / Cloud Alpha Wireless 300h)
HYPERX_PROBE_ALPHA = bytes([0x21, 0xBB, 0x0B, 0x00, 0x00, 0x00, 0x00, 0x00])
# Probe 2 (Cloud Flight / Flight S)
HYPERX_PROBE_FLIGHT = bytes([0x21, 0xFF, 0x05, 0x00, 0x00, 0x00, 0x00, 0x00])

class HyperXHIDCollector:
    def _probe_headset(self, path: bytes) -> Tuple[int, bool]:
        dev = hid.device()
        try:
            dev.open_path(path)
            dev.set_nonblocking(1)

            for probe_payload in (HYPERX_PROBE_ALPHA, HYPERX_PROBE_FLIGHT):
                try:
                    dev.write(probe_payload)
                    time.sleep(0.025)
                    resp = dev.read(32)
                    if resp and len(resp) >= 5 and resp[0] == 0x21:
                        # Cloud II / Alpha payload: resp[3] is battery, resp[4] is charging status
                        pct = resp[3]
                        chg_state = resp[4]
                        if 1 <= pct <= 100:
                            is_charging = (chg_state == 0x01)
                            return pct, is_charging
                        # Some models use resp[4] for battery and resp[3] for status
                        pct_alt = resp[4]
                        if 1 <= pct_alt <= 100 and resp[3] in (0x00, 0x01, 0x02):
                            is_charging = (resp[3] == 0x01)
                            return pct_alt, is_charging
                except Exception:
                    continue

        except Exception as e:
            logging.debug(f"HyperX probe error: {e}")
        finally:
            try:
                dev.close()
            except Exception:
                pass
        return -1, False

    def collect(self) -> List[DeviceBattery]:
        results: List[DeviceBattery] = []
        seen_pids = set()

        for vid in HYPERX_VIDS:
            try:
                devices = hid.enumerate(vid, 0)
            except Exception:
                continue

            for d in devices:
                pid = d.get("product_id", 0)
                path = d.get("path")
                prod_name = d.get("product_string") or f"HyperX Device (0x{pid:04X})"

                # Focus on wireless headsets
                if "wireless" not in prod_name.lower() and "cloud" not in prod_name.lower() and "hyperx" not in prod_name.lower():
                    continue

                if pid in seen_pids:
                    continue

                percent, is_charging = self._probe_headset(path)
                if 1 <= percent <= 100:
                    seen_pids.add(pid)
                    results.append(
                        DeviceBattery(
                            id=f"hyperx_{vid:04x}_{pid:04x}",
                            name=prod_name,
                            protocol="HyperX 2.4G",
                            battery_percent=percent,
                            is_charging=is_charging,
                            status_text="Connected"
                        )
                    )

        return results
