# syntax=docker/dockerfile:1
# Light ROS 2 Humble image (x86): go2_ros2_sdk WebRTC bridge + our ros_ws packages.
# Same image on the 22.04 workstation and the 24.04 laptop. No torch: NaVILA runs in
# docker/navila and is reached over HTTP.
#
# Two workspaces so editing our packages doesn't rebuild the driver:
#   /opt/go2_ws   underlay, go2_ros2_sdk (our fork)
#   /ros2_ws      overlay, ros_ws/src from this repo
#
# Build contexts (docker/compose/ros.yml):
#   default  repo root
#   go2_sdk  $GO2_SDK_DIR
#   navila   $NAVILA_DIR (only navila_go2/common, pure Python)
ARG ROS_DISTRO=humble
FROM ros:${ROS_DISTRO}-ros-base

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_DEFAULT_TIMEOUT=120 \
    PIP_RETRIES=10

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3-pip portaudio19-dev ffmpeg \
        ros-${ROS_DISTRO}-rmw-cyclonedds-cpp \
        ros-${ROS_DISTRO}-rosbag2-storage-mcap \
    && rm -rf /var/lib/apt/lists/*

# --- go2_ros2_sdk underlay ---
WORKDIR /opt/go2_ws

# torch/torchvision are only used by coco_detector, which is not built
COPY --from=go2_sdk requirements.txt /tmp/go2_requirements.txt
RUN --mount=type=cache,target=/root/.cache/pip \
    grep -vE '^(torch|torchvision)\b' /tmp/go2_requirements.txt > /tmp/go2_req.txt \
    && env -u PIP_NO_CACHE_DIR pip3 install -r /tmp/go2_req.txt

COPY --from=go2_sdk --exclude=**/.git --exclude=*.ply . src/go2_ros2_sdk
RUN touch src/go2_ros2_sdk/coco_detector/COLCON_IGNORE \
    && apt-get update \
    && rosdep update --rosdistro ${ROS_DISTRO} \
    && rosdep install --from-paths src --ignore-src -r -y \
    && rm -rf /var/lib/apt/lists/* \
    && . /opt/ros/${ROS_DISTRO}/setup.sh \
    && colcon build --cmake-args -DCMAKE_BUILD_TYPE=Release \
    && rm -rf build log

# The driver exits for good when its first connection attempt times out (a lossy Wi-Fi moment):
# respawn it, so it keeps trying. deictic_bringup's go2_watchdog restarts it when odometry stalls.
COPY docker/ros/patch_go2_respawn.py /tmp/patch_go2_respawn.py
RUN python3 /tmp/patch_go2_respawn.py \
        /opt/go2_ws/install/go2_robot_sdk/share/go2_robot_sdk/launch/robot.launch.py

# NaVILA client + mid-level controller from the NaVILA fork, used by navila_node.
# Pure Python (numpy, opencv, requests), so the same code runs in sim and on the robot.
COPY --from=navila navila_go2/__init__.py /opt/navila/navila_go2/__init__.py
COPY --from=navila navila_go2/common /opt/navila/navila_go2/common
ENV PYTHONPATH=/opt/navila

# Mic speech-to-text for audio_stt (Moonshine, CPU; portaudio comes from apt above). numpy stays
# on 1.x, which ROS Humble's compiled Python modules and go2's requirements both need.
RUN --mount=type=cache,target=/root/.cache/pip \
    env -u PIP_NO_CACHE_DIR pip3 install "numpy<2" moonshine-voice

# Spoken replies for voice: Gemini client, Piper TTS (+ the voice, baked in) and sox for the
# voice effects. Same numpy pin.
RUN --mount=type=cache,target=/root/.cache/pip \
    apt-get update && apt-get install -y --no-install-recommends sox \
    && rm -rf /var/lib/apt/lists/* \
    && env -u PIP_NO_CACHE_DIR pip3 install "numpy<2" piper-tts google-genai \
    && python3 -m piper.download_voices --data-dir /opt/piper-voices en_US-ryan-medium

# Hand-gesture driving for go2_gesture: MediaPipe hand landmarks + LiteRT for the keypoint
# classifier (much smaller than full TensorFlow). Same numpy pin.
RUN --mount=type=cache,target=/root/.cache/pip \
    env -u PIP_NO_CACHE_DIR pip3 install "numpy<2" mediapipe ai-edge-litert

# --- our packages overlay ---
WORKDIR /ros2_ws
COPY ros_ws/src src
RUN apt-get update \
    && . /opt/go2_ws/install/setup.sh \
    && rosdep install --from-paths src --ignore-src -r -y \
    && rm -rf /var/lib/apt/lists/* \
    && colcon build \
    && rm -rf build log

COPY --chmod=755 docker/ros/entrypoint.sh /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]
CMD ["bash"]
