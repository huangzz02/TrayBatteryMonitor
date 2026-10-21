import time
import logging
from typing import List, Dict, Tuple
import hid
from .models import DeviceBattery

RAZER_VID = 0x1532
TRANSACTION_IDS = [0x1F, 0x3F, 0xFF]

class RazerHIDCollector:
    def __init__(self):
        # Cache successful (path, tid) for each PID to ensure lightning-fast subsequent polls (~15ms)
        self._successful_path_tid: Dict[int, Tuple[bytes, int]] = {}

    @staticmethod
    def _create_razer_report(transaction_id: int, command_class: int, command_id: int, data_size: int) -> bytes:
        buf = bytearray(91)
        buf[0] = 0x00
        buf[1] = 0x00
        buf[2] = transaction_id
        buf[3] = 0x00
        buf[4] = 0x00
        buf[5] = 0x00
        buf[6] = data_size
        buf[7] = command_class
        buf[8] = command_id

        crc = 0
        for i in range(3, 89):
            crc ^= buf[i]
        buf[89] = crc
        buf[90] = 0x00
        return bytes(buf)

    def _query_interface(self, path: bytes, tid: int, cmd_id: int) -> int:
        dev = hid.device()
        try:
            dev.open_path(path)
            report = self._create_razer_report(tid, command_class=0x07, command_id=cmd_id, data_size=0x02)
            dev.send_feature_report(report)
            time.sleep(0.015)
            resp = dev.get_feature_report(0x00, 91)

            if resp and len(resp) >= 11:
                offset = 1 if len(resp) == 91 else 0
                status = resp[0 + offset]
                if status == 0x02:
                    return resp[9 + offset]
        except Exception:
            pass
        finally:
            try:
                dev.close()
            except Exception:
                pass
        return -1

    def collect(self) -> List[DeviceBattery]:
        results: List[DeviceBattery] = []
        try:
            all_devs = hid.enumerate(RAZER_VID, 0)
        except Exception as e:
            logging.error(f"Razer HID enumerate error: {e}")
            return results

        grouped_by_pid: Dict[int, list] = {}
        for d in all_devs:
            pid = d["product_id"]
            grouped_by_pid.setdefault(pid, []).append(d)

        for pid, interfaces in grouped_by_pid.items():
            product_name = interfaces[0].get("product_string") or f"Razer Device (0x{pid:04X})"
            battery_raw = -1
            charging_raw = 0

            # 1. Fast path: try cached (path, tid) first
            if pid in self._successful_path_tid:
                cached_path, cached_tid = self._successful_path_tid[pid]
                val = self._query_interface(cached_path, cached_tid, cmd_id=0x80)
                if val > 0:
                    battery_raw = val
                    charging_raw = self._query_interface(cached_path, cached_tid, cmd_id=0x84)

            # 2. Probe interfaces if cache missed or device woke up on different interface
            if battery_raw <= 0:
                for iface in interfaces:
                    path = iface["path"]
                    for tid in TRANSACTION_IDS:
                        val = self._query_interface(path, tid, cmd_id=0x80)
                        if val > 0:
                            battery_raw = val
                            charging_raw = self._query_interface(path, tid, cmd_id=0x84)
                            self._successful_path_tid[pid] = (path, tid)
                            break
                    if battery_raw > 0:
                        break

            if battery_raw > 0:
                percent = max(0, min(100, round((battery_raw / 255.0) * 100)))
                results.append(
                    DeviceBattery(
                        id=f"razer_{pid:04x}",
                        name=product_name,
                        protocol="Razer 2.4G",
                        battery_percent=percent,
                        is_charging=(charging_raw == 1),
                        status_text="Connected"
                    )
                )
            else:
                results.append(
                    DeviceBattery(
                        id=f"razer_{pid:04x}",
                        name=product_name,
                        protocol="Razer 2.4G",
                        battery_percent=None,
                        status_text="Sleeping / Disconnected"
                    )
                )

        return results
