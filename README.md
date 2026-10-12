# Deictic-VLA

Gesture-conditioned VLA navigation on a Unitree Go2 Air ("Roxy"). A person points at a target and gives
an instruction, and the robot turns and navigates there. Current goal: collect a pilot dataset (our own
episodes + DeePoint augmented with robot turn actions), fine-tune NaVILA with LoRA, and evaluate.

Collection and inference are the same system: one bringup with `mode:=collect|infer`. The only difference
is who publishes `cmd_vel`.

## Quickstart

```bash
make setup        # .env from .env.example, submodules, build the x86 images
$EDITOR .env      # GO2_IP, GO2_AES_KEY (per-device, firmware >= 1.1.15), DDS_IFACE
make collect      # data collection from the terminal: Enter, speak, Enter, drive, Ctrl-C; one bag per episode
make infer        # same + NaVILA server (GPU machine)
make help         # all targets
```

On the Pi: `make .env`, set `DDS_IFACE`, `PI_IP` and `BRIDGE_IP`, then `make pi-up` ([robot/pi](robot/pi/README.md)).

Voice: say "Hey Roxy, walk to that chair" into the mic on the bringup machine; it reaches NaVILA word for word,
and "stop" ends the episode at once. With `VOICE=true` and `GEMINI_API_KEY` in `.env`, Roxy also tells
commands from questions and answers aloud through the Pi speaker.

Requires Docker with Compose v2. GPU machines also need `nvidia-container-toolkit`. DDS setup and the
firewall rule for the Pi: [docs/runbooks/dds.md](docs/runbooks/dds.md).

## Make targets

| Target | What it does |
|---|---|
| `make setup` | Create `.env`, fetch submodules, build all x86 images |
| `make build-ros` / `make build-navila` | Build one image |
| `make collect` | Data collection: starts bringup (Go2, mic) if needed, then the terminal recorder, one bag per episode ([docs/collection_protocol.md](docs/collection_protocol.md)) |
| `make logs` | Follow the bringup logs (Go2 bridge, STT) in a second terminal |
| `make preview` | Look inside a session's episode bags: video, odometry path, labels, every topic (`SESSION=...`, default latest) |
| `make infer` | Inference stack: collection stack + NaVILA server + navila_node. Dry run; `MOTION=1` to move |
| `make instruct TEXT="..."` | Give NaVILA an instruction (while `make infer` runs) |
| `make navila-stop` | End the NaVILA episode, robot holds still |
| `make robot-stop` | Send the robot a direct StopMove |
| `make calibrate` | Measure the robot's real turn (and with `FORWARD=1` walk) speeds, for `configs/robots/roxy.yaml` |
| `make stt-echo` | Print what the mic hears (`stt/transcript`) while `make collect` / `make infer` runs |
| `make say TEXT="..."` | Make Roxy speak through the Pi speaker (`VOICE=true`) |
| `make voice-preset PRESET=robot` | Switch Roxy's voice (`scooby` / `robot`) while it runs |
| `make navila-server` | NaVILA server alone (GPU machine) |
| `make demo-gpu` | Live demo, workstation: NaVILA server for the venue laptop over Tailscale; prints the URL for the laptop |
| `make demo-site` | Live demo, venue laptop: Go2 bridge, joystick, mic + voice, NaVILA on the workstation. Dry run; `MOTION=1` to move |
| `make navila-health` | Check the NaVILA server answers at `NAVILA_SERVER_URL` (from the laptop) |
| `make ros-shell` | Shell in the ROS image on the host network, for `ros2 topic list` etc. |
| `make down` | Stop every stack |
| `make build-pi` | On the Pi: build the Pi image (arm64 Humble + Orbbec driver) |
| `make pi-up` / `make pi-down` | On the Pi: start (in the background, survives reboots) / stop the Orbbec camera |
| `make pi-logs` / `make pi-shell` | On the Pi: follow the camera's logs / open a shell in its container |

Planned: `bag2ep` (5), `export` (8), `train-submit` (9).

## Layout

```
configs/          topics.yaml (single source of truth for topics), cyclonedds.xml, robots/, zenoh/
docker/
  ros/            Humble + go2_ros2_sdk + ros_ws (x86, no torch)
  navila/         CUDA + NaVILA server deps; code and weights are mounted
  pi/             arm64 Humble + Orbbec driver (step 2)
  compose/        building blocks (ros.yml, navila.yml) + mode overlays (collect.yml, infer.yml)
ros_ws/src/       deictic_bringup, deictic_interfaces, audio_stt, voice, recorder (terminal collection, one bag per episode), teleop, navila_node
third_party/      NaVILA fork (submodule, pending)
robot/            pi/ (deictic_pi package + setup guide), jetson/
data/             gitignored except README.md and schema/
data_pipeline/    real/ (bag -> episodes), deepoint/, export/, validate/
training/         LoRA configs, job scripts, envs for aberdeen / the Singapore cluster
eval/  scripts/  docs/
```

### Forks (temporary locations)

The modified forks currently sit untracked in the repo root, and the images read them through paths in
`.env`:

| Fork | Now | Final location |
|---|---|---|
| go2_ros2_sdk | `go2_sdk/src` (`GO2_SDK_DIR`) | `ros_ws/src/go2_ros2_sdk` (submodule) |
| NaVILA | `NaVila/NaVILA` (`NAVILA_DIR`) | `third_party/NaVILA` (submodule) |
| NaVILA weights | `NaVila/NaVILA/checkpoints` (`NAVILA_CHECKPOINTS_DIR`) | anywhere outside git, mounted read-only |

## Build order

- [x] 0. Skeleton
- [ ] 1. `docker/ros` + go2_ros2_sdk, robot topics visible on the workstation (image and bringup work, untested with the robot)
- [x] 2. `docker/pi` arm64 image + Orbbec topics (tested on the Pi: colour 15 Hz + bringup decode; depth needs the 5 A supply)
- [ ] 3. `topics.yaml` + `deictic_bringup mode:=collect`, teleop + mic STT (mic STT + "Hey Roxy" in `audio_stt`: nodes tested on the Pi, not yet in the workstation stack)
- [ ] 4. `recorder`: terminal collection, one bag per episode, `make preview` (tested on the laptop through a real
  terminal with simulated camera + odom and the real mic; not yet a full episode with Roxy and the Pi)
- [ ] 5. `data_pipeline/real`: bag -> canonical episodes (define `data/schema` first)
- [ ] 6. `docker/navila` + `navila_node` + `mode:=infer` (pretrained). Server image done and tested
- [ ] 7. Remote demo: `make demo-gpu` (workstation) + `make demo-site` (venue laptop), NaVILA over HTTP on
  Tailscale ([docs/runbooks/live-demo.md](docs/runbooks/live-demo.md)). Still open: zenoh for live topics, latency test
- [ ] 8. `data_pipeline/deepoint` augmentation + `export` to NaVILA format
- [ ] 9. `training/`: env, LoRA config, aberdeen job scripts

## Rules

Branching and commit conventions: [CONTRIBUTING.md](CONTRIBUTING.md).

- Never commit data, bags, checkpoints or `.env`. Data lives on HF Hub / Drive / the servers (`data/README.md`).
- Every new topic goes in `configs/topics.yaml` first.
- Every new workflow gets a `make` target and a line in this README.
