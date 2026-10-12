# Front door for every workflow. `make help` lists the targets.
# Compose stacks = building blocks (ros.yml, navila.yml) + a mode overlay, all resolved
# from the repo root so .env and relative paths work.

SHELL := /bin/bash
.DEFAULT_GOAL := help

COMPOSE := docker compose --project-directory . -f docker/compose
STACK_COLLECT := $(COMPOSE)/ros.yml -f docker/compose/collect.yml
STACK_INFER   := $(COMPOSE)/ros.yml -f docker/compose/navila.yml -f docker/compose/infer.yml
STACK_NAVILA  := $(COMPOSE)/navila.yml
STACK_SITE    := $(COMPOSE)/ros.yml -f docker/compose/infer.yml
STACK_PI      := $(COMPOSE)/pi.yml

# The recorder container runs as you, so recorded sessions in DATA_DIR are not root's
export HOST_UID := $(shell id -u)
export HOST_GID := $(shell id -g)
DATA_DIR := $(or $(shell grep -oP '^DATA_DIR=\K\S+' .env 2>/dev/null),./data)

.PHONY: help setup build build-ros build-navila collect infer navila-server down ros-shell \
        instruct navila-stop robot-stop calibrate stt-echo say voice-preset \
        demo-gpu demo-site navila-health build-pi pi-up pi-down pi-logs pi-shell \
        logs preview orbbec-view \
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
collect: ## Collect data: starts bringup (Go2, mic) if needed, then the terminal recorder, one bag per episode
	@mkdir -p $(DATA_DIR)/sessions
	$(STACK_COLLECT) up -d bringup
	$(STACK_COLLECT) run --rm recorder

logs: ## Follow the bringup logs (Go2 bridge, STT) while `make collect` runs, in a second terminal
	$(STACK_COLLECT) logs -f --tail 50 bringup

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

stt-echo: ## Print what the mic hears (stt/transcript) while collect / infer runs
	$(STACK_COLLECT) exec bringup /entrypoint.sh ros2 topic echo /stt/transcript std_msgs/msg/String

say: ## Make Roxy speak through the Pi speaker (VOICE=true): TEXT="Ruh-roh, hello!"
	@test -n "$(TEXT)" || { echo 'usage: make say TEXT="Ruh-roh, hello!"'; exit 1; }
	$(STACK_COLLECT) exec bringup /entrypoint.sh ros2 topic pub --once -w 1 \
		/voice/say std_msgs/msg/String "{data: \"$(TEXT)\"}"

voice-preset: ## Switch Roxy's voice while it runs (VOICE=true): PRESET=scooby|robot
	@test -n "$(PRESET)" || { echo 'usage: make voice-preset PRESET=robot'; exit 1; }
	$(STACK_COLLECT) exec bringup /entrypoint.sh ros2 param set /tts_node voice_preset $(PRESET)

orbbec-view: ## Watch the Pi's Orbbec colour feed in a browser, with its FOV (http://localhost:8090; PORT=...)
	$(STACK_COLLECT) run --rm --no-deps -v $(CURDIR)/scripts:/deictic/scripts:ro bringup \
		python3 /deictic/scripts/orbbec_view.py --port $(or $(PORT),8090)

# --- data collection (docs/collection_protocol.md) -----------------------------
preview: ## Look inside a session's episode bags: video, odom path, labels, topics (SESSION=..., default latest)
	@s="$(SESSION)"; [ -n "$$s" ] || s=$$(ls -1 $(DATA_DIR)/sessions 2>/dev/null | tail -1); \
	test -n "$$s" || { echo "no sessions in $(DATA_DIR)/sessions"; exit 1; }; \
	$(STACK_COLLECT) run --rm --no-deps -T recorder python3 /deictic/data_pipeline/validate/preview.py \
		/deictic/data/sessions/$$s \
	&& echo "open $(DATA_DIR)/sessions/$$s/preview/index.html" \
	&& (xdg-open "$(DATA_DIR)/sessions/$$s/preview/index.html" >/dev/null 2>&1 &)

# --- live demo: venue laptop + remote GPU (docs/runbooks/live-demo.md) -------
demo-gpu: ## Live demo, workstation: NaVILA server for the venue laptop, over Tailscale
	@ip=$$(tailscale ip -4 2>/dev/null | head -1); port=$$(grep -oP '^NAVILA_PORT=\K[0-9]+' .env 2>/dev/null || echo 8000); \
	echo "On the laptop, in .env: NAVILA_SERVER_URL=http://$${ip:-TAILSCALE_IP}:$$port"
	$(STACK_NAVILA) up

demo-site: ## Live demo, venue laptop: Go2 bridge, joystick, mic + voice, NaVILA via the workstation (MOTION=1 to move)
	@grep -qE '^NAVILA_SERVER_URL=http://(127\.0\.0\.1|localhost)' .env \
		&& { echo "NAVILA_SERVER_URL in .env points at this machine: set it to the workstation (make demo-gpu prints it)"; exit 1; } || true
	NAVILA_MOTION=$(if $(MOTION),true,false) $(STACK_SITE) up

navila-health: ## Check the NaVILA server answers at NAVILA_SERVER_URL (run on the laptop)
	@url=$$(grep -oP '^NAVILA_SERVER_URL=\K\S+' .env); \
	curl -fsS --max-time 5 "$$url/health" && echo "  <- NaVILA server OK at $$url" \
		|| { echo "NaVILA server not reachable at $$url (make demo-gpu running? Tailscale up on both?)"; exit 1; }

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
bag2ep: ## [step 5] Bag -> canonical episodes (BAG=...)
	@echo "bag2ep: not implemented yet (build step 5)"; exit 1

export: ## [step 8] Episodes -> NaVILA training format
	@echo "export: not implemented yet (build step 8)"; exit 1

train-submit: ## [step 9] Submit LoRA training (TARGET=aberdeen|sg)
	@echo "train-submit: not implemented yet (build step 9)"; exit 1
