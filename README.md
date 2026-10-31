# Raspberry Pi Zero 2 W + Ubuntu 22.04 + ROS2 Humble 設定筆記

Zero 2 W 搭配 DFRobot 馬達驅動板、UPS-Lite、Waveshare USB HUB HAT 與 ESP32-S3 相機板的設定紀錄，
包含踩到的坑（尤其是 **Ubuntu 上 USB HUB HAT 認不到**）。

## 硬體

| 裝置 | 介面 | 位址 / 節點 |
|---|---|---|
| Raspberry Pi Zero 2 W Rev 1.0 | — | 4 核、414 MB RAM |
| DFRobot DC Motor Driver HAT（DFR0592） | I2C | `0x10` |
| SparkFun Servo pHAT（PCA9685），鏡頭雲台 | I2C | `0x40`（+ All-Call `0x70`） |
| UPS-Lite v1.3（CW2015 電量計） | I2C + GPIO4 | `0x62` |
| Waveshare USB HUB HAT（Hub：FE1.1s `1a40:0101`，序列埠：CP2102） | USB / UART | 4 口 Hub |
| ESP32-S3 相機板（CH340 `1a86:7523`，OV3660） | USB（接 HUB HAT） | `/dev/ttyUSB0` |

## 系統

- Ubuntu 22.04.5 LTS（Server，arm64），核心 5.15.0-raspi
- 網路：WiFi，netplan / cloud-init（不是 NetworkManager）
- 使用者 `pi`，主機名 `pi`

## 1. SSH 金鑰登入

在 Pi 上加入電腦的公鑰（用 `printf`，避免從 Windows 複製時帶進 UTF-8 BOM 或 `\r`，否則 sshd 會忽略那行）：

```bash
mkdir -p ~/.ssh && chmod 700 ~/.ssh
printf '%s\n' 'ssh-ed25519 AAAA... your-key-comment' >> ~/.ssh/authorized_keys
chmod 600 ~/.ssh/authorized_keys
wc -c ~/.ssh/authorized_keys   # 長度應等於公鑰字元數 + 1
```

## 2. sudo 免密碼（方便遠端管理）

```bash
echo 'pi ALL=(ALL) NOPASSWD:ALL' | sudo tee /etc/sudoers.d/010-pi-nopasswd
sudo chmod 440 /etc/sudoers.d/010-pi-nopasswd
sudo visudo -c
```

## 3. 2 GB swap（414 MB RAM 一定要加）

```bash
sudo fallocate -l 2G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
echo 'vm.swappiness=10' | sudo tee /etc/sysctl.d/99-swappiness.conf
sudo sysctl -p /etc/sysctl.d/99-swappiness.conf
```

## 4. ROS2 Humble（ros-base）

用官方 `ros2-apt-source` 套件加入來源（舊的 `ros.key` 方法已淘汰）：

```bash
VER=1.3.0   # 最新版見 https://github.com/ros-infrastructure/ros-apt-source/releases
curl -fsSL -o /tmp/ros2-apt-source.deb \
  https://github.com/ros-infrastructure/ros-apt-source/releases/download/$VER/ros2-apt-source_$VER.jammy_all.deb
sudo apt install -y /tmp/ros2-apt-source.deb
sudo apt update && sudo apt upgrade -y          # 官方要求先升級，避免 systemd/udev 衝突
sudo apt install -y ros-humble-ros-base ros-dev-tools
echo 'source /opt/ros/humble/setup.bash' >> ~/.bashrc
sudo rosdep init && rosdep update
```

- 在 Zero 2 W 上：系統升級 253 個套件 + ROS2 481 個套件，**約 55 分鐘**
- 建議用 `systemd-run` 在背景跑，SSH 斷線也不會中斷：
  ```bash
  sudo systemd-run --unit=ros-humble-install --setenv=DEBIAN_FRONTEND=noninteractive \
    /bin/bash -c "apt-get update && apt-get -y upgrade && apt-get -y install ros-humble-ros-base ros-dev-tools > /var/log/ros-install.log 2>&1"
  ```
- `ros-base` 沒有 demo nodes，用 topic 測試：
  ```bash
  ros2 topic pub -r 2 /chatter std_msgs/msg/String "{data: hello}" &
  ros2 topic echo --once /chatter
  ```

## 5. ⚠️ USB HUB HAT 在 Ubuntu 上認不到 → 改 USB 主機模式

**症狀**：照 Waveshare Wiki 接好，`lsusb` 只有 root hub，HAT 上的裝置都不出現。

**原因**：Ubuntu 的 `/boot/firmware/config.txt` 在 `[all]` 區段有 `dtoverlay=dwc2`，USB 控制器是 **OTG 模式**
（`/proc/device-tree/soc/usb@7e980000/dr_mode` = `otg`，且有 `/sys/class/udc/3f980000.usb`）。
Zero 會依 ID 腳判斷當主機還是裝置，接 HUB HAT 時被當成裝置 → 不會列舉 Hub。
Waveshare 的教學是用 Raspberry Pi OS，那邊預設是主機模式，所以沒寫這一步。

**解法**：

```bash
sudo cp /boot/firmware/config.txt /boot/firmware/config.txt.bak
sudo sed -i '0,/^dtoverlay=dwc2$/s//dtoverlay=dwc2,dr_mode=host/' /boot/firmware/config.txt
sudo reboot
```

改完後：

```
lsusb -t
/:  Bus 01.Port 1: Dev 1, Class=root_hub, Driver=dwc2/1p, 480M
    |__ Port 1: Dev 2, If 0, Class=Hub, Driver=hub/4p, 480M
        |__ Port 1: Dev 3, If 0, Class=Vendor Specific Class, Driver=ch341, 12M   ← ESP32-S3
```

代價：Zero 的 USB 口不能再當 USB gadget（例如 USB 網路卡）。

**接線**（HAT 有三個 USB 介面）：

| HAT 介面 | 接到 |
|---|---|
| USB HUB（Micro USB） | Zero 2 W 的 **USB** 口（靠中間那個，不是 PWR），要用**能傳資料的線** |
| USB1～USB4（USB-A） | ESP32 等 USB 裝置 |
| USB to UART（Micro USB） | 電腦，序列埠登入用 |

## 6. USB HUB HAT 序列埠登入

- HAT 背面兩個開關都撥到 **ON**，才會把 Pi 的 GPIO14/15 接到 CP2102（OFF 時電腦會有 COM 埠，但收不到任何資料）
- Ubuntu 已預設開啟序列埠 console（`console=serial0,115200`，`serial0 → ttyS0`）
- 電腦端：CP210x 的 COM 埠，**115200 8N1**，按 Enter 會看到 `pi login:`

## 7. I2C 裝置

I2C 已在 `config.txt` 開啟（`dtparam=i2c_arm=on`）。沒有 i2c-tools 時可用 `tools/i2c_scan.py`：

```bash
sudo python3 tools/i2c_scan.py      # /dev/i2c-1: 0x10 0x62
```

### DFRobot DC Motor Driver HAT（0x10）

- 官方函式庫：<https://github.com/DFRobot/DFRobot_RaspberryPi_Motor>
- 產品 ID 暫存器 `0x01` 應為 `0xDF`；2 路直流馬達，支援編碼器
- `sudo python3 tools/motor_hat_info.py`（唯讀檢查）

### UPS-Lite v1.3（0x62，CW2015）

- 官方程式：<https://github.com/linshuqin329/UPS-Lite/tree/master/UPS-Lite_V1.3_CW2015>
- **CW2015 開機後在休眠**（MODE `0x0A` = `0xC0`），電壓/電量都讀 0，要先喚醒
- ⚠️ 喚醒要用 **quick start 脈衝：MODE 寫 `0x30` 再寫回 `0x00`**。官方程式只寫 `0x30` 不清掉，
  MODE 會停在 `0x30`，電量會一直是 0% 或停在舊值（實測 3.94 V 卻顯示 0%；改用脈衝後 64%）
- 這片沒有載入電池參數（CONFIG `0x08` 的 UFG = 0），電量是晶片內建模型估算，僅供參考
- 電壓 = VCELL × 0.305 mV；電量 = SOC 高位元組 + 低位元組/256
- GPIO4 = 1 表示外接電源插著
- `sudo python3 tools/ups_lite.py`（加 `--watch` 每 5 秒更新）
- 注意：充電中量到的是充電電壓，會比電池實際電壓高

## 8. ESP32-S3 相機板

- 接在 HUB HAT 的 USB1，CH340 → `/dev/ttyUSB0`（`pi` 已在 `dialout` 群組，不需 sudo）
- 重置並看開機訊息：`python3 tools/esp32_boot_log.py`
  ```
  ESP-ROM:esp32s3-20210327
  rst:0x1 (POWERON),boot:0x8 (SPI_FAST_FLASH_BOOT)
  E (219) esp_core_dump_flash: Incorrect size of core dump image   ← 無害，core dump 分區是空的
  相機 OK, PID = 0x3660                                              ← OV3660
  ```

## 8.5 CSI 攝影機（OV5647）

- 偵測：`ov5647 10-0036` + `bcm2835_unicam` → `/dev/video0`（`vcgencmd get_camera` 的 `detected=0` 是舊相機堆疊，不用理會）
- Ubuntu 22.04 的 libcamera 是 2020 年版本，不採用；只裝 `v4l-utils` 抓 10-bit 原始 Bayer（GBRG）
- `tools/camera_snapshot.py`：軟體自動曝光 + 半解析度解馬賽克 + 灰世界白平衡 + gamma，輸出 JPEG
  ```bash
  sudo apt install -y v4l-utils
  python3 tools/camera_snapshot.py photo.jpg 2592 1944   # 全幅，輸出 1296x972，約 9 秒、110 MB RAM
  python3 tools/camera_snapshot.py photo.jpg 1296 972    # 輸出 648x486，約 3.5 秒
  ```
- 感光模式：2592×1944、1920×1080（裁切）、1296×972（2×2 合併、全視角）、640×480
- CMA 只有 64 MB，所以只用 2 個影像緩衝區

## 8.6 顏色辨識 ROS2 節點（color_vision）

OpenCV 用 pip 裝（apt 的 `python3-opencv` 要 175 個套件）：

```bash
sudo apt install -y python3-pip v4l-utils
pip3 install --user --no-deps opencv-python-headless==4.10.0.84
```

- `ov5647_camera.py`：OpenCV 讀 10-bit packed Bayer，每 5 bytes 取前 4 bytes 就是 8-bit 原始影像，再用 OpenCV 解馬賽克；軟體自動曝光 + 灰世界白平衡；640×480 約 29 FPS
- `traffic_light.py`：HSV 找紅/黃/綠、夠圓、夠亮的區塊。`detect()` 給亮燈用；`detect_objects()` 給色塊、標誌用（放寬飽和度/亮度，用凸包判斷形狀）
- 節點 `ros2 run color_vision color_detector`（約 20 FPS）

| Topic | 型別 | 內容 |
|---|---|---|
| `/traffic_light/state` | `std_msgs/String` | `red` / `yellow` / `green` / `none`（連續 3 張相同才切換） |
| `/color_detections` | `std_msgs/String` | JSON：`[{"color","x","y","w","h","score"}]`，原始解析度座標，分數高的在前 |
| `/color_vision/image/compressed` | `sensor_msgs/CompressedImage` | 標註後的 JPEG（`publish_image:=true`，預設 2 Hz） |

開機自動啟動：`systemd/color-vision.service`（`User=pi`，預設不發布影像）

```bash
sudo cp systemd/color-vision.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now color-vision
sudo journalctl -u color-vision -f
```

實測：開機約 1 分鐘後開始發布，三個服務（color-vision、v7rc-car、v7rc-ble）同時執行時約 21 FPS，可用記憶體約 107 MB

參數：`mode`（`objects` 預設／`lamp`）、`width`/`height`、`detect_scale`（0.5）、`stable_frames`（3）、`min_score`（5）、`publish_image`、`image_rate`

### AprilTag 指令標記

同一個節點也偵測 **AprilTag 36h11** 標記方塊（OpenCV `cv2.aruco`，免另外安裝）：

| Tag ID | 指令 |
|---|---|
| 1 | RIGHT 右轉 |
| 2 | SLOW 減速 |
| 3 | FAST 加速 |
| 4 | LEFT 左轉 |

| Topic | 內容 |
|---|---|
| `/tag_command` | 看到標記時發布一次指令（離開畫面 2 秒後再出現才會再發布） |
| `/tag_detections` | JSON：`[{"id","command","cx","cy","size"}]`，`size` 越大代表越近 |

參數：`enable_tags`、`tag_family`（`DICT_APRILTAG_36h11`）、`tag_commands`（`["1:RIGHT","2:SLOW","3:FAST","4:LEFT"]`）、`tag_rate`（5 Hz）、`tag_repeat_after`（2 秒）

實測（640×480）：
- 標記約 60 px 寬時，10/10 張畫面都讀到；每次偵測約 104 ms（單執行緒）
- QR Code 每張約 135 ms，且不容易斜看，所以改用 AprilTag
- OpenCV 預設多執行緒在 Zero 2W 上反而浪費：4 執行緒 153 ms CPU vs 1 執行緒 104 ms，節點裡用 `cv2.setNumThreads(1)`
- 顏色 + AprilTag 一起跑：CPU 約 177%、顏色 20 FPS、標記 5 Hz

注意：
- `package.xml` 的 XML 註解裡不能有 `--`，否則 colcon 解析失敗、套件不會註冊到 ROS2（`Package not found`）
- Zero 2W 上 `ros2 topic echo` 啟動約 7 秒，用 `timeout` 測試要給足時間
- 實體紅綠燈模型的燈罩本身有顏色，沒亮的燈也會被當成候選；`lamp` 模式還需要比較亮度才能分出哪顆亮著

## 8.7 鏡頭雲台（SparkFun Servo pHAT，PCA9685）

- I2C：`0x40` = PCA9685（7 個暫存器開機時都是資料手冊預設值，MODE1 `0x11` 休眠、PRE_SCALE `0x1E` ≈ 197 Hz）；`0x70` = PCA9685 的 All-Call 位址，不是另一個裝置
- 伺服馬達用 50 Hz：PRE_SCALE = round(25 MHz / (4096 × 50)) − 1 = **121**，只能在休眠時寫入
- 通道 0 = 水平（1.8 ms 往左），通道 1 = 垂直（1.2 ms 往上）
- 伺服馬達電源要另外接（pHAT 的 USB-C），不要只靠 Pi 的 5V

ROS2 節點 `ros2 run pan_tilt pan_tilt_node`：

| Topic | 型別 | 說明 |
|---|---|---|
| `/pan_tilt/cmd` | `geometry_msgs/Vector3` | `x` = 水平、`y` = 垂直（度，正值 = 左 / 上） |
| `/pan_tilt/state` | `geometry_msgs/Vector3` | 目前角度（10 Hz） |

```bash
ros2 topic pub --once /pan_tilt/cmd geometry_msgs/msg/Vector3 "{x: 30.0, y: 10.0}"
```

參數：`pan_min`/`pan_max`（±45°）、`tilt_min`/`tilt_max`（±30°）、`max_speed`（90°/s）、`us_per_degree`（11.1）、`pan_center_us`/`tilt_center_us`（1500）、`pan_channel`/`tilt_channel`

- 啟動時回中間，結束時放鬆伺服馬達（PWM full OFF）
- 實測：水平 ±30°、垂直 ±20° 方向與幅度正確
- 開機自動啟動：`systemd/pan-tilt.service`

### ⚠️ Servo pHAT 供電

依 [SparkFun Pi Servo pHAT v2 說明](https://learn.sparkfun.com/tutorials/pi-servo-phat-v2-hookup-guide/hardware-overview)：
- pHAT 的 USB-C **同時供電給伺服馬達和 Pi**（Power Isolation jumper 出廠是接通的），伺服馬達的電流尖峰會直接拉低 Pi 的 5V
- USB-C 端有保險絲（2.5 A 持續、5 A 斷開），請用 5V 3A 以上電源，不要接電腦 USB（約 0.5 A）
- Zero 這類板子沒有防逆流保護；UPS-Lite 也接在 5V 上時，等於兩個電源並聯
- 實測一次：開機中途日誌在第 28 秒中斷（疑似斷電），另一次相機初始化時出現 kernel Oops；之後重開與伺服滿幅擺動 12 次都沒有 under-voltage，原因尚未確認

### 電源監控（power-monitor）

`tools/power_monitor.py` + `systemd/power-monitor.service`：開機早期就啟動，每 10 秒寫一行到 `/var/log/power-monitor.csv`（每行 fsync，斷電也留得下來）：

```
time,uptime_s,throttled,flags,core_v,temp_c,battery_v,battery_pct,ext_power
2026-10-30 22:20:06,502,0x0,ok,1.2688,48.3,4.141,97.2,1
```

- `flags`：`UV`（現在電壓不足）、`uv-seen`（開機後曾發生）、`CAP`/`THR`/`TEMP` 等；這些「曾發生」的位元重開機就清掉，所以 CSV 是斷電後唯一的線索
- 每次啟動會寫一行 `BOOT`；兩個 `BOOT` 之間最後一行的時間 ≈ 當機或斷電的時間
- 狀態改變也會寫進 journal：`sudo journalctl -u power-monitor`


## 9. V7RC 藍牙遙控（ROS2）

```
手機 V7RC ──BLE──> v7rc_ble_relay（廣播 Zero2W-Rover）──UDP 6188──> v7rc_bridge ──/cmd_vel──> dfrobot_motor_driver ──I2C──> M1/M2
```

| 檔案 | 說明 |
|---|---|
| `ros2_ws/src/dfrobot_motor_driver/` | `/cmd_vel` → DFR0592，開迴路差速（不使用編碼器），0.5 秒沒指令停車 |
| `ros2_ws/src/v7rc_bridge/` | V7RC 車輛（`SRV`）/ 坦克（`SRT`）模式 → `/cmd_vel` + 鏡頭雲台，斷線停車、死區、原地轉最低輪速 |
| `scripts/v7rc_ble_relay.py` | BlueZ D-Bus 的 Nordic UART 服務，名稱由 `V7RC_BLE_NAME` 設定 |
| `scripts/v7rc_start.sh` | 啟動 motor_driver + v7rc_bridge（含參數） |
| `systemd/v7rc-car.service`、`systemd/v7rc-ble.service` | 開機自動啟動 |

安裝：

```bash
cp -r ros2_ws/src/* ~/ros2_ws/src/
cd ~/ros2_ws && colcon build --symlink-install
cp scripts/v7rc_start.sh scripts/v7rc_ble_relay.py ~/ros2_ws/ && chmod +x ~/ros2_ws/v7rc_start.sh
sudo cp systemd/*.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now v7rc-car v7rc-ble
```

- `/dev/i2c-1` 屬於 `dialout` 群組，`pi` 不需 sudo 就能控制馬達
- V7RC App：連線選**藍牙**、選 `Zero2W-Rover`（App 一連上會送一筆 `FAL...#`，會被忽略）
  - **車輛模式**（`SRV`）：只能開車。實測 CH3、CH4 永遠是 1500（兩支搖桿各只能動一個方向）
  - **坦克模式**（`SRT`）：開車 + 鏡頭雲台。實測四個通道都會動：

    | 通道 | 搖桿 | 功能 |
    |---|---|---|
    | CH1 | 左搖桿 左／右 | 轉向（同車輛模式的 CH1） |
    | CH2 | 左搖桿 上／下 | 前進／後退 |
    | CH3 | 右搖桿 左／右 | 鏡頭水平（往右推 = 往右轉，`invert_pan` 預設 true） |
    | CH4 | 右搖桿 上／下 | 鏡頭垂直（往上推 = 往上） |

    鏡頭是**速度模式**：推越多轉越快（滿推 60°/s），放開就停在原角度；發布到 `/pan_tilt/cmd`
  - 雲台相關參數：`pan_tilt_enable`、`pan_input`/`tilt_input`（3/4）、`invert_pan`/`invert_tilt`、`pan_tilt_rate`、`pan_min`/`pan_max`、`tilt_min`/`tilt_max`
  - 想看 App 實際送出的數值：`v7rc_bridge` 加 `-p log_channels:=true`（也會記錄其他模式的原始封包）
- 實測搖桿：前進推到底 `linear.x = 0.30`，右轉推到底 `angular.z = -3.00`，輕推原地轉從約 2.4 rad/s 起跳
- DFR0592 暫存器：`0x03` 模式、`0x0E` PWM 頻率/50、`0x0F`/`0x12` 方向（1 CW、2 CCW、5 STOP）、`0x10`/`0x13` 占空比 [整數%, 十分位]
- 馬達對應（實測，架高後單顆測試）：**M1 = 右輪**（CW 往前）、**M2 = 左輪**（CW 往後）
  → `left_motor:=2 right_motor:=1 invert_left:=true invert_right:=false`（寫在 `scripts/v7rc_start.sh`）
- `/cmd_vel` 實測：前進、後退、原地左轉（左輪後、右輪前）、原地右轉都正確，馬達轉動時沒有 under-voltage
- **待辦**：地面實測距離與轉速，調 `max_wheel_speed`、`wheel_separation`（目前是估計值 0.5 m/s、0.15 m）

## 10. 其他注意事項

- 接了 UPS 的 Pi 拔電源不會斷電，關機請用 `sudo shutdown -h now`，等綠燈熄滅再關 UPS 開關
- `dmesg` 需要 sudo（Ubuntu 預設限制）
- 硬體狀態：`vcgencmd get_throttled` 應為 `0x0`

## tools/

| 檔案 | 用途 |
|---|---|
| `i2c_scan.py` | I2C 掃描（只讀，免 i2c-tools） |
| `motor_hat_info.py` | DFRobot 馬達驅動板唯讀檢查 |
| `ups_lite.py` | UPS-Lite v1.3 電壓、電量、外接電源 |
| `esp32_boot_log.py` | 經 USB 序列晶片重置 ESP32 並印出開機訊息 |
| `camera_snapshot.py` | OV5647 拍照（免 libcamera） |
