# TrayBatteryMonitor 🔋

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Platform: Windows 11](https://img.shields.io/badge/Platform-Windows%2011%2064--bit-0078D6.svg)](https://microsoft.com/windows)
[![Python: 3.13+](https://img.shields.io/badge/Python-3.13+-3776AB.svg?logo=python&logoColor=white)](https://python.org)

極致輕量、低資源佔用（**CPU ~0%、記憶體極低**）的 Windows 11 原生系統托盤（System Tray）多設備電量即時監控工具。

告別臃腫的各大原廠驅動程式（如 Razer Synapse、Armoury Crate、iCUE 等），直接在 Windows 11 工作列右下角托盤以最優雅、直覺的方式檢視所有無線周邊設備的電量與充電狀態。

---

## ✨ 核心特色 (Key Features)

- **嚴格原生極簡**：無任何桌面懸浮窗、彈窗或多餘 UI，純靠 Windows 11 工作列托盤圖示與右鍵選單。
- **純文字選單風格**：右鍵選單嚴格禁止 Emoji，保持簡潔俐落的 Windows 11 原生桌面風貌。
- **Theme 1 專屬視覺**：超粗外圈進度圓環 + 正中心超大數字；充電中文字反黃、低電量紅圈警示、離線顯示 `--`。
- **Windows 11 拖曳排序修復**：底層 Monkey-patch 攔截注入獨立唯一的 `uID`，徹底解決 Windows 11 托盤拖曳倒置的系統 Bug。
- **全自動工作列釘選 (Auto-Promoted)**：自動掃描系統登錄檔提拔新圖示，再也不用手動將圖示從 `^` 溢出摺疊區拖出來。
- **動態設備屏蔽機制 (Dynamic Hide/Unhide)**：
  - 點擊選單即可隨時隱藏任意設備。
  - **斷連自動隱形**：關機或斷開連線的設備不會佔用 `Hidden Devices` 子清單，只有在連線時才動態出現。
- **單一實例防重複啟動 (Single Instance Mutex)**：採用 Windows 原生具名互斥鎖，重複點擊 exe 0.9 秒內靜默自動退出，防止多開。
- **開機自動啟動 (Start with Windows)**：整合 `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`，免 UAC 提權，登入即啟動。

---

## 🎮 支援設備體系 (Supported Devices)

本工具採用**完全解耦的模組化收集器 (Collectors)** 架構，涵蓋 PC 主流無線外設：

| 設備類別 / 品牌 | 收集器模組 | 支援協定與代表設備 |
| :--- | :--- | :--- |
| **通用藍牙設備** | `ble_collector.py` | Windows 原生 WinRT GATT Standard Battery Service (`0x180F`)。支援絕大多數客製化三模藍牙鍵盤、藍牙滑鼠、藍牙手柄等。 |
| **微軟 Xbox** | `xinput_collector.py` | Windows 原生 `XInput1_4.dll`。支援 Xbox One、Series X\|S、Elite 手柄。 |
| **雷蛇 Razer 2.4G** | `razer_hid.py` | Razer 專屬 2.4G HID Feature Report (`0x07` Command Class)。支援毒蝰 (Viper)、巴蛇 (Basilisk)、煉獄蝰蛇 (DeathAdder) 等。 |
| **羅技 Logitech 2.4G** | `logitech_hid.py` | Logitech Lightspeed / Unifying HID++ 1.0 & 2.0 協定。支援 G502 系列、GPW、Superlight 1/2、G304、G703 等（自動過濾有線設備）。 |
| **SONY PlayStation** | `sony_hid.py` | Sony 官方標準 HID 協定。支援 PS5 DualSense、DualSense Edge、PS4 DualShock 4（藍牙 / USB）。 |
| **ATK / VXE / 蜻蜓** | `atk_hid.py` | Nordic 52840 & 54L15 雙世代專屬 HID 協定。支援 ATK A9 系列 (A9 SE / Ultimate)、烈空 F1 / X1、Zero、VXE R1、MAD R 等 2.4G 滑鼠（支援無線 / 有線即時充電狀態識別）。 |
| **HyperX 2.4G 耳機** | `hyperx_hid.py` | HyperX 專屬 HID 遙測。支援 Cloud II Wireless、Cloud Alpha Wireless (300h)、Cloud Flight 等。 |
| **SteelSeries 賽睿** | `steelseries_hid.py` | 賽睿 2.4G HID Feature Report。支援 Aerox 3/5/9、Rival 3/650、Prime、Arctis/Nova 系列耳機。 |
| **Corsair 海盜船** | `corsair_hid.py` | Corsair Slipstream 2.4G 協定。支援 Dark Core、Harpoon、HS70/HS80、Virtuoso 等。 |
| **ASUS ROG 華碩** | `asus_hid.py` | ROG 2.4G Feature Report。支援 Harpe Ace (龍鱗)、Keris (月刃)、Gladius III、Azoth (夜魔鍵盤) 等。 |

---

## 🛠️ 專案結構 (Architecture)

```text
TrayBatteryMonitor/
├── collectors/                # 獨立設備收集器模組 (完全解耦)
│   ├── __init__.py
│   ├── models.py              # DeviceBattery 資料模型
│   ├── ble_collector.py       # WinRT 藍牙 BLE
│   ├── razer_hid.py           # 雷蛇 2.4G
│   ├── logitech_hid.py        # 羅技 Lightspeed / HID++
│   ├── sony_hid.py            # SONY DualSense / DualShock
│   ├── atk_hid.py             # ATK / VXE
│   ├── hyperx_hid.py          # HyperX 2.4G 耳機
│   ├── steelseries_hid.py     # 賽睿 2.4G
│   ├── corsair_hid.py         # 海盜船 2.4G
│   ├── asus_hid.py            # 華碩 ROG 2.4G
│   └── xinput_collector.py    # Xbox XInput
├── dist/
│   ├── TrayBatteryMonitor.exe # 打包編譯成品 (獨立無黑視窗背景執行)
│   └── config.json            # 執行時期永久記憶設定
├── config.example.json        # 預設設定檔範本
├── main.py                    # 主程式核心、托盤管理、繪圖、自動釘選引擎
├── requirements.txt           # 最小依賴清單
├── TrayBatteryMonitor.spec    # PyInstaller 打包規格
├── LICENSE                    # MIT License
└── README.md
```

---

## 🚀 快速上手 (Quick Start)

### 直接執行
下載並執行 `dist/TrayBatteryMonitor.exe` 即可（無任何安裝程序，綠色免安裝）。

### 從原始碼運行與自行編譯

1. **複製專案庫**：
   ```bash
   git clone https://github.com/huangzz02/TrayBatteryMonitor.git
   cd TrayBatteryMonitor
   ```

2. **安裝依賴套件**：
   ```bash
   pip install -r requirements.txt
   ```

3. **直接以 Python 執行**：
   ```bash
   python main.py
   ```

4. **使用 PyInstaller 打包為單一乾淨 EXE**：
   ```bash
   pyinstaller --clean TrayBatteryMonitor.spec
   ```
   打包完成後的執行檔將輸出於 `dist/TrayBatteryMonitor.exe`。

---

## ⚙️ 右鍵選單操作說明 (Menu Guide)

```text
[設備名稱]: [電量]% [(Charging)]
-----------------------------
Hide Device      >  [列出所有當前在線設備，點擊即可隱藏]
Hidden Devices   >  [僅列出當前在線且被隱藏的設備，點擊解封]
-----------------------------
Refresh Now         [手動強制立即刷新一次]
Update Speed     >  Fast (2s) / Normal (10s) / Eco (60s)
Start with Windows  [開機自動啟動開關]
-----------------------------
Exit                [退出程式]
```

---

## 📄 授權條款 (License)

本專案基於 [MIT License](LICENSE) 條款開源。
