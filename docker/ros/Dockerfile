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
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3-pip portaudio19-dev ffmpeg \
        ros-${ROS_DISTRO}-rmw-cyclonedds-cpp \
        ros-${ROS_DISTRO}-rosbag2-storage-mcap \
    && rm -rf /var/lib/apt/lists/*

# --- go2_ros2_sdk underlay ---
WORKDIR /opt/go2_ws

# torch/torchvision are only used by coco_detector, which is not built
COPY --from=go2_sdk requirements.txt /tmp/go2_requirements.txt
RUN grep -vE '^(torch|torchvision)\b' /tmp/go2_requirements.txt > /tmp/go2_req.txt \
    && pip3 install -r /tmp/go2_req.txt

COPY --from=go2_sdk --exclude=**/.git --exclude=*.ply . src/go2_ros2_sdk
RUN touch src/go2_ros2_sdk/coco_detector/COLCON_IGNORE \
    && apt-get update \
    && rosdep update --rosdistro ${ROS_DISTRO} \
    && rosdep install --from-paths src --ignore-src -r -y \
    && rm -rf /var/lib/apt/lists/* \
    && . /opt/ros/${ROS_DISTRO}/setup.sh \
    && colcon build --cmake-args -DCMAKE_BUILD_TYPE=Release \
    && rm -rf build log

# NaVILA client + mid-level controller from the NaVILA fork, used by navila_node.
# Pure Python (numpy, opencv, requests), so the same code runs in sim and on the robot.
COPY --from=navila navila_go2/__init__.py /opt/navila/navila_go2/__init__.py
COPY --from=navila navila_go2/common /opt/navila/navila_go2/common
ENV PYTHONPATH=/opt/navila

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
