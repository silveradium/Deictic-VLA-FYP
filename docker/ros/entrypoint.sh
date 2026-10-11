#!/bin/bash
set -e
if [ -n "${CYCLONEDDS_URI}" ] && [ -z "${DDS_IFACE}" ]; then
    echo "DDS_IFACE is not set: put the robot/Pi LAN interface in .env (ip route get 1.1.1.1)," >&2
    echo "see configs/cyclonedds.xml and docs/runbooks/dds.md" >&2
    exit 1
fi
source "/opt/ros/${ROS_DISTRO}/setup.bash"
source /opt/go2_ws/install/setup.bash
source /ros2_ws/install/setup.bash
exec "$@"
