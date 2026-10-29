import ctypes
from ctypes import wintypes
import threading
import time
from dataclasses import dataclass
from typing import Optional, Tuple

# Virtual-Key codes for all standard Windows mouse buttons
_MOUSE_VKS = (0x01, 0x02, 0x04, 0x05, 0x06)


class _MouseActivityTracker:
    """
    Tracks physical mouse cursor movement and mouse button presses via Win32 APIs.
    Unlike GetLastInputInfo, keyboard typing does NOT reset this timer, allowing
    wireless mice to enter hardware sleep normally while the user types.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._last_pos: Optional[Tuple[int, int]] = None
        self._last_active_mono: float = -1e9
        self._user32 = ctypes.windll.user32

    def sample(self) -> Tuple[float, float]:
        """
        Sample current mouse cursor position and button states.
        Returns: (idle_seconds, last_active_monotonic)
        """
        now = time.monotonic()
        pt = wintypes.POINT()
        pos_ok = bool(self._user32.GetCursorPos(ctypes.byref(pt)))
        cur_pos = (pt.x, pt.y) if pos_ok else None

        btn_down = any(
            bool(self._user32.GetAsyncKeyState(vk) & 0x8000)
            for vk in _MOUSE_VKS
        )

        with self._lock:
            if self._last_pos is None:
                self._last_pos = cur_pos
                if btn_down:
                    self._last_active_mono = now
            else:
                if (cur_pos is not None and cur_pos != self._last_pos) or btn_down:
                    self._last_active_mono = now
                if cur_pos is not None:
                    self._last_pos = cur_pos

            last_act = self._last_active_mono

        idle_sec = max(0.0, now - last_act)
        return idle_sec, last_act


_TRACKER = _MouseActivityTracker()
_REFRESH_LOCK = threading.Lock()
_REFRESH_GEN: int = 1


def sample_mouse_activity() -> Tuple[float, float]:
    """Return (mouse_idle_seconds, last_mouse_active_monotonic)."""
    return _TRACKER.sample()


def trigger_manual_refresh() -> None:
    """Increment global manual refresh generation so all collectors force a fresh read once."""
    global _REFRESH_GEN
    with _REFRESH_LOCK:
        _REFRESH_GEN += 1


def get_refresh_generation() -> int:
    with _REFRESH_LOCK:
        return _REFRESH_GEN


@dataclass
class WirelessMouseState:
    percent: Optional[int] = None
    is_charging: bool = False
    is_wired: bool = False
    is_sleeping: bool = False
    sleep_timeout_sec: float = 60.0
    last_rf_poll_mono: float = 0.0
    last_wake_check_mono: float = -1e9
    sleep_confirmed_mono: float = -1e9
    last_dongle_check_mono: float = -1e9
    last_refresh_gen: int = 0
    name: str = ""


def decide_mouse_rf_action(
    state: WirelessMouseState,
    active_interval: float = 30.0,
    charging_interval: float = 30.0,
    sleep_buffer_sec: float = 5.0,
) -> str:
    """
    Determine whether a 2.4G wireless mouse collector should send RF packets on this tick.

    Returns one of:
      - "poll_full": Perform live status + battery RF query.
      - "check_sleep_only": Stationary silence exceeded sleep timeout; do one sleep check.
      - "skip_use_cache": Suppress RF traffic to protect sleep timer / avoid RF jitter; use cached battery.
      - "skip_sleeping": Device is confirmed sleeping and mouse has not moved; suppress all RF traffic.
    """
    now = time.monotonic()
    cur_gen = get_refresh_generation()
    idle_sec, last_active_mono = sample_mouse_activity()

    # 1. Initial discovery or explicit "Refresh Now" click
    if state.last_refresh_gen != cur_gen or state.last_rf_poll_mono <= 0.0:
        return "poll_full"

    # 2. Currently confirmed sleeping
    if state.is_sleeping:
        # Wake check if physical mouse activity occurred after sleep was confirmed
        # and after our most recent wake probe (works across 2s, 10s, and 60s poll intervals)
        if last_active_mono > state.sleep_confirmed_mono and last_active_mono > state.last_wake_check_mono:
            if state.last_wake_check_mono < state.sleep_confirmed_mono or (now - state.last_wake_check_mono) >= 15.0:
                return "poll_full"
        return "skip_sleeping"

    # 3. Currently charging: sleep protection is unnecessary; poll gently to track % climb
    if state.is_charging:
        if (now - state.last_rf_poll_mono) >= charging_interval:
            return "poll_full"
        if idle_sec >= 1.5 and state.last_rf_poll_mono < last_active_mono:
            return "poll_full"
        return "skip_use_cache"

    # 4. Awake and user is actively moving/clicking the mouse right now
    if idle_sec < 1.5:
        if state.percent is None or (now - state.last_rf_poll_mono) >= active_interval:
            return "poll_full"
        return "skip_use_cache"

    # 5. Just stopped moving: allow ONE settle check after movement ends (catches dock / wall-charger
    #    placement within 1 poll tick across 2s/10s poll intervals), then go 100% RF-silent
    settle_window = min(12.0, max(4.5, state.sleep_timeout_sec * 0.4))
    if idle_sec < settle_window and (state.percent is None or state.last_rf_poll_mono < last_active_mono):
        return "poll_full"

    # 6. Mouse is stationary (idle >= 4.5s) and discharging:
    #    CRITICAL: Send ZERO RF packets until full hardware sleep timeout + buffer has elapsed
    #    since BOTH last physical input AND our last RF packet.
    silence_sec = now - max(last_active_mono, state.last_rf_poll_mono)
    required_silence = max(15.0, state.sleep_timeout_sec) + sleep_buffer_sec
    if silence_sec >= required_silence:
        return "check_sleep_only"

    if state.percent is not None:
        return "skip_use_cache"
    return "poll_full"
