# Network and topic map

First version. Topic names and types live in `configs/topics.yaml`; this page shows which machine
publishes what over which link.

## Links

```
                      Wi-Fi LAN (STA-L)
 Go2 Air ───── WebRTC ─────────────┐
                                   │
 Raspberry Pi 5 ── DDS (camera) ───┤──── Workstation or laptop ──── HTTP :8000 ──── NaVILA server
   Orbbec Gemini 335               │       bringup container              (same machine, or the
                                   │       go2 bridge, teleop,             workstation over Tailscale
 Handheld mic ── USB ──────────────┘       recorder, STT, navila_node      for remote demos)
```

| Link | Transport | Carries | Notes |
|---|---|---|---|
| Go2 ↔ bridge | WebRTC over Wi-Fi | odom, imu, lidar, state, front camera; velocity commands | STA-L: the bridge must be on the robot's LAN. Per-device AES-128 key. |
| Pi → bridge machine | DDS (CycloneDDS, unicast peers) | Orbbec RGB + depth | Same `ROS_DOMAIN_ID`, `PI_IP` as peer, firewall rule ([runbooks/dds.md](runbooks/dds.md)) |
| Mic → bridge machine | USB / ALSA | audio | STT runs on the bridge machine (step 3) |
| navila_node → NaVILA server | HTTP (JSON + JPEG frames) | `/act`, `/health` | Works over Tailscale as is: no DDS needed on this link |
| Remote demo, ROS topics | zenoh-bridge-ros2dds | selected topics, compressed images | Step 7. DDS can't cross Tailscale (no multicast) |
| Any machine | Foxglove WebSocket :8765 | live topics | From the bringup container |

## Velocity chain

```
joystick ─ joy ─► teleop_twist_joy ─ cmd_vel_joy (prio 10) ─┐
                                                            ├─► twist_mux ─ cmd_vel_out ─► go2_driver_node ─► Go2
navila_node ───────────────────────── cmd_vel (prio 5) ─────┘
```

The joystick always wins. twist_mux drops an input after 0.5 s of silence, so navila_node publishes
every tick, zeros included.

## Ports

| Port | Service |
|---|---|
| 8000 | NaVILA server (`NAVILA_PORT`) |
| 8765 | Foxglove bridge |
| 9991 | go2 WebRTC web UI (`webrtc_web.launch.py`, not started by bringup) |
