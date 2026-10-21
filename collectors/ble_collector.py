import asyncio
import logging
from uuid import UUID
from typing import List
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
    async def collect(self) -> List[DeviceBattery]:
        results: List[DeviceBattery] = []
        if not WINRT_AVAILABLE:
            return results

        try:
            selector = BluetoothLEDevice.get_device_selector_from_connection_status(
                BluetoothConnectionStatus.CONNECTED
            )
            devices = await (DeviceInformation.find_all_async_aqs_filter(selector) if hasattr(DeviceInformation, 'find_all_async_aqs_filter') else DeviceInformation.find_all_async(selector))

            for dev_info in devices:
                ble_dev = None
                try:
                    ble_dev = await BluetoothLEDevice.from_id_async(dev_info.id)
                    if not ble_dev:
                        continue

                    srv_result = await ble_dev.get_gatt_services_for_uuid_async(BATTERY_SERVICE_UUID)
                    if srv_result.status != GattCommunicationStatus.SUCCESS or not srv_result.services:
                        continue

                    percent = None
                    for service in srv_result.services:
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
                        results.append(
                            DeviceBattery(
                                id=f"ble_{ble_dev.bluetooth_address:x}",
                                name=name,
                                protocol="BLE",
                                battery_percent=percent,
                                status_text="Connected"
                            )
                        )
                except Exception as e:
                    logging.debug(f"Failed to read BLE device {dev_info.name}: {e}")
                finally:
                    if ble_dev:
                        ble_dev.close()
        except Exception as e:
            logging.error(f"Error enumerating BLE devices: {e}")

        return results

