# Pi Long Exposure Experiment

This is a removable experiment for the Raspberry Pi Camera Rev 1.3 / OV5647.
It records frames from the Pi camera, blends them into one long-exposure-style
image, and saves the result locally.

The main web server imports this folder only as an optional experiment. To
remove it, delete `experiments/pi_long_exposure/` and the matching `.gitignore`
line; the controller will keep running and hide the Pi Camera panel.

## Install

On the Pi, install the camera Python stack if it is not already present:

```bash
sudo apt update
sudo apt install -y python3-picamera2 python3-numpy python3-pil
```

Check that the Pi sees the camera:

```bash
rpicam-hello --list-cameras
rpicam-hello -t 2000
```

Older Raspberry Pi OS images may call this command `libcamera-hello` instead.
If the camera is not listed, check the ribbon cable orientation and enable the
camera interface with `sudo raspi-config` on older Raspberry Pi OS images.

## Basic Capture

Start the normal Time Volume display/controller, frame the moving monitor in the
Pi camera, then run:

```bash
python3 experiments/pi_long_exposure/pi_long_exposure.py --duration 25
```

Images are saved in:

```text
experiments/pi_long_exposure/captures/
```

## Controller Panel

The main controller shows a collapsible `Pi Camera` section only when the Pi
camera is detected by the server. It includes a full-frame 4:3 snapshot preview,
record button, duration, blend mode, frame gap, threshold, manual shutter,
ISO-like gain, and optional lift start/stop toggles.

The `Manual Preview` toggle applies the current shutter and ISO settings to the
preview. Leave it off for a faster auto-exposure preview while framing.

The Raspberry Pi Camera Rev 1.3 lens has a fixed aperture, so aperture cannot be
changed in software. Use shutter and ISO/gain for manual exposure control.
Set `Frame Gap` to `0.00s` for the shortest possible pause between frames; the
server automatically keeps the actual frame interval valid for the selected
resolution and shutter.

Saved images appear behind the `Last Capture` link and are stored in:

```text
experiments/pi_long_exposure/captures/
```

## Trigger The Lift

The script can trigger the existing actuator API without adding any new server
routes. Set the target/speed in the controller first, then run:

```bash
python3 experiments/pi_long_exposure/pi_long_exposure.py \
  --duration 25 \
  --server-url http://127.0.0.1:8000 \
  --start-action play_pause \
  --finish-action stop
```

Useful options:

- `--start-delay 5` gives you a few seconds after camera warmup before capture.
- `--mode add` sums frames like a photographic exposure and is the default.
- `--mode lighten` keeps the brightest pixel trails without adding brightness.
- `--mode mean` makes a softer average blur.
- `--mode screen` stacks light more aggressively and can become bright quickly.
- `--threshold 12` suppresses dim camera noise before blending.
- `--hflip` and `--vflip` fix upside-down or mirrored camera mounting.
- `--shutter-us 8000 --gain 1.5` sets a fixed per-frame exposure and gain.
- `--shutter-us 1000000 --fps 1` stacks one-second exposures.

For a first test in a dark room, try:

```bash
python3 experiments/pi_long_exposure/pi_long_exposure.py \
  --duration 30 \
  --mode add \
  --threshold 10 \
  --shutter-us 1000000 \
  --fps 1 \
  --server-url http://127.0.0.1:8000 \
  --start-action play_pause \
  --finish-action stop
```
