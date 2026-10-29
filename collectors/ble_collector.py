import asyncio
import logging
from uuid import UUID
from typing import List, Optional
from .models import DeviceBattery

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


class BLECollector:
    async def _read_single_device(self, dev_info) -> Optional[DeviceBattery]:
        ble_dev = None
        services_to_close = []
        try:
            ble_dev = await BluetoothLEDevice.from_id_async(dev_info.id)
            if not ble_dev:
                return None

            srv_result = await ble_dev.get_gatt_services_for_uuid_async(BATTERY_SERVICE_UUID)
            if srv_result.status != GattCommunicationStatus.SUCCESS or not srv_result.services:
                return None

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
                return DeviceBattery(
                    id=f"ble_{ble_dev.bluetooth_address:x}",
                    name=name,
                    protocol="BLE",
                    battery_percent=percent,
                    status_text="Connected",
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
        return None

    async def collect(self) -> List[DeviceBattery]:
        results: List[DeviceBattery] = []
        if not WINRT_AVAILABLE:
            return results

        try:
            selector = BluetoothLEDevice.get_device_selector_from_connection_status(
                BluetoothConnectionStatus.CONNECTED
            )
            find_fn = (
                DeviceInformation.find_all_async_aqs_filter
                if hasattr(DeviceInformation, "find_all_async_aqs_filter")
                else DeviceInformation.find_all_async
            )
            devices = await asyncio.wait_for(find_fn(selector), timeout=3.0)

            for dev_info in devices:
                try:
                    batt = await asyncio.wait_for(self._read_single_device(dev_info), timeout=2.5)
                    if batt is not None:
                        results.append(batt)
                except asyncio.TimeoutError:
                    logging.debug(f"BLE read timed out for {dev_info.name}")
        except asyncio.TimeoutError:
            logging.debug("BLE device enumeration timed out")
        except Exception as e:
            logging.error(f"Error enumerating BLE devices: {e}")

        return results
