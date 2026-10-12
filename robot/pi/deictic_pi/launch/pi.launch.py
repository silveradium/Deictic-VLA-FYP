"""Pi side of Roxy: the Orbbec Gemini 335 under orbbec/ (configs/topics.yaml, camera section).

  ros2 launch deictic_pi pi.launch.py                      what `make pi-up` runs
  ros2 launch deictic_pi pi.launch.py enable_depth:=true   needs a 5 A supply (robot/pi/README.md)

Colour at 640x480, 15 Hz. navila_node holds still on frames older than 0.5 s and adds a history
frame every 25 cm / 15 deg, so it needs the full rate, not a preview. Raw frames stay on the Pi:
other machines subscribe to the JPEG stream (orbbec/color/image_raw/compressed, ~45 KB a frame),
and bringup decodes it back to raw for NaVILA (orbbec/color/image_decoded). 4:3 also loses less
than 16:9 to NaVILA's centre-square crop (configs/robots/roxy.yaml, image_mode).

Depth is off by default: it switches on the IR projector, and on a supply the Pi doesn't detect as
5 A (USB limited to 600 mA) the camera browns out and drops off USB a moment after streaming starts.
"""

import os

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, IncludeLaunchDescription
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import LaunchConfiguration


def generate_launch_description():
    arg = LaunchConfiguration
    orbbec = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(os.path.join(
            get_package_share_directory("orbbec_camera"), "launch", "gemini_330_series.launch.py")),
        launch_arguments={
            "camera_name": "orbbec",  # orbbec/...: the Go2 driver already publishes camera/image_raw
            "color_width": arg("color_width"),
            "color_height": arg("color_height"),
            "color_fps": arg("fps"),
            "enable_depth": arg("enable_depth"),
            "depth_width": arg("depth_width"),
            "depth_height": arg("depth_height"),
            "depth_fps": arg("fps"),
            "enable_left_ir": "false",
            "enable_right_ir": "false",
            "enable_point_cloud": "false",
            "enable_colored_point_cloud": "false",
            "depth_registration": "false",
        }.items(),
    )

    return LaunchDescription([
        DeclareLaunchArgument("color_width", default_value="640"),
        DeclareLaunchArgument("color_height", default_value="480"),
        DeclareLaunchArgument("fps", default_value="15", description="Colour (and depth) frame rate"),
        DeclareLaunchArgument("enable_depth", default_value="false",
                              description="Depth stream; needs a 5 A supply (robot/pi/README.md)"),
        DeclareLaunchArgument("depth_width", default_value="640"),
        DeclareLaunchArgument("depth_height", default_value="480"),
        orbbec,
    ])
