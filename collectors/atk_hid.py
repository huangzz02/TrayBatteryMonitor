import time
import logging
from typing import Callable, Dict, List, Optional, Tuple
import hid
from .models import DeviceBattery

# Vendor IDs for Compx / Nordic MCUs (ATK, VXE, MADLIONS, VGN, Zaopin, Scyrox, Dareu, G-Wolves)
ATK_VIDS = (
    0x373B,  # ATK, MADLIONS, VXE (Nordic 54L15 & Nordic 52840)
    0x3710,  # ATK WebHID variant
    0x3554,  # VXE, VGN, Zaopin, Scyrox (Compx / Nordic 52840)
    0x248A,  # Maxxter / Compx
    0x260D,  # Dareu
    0x33E4,  # G-Wolves
)

# Known PID -> Display Name table for receivers that report generic internal strings
KNOWN_PID_NAMES: Dict[int, str] = {
    0x1216: "ATK A9 SE Wireless Mouse",
    0x11D9: "ATK A9 Ultimate",
    0x11B6: "ATK A9 Ultimate",
    0x1031: "ATK F1 Ultimate",
    0x102E: "ATK F1 Ultimate",
    0x104D: "VXE MAD R Wireless Mouse",
    0x103F: "VXE MAD R Wireless Mouse",
    0x1040: "VXE MAD R Major Plus",
    0x104C: "VXE MAD R Major Plus",
    0x1155: "ATK Zero Wireless Mouse",
    0x124F: "ATK Zero Wireless Mouse",
    0x1154: "ATK Zero Wireless Mouse",
    0xF58A: "VXE R1 Pro Max",
    0xF58C: "VXE R1 Pro Max",
    0xF58E: "VXE R1 SE+",
    0xF58F: "VXE R1 SE+",
    0xF503: "VGN F1 Pro",
    0xF502: "VGN F1 Pro",
    0xFB3E: "VGN F2 Pro Max",
    0xFB3D: "VGN F2 Pro Max",
    0xF524: "Zaopin Z2 Mini",
    0xF526: "Zaopin Z2 Mini",
    0xF5F7: "Scyrox V8",
    0xF5F6: "Scyrox V8",
}

ATK_FALLBACK_PROBES = (
    (0x05, bytes([0x05, 0x15] + [0x00] * 30), 8, 9),
    (0x06, bytes([0x06, 0x01] + [0x00] * 30), 8, 9),
)


def _clean_device_name(prod: str, mfr: str, pid: int) -> str:
    """Format a clean, recognizable device name without raw internal dongle strings."""
    if pid in KNOWN_PID_NAMES:
        return KNOWN_PID_NAMES[pid]

    if not prod:
        return f"ATK Mouse (0x{pid:04X})"

    lower_prod = prod.lower()
    lower_mfr = mfr.lower()

    if "dongle" in lower_prod or "receiver" in lower_prod:
        if "a9" in lower_prod:
            return "ATK A9 Wireless Mouse"
        if "mad" in lower_prod or "mad" in lower_mfr:
            return "VXE MAD R Wireless Mouse"
        if "f1" in lower_prod:
            return "ATK F1 Wireless Mouse"
        if "x1" in lower_prod:
            return "ATK X1 Wireless Mouse"
        if "zero" in lower_prod:
            return "ATK Zero Wireless Mouse"
        if "r1" in lower_prod:
            return "VXE R1 Wireless Mouse"
        if "vgn" in lower_prod or "vgn" in lower_mfr:
            return "VGN Wireless Mouse"
        return "ATK Wireless Mouse"

    return prod


class ATKHIDCollector:
    """
    Collector for ATK, VXE, MADLIONS, and VGN wireless and wired mice.
    Supports:
      - Nordic 54L15 MCU (64-byte 0x72 protocol on Usage Page 0xFF05, sub-cmd 0x07 battery & 0x0F charging)
      - Nordic 52840 MCU (17-byte 0x04 protocol on Usage Page 0xFF02)
      - Legacy Compx feature/output report protocols
    """

    def __init__(self):
        # Cache verified vendor endpoint path per (vid, pid) to avoid probing unrelated HID collections
        self._cached_paths: Dict[Tuple[int, int], Tuple[bytes, int]] = {}

    @staticmethod
    def _transact(
        dev,
        payload: bytes,
        match_fn: Callable[[List[int]], bool],
        timeout: float = 0.12,
    ) -> Optional[List[int]]:
        """Drain stale reports, write request, and wait up to `timeout` seconds for a matching reply."""
        while dev.read(64):
            pass
        dev.write(payload)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            res = dev.read(64)
            if res:
                if match_fn(res):
                    return res
            else:
                time.sleep(0.005)
        return None

    def _probe_protocol2_nordic54(self, dev) -> Tuple[Optional[int], bool, bool, bool]:
        """
        Protocol 2: Nordic 54L15 (ATK A9 SE, ATK Zero, Nano/8K receivers on Usage Page 0xFF05).
        64-byte report ID 0x08, Command 0x72:
          - sub-index 0x07 -> byte 7 is battery percentage (1-100)
          - sub-index 0x0F -> byte 7 is VBUS / charging flag (0x00 = unplugged, 0x01 = charging)
        Returns: (battery_percent, is_charging, is_wired, protocol_matched)
        """
        protocol_matched = False

        for link in (1, 0):
            req = bytearray(64)
            req[0] = 0x08
            req[1] = 0x7D
            req[2] = 0x72
            req[3] = 0x02
            req[5] = link  # 1 = wireless link via dongle, 0 = direct USB cable
            req[6] = 0x07  # sub-index 7: battery percentage
            req[7] = 0x01

            try:
                res = self._transact(
                    dev,
                    bytes(req),
                    lambda r: len(r) >= 8 and r[0] == 0x08 and r[1] == 0x72 and r[5] == 0x07,
                    timeout=0.12,
                )
            except Exception:
                continue

            if not res:
                continue

            protocol_matched = True
            # res[2] == 0 means the mouse actively answered; res[2] == 1 means asleep / out of range
            if res[2] == 0 and 1 <= res[7] <= 100:
                pct = res[7]
                is_wired = (link == 0)
                is_charging = is_wired

                # Query sub-index 0x0F for real-time VBUS / charging flag (0x00 = unplugged, 0x01 = charging)
                chg_req = bytearray(64)
                chg_req[0] = 0x08
                chg_req[1] = 0x7D
                chg_req[2] = 0x72
                chg_req[3] = 0x02
                chg_req[5] = link
                chg_req[6] = 0x0F  # sub-index 15 (0x0F): charging flag
                chg_req[7] = 0x01

                try:
                    chg_res = self._transact(
                        dev,
                        bytes(chg_req),
                        lambda r: len(r) >= 8 and r[0] == 0x08 and r[1] == 0x72 and r[5] == 0x0F,
                        timeout=0.10,
                    )
                    if chg_res and chg_res[2] == 0:
                        is_charging = is_wired or (chg_res[7] > 0)
                except Exception:
                    pass

                return pct, is_charging, is_wired, True

        return None, False, False, protocol_matched

    def _probe_protocol1_nordic52(self, dev) -> Tuple[Optional[int], bool, bool]:
        """
        Protocol 1: Nordic 52840 (ATK F1, VXE MAD R, VXE R1, VGN F1 on Usage Page 0xFF02).
        17-byte report ID 0x08, sub-command 0x04, checksum 0x49 in byte 16.
        Response byte 6 = battery %, byte 7 = charging / external VBUS flag.
        """
        req = bytearray(17)
        req[0] = 0x08
        req[1] = 0x04
        req[16] = 0x49
        try:
            res = self._transact(
                dev,
                bytes(req),
                lambda r: len(r) >= 8 and r[0] == 0x08 and r[1] == 0x04,
                timeout=0.10,
            )
            if res and 1 <= res[6] <= 100:
                pct = res[6]
                has_power = bool(res[7])
                return pct, has_power, False
        except Exception:
            pass
        return None, False, False

    def _probe_device(self, path: bytes, usage_page: int = 0) -> Tuple[Optional[int], bool, bool, bool]:
        """
        Probe a single HID device path.
        Returns: (battery_percent, is_charging, is_wired, endpoint_recognized)
        """
        dev = hid.device()
        try:
            dev.open_path(path)
            dev.set_nonblocking(1)

            # Usage Page 0xFF05 is dedicated to Nordic 54L15 Protocol 2
            if usage_page in (0xFF05, 0):
                pct, chg, wired, matched = self._probe_protocol2_nordic54(dev)
                if pct is not None or (usage_page == 0xFF05 and matched):
                    return pct, chg, wired, True

            # Usage Page 0xFF02 is dedicated to Nordic 52840 Protocol 1
            if usage_page != 0xFF05:
                pct, chg, wired = self._probe_protocol1_nordic52(dev)
                if pct is not None:
                    return pct, chg, wired, True

                if usage_page != 0xFF02:
                    # Fallback legacy probes (0x05, 0x06) only on unknown vendor pages
                    for rep_id, payload, batt_idx, chg_idx in ATK_FALLBACK_PROBES:
                        try:
                            res = self._transact(
                                dev,
                                payload,
                                lambda r, bi=batt_idx: len(r) > bi and 1 <= r[bi] <= 100,
                                timeout=0.05,
                            )
                            if res:
                                chg = bool(res[chg_idx]) if len(res) > chg_idx else False
                                return res[batt_idx], chg, False, True

                            dev.send_feature_report(payload)
                            time.sleep(0.025)
                            f_resp = dev.get_feature_report(rep_id, len(payload) + 1)
                            if f_resp and len(f_resp) > batt_idx and 1 <= f_resp[batt_idx] <= 100:
                                chg = bool(f_resp[chg_idx]) if len(f_resp) > chg_idx else False
                                return f_resp[batt_idx], chg, False, True
                        except Exception:
                            continue

        except Exception as e:
            logging.debug(f"Failed to access ATK HID path {path}: {e}")
        finally:
            try:
                dev.close()
            except Exception:
                pass

        return None, False, False, False

    def collect(self) -> List[DeviceBattery]:
        results: List[DeviceBattery] = []

        # Group all vendor endpoints (>= 0xFF00) by (vid, pid)
        devs_by_pid: Dict[Tuple[int, int], List[dict]] = {}
        for vid in ATK_VIDS:
            try:
                for d in hid.enumerate(vid, 0):
                    if d.get("usage_page", 0) >= 0xFF00:
                        key = (vid, d.get("product_id", 0))
                        devs_by_pid.setdefault(key, []).append(d)
            except Exception as e:
                logging.debug(f"Error enumerating ATK VID 0x{vid:04X}: {e}")

        for key, dev_list in devs_by_pid.items():
            vid, pid = key
            best_percent: Optional[int] = None
            is_charging = False
            is_wired = False

            mfr0 = (dev_list[0].get("manufacturer_string") or "").strip()
            prod0 = (dev_list[0].get("product_string") or "").strip()
            best_name = _clean_device_name(prod0, mfr0, pid)

            # 1. Fast path: try previously verified endpoint first
            probed_paths = set()
            cached = self._cached_paths.get(key)
            if cached is not None:
                cached_path, cached_up = cached
                if any(d.get("path") == cached_path for d in dev_list):
                    probed_paths.add(cached_path)
                    pct, chg, wired, recognized = self._probe_device(cached_path, cached_up)
                    if pct is not None:
                        best_percent = pct
                        is_charging = chg
                        is_wired = wired
                    elif recognized:
                        # Dongle responded that the mouse is currently asleep; skip unrelated interfaces
                        probed_paths.update(d.get("path") for d in dev_list)

            # 2. Full scan across vendor usage pages: prioritize 0xFF05 (Nordic 54), then 0xFF02 (Nordic 52)
            if best_percent is None:
                dev_list.sort(
                    key=lambda d: 0 if d.get("usage_page") == 0xFF05 else (1 if d.get("usage_page") == 0xFF02 else 2)
                )
                for d in dev_list:
                    path = d.get("path")
                    if path in probed_paths:
                        continue
                    up = d.get("usage_page", 0)
                    pct, chg, wired, recognized = self._probe_device(path, up)
                    if recognized:
                        self._cached_paths[key] = (path, up)
                    if pct is not None:
                        best_percent = pct
                        is_charging = chg
                        is_wired = wired
                        mfr = (d.get("manufacturer_string") or "").strip()
                        prod = (d.get("product_string") or "").strip()
                        best_name = _clean_device_name(prod, mfr, pid)
                        break
                    if recognized:
                        break

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
                        status_text="Connected",
                    )
                )
            else:
                results.append(
                    DeviceBattery(
                        id=dev_id,
                        name=best_name,
                        protocol=proto_str,
                        battery_percent=None,
                        status_text="Sleeping / Inactive",
                    )
                )

        return results
