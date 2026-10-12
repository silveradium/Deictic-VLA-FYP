# Front door for every workflow. `make help` lists the targets.
# Compose stacks = building blocks (ros.yml, navila.yml) + a mode overlay, all resolved
# from the repo root so .env and relative paths work.

SHELL := /bin/bash
.DEFAULT_GOAL := help

COMPOSE := docker compose --project-directory . -f docker/compose
STACK_COLLECT := $(COMPOSE)/ros.yml -f docker/compose/collect.yml
STACK_INFER   := $(COMPOSE)/ros.yml -f docker/compose/navila.yml -f docker/compose/infer.yml
STACK_NAVILA  := $(COMPOSE)/navila.yml
STACK_PI      := $(COMPOSE)/pi.yml

.PHONY: help setup build build-ros build-navila collect infer navila-server down ros-shell \
        instruct navila-stop robot-stop calibrate build-pi pi-up pi-down pi-logs pi-shell \
        demo-site demo-gpu bag2ep export train-submit

help: ## List targets
	@grep -hE '^[a-zA-Z0-9_-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  \033[36m%-15s\033[0m %s\n", $$1, $$2}'

# --- setup -------------------------------------------------------------------
setup: .env ## Fetch submodules, create .env from the example, build all x86 images
	git submodule update --init --recursive
	$(MAKE) build

.env:
	cp .env.example $@
	@echo "Created .env: fill in GO2_IP, GO2_AES_KEY and DDS_IFACE"

build: build-ros build-navila ## Build all x86 images

build-ros: ## Build the ROS image (Humble + go2_ros2_sdk + ros_ws)
	$(STACK_COLLECT) build bringup

build-navila: ## Build the NaVILA CUDA image
	$(STACK_NAVILA) build navila_server

# --- run ---------------------------------------------------------------------
collect: ## Collection stack: Go2 bridge + teleop (Ctrl-C to stop)
	$(STACK_COLLECT) up

infer: ## Inference stack: Go2 bridge + teleop + NaVILA (dry run; MOTION=1 to move)
	NAVILA_MOTION=$(if $(MOTION),true,false) $(STACK_INFER) up

instruct: ## Send NaVILA an instruction while `make infer` runs: TEXT="walk to the door"
	@test -n "$(TEXT)" || { echo 'usage: make instruct TEXT="walk to the door and stop"'; exit 1; }
	$(STACK_INFER) exec bringup /entrypoint.sh ros2 topic pub --once -w 1 \
		/navila/instruction std_msgs/msg/String "{data: \"$(TEXT)\"}"

navila-stop: ## End the NaVILA episode and hold still
	$(STACK_INFER) exec bringup /entrypoint.sh ros2 topic pub --once -w 1 \
		/navila/instruction std_msgs/msg/String "{data: stop}"

navila-server: ## NaVILA server alone (GPU machine, e.g. remote demos)
	$(STACK_NAVILA) up

down: ## Stop every stack
	$(STACK_INFER) down --remove-orphans

ros-shell: ## Shell in the ROS image, on the host network (ros2 topic list, ...)
	$(STACK_COLLECT) run --rm bringup bash

robot-stop: ## Send the robot Unitree's StopMove directly (any running stack)
	$(STACK_INFER) exec bringup /entrypoint.sh ros2 topic pub --once -w 1 /webrtc_req \
		go2_interfaces/msg/WebRtcReq "{topic: 'rt/api/sport/request', api_id: 1003, parameter: '', priority: 1}"

calibrate: ## Measure the robot's real turn rates (with `make collect`; FORWARD=1 also walks)
	$(STACK_COLLECT) exec bringup /entrypoint.sh ros2 run navila_node calibrate_motion \
		--ros-args -p forward:=$(if $(FORWARD),true,false)

# --- Pi (run these on the Pi; robot/pi/README.md) ------------------------------
build-pi: ## On the Pi: build the Pi image (arm64 Humble + Orbbec driver)
	$(STACK_PI) build pi

pi-up: ## On the Pi: start the Orbbec camera in the background (comes back after a reboot)
	$(STACK_PI) up -d
	@echo "Camera starting: make pi-logs to follow it"

pi-down: ## On the Pi: stop the camera container
	$(STACK_PI) down

pi-logs: ## On the Pi: follow the camera container's logs
	$(STACK_PI) logs -f pi

pi-shell: ## On the Pi: shell in the running camera container (ros2 topic hz ...)
	$(STACK_PI) exec pi /entrypoint.sh bash

# --- not implemented yet (build order in README) -----------------------------
demo-site: ## [step 7] Remote demo, robot site half (laptop)
	@echo "demo-site: not implemented yet (build step 7)"; exit 1

demo-gpu: ## [step 7] Remote demo, GPU half (workstation)
	@echo "demo-gpu: not implemented yet (build step 7)"; exit 1

bag2ep: ## [step 5] Bag -> canonical episodes (BAG=...)
	@echo "bag2ep: not implemented yet (build step 5)"; exit 1

export: ## [step 8] Episodes -> NaVILA training format
	@echo "export: not implemented yet (build step 8)"; exit 1

train-submit: ## [step 9] Submit LoRA training (TARGET=aberdeen|sg)
	@echo "train-submit: not implemented yet (build step 9)"; exit 1
