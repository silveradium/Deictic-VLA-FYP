"""Adds respawn to go2_driver_node in go2_robot_sdk's robot.launch.py (docker/ros/Dockerfile).

  python3 patch_go2_respawn.py <installed robot.launch.py>

Fails the build if the SDK's launch file no longer has the expected Node(...) block, so a changed
SDK never silently loses the respawn.
"""

import sys
from pathlib import Path

path = Path(sys.argv[1])
text = path.read_text()
anchor = "executable='go2_driver_node',\n                name='go2_driver_node',\n"
if text.count(anchor) != 1:
    sys.exit(f"{path}: go2_driver_node block not found, update docker/ros/patch_go2_respawn.py")
path.write_text(text.replace(anchor, anchor + "                respawn=True,\n                respawn_delay=5.0,\n"))
print(f"{path}: go2_driver_node respawns")
