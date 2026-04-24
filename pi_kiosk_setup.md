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

The same server also hosts the phone controller and camera app.

Camera access from a phone/tablet browser generally needs HTTPS unless the page
is opened on `localhost`. If `/camera/` loads but camera initialization fails
over `http://<pi-address>:8000`, put the Pi behind HTTPS or a trusted local
tunnel for the camera app.

If you want it to auto-start at login too:

```bash
./install_pi_launchers.sh --autostart
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

If you use Raspberry Pi OS with desktop, create this autostart entry:

```ini
[Desktop Entry]
Type=Application
Name=Time Volume Display
Exec=chromium-browser --kiosk --app=http://127.0.0.1:8000/display
```

Save as:

```text
/home/pi/.config/autostart/time-volume-display.desktop
```

On some Pi images the browser command is `chromium` instead of `chromium-browser`.

## Why this setup

- `actuator_web.py` stays the control server.
- Chromium acts as the fullscreen display client on the Pi monitor.
- `/display` is now a clean black canvas with no corner text by default.
- Image-sequence folders are the most reliable option for exact position-to-frame mapping.
