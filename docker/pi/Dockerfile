# syntax=docker/dockerfile:1
# Pi 5 image (arm64): ROS 2 Humble + Orbbec Gemini 335 driver, publishing orbbec/ topics.
# Built and run on the Pi (`make pi-up`, robot/pi/README.md). Raw frames stay on the Pi; other
# machines use the JPEG stream (configs/topics.yaml, camera section).
#
# Build context: repo root (docker/compose/pi.yml). .dockerignore lets in robot/pi/deictic_pi
# and this folder's entrypoint only.
ARG ROS_DISTRO=humble
FROM ros:${ROS_DISTRO}-ros-base

ENV DEBIAN_FRONTEND=noninteractive

# Orbbec driver v2 (Gemini 330 series) from the ROS apt repo: an arm64 binary, nothing to compile.
# image-transport-plugins: the driver also publishes .../compressed, which other machines use.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ros-${ROS_DISTRO}-orbbec-camera \
        ros-${ROS_DISTRO}-image-transport-plugins \
        ros-${ROS_DISTRO}-rmw-cyclonedds-cpp \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /pi_ws
COPY robot/pi/deictic_pi src/deictic_pi
RUN . /opt/ros/${ROS_DISTRO}/setup.sh \
    && colcon build \
    && rm -rf build log

COPY --chmod=755 docker/pi/entrypoint.sh /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]
CMD ["bash"]
