#!/bin/bash
set -e
if [ -n "${CYCLONEDDS_URI}" ] && [ -z "${DDS_IFACE}" ]; then
    echo "DDS_IFACE is not set: put the Pi's LAN interface in .env (wlan0 on Wi-Fi)," >&2
    echo "see configs/cyclonedds.xml and robot/pi/README.md" >&2
    exit 1
fi
source "/opt/ros/${ROS_DISTRO}/setup.bash"
source /pi_ws/install/setup.bash
exec "$@"
