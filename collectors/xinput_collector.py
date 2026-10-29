import ctypes
from ctypes import wintypes
from typing import List
from .models import DeviceBattery

BATTERY_DEVTYPE_GAMEPAD = 0x00
BATTERY_TYPE_DISCONNECTED = 0x00
BATTERY_TYPE_WIRED = 0x01

BATTERY_LEVEL_EMPTY = 0x00
BATTERY_LEVEL_LOW = 0x01
BATTERY_LEVEL_MEDIUM = 0x02
BATTERY_LEVEL_FULL = 0x03

LEVEL_MAP = {
    BATTERY_LEVEL_EMPTY: (10, "Empty (~10%)"),
    BATTERY_LEVEL_LOW: (30, "Low (~30%)"),
    BATTERY_LEVEL_MEDIUM: (65, "Medium (~65%)"),
    BATTERY_LEVEL_FULL: (100, "Full (100%)"),
}


class XINPUT_BATTERY_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BatteryType", wintypes.BYTE),
        ("BatteryLevel", wintypes.BYTE),
    ]


class XInputCollector:
    def __init__(self):
        self.dll = None
        for dll_name in ("XInput1_4.dll", "xinput1_3.dll"):
            try:
                dll = ctypes.WinDLL(dll_name)
                dll.XInputGetState.argtypes = [wintypes.DWORD, ctypes.c_void_p]
                dll.XInputGetState.restype = wintypes.DWORD
                dll.XInputGetBatteryInformation.argtypes = [
                    wintypes.DWORD,
                    wintypes.BYTE,
                    ctypes.POINTER(XINPUT_BATTERY_INFORMATION),
                ]
                dll.XInputGetBatteryInformation.restype = wintypes.DWORD
                self.dll = dll
                break
            except (OSError, AttributeError):
                continue

    def collect(self) -> List[DeviceBattery]:
        results: List[DeviceBattery] = []
        if not self.dll:
            return results

        state_buf = (ctypes.c_byte * 16)()

        for user_idx in range(4):
            res_state = self.dll.XInputGetState(user_idx, ctypes.byref(state_buf))
            if res_state != 0:
                continue

            batt_info = XINPUT_BATTERY_INFORMATION()
            res_batt = self.dll.XInputGetBatteryInformation(
                user_idx,
                BATTERY_DEVTYPE_GAMEPAD,
                ctypes.byref(batt_info),
            )
            if res_batt == 0 and batt_info.BatteryType != BATTERY_TYPE_DISCONNECTED:
                if batt_info.BatteryType == BATTERY_TYPE_WIRED:
                    results.append(
                        DeviceBattery(
                            id=f"xinput_{user_idx}",
                            name=f"Xbox Controller #{user_idx + 1}",
                            protocol="XInput",
                            battery_percent=100,
                            is_charging=True,
                            status_text="Wired",
                        )
                    )
                else:
                    pct, desc = LEVEL_MAP.get(batt_info.BatteryLevel, (50, "Unknown"))
                    results.append(
                        DeviceBattery(
                            id=f"xinput_{user_idx}",
                            name=f"Xbox Controller #{user_idx + 1}",
                            protocol="XInput",
                            battery_percent=pct,
                            status_text=desc,
                        )
                    )
        return results
