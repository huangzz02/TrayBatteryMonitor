import time
import logging
from typing import List, Tuple, Optional, Dict
import hid
from .models import DeviceBattery

# Common Vendor IDs for Compx / Nordic MCUs (ATK, VXE, MADLIONS, VGN, Zaopin, Scyrox, Dareu, G-Wolves)
ATK_VIDS = [
    0x373B,  # ATK, MADLIONS, VXE (Nordic 54L15 & Nordic 52840)
    0x3554,  # VXE, VGN, Zaopin, Scyrox (Compx / Nordic 52840)
    0x248A,  # Maxxter / Compx
    0x260D,  # Dareu
    0x33E4,  # G-Wolves
]

ATK_FALLBACK_PROBES = [
    (0x05, [0x05, 0x15] + [0x00] * 30, 8, 9),
    (0x06, [0x06, 0x01] + [0x00] * 30, 8, 9),
]


def _clean_device_name(prod: str, mfr: str, pid: int) -> str:
    """Format a clean, recognizable name without raw internal dongle strings."""
    if not prod:
        return f"ATK Mouse (0x{pid:04X})"

    lower_prod = prod.lower()
    lower_mfr = mfr.lower()

    if "dongle" in lower_prod or "receiver" in lower_prod:
        if "mad" in lower_prod or "mad" in lower_mfr:
            return "ATK MAD Wireless Mouse"
        elif "f1" in lower_prod:
            return "ATK F1 Wireless Mouse"
        elif "x1" in lower_prod:
            return "ATK X1 Wireless Mouse"
        elif "zero" in lower_prod:
            return "ATK Zero Wireless Mouse"
        elif "r1" in lower_prod:
            return "VXE R1 Wireless Mouse"
        elif "vgn" in lower_prod or "vgn" in lower_mfr:
            return "VGN Wireless Mouse"
        else:
            return "ATK Wireless Mouse"

    return prod


class ATKHIDCollector:
    """
    Collector for ATK, VXE, MADLIONS, and VGN wireless and wired mice.
    Supports:
      - Nordic 54L15 MCU (64-byte 0x72 protocol on Usage Page 0xFF05)
      - Nordic 52840 MCU (17-byte 0x04 protocol on Usage Page 0xFF02)
      - Legacy Compx feature report protocols
    """

    def _probe_device(self, path: bytes) -> Tuple[Optional[int], bool, bool]:
        """
        Probe a single HID device path.
        Returns: (battery_percent, is_charging, is_wired)
        """
        dev = hid.device()
        try:
            dev.open_path(path)
            dev.set_nonblocking(1)

            # -------------------------------------------------------------
            # Protocol 2: Nordic 54L15 (ATK Zero, MADLIONS MAD R Nano Dongle, etc.)
            # 64-byte report with ID 0x08, CMD 0x72
            # link = 1 (2.4G wireless link behind dongle), link = 0 (wired cable)
            # -------------------------------------------------------------
            for link in (1, 0):
                req = [0] * 64
                req[0] = 0x08
                req[1] = 0x7D
                req[2] = 0x72
                req[3] = 0x02
                req[5] = link
                req[6] = 0x07  # battery query sub-command
                req[7] = 0x01
                try:
                    dev.write(bytes(req))
                    time.sleep(0.04)
                    res = dev.read(64)
                    if (
                        res
                        and len(res) >= 8
                        and res[0] == 0x08
                        and res[1] == 0x72
                        and res[5] == 0x07
                    ):
                        # res[2] == 0 means the mouse answered actively
                        if res[2] == 0 and 1 <= res[7] <= 100:
                            is_wired = (link == 0)
                            is_charging = (is_wired and res[7] < 100)
                            return res[7], is_charging, is_wired
                except Exception:
                    pass

            # -------------------------------------------------------------
            # Protocol 1: Nordic 52840 (ATK F1, VXE MAD R, VXE R1, VGN F1, etc.)
            # 17-byte report with ID 0x08, sub-command 0x04, checksum 0x49 in byte 16
            # -------------------------------------------------------------
            req1 = [0] * 17
            req1[0] = 0x08
            req1[1] = 0x04
            req1[16] = 0x49
            try:
                dev.write(bytes(req1))
                time.sleep(0.04)
                res1 = dev.read(64)
                if (
                    res1
                    and len(res1) >= 8
                    and res1[0] == 0x08
                    and res1[1] == 0x04
                ):
                    pct = res1[6]
                    if 1 <= pct <= 100:
                        is_wired = bool(res1[7])
                        is_charging = (is_wired and pct < 100)
                        return pct, is_charging, is_wired
            except Exception:
                pass

            # -------------------------------------------------------------
            # Fallback legacy probes (0x05, 0x06 feature reports)
            # -------------------------------------------------------------
            for rep_id, payload, batt_idx, chg_idx in ATK_FALLBACK_PROBES:
                try:
                    dev.write(bytes(payload))
                    time.sleep(0.03)
                    resp = dev.read(64)
                    if resp and len(resp) > batt_idx and 1 <= resp[batt_idx] <= 100:
                        chg = bool(resp[chg_idx]) if len(resp) > chg_idx else False
                        return resp[batt_idx], chg, False

                    dev.send_feature_report(bytes(payload))
                    time.sleep(0.03)
                    f_resp = dev.get_feature_report(rep_id, len(payload) + 1)
                    if f_resp and len(f_resp) > batt_idx and 1 <= f_resp[batt_idx] <= 100:
                        chg = bool(f_resp[chg_idx]) if len(f_resp) > chg_idx else False
                        return f_resp[batt_idx], chg, False
                except Exception:
                    continue

        except Exception as e:
            logging.debug(f"Failed to access ATK HID path {path}: {e}")
        finally:
            try:
                dev.close()
            except Exception:
                pass

        return None, False, False

    def collect(self) -> List[DeviceBattery]:
        results: List[DeviceBattery] = []

        # Group all vendor endpoints by (vid, pid)
        devs_by_pid: Dict[Tuple[int, int], List[dict]] = {}
        for vid in ATK_VIDS:
            try:
                for d in hid.enumerate(vid, 0):
                    up = d.get("usage_page", 0)
                    if up >= 0xFF00:
                        key = (vid, d.get("product_id", 0))
                        devs_by_pid.setdefault(key, []).append(d)
            except Exception as e:
                logging.debug(f"Error enumerating ATK VID 0x{vid:04X}: {e}")

        for (vid, pid), dev_list in devs_by_pid.items():
            best_percent: Optional[int] = None
            is_charging = False
            is_wired = False
            best_name: Optional[str] = None

            # Prioritize dedicated battery vendor usage pages:
            # 0xFF05 (Nordic 54), 0xFF02 (Nordic 52), then others
            dev_list.sort(
                key=lambda d: 0 if d.get("usage_page") == 0xFF05 else (1 if d.get("usage_page") == 0xFF02 else 2)
            )

            for d in dev_list:
                pct, chg, wired = self._probe_device(d["path"])
                mfr = (d.get("manufacturer_string") or "").strip()
                prod = (d.get("product_string") or "").strip()

                if not best_name:
                    best_name = _clean_device_name(prod, mfr, pid)

                if pct is not None and 1 <= pct <= 100:
                    best_percent = pct
                    is_charging = chg
                    is_wired = wired
                    best_name = _clean_device_name(prod, mfr, pid)
                    break

            if not best_name:
                best_name = f"ATK Mouse (0x{pid:04X})"

            proto_str = "ATK Wired" if is_wired else "ATK 2.4G"
            dev_id = f"atk_{vid:04x}_{pid:04x}"

            if best_percent is not None:
                results.append(
                    DeviceBattery(
                        id=dev_id,
                        name=best_name,
                        protocol=proto_str,
                        battery_percent=best_percent,
                        is_charging=is_charging,
                        status_text="Connected"
                    )
                )
            else:
                results.append(
                    DeviceBattery(
                        id=dev_id,
                        name=best_name,
                        protocol=proto_str,
                        battery_percent=None,
                        status_text="Sleeping / Inactive"
                    )
                )

        return results
