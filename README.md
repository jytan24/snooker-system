# Snooker System

A snooker ball detection and analysis system built with a Python/Flask backend (YOLO + ONNX) and a Flutter mobile/desktop frontend.

---

## Project Structure

```
snooker-system/
├── server.py            # Flask backend — detection, tracking, video processing
├── snooker_engine.py    # Core snooker logic (ball tracking, potting detection)
├── app.py               # Alternative/older backend runner
├── requirements.txt     # Python dependencies
├── yolo11s.onnx         # YOLO model weights (ONNX format)
├── data_v2.yaml         # YOLO dataset config (for training)
├── train_v2.py          # Model training script
├── train_script.py      # Training helper script
├── predict.py           # Standalone prediction script
├── flutter_app/         # Flutter frontend (Android, iOS, Windows)
└── datasets/            # Training images and labels
```

---

## Prerequisites

### Backend
- Python 3.10 or later
- FFmpeg installed and available on your system PATH ([download here](https://ffmpeg.org/download.html))

### Frontend
- [Flutter SDK](https://docs.flutter.dev/get-started/install) 3.0.0 or later
- Android Studio / Xcode (for mobile) or Windows build tools (for desktop)

---

## Backend Setup

### 1. Clone the repository

```bash
git clone https://github.com/jytan24/snooker-system.git
cd snooker-system
```

### 2. Create and activate a virtual environment

```bash
# Windows
python -m venv venv
venv\Scripts\activate

# macOS / Linux
python -m venv venv
source venv/bin/activate
```

### 3. Install Python dependencies

```bash
pip install -r requirements.txt
```

> **Note:** The `requirements.txt` includes `onnxruntime-directml` for AMD GPU acceleration on Windows. If you are on macOS or Linux (or do not have an AMD GPU), replace `onnxruntime-directml` with `onnxruntime` before installing.

### 4. Add Firebase credentials

The backend requires a Firebase service account key. Download it from your [Firebase Console](https://console.firebase.google.com/) under **Project Settings → Service Accounts → Generate new private key**, then save it as:

```
snooker-system/
└── firebase-key.json   ← place it here
```

### 5. Start the backend server

```bash
python server.py
```

The server runs on `http://0.0.0.0:5000`. You should see:

```
Serving on http://0.0.0.0:5000
```

---

## Flutter App Setup

### 1. Navigate to the Flutter app folder

```bash
cd flutter_app
```

### 2. Install Flutter dependencies

```bash
flutter pub get
```

### 3. Configure the server URL

By default the app connects to:
- `http://10.0.2.2:5000` on Android emulator (points to host machine)
- `http://127.0.0.1:5000` on all other platforms

**For a physical Android/iOS device**, you must pass your machine's local IP address at run time:

```bash
flutter run --dart-define=SERVER_URL=http://<your-local-ip>:5000
```

To find your local IP:
- **Windows**: run `ipconfig` in a terminal, look for IPv4 Address
- **macOS/Linux**: run `ifconfig` or `ip addr`

Example:
```bash
flutter run --dart-define=SERVER_URL=http://192.168.1.5:5000
```

### 4. Run the app

```bash
# Android (emulator or physical device)
flutter run

# Windows desktop
flutter run -d windows
```

---

## Firebase Setup (Flutter)

The app uses Firebase for authentication and match history. To connect it to your own Firebase project:

1. Go to [Firebase Console](https://console.firebase.google.com/) and create a project.
2. Add an Android/iOS app and download the `google-services.json` (Android) or `GoogleService-Info.plist` (iOS).
3. Place the file in the correct Flutter platform folder:
   - Android: `flutter_app/android/app/google-services.json`
   - iOS: `flutter_app/ios/Runner/GoogleService-Info.plist`
4. Enable **Email/Password** authentication in Firebase Console under **Authentication → Sign-in method**.
5. Enable **Cloud Firestore** in Firebase Console under **Firestore Database**.

---

## Usage

1. Start the backend server (`python server.py`).
2. Launch the Flutter app on your device or emulator.
3. Log in or register an account.
4. Use **Live Camera** to detect snooker balls in real time, or **Upload Video** to analyse a recorded match.
5. View match history and tactical analysis in the History screen.

---

## Troubleshooting

| Problem | Solution |
|---|---|
| `onnxruntime` install fails | Replace `onnxruntime-directml` with `onnxruntime` in `requirements.txt` |
| App cannot connect to server | Make sure server is running and `SERVER_URL` points to the correct IP |
| FFmpeg not found | Install FFmpeg and ensure it is on your system PATH |
| Firebase auth errors | Check that `firebase-key.json` is present and `google-services.json` is configured |
