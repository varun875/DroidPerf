# DroidPerf 🚀

**DroidPerf** is a free, open-source Android game performance profiler (an open alternative to PerfDog). It tracks real-time performance metrics over ADB and presents live interactive charts and comprehensive telemetry visualization.

---

## ✨ Key Features

- **Live FPS & Frametime Profiling**: Tracks continuous FPS along with 1% and 10% low FPS calculations to identify micro-stutters.
- **Hardware Telemetry**: Monitors CPU core clock speeds, GPU frequencies (where supported), RAM usage (PSS), and battery temperatures in real time.
- **Automatic Foreground App Detection**: Automatically detects the active Android game or app window.
- **Real-Time Web Dashboard**: Server-Sent Events (SSE) streaming web UI with interactive Chart.js graphs and session history management.

---

## 📋 Prerequisites

1. **Python 3.10+** installed.
2. **Android SDK Platform Tools (`adb`)** installed and added to system PATH.
3. An **Android device** (or emulator) connected via USB with **USB Debugging** enabled.

---

## ⚡ Quick Start

### 1. Clone & Install Dependencies

```bash
git clone https://github.com/varun875/DroidPerf.git
cd DroidPerf
pip install -r requirements.txt
```

### 2. Launch the Server

```bash
python app.py
```

Open your browser and visit: **`http://127.0.0.1:5000`**

---

## 🎮 Usage Guide

1. **Connect Device**: Plug in your Android device via USB and verify ADB connection (`adb devices`).
2. **Detect / Select App**: Open the web dashboard and click **Detect App** or enter the target Android package name manually.
3. **Start Profiling**: Click **Start Session**. Launch your game or application on the device.
4. **Stop & Analyze**: Click **Stop Session**. The session will finalize, save telemetry to `./sessions/<session-id>.json`, and render performance summary statistics.

---

## 🛠 Tech Stack

- **Backend**: Python 3, Flask, Server-Sent Events (SSE)
- **Frontend**: Vanilla HTML5, CSS3, JavaScript, Chart.js
- **Telemetry**: Android Debug Bridge (`adb`), SurfaceFlinger / Frame Statistics, Sysfs

---

## 📜 License

Distributed under the MIT License. See [LICENSE](file:///c:/Users/Varun/Documents/GitHub/DroidPerf/LICENSE) for details.


