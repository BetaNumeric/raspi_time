## Raspberry Pi Kiosk Setup

The clean installation output is:

```text
http://127.0.0.1:8000/display
```

For a debug overlay, use:

```text
http://127.0.0.1:8000/display?hud=1&countdown=1
```

## One-click launchers

If you want clickable desktop files on the Pi, use the launcher installer in the project folder:

```bash
chmod +x install_pi_launchers.sh start_time_volume.sh stop_time_volume.sh
./install_pi_launchers.sh
```

That creates:

- `Start Time Volume.desktop`
- `Stop Time Volume.desktop`

on the Pi desktop. Clicking `Start Time Volume.desktop` will:

1. start `actuator_web.py`
2. wait for the server to come online
3. open the fullscreen display at `/display`

The same server hosts the phone controller. The camera app is hosted separately
on GitHub Pages:

```text
https://betanumeric.github.io/volumetric_time_camera/
```

The Pi controller uses that URL by default, and `/camera/` redirects there. To
override it later, set `TIME_VOLUME_CAMERA_URL`:

```bash
TIME_VOLUME_CAMERA_URL=https://example.com/camera/ ./start_time_volume.sh
```

For a boot-managed server, pass the override during install:

```bash
sudo env TIME_VOLUME_CAMERA_URL=https://example.com/camera/ ./install_boot_service.sh
```

The monitor shows a QR code during standby/cycle pauses. It still scans as the
camera app URL, with sync timing in a `tvs` query parameter. Turn on **QR Sync**
inside the camera app settings to let the PWA read that code and schedule the
long-exposure stack automatically, or set **QR Action** to **Info only** to
show the decoded timing without starting a capture. iPhones use the camera
app's JavaScript QR decoder fallback. The controller's Advanced settings have
**Show QR**, **QR Info Text**, and one **QR Blackout** delay. That delay is used
both before motion starts and after motion ends, defaults to 5 seconds, and
shows a black screen instead of frames. The MPV QR screen centers the QR and
shows large rotated countdown numbers along the left and right edges.

If you want it to auto-start at login too:

```bash
./install_pi_launchers.sh --autostart
```

If the boot service is already running, the launcher reuses that server and
passes the desktop session environment to it before starting MPV. This matters
because a boot-time systemd service normally does not know `DISPLAY`,
`WAYLAND_DISPLAY`, or `XDG_RUNTIME_DIR`, and MPV needs those values to open a
fullscreen window on the logged-in desktop.

The autostart launcher logs to:

```text
.run/time_volume_launcher.log
```

## 1. Start switch control on boot

The physical switch is polled by `actuator_web.py`. To make that available as
soon as the Pi has booted, install the systemd service from the project folder:

```bash
chmod +x install_boot_service.sh
sudo ./install_boot_service.sh
```

This starts the actuator server before the desktop session. It is still limited
by Pi/Linux boot time; if the lift must respond the instant power is applied,
use a hardware manual control path or a dedicated motor controller.

The boot service uses required GPIO mode, so it will retry through systemd
instead of quietly running without switch/motor access.

## 2. Auto-launch the fullscreen monitor output

For Raspberry Pi OS with desktop, use:

```bash
./install_pi_launchers.sh --autostart
```

That installs a desktop-session autostart entry at:

```text
~/.config/autostart/Start Time Volume.desktop
```

The entry runs `start_time_volume_autostart.sh`, which waits briefly for the
graphical session, starts or reuses the controller server, and retries the MPV
fullscreen display startup.

## Why this setup

- `actuator_web.py` stays the control server.
- MPV acts as the fullscreen display client on the Pi monitor.
- `/display` is now a clean black canvas with no corner text by default.
- Image-sequence folders are the most reliable option for exact position-to-frame mapping.
