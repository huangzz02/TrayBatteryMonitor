import sys
import os
import time
import math
import json
import asyncio
import threading
import logging
import winreg
import ctypes
from ctypes import wintypes
from typing import Dict, Optional, List, Tuple

from PIL import Image, ImageDraw, ImageFont
import pystray
from pystray import Menu, MenuItem
import pystray._win32 as pwin32
import pystray._util.win32 as win32

from collectors import (
    DeviceBattery,
    BLECollector,
    RazerHIDCollector,
    ATKHIDCollector,
    XInputCollector,
    LogitechHIDCollector,
    SonyHIDCollector,
    HyperXHIDCollector,
    SteelSeriesHIDCollector,
    CorsairHIDCollector,
    ASUSHIDCollector,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

APP_NAME = "TrayBatteryMonitor"

# ---------------------------------------------------------
# Monkey-patch pystray Windows backend to fix uID collision
# In standard pystray, hID=id(self) is passed to NOTIFYICONDATAW,
# but NOTIFYICONDATAW only has uID (UINT), causing uID to default
# to 0 for all icons. On Windows 11, sharing uID=0 causes
# taskbar drag-and-drop to scramble and invert the icons.
# ---------------------------------------------------------
def _patched_message(self, code, flags, **kwargs):
    hwnd = getattr(self, "_hwnd", None)
    if not hwnd:
        return 0
    uid = getattr(self, "uid", None)
    if uid is None:
        name_str = getattr(self, "name", "")
        uid = (abs(hash(name_str)) % 100000) + 100
    return win32.Shell_NotifyIcon(code, win32.NOTIFYICONDATAW(
        cbSize=ctypes.sizeof(win32.NOTIFYICONDATAW),
        hWnd=hwnd,
        uID=uid,
        uFlags=flags,
        **kwargs))

pwin32.Icon._message = _patched_message

# ---------------------------------------------------------
# Configuration Persistence (config.json)
# ---------------------------------------------------------
def get_config_path() -> str:
    if getattr(sys, "frozen", False):
        return os.path.join(os.path.dirname(sys.executable), "config.json")
    # In development mode, prioritize dist/config.json to share a single configuration
    dist_config = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dist", "config.json")
    if os.path.exists(dist_config):
        return dist_config
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

def auto_promote_icons():
    r"""
    Windows 11 automatically places new tray icons inside the overflow flyout menu.
    This function sets IsPromoted=1 in HKCU\Control Panel\NotifyIconSettings for all
    TrayBatteryMonitor icons so they permanently stay visible on the outer taskbar.
    """
    key_path = r"Control Panel\NotifyIconSettings"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_READ) as root:
            idx = 0
            while True:
                try:
                    sub_name = winreg.EnumKey(root, idx)
                    idx += 1
                except OSError:
                    break
                sub_path = f"{key_path}\\{sub_name}"
                try:
                    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, sub_path, 0, winreg.KEY_READ | winreg.KEY_WRITE) as sub_key:
                        try:
                            exe, _ = winreg.QueryValueEx(sub_key, "ExecutablePath")
                        except OSError:
                            continue
                        if "traybatterymonitor" in exe.lower():
                            try:
                                val, _ = winreg.QueryValueEx(sub_key, "IsPromoted")
                            except OSError:
                                val = 0
                            if val != 1:
                                winreg.SetValueEx(sub_key, "IsPromoted", 0, winreg.REG_DWORD, 1)
                                logging.info(f"Auto-promoted tray icon to visible taskbar: {sub_name}")
                except Exception:
                    pass
    except Exception as e:
        logging.debug(f"auto_promote_icons error: {e}")

def load_config() -> dict:
    path = get_config_path()
    defaults = {
        "poll_interval": 10.0,
        "hidden_devices": [],
        "hidden_device_names": {},
        "device_order": [],
        "device_uids": {}
    }
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
                defaults.update(data)
        except Exception as e:
            logging.error(f"Error loading config.json: {e}")
    return defaults

def save_config(cfg: dict):
    path = get_config_path()
    tmp_path = f"{path}.tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, path)
    except Exception as e:
        logging.error(f"Error saving config.json: {e}")
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass

# ---------------------------------------------------------
# Icon Rendering (Theme 1: Thick Ring + Centered Digits)
# ---------------------------------------------------------
_FONT_CACHE: Dict[int, ImageFont.ImageFont] = {}

def _get_icon_font(font_size: int) -> ImageFont.ImageFont:
    font = _FONT_CACHE.get(font_size)
    if font is None:
        try:
            font = ImageFont.truetype("segoeuib.ttf", font_size)
        except Exception:
            font = ImageFont.load_default()
        _FONT_CACHE[font_size] = font
    return font

def render_battery_icon(
    percent: Optional[int] = None,
    is_charging: bool = False,
    is_active: bool = True
) -> Image.Image:
    size = 256
    im = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(im)

    cx, cy = 128, 128
    r = 106
    w = 26
    box = [cx - r, cy - r, cx + r, cy + r]

    # Background subtle track ring
    draw.ellipse(box, outline=(255, 255, 255, 45), width=w)

    pct = max(0, min(100, percent)) if percent is not None else 0
    if percent is not None and percent > 0:
        if not is_active:
            color = (130, 130, 130, 180)  # Sleeping / inactive: dimmed gray
        elif pct <= 20:
            color = (255, 69, 58, 255)   # Warning Red
        elif pct <= 45:
            color = (255, 214, 10, 255)  # Amber / Yellow
        else:
            color = (48, 209, 88, 255)   # Vibrant Green

        start_angle = -90
        end_angle = -90 + (pct / 100.0) * 360
        draw.arc(box, start=start_angle, end=end_angle, fill=color, width=w)

        # Rounded end caps
        cap_r = w / 2.0
        for ang in (start_angle, end_angle):
            rad = math.radians(ang)
            draw.ellipse([cx + r * math.cos(rad) - cap_r, cy + r * math.sin(rad) - cap_r,
                          cx + r * math.cos(rad) + cap_r, cy + r * math.sin(rad) + cap_r], fill=color)

    # Number: large, bold, and mathematically centered in the circle
    text = "--" if percent is None else str(pct)
    font_size = 92 if len(text) >= 3 else 122
    font = _get_icon_font(font_size)

    bbox = draw.textbbox((0, 0), text, font=font)
    tx = cx - (bbox[0] + bbox[2]) / 2.0
    ty = cy - (bbox[1] + bbox[3]) / 2.0

    if is_charging:
        num_color = (255, 214, 10, 255)  # Yellow when charging
    elif not is_active:
        num_color = (140, 140, 140, 200)
    else:
        num_color = (255, 255, 255, 255)

    # High-contrast dark shadow
    for dx, dy in ((-2, -2), (2, -2), (-2, 2), (2, 2), (0, 2), (0, -2)):
        draw.text((tx + dx, ty + dy), text, font=font, fill=(0, 0, 0, 240))
    draw.text((tx, ty), text, font=font, fill=num_color)

    return im.resize((64, 64), Image.Resampling.LANCZOS)

def create_fallback_icon() -> Image.Image:
    return render_battery_icon(percent=None, is_charging=False, is_active=False)

# ---------------------------------------------------------
# Application Core
# ---------------------------------------------------------
def is_device_connected(d: Optional[DeviceBattery]) -> bool:
    if not d:
        return False
    if d.battery_percent is not None:
        return True
    status = (d.status_text or "").lower()
    for offline_word in ("disconnected", "sleeping", "inactive", "offline"):
        if offline_word in status:
            return False
    return status == "connected"

# hidapi (C library) is not thread-safe on Windows.
# Multiple concurrent calls to hid_enumerate / hid_open from different worker threads
# can corrupt the CRT heap (STATUS_HEAP_CORRUPTION 0xC0000374).
# We run all HID collectors sequentially in a single worker thread under a dedicated lock.
HID_LOCK = threading.Lock()

def _collect_hid_devices_safe(collectors: list) -> list:
    with HID_LOCK:
        devices = []
        for c in collectors:
            try:
                res = c.collect()
                if res and isinstance(res, list):
                    devices.extend(res)
            except Exception as e:
                logging.debug(f"Collector {c.__class__.__name__} error: {e}")
        return devices

class BatteryMonitorApp:
    def __init__(self):
        self.config = load_config()
        self.devices: Dict[str, DeviceBattery] = {}
        self.last_known_battery: Dict[str, int] = {}
        self.device_icons: Dict[str, pystray.Icon] = {}
        self.last_icon_state: Dict[str, Tuple] = {}
        self.fallback_icon: Optional[pystray.Icon] = None

        self.lock = threading.RLock()
        self.running = True
        self.force_refresh_event = threading.Event()
        self.poll_interval: float = float(self.config.get("poll_interval", 10.0))

        self.ble_collector = BLECollector()
        self.razer_collector = RazerHIDCollector()
        self.atk_collector = ATKHIDCollector()
        self.xinput_collector = XInputCollector()
        self.logitech_collector = LogitechHIDCollector()
        self.sony_collector = SonyHIDCollector()
        self.hyperx_collector = HyperXHIDCollector()
        self.steelseries_collector = SteelSeriesHIDCollector()
        self.corsair_collector = CorsairHIDCollector()
        self.asus_collector = ASUSHIDCollector()

        # Startup fallback icon with fixed uid 1
        self.fallback_icon = pystray.Icon(
            name=f"{APP_NAME}_fallback",
            icon=create_fallback_icon(),
            title="Battery Monitor: Scanning devices...",
            menu=self._build_menu_for_device(None)
        )
        self.fallback_icon.uid = 1
        self.fallback_icon.run_detached()
        threading.Timer(1.0, auto_promote_icons).start()

    def _save_config(self):
        self.config["poll_interval"] = self.poll_interval
        save_config(self.config)

    def _get_or_create_uid(self, dev_id: str) -> int:
        uids = self.config.setdefault("device_uids", {})
        if dev_id in uids:
            return int(uids[dev_id])
        used = set(int(v) for v in uids.values())
        cand = 101
        while cand in used:
            cand += 1
        uids[dev_id] = cand
        self._save_config()
        return cand

    def _set_poll_interval(self, sec: float):
        with self.lock:
            self.poll_interval = sec
            self._save_config()
            self.last_icon_state.clear()
        self.force_refresh_event.set()
        self._sync_icons()

    def _get_exe_path(self) -> str:
        if getattr(sys, "frozen", False):
            return f'"{sys.executable}"'
        return f'"{sys.executable}" "{os.path.abspath(__file__)}"'

    def is_startup_enabled(self) -> bool:
        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Run",
                0,
                winreg.KEY_READ
            ) as key:
                winreg.QueryValueEx(key, APP_NAME)
                return True
        except FileNotFoundError:
            return False
        except Exception:
            return False

    def toggle_startup(self, icon, item):
        key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_ALL_ACCESS) as key:
                if self.is_startup_enabled():
                    winreg.DeleteValue(key, APP_NAME)
                else:
                    winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, self._get_exe_path())
        except Exception as e:
            logging.error(f"Failed to toggle startup: {e}")
        with self.lock:
            self.last_icon_state.clear()
        self._sync_icons()

    def hide_device(self, dev_id: str):
        with self.lock:
            hidden_list = self.config.setdefault("hidden_devices", [])
            if dev_id not in hidden_list:
                hidden_list.append(dev_id)
            if dev_id in self.devices:
                self.config.setdefault("hidden_device_names", {})[dev_id] = self.devices[dev_id].name
            self._save_config()

            if dev_id in self.device_icons:
                try:
                    self.device_icons[dev_id].stop()
                except Exception:
                    pass
                del self.device_icons[dev_id]
                self.last_icon_state.pop(dev_id, None)

        self._sync_icons(force_update_menus=True)

    def unhide_device(self, dev_id: str):
        with self.lock:
            hidden_list = self.config.setdefault("hidden_devices", [])
            if dev_id in hidden_list:
                hidden_list.remove(dev_id)
            self._save_config()
            self.last_icon_state.clear()

        self._sync_icons(force_update_menus=True)

    def _build_menu_for_device(self, current_dev: Optional[DeviceBattery]) -> Menu:
        items = []

        with self.lock:
            all_devs = list(self.devices.values())
            hidden_set = set(self.config.get("hidden_devices", []))
            hidden_names = dict(self.config.get("hidden_device_names", {}))
            order = list(self.config.get("device_order", []))

            visible_devs = [d for d in all_devs if d.id not in hidden_set]
            visible_devs.sort(key=lambda d: order.index(d.id) if d.id in order else 9999)

        if current_dev and current_dev.id not in hidden_set:
            pct_val = current_dev.battery_percent
            if pct_val is None:
                last_val = self.last_known_battery.get(current_dev.id)
                pct_str = f"{current_dev.status_text} (Last: {last_val}%)" if last_val is not None else current_dev.status_text
            else:
                charge_mark = " (Charging)" if current_dev.is_charging else ""
                pct_str = f"{pct_val}%{charge_mark}"

            # Clean header with device name and battery percentage (Pure text, no emojis)
            items.append(MenuItem(f"{current_dev.name}: {pct_str}", None, enabled=False))
        else:
            if not visible_devs:
                items.append(MenuItem("Battery Monitor: No visible devices", None, enabled=False))
            else:
                items.append(MenuItem("Battery Monitor: Scanning...", None, enabled=False))

        items.append(Menu.SEPARATOR)

        # Hide Device Submenu (Lists all visible devices to hide on click)
        hide_items = []
        for vd in visible_devs:
            hide_items.append(
                MenuItem(
                    vd.name,
                    (lambda did: lambda icon, item: self.hide_device(did))(vd.id)
                )
            )

        if hide_items:
            items.append(MenuItem("Hide Device", Menu(*hide_items)))
        else:
            items.append(MenuItem("Hide Device", Menu(MenuItem("(None)", None, enabled=False))))

        # Hidden Devices Submenu (Only shows devices that are currently CONNECTED and HIDDEN)
        hidden_items = []
        for h_id in self.config.get("hidden_devices", []):
            dev = self.devices.get(h_id)
            if dev and is_device_connected(dev):
                h_name = dev.name or hidden_names.get(h_id, h_id)
                hidden_items.append(
                    MenuItem(
                        h_name,
                        (lambda did: lambda icon, item: self.unhide_device(did))(h_id)
                    )
                )

        if hidden_items:
            items.append(MenuItem("Hidden Devices", Menu(*hidden_items)))
        else:
            items.append(MenuItem("Hidden Devices", Menu(MenuItem("(None)", None, enabled=False))))

        items.append(Menu.SEPARATOR)
        items.append(MenuItem("Refresh Now", self.on_refresh_clicked))

        # Update Speed Submenu (Pure text)
        def make_speed_action(sec):
            return lambda icon, item: self._set_poll_interval(sec)

        def make_speed_check(sec):
            return lambda item: abs(self.poll_interval - sec) < 0.1

        speed_menu = Menu(
            MenuItem("Fast (2s)", make_speed_action(2.0), checked=make_speed_check(2.0), radio=True),
            MenuItem("Normal (10s)", make_speed_action(10.0), checked=make_speed_check(10.0), radio=True),
            MenuItem("Eco (60s)", make_speed_action(60.0), checked=make_speed_check(60.0), radio=True)
        )
        items.append(MenuItem("Update Speed", speed_menu))

        items.append(
            MenuItem(
                "Start with Windows",
                self.toggle_startup,
                checked=lambda item: self.is_startup_enabled()
            )
        )
        items.append(Menu.SEPARATOR)
        items.append(MenuItem("Exit", self.on_exit_clicked))

        return Menu(*items)

    def _sync_icons(self, force_update_menus: bool = False):
        with self.lock:
            all_devs = list(self.devices.values())
            hidden_set = set(self.config.get("hidden_devices", []))
            config_changed = False

            # Store friendly names
            names_map = self.config.setdefault("hidden_device_names", {})
            for d in all_devs:
                if names_map.get(d.id) != d.name:
                    names_map[d.id] = d.name
                    config_changed = True

            # Maintain deterministic device_order
            order = self.config.setdefault("device_order", [])
            for d in all_devs:
                if d.id not in order:
                    order.append(d.id)
                    config_changed = True

            if config_changed:
                self._save_config()

            visible_devs = [d for d in all_devs if d.id not in hidden_set]
            visible_devs.sort(key=lambda d: order.index(d.id) if d.id in order else 9999)
            active_devs = {d.id: d for d in visible_devs}
            connected_hidden_ids = tuple(
                sorted(
                    h_id for h_id in hidden_set
                    if is_device_connected(self.devices.get(h_id))
                )
            )

            if not active_devs:
                for dev_id, icon in list(self.device_icons.items()):
                    try:
                        icon.stop()
                    except Exception:
                        pass
                self.device_icons.clear()
                self.last_icon_state.clear()

                fallback_state = (
                    self.poll_interval,
                    self.is_startup_enabled(),
                    tuple(sorted(hidden_set)),
                    connected_hidden_ids
                )

                if not self.fallback_icon:
                    fallback_title = "Battery Monitor: All devices hidden" if hidden_set else "Battery Monitor: Scanning for devices..."
                    self.fallback_icon = pystray.Icon(
                        name=f"{APP_NAME}_fallback",
                        icon=create_fallback_icon(),
                        title=fallback_title,
                        menu=self._build_menu_for_device(None)
                    )
                    self.fallback_icon.uid = 1
                    self.fallback_icon.run_detached()
                    self._last_fallback_state = fallback_state
                elif force_update_menus or getattr(self, "_last_fallback_state", None) != fallback_state:
                    self._last_fallback_state = fallback_state
                    self.fallback_icon.menu = self._build_menu_for_device(None)
                    self.fallback_icon.update_menu()
                return

            if self.fallback_icon:
                try:
                    self.fallback_icon.stop()
                except Exception:
                    pass
                self.fallback_icon = None

            # Remove disconnected or hidden devices
            removed_ids = [did for did in list(self.device_icons.keys()) if did not in active_devs]
            for did in removed_ids:
                try:
                    self.device_icons[did].stop()
                except Exception:
                    pass
                del self.device_icons[did]
                self.last_icon_state.pop(did, None)

            # Update or create icons in the sequence of visible_devs
            for dev in visible_devs:
                dev_id = dev.id
                pct = dev.battery_percent
                is_active = (pct is not None)
                display_pct = pct if pct is not None else self.last_known_battery.get(dev_id)

                current_state = (
                    display_pct,
                    dev.is_charging,
                    is_active,
                    self.poll_interval,
                    self.is_startup_enabled(),
                    tuple(sorted(hidden_set)),
                    tuple(d.id for d in visible_devs),
                    connected_hidden_ids
                )

                if not force_update_menus and dev_id in self.device_icons and self.last_icon_state.get(dev_id) == current_state:
                    continue

                self.last_icon_state[dev_id] = current_state

                img = render_battery_icon(
                    percent=display_pct,
                    is_charging=dev.is_charging,
                    is_active=is_active
                )

                if pct is not None:
                    chg = " (Charging)" if dev.is_charging else ""
                    title = f"{dev.name}: {pct}%{chg} [{dev.protocol}]"
                else:
                    last_val = self.last_known_battery.get(dev_id)
                    last_str = f" (Last: {last_val}%)" if last_val is not None else ""
                    title = f"{dev.name}: {dev.status_text}{last_str} [{dev.protocol}]"
                title = title[:126]

                if dev_id in self.device_icons:
                    icon = self.device_icons[dev_id]
                    icon.icon = img
                    icon.title = title
                    icon.menu = self._build_menu_for_device(dev)
                    icon.update_menu()
                else:
                    icon = pystray.Icon(
                        name=f"{APP_NAME}_{dev_id}",
                        icon=img,
                        title=title,
                        menu=self._build_menu_for_device(dev)
                    )
                    icon.uid = self._get_or_create_uid(dev_id)
                    icon.run_detached()
                    self.device_icons[dev_id] = icon
                    threading.Timer(1.0, auto_promote_icons).start()

    async def _poll_once(self):
        new_devices: Dict[str, DeviceBattery] = {}

        try:
            ble_devs = await self.ble_collector.collect()
            for d in ble_devs:
                new_devices[d.id] = d
        except Exception as e:
            logging.debug(f"BLE poll error: {e}")

        loop = asyncio.get_running_loop()
        hid_collectors = [
            self.razer_collector,
            self.atk_collector,
            self.logitech_collector,
            self.sony_collector,
            self.hyperx_collector,
            self.steelseries_collector,
            self.corsair_collector,
            self.asus_collector,
        ]

        # XInput uses DirectX COM API (not hidapi) and is safe to run alongside
        xinput_task = loop.run_in_executor(None, self.xinput_collector.collect)
        # All HID collectors run sequentially in a single worker thread to guarantee 100% C-heap safety
        hid_task = loop.run_in_executor(None, _collect_hid_devices_safe, hid_collectors)

        xinput_res, hid_res = await asyncio.gather(xinput_task, hid_task, return_exceptions=True)

        if isinstance(xinput_res, list):
            for d in xinput_res:
                new_devices[d.id] = d
        if isinstance(hid_res, list):
            for d in hid_res:
                new_devices[d.id] = d

        with self.lock:
            self.devices = new_devices
            for dev_id, d in new_devices.items():
                if d.battery_percent is not None:
                    self.last_known_battery[dev_id] = d.battery_percent

        self._sync_icons()

    def _worker_loop(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        while self.running:
            try:
                loop.run_until_complete(self._poll_once())
            except Exception as e:
                logging.error(f"Polling loop error: {e}")

            self.force_refresh_event.wait(timeout=self.poll_interval)
            self.force_refresh_event.clear()

        loop.close()

    def on_refresh_clicked(self, icon, item):
        with self.lock:
            self.last_icon_state.clear()
        self.force_refresh_event.set()

    def on_exit_clicked(self, icon, item):
        self.running = False
        self.force_refresh_event.set()
        with self.lock:
            for i in self.device_icons.values():
                try:
                    i.stop()
                except Exception:
                    pass
            self.device_icons.clear()
            if self.fallback_icon:
                try:
                    self.fallback_icon.stop()
                except Exception:
                    pass
                self.fallback_icon = None
        threading.Thread(target=lambda: (time.sleep(0.3), os._exit(0)), daemon=True).start()

    def run(self):
        worker = threading.Thread(target=self._worker_loop, daemon=True)
        worker.start()

        while self.running:
            try:
                time.sleep(0.5)
            except KeyboardInterrupt:
                break
        self.on_exit_clicked(None, None)

_SINGLE_INSTANCE_MUTEX = None

def ensure_single_instance() -> bool:
    global _SINGLE_INSTANCE_MUTEX
    mutex_name = r"Local\TrayBatteryMonitor_SingleInstance_Mutex"
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    ctypes.set_last_error(0)
    _SINGLE_INSTANCE_MUTEX = kernel32.CreateMutexW(None, False, mutex_name)
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        logging.warning("Another instance of TrayBatteryMonitor is already running. Exiting.")
        return False
    return True

if __name__ == "__main__":
    if not ensure_single_instance():
        sys.exit(0)
    app = BatteryMonitorApp()
    app.run()
