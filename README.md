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

Image frames are sorted in natural numeric order, so zero-padded names like
`0001.png`, `0002.png`, `0010.png` and Windows-style names like `(1).png`,
`(2).png`, `(10).png` play in counting order.

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

For a full stop, use **Quit Time Volume** on the Pi desktop. Run
`./install_pi_launchers.sh` to add the shortcut. You can also quit or shut the Pi
down from a terminal:

```bash
./stop_time_volume.sh --quit
./stop_time_volume.sh --poweroff
```

Both actions wait for the controller to exit through its normal shutdown path,
which stops the lift and saves its estimated position. The Pi's permissions
apply when stopping the boot service or shutting down. If the OS asks for
authentication, use an authorized local account. If you started the Python
server directly in a terminal, use Ctrl+C there and wait for it to exit.
Wait for the Pi itself to finish shutting down before unplugging it.

### Shut down from the controller

Once configured, **Advanced → Tools → Shut down Pi** stops motion, checkpoints
the lift position and requests an orderly OS shutdown. Remote poweroff is off
by default. Set it up once on the Pi:

1. As the account that runs the controller, create a private shutdown key in
   the checkout and copy the printed key to your password manager.

   ```bash
   (umask 077; set -o noclobber; python3 -c 'import secrets; print(secrets.token_urlsafe(32))' > .shutdown-token) &&
       cat .shutdown-token
   ```

2. Use `sudo visudo -f /etc/sudoers.d/time-volume-poweroff` to give that account
   permission for the exact command below. Replace `YOUR_USER` with the account
   running Time Volume, which is the boot service's `User=` if you use one.

   ```sudoers
   YOUR_USER ALL=(root) NOPASSWD: /usr/bin/systemctl poweroff
   ```

3. Open the controller through a trusted HTTPS reverse proxy running on the Pi.
   It must forward to `127.0.0.1:8000` (or your configured port) and overwrite
   `X-Forwarded-Proto` with the actual client scheme. A
   [Caddy reverse proxy](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy#defaults)
   does this by default. Use a hostname and certificate your phone trusts.
   Keep the controller on your private network or VPN.

   For example, a Caddyfile site block for a Pi reachable as `raspberrypi.local`
   can use the following configuration. Trust Caddy's
   [local root certificate](https://caddyserver.com/docs/automatic-https#local-https)
   on the phone before entering the shutdown key.

   ```caddyfile
   raspberrypi.local {
       tls internal
       reverse_proxy 127.0.0.1:8000
   }
   ```

Restart Time Volume so it reads the key, open the HTTPS controller, enter the
key under **Advanced → Tools**, and confirm **Shut down Pi**. Wait for the Pi to
finish shutting down before removing power. To disable this feature, remove
`.shutdown-token` and the sudoers entry, then restart Time Volume.

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

## Motion Control Safeguards

In **Advanced → Bottom playback delay**, set the time the motor runs before the
display starts rising from the bottom (0–5 seconds, default 0). Measure it at
100% speed; a setting of 0.5 seconds becomes 1 second at 50% speed. The monitor
stays black during that interval, then plays the entire sequence over the
remaining upstroke. The calibrated motor run time and QR capture window stay
the same. This applies to images and videos in MPV and the browser display.
The setting is saved and takes effect on the next rise from the bottom; pauses
and resumes retain the current upstroke's compensation. Midstroke starts and
descents use their existing timing.

Stop cancels both active motion and workers that have not started yet. Movement,
homing, and GPIO cleanup validate their motion session before changing outputs,
so cancelled workers cannot restart the lift or interfere with a newer movement.
Shutdown disables the motor outputs and prevents further motion reservations.

The controller's press-and-hold Jog buttons renew a one-second lease every 200 ms.
If the page closes, loses focus, or loses its connection, jogging stops when that
lease expires. The physical toggle switch does not depend on browser heartbeats.
After updating, restart the server and reload controller pages together. Custom
clients using Jog must send the current `jog_epoch`, `jog_generation`, and a unique
hold identifier as `epoch`, `generation`, and `session_id` with `jog_start`, renew
with `jog_heartbeat`, and release with `jog_stop` using the same `session_id`.

Position is estimated from running time. A stopped estimate is saved locally in
`actuator_position.json` and restored when the server restarts. A previous valid
copy is retained as `actuator_position.json.bak`. Both position and settings saves
flush file contents, replace files atomically, and sync the containing directory;
settings also retain `actuator_state.json.bak` for recovery from damaged JSON.

Before enabling the motor, the server durably writes `actuator_position.moving`.
That marker is removed only after the motor outputs are disabled and the stopped
checkpoint is durable. An interrupted movement, a missing checkpoint, or a damaged
or checksum-invalid position file means **Position unknown — Home required**.
The controller blocks automatic movement and countdowns until you establish a
home reference. **Home lift** retracts the lift. If the lift is already fully
lowered, choose **Mark as home** in the recovery panel or under **Advanced →
Tools** to save 0% without moving it. Stop any movement, cycle, or countdown and
set the physical switch to off before confirming. Jog and physical-switch
control remain available, and the monitor stays black while the position is
unknown. An older backup is never
silently trusted as the current height. Simulation uses separate position files.

After upgrading from a version that did not save position, establish a home
reference on the first startup. Start automatic playback explicitly if startup
cycling was skipped. A cancelled Home does not establish a reference. Homing is still
timed retraction, not sensor confirmation, and a saved estimate assumes the lift
was not moved while the Pi was off. These measures reduce write-loss risk but
cannot guarantee an SD card or filesystem will survive every abrupt power cut.

Run the hardware-free regressions with:

```bash
python3 -B -m unittest discover -s tests -v
```

With Chromium installed, the optional browser regressions also exercise the Jog
JavaScript using mocked requests and a temporary browser profile:

```bash
TIME_VOLUME_TEST_BROWSER=1 python3 -B -m unittest discover -s tests -v
```

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
controller's **Cycle → Idle display** setting offers **QR code**, **Black screen**,
and **Current frame**. Current frame pauses the selected sequence at the lift's
current position and follows the sequence's playback span. The **QR Blackout**
delay in Advanced applies before scheduled movement and after motion ends,
including in Current frame mode. Set it to zero to show the frame immediately.
Advanced also includes **QR Info Text**. The MPV QR screen centers the QR and shows
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
- `actuator_state.json.bak`
- `actuator_position*.json`, `actuator_position*.json.bak`, and `actuator_position*.moving`
- `*.log`
- `*.tmp`
- `experiments/pi_long_exposure/captures/`

## Cleanup Notes

The `media/` folder is intentionally large. Keep active sequences there, but move retired frame sets outside the repo or into a clearly named external archive to keep deploys and backups manageable.

The old Tkinter/OpenCV control station has been archived outside the active runtime. The production path is `actuator_web.py` plus the web apps listed above.
