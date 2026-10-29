import asyncio
import logging
import time
from dataclasses import dataclass
from uuid import UUID
from typing import Dict, List, Optional, Tuple
from .models import DeviceBattery
from .power_guard import get_refresh_generation, sample_mouse_activity

try:
    from winrt.windows.devices.bluetooth import BluetoothLEDevice, BluetoothConnectionStatus
    from winrt.windows.devices.bluetooth.genericattributeprofile import GattCommunicationStatus
    from winrt.windows.devices.enumeration import DeviceInformation
    from winrt.windows.storage.streams import DataReader
    WINRT_AVAILABLE = True
except ImportError:
    WINRT_AVAILABLE = False
    logging.warning("winrt modules not found. BLE collector disabled.")

BATTERY_SERVICE_UUID = UUID("0000180f-0000-1000-8000-00805f9b34fb")
BATTERY_LEVEL_UUID = UUID("00002a19-0000-1000-8000-00805f9b34fb")

# Interval between active BLE GATT battery reads while a BLE peripheral stays continuously connected.
# Passive OS connection enumeration still runs every poll tick (0 radio traffic), while GATT reads
# are spaced out to preserve BLE Peripheral Slave Latency and let BLE keyboards/mice enter deep sleep.
_BLE_GATT_POLL_INTERVAL_SEC = 180.0
_BLE_NO_BATT_RETRY_SEC = 300.0
_BLE_TRANSIENT_RETRY_SEC = 30.0


@dataclass
class _BLECacheEntry:
    battery: Optional[DeviceBattery]
    last_read_mono: float
    last_refresh_gen: int
    confirmed_no_battery: bool = False


class BLECollector:
    def __init__(self):
        self._cache: Dict[str, _BLECacheEntry] = {}

    async def _read_single_device(self, dev_info) -> Tuple[Optional[DeviceBattery], bool]:
        """
        Read BLE GATT Battery Level (0x180F / 0x2A19).
        Returns: (DeviceBattery or None, confirmed_no_battery_service)
        """
        ble_dev = None
        services_to_close = []
        try:
            ble_dev = await BluetoothLEDevice.from_id_async(dev_info.id)
            if not ble_dev:
                return None, False

            srv_result = await ble_dev.get_gatt_services_for_uuid_async(BATTERY_SERVICE_UUID)
            if srv_result.status == GattCommunicationStatus.SUCCESS and not srv_result.services:
                return None, True
            if srv_result.status != GattCommunicationStatus.SUCCESS or not srv_result.services:
                return None, False

            services_to_close = list(srv_result.services)
            percent = None
            for service in services_to_close:
                char_result = await service.get_characteristics_for_uuid_async(BATTERY_LEVEL_UUID)
                if char_result.status == GattCommunicationStatus.SUCCESS and char_result.characteristics:
                    char = char_result.characteristics[0]
                    read_res = await char.read_value_async()
                    if read_res.status == GattCommunicationStatus.SUCCESS and read_res.value:
                        reader = DataReader.from_buffer(read_res.value)
                        percent = reader.read_byte()
                        break

            if percent is not None and 0 <= percent <= 100:
                name = ble_dev.name or dev_info.name or "BLE Device"
                return (
                    DeviceBattery(
                        id=f"ble_{ble_dev.bluetooth_address:x}",
                        name=name,
                        protocol="BLE",
                        battery_percent=percent,
                        status_text="Connected",
                    ),
                    False,
                )
        except Exception as e:
            logging.debug(f"Failed to read BLE device {dev_info.name}: {e}")
        finally:
            for srv in services_to_close:
                try:
                    srv.close()
                except Exception:
                    pass
            if ble_dev:
                try:
                    ble_dev.close()
                except Exception:
                    pass
        return None, False

    async def collect(self) -> List[DeviceBattery]:
        results: List[DeviceBattery] = []
        if not WINRT_AVAILABLE:
            return results

        try:
            # Pure OS PnP lookup in RAM (0 Bluetooth radio traffic): reflects BLE deep-sleep disconnects within 1 tick
            selector = BluetoothLEDevice.get_device_selector_from_connection_status(
                BluetoothConnectionStatus.CONNECTED
            )
            find_fn = (
                DeviceInformation.find_all_async_aqs_filter
                if hasattr(DeviceInformation, "find_all_async_aqs_filter")
                else DeviceInformation.find_all_async
            )
            devices = await asyncio.wait_for(find_fn(selector), timeout=3.0)

            now = time.monotonic()
            cur_gen = get_refresh_generation()
            idle_sec, _ = sample_mouse_activity()

            connected_ids = {dev_info.id for dev_info in devices}
            # Prune disconnected/sleeping BLE devices so reconnecting immediately triggers a fresh GATT read
            for stale_id in list(self._cache.keys()):
                if stale_id not in connected_ids:
                    self._cache.pop(stale_id, None)

            for dev_info in devices:
                dev_key = dev_info.id
                entry = self._cache.get(dev_key)
                manual_refresh = entry is None or entry.last_refresh_gen != cur_gen

                if not manual_refresh and entry is not None:
                    elapsed = now - entry.last_read_mono
                    if entry.confirmed_no_battery:
                        if elapsed < _BLE_NO_BATT_RETRY_SEC:
                            continue
                    elif entry.battery is not None:
                        is_ble_mouse = "mouse" in (entry.battery.name or dev_info.name or "").lower()
                        # Defer GATT read if a BLE mouse is currently stationary counting down to deep sleep
                        target_interval = (
                            310.0
                            if (is_ble_mouse and 4.5 <= idle_sec < 310.0)
                            else _BLE_GATT_POLL_INTERVAL_SEC
                        )
                        if elapsed < target_interval:
                            results.append(entry.battery)
                            continue
                    else:
                        if elapsed < _BLE_TRANSIENT_RETRY_SEC:
                            continue

                try:
                    batt, no_batt = await asyncio.wait_for(self._read_single_device(dev_info), timeout=2.5)
                    if batt is not None:
                        self._cache[dev_key] = _BLECacheEntry(
                            battery=batt,
                            last_read_mono=now,
                            last_refresh_gen=cur_gen,
                            confirmed_no_battery=False,
                        )
                        results.append(batt)
                    elif no_batt:
                        self._cache[dev_key] = _BLECacheEntry(
                            battery=None,
                            last_read_mono=now,
                            last_refresh_gen=cur_gen,
                            confirmed_no_battery=True,
                        )
                    elif entry is not None and entry.battery is not None:
                        # Transient GATT read failure while OS still reports CONNECTED: preserve icon & retry in 30s
                        entry.last_read_mono = now - (_BLE_GATT_POLL_INTERVAL_SEC - _BLE_TRANSIENT_RETRY_SEC)
                        entry.last_refresh_gen = cur_gen
                        results.append(entry.battery)
                    else:
                        self._cache[dev_key] = _BLECacheEntry(
                            battery=None,
                            last_read_mono=now,
                            last_refresh_gen=cur_gen,
                            confirmed_no_battery=False,
                        )
                except asyncio.TimeoutError:
                    logging.debug(f"BLE read timed out for {dev_info.name}")
                    if entry is not None and entry.battery is not None:
                        entry.last_read_mono = now - (_BLE_GATT_POLL_INTERVAL_SEC - _BLE_TRANSIENT_RETRY_SEC)
                        entry.last_refresh_gen = cur_gen
                        results.append(entry.battery)
        except asyncio.TimeoutError:
            logging.debug("BLE device enumeration timed out")
        except Exception as e:
            logging.error(f"Error enumerating BLE devices: {e}")

        return results
