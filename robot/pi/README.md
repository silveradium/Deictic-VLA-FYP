# Raspberry Pi 5

The Pi on Roxy runs one container:
- the Orbbec Gemini 335 driver, publishing under `orbbec/` (`configs/topics.yaml`, camera
  section). Raw frames stay on the Pi. Other machines use the JPEG stream, and bringup decodes it
  back to raw for NaVILA (`orbbec/color/image_decoded`).
- `speaker_node`, which plays Roxy's spoken replies (`voice/audio`, from bringup with `VOICE=true`)
  on the USB sound card.

| | |
|---|---|
| OS | Ubuntu 24.04 (arm64) + Docker with the Compose plugin |
| Image | `deictic/pi:humble` from `docker/pi/Dockerfile`, built on the Pi |
| Package | `robot/pi/deictic_pi`: `launch/pi.launch.py` |
| Colour | 640x480 at 15 Hz, JPEG ~45 KB a frame (~6 Mbit/s over Wi-Fi) |
| Speaker | USB sound card + 3.5 mm speaker, `alsa_device` in `configs/robots/roxy.yaml` (`aplay -L`) |

## Hardware

- **Camera on a blue USB 3 port**, with a USB 3 cable. `lsusb -t` must show the Orbbec at `5000M`,
  not `480M`.
- **Power: depth needs a 5 A supply.** Depth switches on the IR projector. If the Pi doesn't detect
  a 5 A supply, it limits USB to 600 mA, and the camera browns out and drops off USB a moment after
  depth starts. Colour alone works on any supply.
  - Check with `vcgencmd get_config usb_max_current_enable`: `0` means 600 mA.
  - Fix with the official 27 W (5 V/5 A) PSU. If your supply really is 5 A, you can instead add
    `usb_max_current_enable=1` to `/boot/firmware/config.txt` and reboot.
  - On the Go2, power the Pi from a 5 V/5 A DC-DC converter.
- **Camera firmware** is 1.4.60 on Roxy's unit. Orbbec recommends 1.8.x for this driver (SDK 2.9).
  Update it with OrbbecViewer if depth still fails on a 5 A supply.

## Setup (once)

```bash
sudo apt install -y iw && sudo iw dev wlan0 set power_save off   # Wi-Fi power save adds latency; resets on reboot
git clone git@github.com:silveradium/Diectic-VLA-FYP.git ~/projects/Diectic-VLA-FYP
cd ~/projects/Diectic-VLA-FYP
make .env
$EDITOR .env      # DDS_IFACE=wlan0, PI_IP=<this Pi>, BRIDGE_IP=<machine running bringup>, same ROS_DOMAIN_ID
make build-pi
```

The Pi's `.env` only needs the DDS values. The Go2 and NaVILA ones are for the bringup machine.

## Run

```bash
make pi-up        # starts in the background and comes back after a reboot
make pi-logs      # follow the driver
make pi-shell     # then: ros2 topic hz /orbbec/color/image_raw   (expect ~15)
make pi-down      # stop
```

Depth (5 A supply only): set `PI_DEPTH=true` in the Pi's `.env`, then `make pi-up`. Don't start a
second `ros2 launch` inside `make pi-shell`: two drivers fight over the camera and it keeps dropping
off USB.

On the bringup machine:
- Watch the feed with `make orbbec-view`, then open http://localhost:8090. It also shows the
  resolution, fps and horizontal/vertical FOV, worked out from `camera_info`.
- Check the stream with `make ros-shell`, then
  `ros2 topic hz /orbbec/color/image_raw/compressed`.
- To give NaVILA the Orbbec, set `NAVILA_CAMERA_TOPIC=orbbec/color/image_decoded` in `.env`.

If the topics don't show up there, see [docs/runbooks/dds.md](../../docs/runbooks/dds.md).
The usual causes are `BRIDGE_IP`/`PI_IP`, `ROS_DOMAIN_ID`, or the firewall rule.

Camera without the speaker: `speaker:=false` (`robot/pi/deictic_pi/launch/pi.launch.py`).
Test the speaker from the bringup machine with `make say TEXT="hello"` (`VOICE=true`).

## Notes

- Only one process can open the camera. Stop anything else using it before `make pi-up`.
- When the driver starts, the camera re-enumerates on USB once. That's why the container mounts
  `/dev` instead of passing the device.
