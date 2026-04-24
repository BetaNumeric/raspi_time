# Time Volume

Raspberry Pi 4 control stack for a scissor-lift monitor.

The active runtime is the web stack:

- `actuator_web.py` hosts the actuator API, controller PWA, display page, camera app, and media files.
- `web/controller.html` is the phone/tablet lift controller.
- `web/display.html` is the fullscreen monitor output shown on the lift.
- `index.html`, `manifest.json`, `service-worker.js`, and `icons/` are the long-exposure camera PWA served at `/camera/`.
- `media/` contains image sequences and video clips used by the display.
- `start_time_volume.sh`, `stop_time_volume.sh`, and `install_pi_launchers.sh` are Raspberry Pi launch helpers.

## Run

```bash
python3 actuator_web.py --host 0.0.0.0 --port 8000
```

Then open:

- Controller: `http://<pi-address>:8000/controller`
- Display: `http://127.0.0.1:8000/display`
- Camera: `http://<pi-address>:8000/camera/`

Camera access from phones/tablets usually requires HTTPS, except on `localhost`.
If the camera PWA opens but cannot initialize the camera over a Pi LAN address,
serve the app through HTTPS or a trusted local tunnel.

For the Pi kiosk flow, run:

```bash
./start_time_volume.sh
```

## Runtime Files

These are generated while the installation runs and should not be treated as source:

- `.run/`
- `__pycache__/`
- `actuator_state.json`
- `*.log`
- `*.tmp`

## Cleanup Notes

The `media/` folder is intentionally large. Keep active sequences there, but move retired frame sets outside the repo or into a clearly named external archive to keep deploys and backups manageable.

The old Tkinter/OpenCV control station has been archived outside the active runtime. The production path is `actuator_web.py` plus the web apps listed above.
