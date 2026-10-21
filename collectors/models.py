from dataclasses import dataclass
from typing import Optional
import time

@dataclass
class DeviceBattery:
    id: str
    name: str
    protocol: str
    battery_percent: Optional[int]
    status_text: str = "Connected"
    is_charging: bool = False
    last_updated: float = 0.0

    def __post_init__(self):
        if self.last_updated == 0.0:
            self.last_updated = time.time()

    @property
    def display_str(self) -> str:
        if self.battery_percent is not None:
            charge_mark = "(Charging)" if self.is_charging else ""
            return f"[{self.protocol}] {self.name}: {self.battery_percent}% {charge_mark}".strip()
        return f"[{self.protocol}] {self.name}: {self.status_text}"
