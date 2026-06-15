# Time Volume

Raspberry Pi 4 control stack for a scissor-lift monitor.

The active runtime is the web stack:

- `actuator_web.py` hosts the actuator API, controller PWA, display page, optional camera redirect, and media files.
- `web/controller.html` is the phone/tablet lift controller.
- `web/display.html` is the fullscreen monitor output shown on the lift.
- The long-exposure camera PWA is hosted separately at `https://betanumeric.github.io/volumetric_time_camera/`.
- `media/` contains image sequences and video clips used by the display.
- `start_time_volume.sh`, `stop_time_volume.sh`, `install_pi_launchers.sh`, and `install_boot_service.sh` are Raspberry Pi launch helpers.

## Sequence Height Metadata

Image-sequence folders can opt into a shorter physical playback span by adding
`sequence.json` next to the frames:

```json
{
  "volume_height_cm": 32,
  "start_cm": 0
}
```

The lift stroke is calibrated as 48 cm. Without `sequence.json`, a sequence
plays over the full 0-48 cm stroke. With the example above, the sequence plays
from 0-32 cm and the display is black outside that span. For videos, use a JSON
sidecar with the same base name as the clip, such as `clip.json` for `clip.mp4`.

`media/` can contain organizing subfolders. Each folder that directly contains
image frames becomes one image sequence, and video files become sequences in
the folder where they live:

```text
media/
  astronomy/
    moon/
      0001.png
      sequence.json
    jwst.mp4
  objects/
    apple/
      0001.png
```

The controller groups sequences by folder, so these appear under `astronomy`
and `objects`. A `sequence.json` or video sidecar can also set `"name"` to
override the displayed sequence name.

To add a controller preview image for an image-sequence folder, place a file
named `preview`, `poster`, `thumbnail`, or `cover` with an image extension in
that folder, for example `preview.png` or `poster.jpg`. These files are ignored
for playback. For video sequences, use a sidecar next to the video, such as
`jwst.preview.jpg` for `jwst.mp4`.

## Updating Media

For the most reliable updates, copy new or replacement folders into `media/`
with a temporary name first, then rename them when the copy is complete:

```bash
cp -a ~/new-content/apple media/apple.incoming
mv media/apple media/apple.old
mv media/apple.incoming media/apple
```

After that, tap **Refresh Library** in the controller. The scanner ignores
hidden names and names ending in `.incoming`, `.partial`, `.tmp`, or `.copying`,
so partially copied folders do not appear as playable sequences. Same-name
replacements are also detected by file size/modified-time signatures so MPV
does not keep using an old image playlist for a replaced folder.

## Run

```bash
python3 actuator_web.py --host 0.0.0.0 --port 8000
```

Then open:

- Controller: `http://<pi-address>:8000/controller`
- Browser display fallback: `http://127.0.0.1:8000/display`
- Camera: `https://betanumeric.github.io/volumetric_time_camera/`

Camera access from phones/tablets usually requires HTTPS, except on `localhost`.
If the camera PWA opens but cannot initialize the camera over a Pi LAN address,
serve the app through HTTPS or a trusted local tunnel.

For the Pi kiosk flow, run:

```bash
./start_time_volume.sh
```

Desktop autostart created by `./install_pi_launchers.sh --autostart` uses the
same MPV display backend. If the boot service is already running, the launcher
asks that live server to start MPV and passes the desktop session environment
with the request. That lets a boot-managed server open MPV after login without
replacing the early switch-polling process.

To have cycle mode start automatically after the fullscreen display is open,
enable the startup cycle flag on the launcher. The default startup delay is 120
seconds:

```bash
TIME_VOLUME_AUTO_START_CYCLE=1 ./start_time_volume.sh
```

For desktop autostart, pass the flag while creating the launcher so it is saved
into the autostart desktop file:

```bash
TIME_VOLUME_AUTO_START_CYCLE=1 TIME_VOLUME_AUTO_START_CYCLE_DELAY_SEC=120 ./install_pi_launchers.sh --autostart
```

Autostart output is logged to:

```text
.run/time_volume_launcher.log
```

`Stop Time Volume.desktop` stops the current motion/cycle and closes the
fullscreen MPV display while leaving the boot-managed controller server alive.
That keeps the physical switch/control path available and makes the next Start
quick.

The default display backend is a dedicated fullscreen MPV process. To run the
monitor through the browser fallback at `/display` instead:

```bash
TIME_VOLUME_DISPLAY_BACKEND=browser ./start_time_volume.sh
```

Install MPV on the Pi first if it is not already present:

```bash
sudo apt install mpv
```

MPV supports both video files and image-sequence folders. Image folders are
handed to MPV as sampled frame lists capped at 30 fps by default; change that
cap with `TIME_VOLUME_MPV_FPS_CAP=24` if needed. If MPV is missing or fails to
start, the launcher falls back to the web display at `/display`.

## Camera App

The camera app is hosted separately on GitHub Pages:

```text
https://betanumeric.github.io/volumetric_time_camera/
```

The Pi controller uses that URL by default. The local `/camera/` route redirects
there, and the controller/display/actuator stack no longer depends on local
camera files.

During idle/standby and cycle pauses, the monitor shows a camera QR code. The QR
opens the camera app URL and carries a compact `tvs` query parameter with the
next sequence name, direction, countdown, and exposure duration. In the camera
PWA, enable **QR Sync** in Settings to scan that QR from the live preview and
auto-start/stop the stack capture when the next lift movement begins. Set
**QR Action** to **Info only** if you want the camera app to show the decoded
sequence/timing without starting a capture. The camera app uses the browser QR
detector when available and falls back to a JavaScript decoder for iPhones. The
controller's Advanced settings include **Show QR**, **QR Info Text**, and one
**QR Blackout** delay. The blackout delay is used both before motion starts and
after motion ends, defaults to 5 seconds, and shows a black screen instead of
falling back to the selected frame. The MPV QR screen centers the QR and shows
large rotated countdown numbers on the left and right edges for side viewing.
The camera PWA expands its top QR status box when tapped.

To point the Pi at a different hosted camera app later:

```bash
TIME_VOLUME_CAMERA_URL=https://example.com/camera/ ./start_time_volume.sh
```

For the boot service, pass the override while installing:

```bash
sudo env TIME_VOLUME_CAMERA_URL=https://example.com/camera/ ./install_boot_service.sh
```

## Pi Camera Long Exposure Experiment

A removable hardware-camera experiment lives in `experiments/pi_long_exposure/`.
It uses the attached Raspberry Pi camera to stack frames into a saved
long-exposure-style image and can optionally trigger the existing actuator API.
When the Pi camera is detected, the controller shows a collapsible Pi Camera
panel with preview, recording, and basic capture settings, including optional
manual shutter and ISO-like gain. If the experiment folder or camera is missing,
the panel stays hidden.

## Boot Switch Control

The physical switch is handled by `actuator_web.py`, so it works whenever the
server process is running. To start that process automatically on boot:

```bash
chmod +x install_boot_service.sh
sudo ./install_boot_service.sh
```

This makes basic switch control available shortly after Linux finishes booting,
even before the desktop display opens. It cannot respond at the exact instant
the Pi receives power; for truly immediate movement, wire a hardware manual
control path or a dedicated motor controller that does not depend on the Pi
booting first.

The boot service requires GPIO access. If GPIO is unavailable, systemd will keep
retrying instead of starting in simulation mode. By default, this service starts
only the actuator server; the desktop launcher/autostart can then enable the MPV
display once the graphical session is available.

Keep the boot service display backend as `none` unless you are debugging. The
fullscreen MPV window should be owned by the desktop launcher/autostart, because
boot-time services usually start before the graphical desktop environment is
ready.

Do not put the exhibition auto-start countdown on the boot service. The launcher
waits for the boot service, opens MPV from the desktop session, resets the
controller delay to the requested startup value, and then starts the countdown.
If you previously installed the service with `TIME_VOLUME_AUTO_START_CYCLE`, run:

```bash
sudo ./install_boot_service.sh
```

## Runtime Files

These are generated while the installation runs and should not be treated as source:

- `.run/`
- `__pycache__/`
- `actuator_state.json`
- `*.log`
- `*.tmp`
- `experiments/pi_long_exposure/captures/`

## Cleanup Notes

The `media/` folder is intentionally large. Keep active sequences there, but move retired frame sets outside the repo or into a clearly named external archive to keep deploys and backups manageable.

The old Tkinter/OpenCV control station has been archived outside the active runtime. The production path is `actuator_web.py` plus the web apps listed above.
