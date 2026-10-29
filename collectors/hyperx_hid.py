import time
import logging
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple
import hid
from .models import DeviceBattery
from .power_guard import get_refresh_generation

HYPERX_VIDS = [0x03F0, 0x0951]

# Probe commands validated by HeadsetControl
# Probe 1 (Cloud II Wireless / Cloud Alpha Wireless 300h)
HYPERX_PROBE_ALPHA = bytes([0x21, 0xBB, 0x0B, 0x00, 0x00, 0x00, 0x00, 0x00])
# Probe 2 (Cloud Flight / Flight S)
HYPERX_PROBE_FLIGHT = bytes([0x21, 0xFF, 0x05, 0x00, 0x00, 0x00, 0x00, 0x00])


@dataclass
class _HyperXState:
    percent: Optional[int] = None
    is_charging: bool = False
    is_sleeping: bool = False
    last_poll_mono: float = 0.0
    last_refresh_gen: int = 0
    name: str = ""


class HyperXHIDCollector:
    def __init__(self):
        self._cached_endpoints: Dict[Tuple[int, int], Tuple[bytes, bytes]] = {}
        self._states: Dict[Tuple[int, int], _HyperXState] = {}
        self._last_discovery_mono: Dict[Tuple[int, int], Tuple[float, int]] = {}

    def _probe_headset(
        self,
        path: bytes,
        specific_probe: Optional[bytes] = None,
    ) -> Tuple[int, bool, Optional[bytes]]:
        dev = hid.device()
        probes = (specific_probe,) if specific_probe is not None else (HYPERX_PROBE_ALPHA, HYPERX_PROBE_FLIGHT)
        try:
            dev.open_path(path)
            dev.set_nonblocking(1)

            for probe_payload in probes:
                try:
                    while dev.read(32):
                        pass
                    dev.write(probe_payload)
                    time.sleep(0.025)
                    resp = dev.read(32)
                    if resp and len(resp) >= 5 and resp[0] == 0x21:
                        # Cloud II / Alpha payload: resp[3] is battery, resp[4] is charging status
                        pct = resp[3]
                        chg_state = resp[4]
                        if 1 <= pct <= 100:
                            is_charging = (chg_state == 0x01)
                            return pct, is_charging, probe_payload
                        # Some models use resp[4] for battery and resp[3] for status
                        pct_alt = resp[4]
                        if 1 <= pct_alt <= 100 and resp[3] in (0x00, 0x01, 0x02):
                            is_charging = (resp[3] == 0x01)
                            return pct_alt, is_charging, probe_payload
                except Exception:
                    continue

        except Exception as e:
            logging.debug(f"HyperX probe error: {e}")
        finally:
            try:
                dev.close()
            except Exception:
                pass
        return -1, False, None

    def collect(self) -> List[DeviceBattery]:
        results: List[DeviceBattery] = []
        grouped: Dict[Tuple[int, int], List[dict]] = {}

        for vid in HYPERX_VIDS:
            try:
                devices = hid.enumerate(vid, 0)
            except Exception:
                continue

            for d in devices:
                pid = d.get("product_id", 0)
                prod_name = (d.get("product_string") or "").strip() or f"HyperX Device (0x{pid:04X})"
                lower_prod = prod_name.lower()
                if "wireless" not in lower_prod and "cloud" not in lower_prod and "hyperx" not in lower_prod:
                    continue
                grouped.setdefault((vid, pid), []).append(d)

        for stale_key in list(self._states.keys()):
            if stale_key not in grouped:
                self._states.pop(stale_key, None)
                self._cached_endpoints.pop(stale_key, None)
                self._last_discovery_mono.pop(stale_key, None)
        for stale_key in list(self._last_discovery_mono.keys()):
            if stale_key not in grouped:
                self._last_discovery_mono.pop(stale_key, None)

        now = time.monotonic()
        cur_gen = get_refresh_generation()

        for key, ifaces in grouped.items():
            vid, pid = key
            prod_name = (ifaces[0].get("product_string") or "").strip() or f"HyperX Device (0x{pid:04X})"

            cached = self._cached_endpoints.get(key)
            if cached is not None and not any(d.get("path") == cached[0] for d in ifaces):
                self._cached_endpoints.pop(key, None)
                cached = None

            # 1. Verified wireless headset fast path (gentle 60s RF poll so headset auto-sleep isn't disturbed)
            if cached is not None:
                c_path, c_probe = cached
                state = self._states.setdefault(key, _HyperXState(name=prod_name))
                state.name = prod_name
                interval = 30.0 if state.is_sleeping else 60.0

                if state.last_refresh_gen != cur_gen or state.last_poll_mono <= 0.0 or (now - state.last_poll_mono) >= interval:
                    state.last_poll_mono = now
                    state.last_refresh_gen = cur_gen
                    pct, is_charging, _ = self._probe_headset(c_path, specific_probe=c_probe)
                    if 1 <= pct <= 100:
                        state.percent = pct
                        state.is_charging = is_charging
                        state.is_sleeping = False
                    else:
                        state.is_sleeping = True
                        state.is_charging = False

                dev_id = f"hyperx_{vid:04x}_{pid:04x}"
                if not state.is_sleeping and state.percent is not None:
                    results.append(
                        DeviceBattery(
                            id=dev_id,
                            name=prod_name,
                            protocol="HyperX 2.4G",
                            battery_percent=state.percent,
                            is_charging=state.is_charging,
                            status_text="Connected",
                        )
                    )
                else:
                    results.append(
                        DeviceBattery(
                            id=dev_id,
                            name=prod_name,
                            protocol="HyperX 2.4G",
                            battery_percent=None,
                            status_text="Sleeping / Inactive",
                        )
                    )
                continue

            # 2. Throttled initial discovery (30s)
            last_disc_mono, last_disc_gen = self._last_discovery_mono.get(key, (-1e9, 0))
            if last_disc_gen == cur_gen and (now - last_disc_mono) < 30.0:
                continue
            self._last_discovery_mono[key] = (now, cur_gen)

            for d in ifaces:
                path = d.get("path")
                percent, is_charging, matched_probe = self._probe_headset(path)
                if 1 <= percent <= 100 and matched_probe is not None:
                    self._cached_endpoints[key] = (path, matched_probe)
                    self._states[key] = _HyperXState(
                        percent=percent,
                        is_charging=is_charging,
                        is_sleeping=False,
                        last_poll_mono=now,
                        last_refresh_gen=cur_gen,
                        name=prod_name,
                    )
                    results.append(
                        DeviceBattery(
                            id=f"hyperx_{vid:04x}_{pid:04x}",
                            name=prod_name,
                            protocol="HyperX 2.4G",
                            battery_percent=percent,
                            is_charging=is_charging,
                            status_text="Connected",
                        )
                    )
                    break

        return results
