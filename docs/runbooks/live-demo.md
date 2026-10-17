# Live demo: venue laptop + remote GPU

At the venue, the laptop runs everything except NaVILA. The workstation stays in the lab and serves
NaVILA over Tailscale. The workstation runs no ROS at all, so DDS only has to cross the venue Wi-Fi.

```
                        venue Wi-Fi (one network, also the Go2's in STA-L mode)
 Go2 Air ──── WebRTC ────────────┐
                                 │
 Pi 5 on the dog ──── DDS ───────┼──── Laptop: make demo-site ──── HTTP over Tailscale ──── Workstation: make demo-gpu
   Orbbec JPEG 15 Hz  ─────►     │       go2 driver, joystick, twist_mux                       NaVILA server (GPU, :8000)
   speaker            ◄─────     │       mic → stt_node → wake_router → dialog_node (Gemini)
                                 │       tts_node, orbbec_decode, navila_node
 DJI mic, joystick ─── USB ──────┘ (into the laptop)
```

| Machine | Runs | Make target |
|---|---|---|
| Go2 | its own firmware; the laptop's go2 driver talks to it over WebRTC | none |
| Pi | Orbbec camera + speaker (`robot/pi/README.md`) | `make pi-up` |
| Laptop | Go2 bridge, joystick, mic STT, Gemini, TTS, Orbbec decode, `navila_node` | `make demo-site` |
| Workstation | NaVILA server only | `make demo-gpu` |

Why Tailscale + HTTP: `navila_node` already talks HTTP to the server (JPEG frames + the instruction, the
action back). Tailscale encrypts it end to end (WireGuard), usually connects the two machines directly,
and exposes nothing to the internet. No DDS crosses it.

## Once

**Workstation** (in the lab):
```bash
git fetch && git switch feat/livedemo && git pull
make down                       # no ROS stack on this machine for the demo
make build-navila               # only if the NaVILA image isn't built yet
tailscale ip -4                 # its Tailscale address: goes into the laptop's .env
sudo ufw allow in on tailscale0 to any port 8000 proto tcp comment 'NaVILA server over Tailscale'
```

**Laptop:**
```bash
curl -fsSL https://tailscale.com/install.sh | sh && sudo tailscale up   # same tailnet as the workstation
git fetch && git switch feat/livedemo && git pull
# The ROS image is built from the forks, which only the workstation has (gitignored):
rsync -a <workstation>:<repo>/go2_sdk/ go2_sdk/
mkdir -p NaVila/NaVILA && rsync -a <workstation>:<repo>/NaVila/NaVILA/navila_go2 NaVila/NaVILA/
make .env                       # then fill it in (table below)
make build-ros
```

**Pi:** already set up (`robot/pi/README.md`). Only its `.env` changes, below.

## .env per machine

| Setting | Laptop | Pi | Workstation |
|---|---|---|---|
| `ROS_DOMAIN_ID` | same as the Pi (e.g. 30) | same as the laptop | unused |
| `DDS_IFACE` | its Wi-Fi interface (`ip route get 1.1.1.1`, the word after `dev`) | `wlan0` | unused |
| `PI_IP` | the Pi's IP at the venue | its own IP | unused |
| `BRIDGE_IP` | **its own IP** | the laptop's IP | unused |
| `GO2_IP`, `GO2_AES_KEY` | the robot's | unused | unused |
| `NAVILA_SERVER_URL` | `http://<workstation Tailscale IP>:8000` | unused | leave the default |
| `CAMERA` | `orbbec`, or `go2` when the Pi is down (the robot's front camera) | unused | unused |
| `STT`, `VOICE`, `GEMINI_API_KEY` | `true`, `true`, the key | unused | unused |
| `PI_DEPTH` | unused | `false` (`true` only with a 5 A supply) | unused |
| `NAVILA_DIR`, `NAVILA_CHECKPOINTS_DIR`, `NAVILA_MODEL` | unused | unused | as today |

A `ROS_DOMAIN_ID` exported in a shell (`echo $ROS_DOMAIN_ID`) overrides `.env`: keep shells clean.

## Every demo

1. **Workstation:** `make down` (a bringup left running here holds the Go2's only WebRTC connection
   and locks the laptop out), then `make demo-gpu` and leave it running. Loading the model takes a
   few minutes. It prints the `NAVILA_SERVER_URL` for the laptop.
2. **At the venue:** put the Go2, the Pi and the laptop on one Wi-Fi network (the venue's, or a travel
   router). IPs change per network: run `hostname -I` on the Pi and the laptop, update `PI_IP` and
   `BRIDGE_IP` in both `.env` files, then `make pi-up` on the Pi.
3. **Laptop:** `make navila-health`. It should print `NaVILA server OK`.
4. **Laptop:** `make demo-site` (dry run, the robot doesn't move). Plug the DJI mic receiver and the
   joystick in first.
5. **Laptop, second terminal:** check the pieces.
   ```bash
   make ros-shell                              # inside: ros2 node list   (run twice)
                                               #   expect the laptop's nodes + /orbbec/orbbec + /speaker_node
                                               # inside: ros2 topic hz /orbbec/color/image_decoded   (~15)
   make stt-echo                               # speak: your words appear
   make say TEXT="hello"                       # the Pi speaker talks
   ```
6. Talk to it:

   | Say | What happens |
   |---|---|
   | "Hey Roxy, walk to that chair and stop" | Gemini: a command. NaVILA gets it word for word; Roxy confirms aloud |
   | "Hey Roxy, what's your name?" | Gemini: a question. Roxy answers; the robot does nothing |
   | "stop" | ends the NaVILA episode at once, wake phrase or not |

7. **Moving for real:** `make down`, then `make demo-site MOTION=1`, joystick in hand. Hold RB
   (button 5, `configs/robots/roxy.yaml`) to take over: the sticks drive and NaVILA is ignored. Let go
   and NaVILA carries on after 0.5 s.

Stop: `make navila-stop` (end the episode), `make robot-stop` (StopMove now), `make down` (everything).

## When something fails

| Symptom | Check |
|---|---|
| `make navila-health` fails | `make demo-gpu` running on the workstation; `tailscale status` on both machines; the ufw rule above; the IP and port in `NAVILA_SERVER_URL` |
| The Go2 never connects: no `/go2_driver_node` in `ros2 node list`, no `/odom` | The Go2 takes one WebRTC client at a time: close the Unitree app on every phone, `make down` on every other machine, and wait ~30 s after a `make down` before starting again. Then `GO2_IP` is what the app shows today (DHCP can change it), `ping` reaches it, and `GO2_AES_KEY` is set. `make demo-site` prints the driver's reason |
| NaVILA decides but the robot doesn't move | `MOTION=1`; `/odom` arriving (the Go2 connected); RB not held; `ros2 topic info -v /cmd_vel_out` lists `go2_driver_node` as a subscriber |
| Pi camera drops off USB, speaker "No such device" | Under-voltage: `vcgencmd get_throttled` on the Pi is not `0x0`. Use a 5 V / 5 A supply ([robot/pi/README.md](../../robot/pi/README.md)) |
| NaVILA replies slowly | `tailscale ping <workstation>`: "via DERP" means relayed, "direct" is fastest. The venue's upload speed matters: each decision sends a few JPEG frames |
| Pi topics missing on the laptop | the same `ROS_DOMAIN_ID` on both (and none exported in the shell); `PI_IP` / `BRIDGE_IP`; the laptop's firewall (`sudo ufw status`: if active, allow UDP from the venue subnet on ports 7400 + 250 × domain onwards, e.g. 14900:15150 for domain 30) |
| `stt_node` can't open the mic | the receiver was plugged in after starting; or another program holds it (an old container, PipeWire) |
| Roxy says its brain is offline | `GEMINI_API_KEY` missing or wrong, or no internet at the venue |
| Camera rate low on the laptop | Wi-Fi quality between the Pi and the laptop: same 5 GHz network, laptop near the router, Wi-Fi power save off on the Pi |

More DDS detail: [dds.md](dds.md).
