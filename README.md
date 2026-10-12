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
make collect      # Go2 bridge + joystick teleop
make infer        # same + NaVILA server (GPU machine)
make help         # all targets
```

Requires Docker with Compose v2. GPU machines also need `nvidia-container-toolkit`. DDS setup and the
firewall rule for the Pi: [docs/runbooks/dds.md](docs/runbooks/dds.md).

## Make targets

| Target | What it does |
|---|---|
| `make setup` | Create `.env`, fetch submodules, build all x86 images |
| `make build-ros` / `make build-navila` | Build one image |
| `make collect` | Collection stack: Go2 bridge + joystick teleop |
| `make infer` | Inference stack: collection stack + NaVILA server + navila_node. Dry run; `MOTION=1` to move |
| `make instruct TEXT="..."` | Give NaVILA an instruction (while `make infer` runs) |
| `make navila-stop` | End the NaVILA episode, robot holds still |
| `make robot-stop` | Send the robot a direct StopMove |
| `make calibrate` | Measure the robot's real turn (and with `FORWARD=1` walk) speeds, for `configs/robots/roxy.yaml` |
| `make navila-server` | NaVILA server alone (GPU machine) |
| `make ros-shell` | Shell in the ROS image on the host network, for `ros2 topic list` etc. |
| `make down` | Stop every stack |

Planned: `pi-up` (step 2), `bag2ep` (5), `demo-site` / `demo-gpu` (7), `export` (8), `train-submit` (9).

## Layout

```
configs/          topics.yaml (single source of truth for topics), cyclonedds.xml, robots/, zenoh/
docker/
  ros/            Humble + go2_ros2_sdk + ros_ws (x86, no torch)
  navila/         CUDA + NaVILA server deps; code and weights are mounted
  pi/             arm64 Humble + Orbbec driver (step 2)
  compose/        building blocks (ros.yml, navila.yml) + mode overlays (collect.yml, infer.yml)
ros_ws/src/       deictic_bringup, deictic_interfaces, audio_stt, recorder, teleop, navila_node
third_party/      NaVILA fork (submodule, pending)
robot/            Pi and Jetson setup
data/             gitignored except README.md and schema/
data_pipeline/    real/ (bag -> episodes), deepoint/, export/, validate/
training/         LoRA configs, job scripts, envs for aberdeen / the Singapore cluster
eval/  scripts/
docs/             network.md, runbooks/, portfolio/ (project site + poster)
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
- [ ] 2. `docker/pi` arm64 image + Orbbec topics
- [ ] 3. `topics.yaml` + `deictic_bringup mode:=collect`, teleop + mic STT
- [ ] 4. `recorder` with episode markers, `make collect` end to end, same image on the 24.04 laptop
- [ ] 5. `data_pipeline/real`: bag -> canonical episodes (define `data/schema` first)
- [ ] 6. `docker/navila` + `navila_node` + `mode:=infer` (pretrained). Server image done and tested
- [ ] 7. Remote demo: zenoh configs, two compose files, latency test
- [ ] 8. `data_pipeline/deepoint` augmentation + `export` to NaVILA format
- [ ] 9. `training/`: env, LoRA config, aberdeen job scripts

## Rules

Branching and commit conventions: [CONTRIBUTING.md](CONTRIBUTING.md).

- Never commit data, bags, checkpoints or `.env`. Data lives on HF Hub / Drive / the servers (`data/README.md`).
- Every new topic goes in `configs/topics.yaml` first.
- Every new workflow gets a `make` target and a line in this README.
