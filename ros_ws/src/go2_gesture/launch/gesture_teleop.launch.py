"""gesture_teleop on its own, next to a running bringup (make collect).

bringup.launch.py starts it itself with gesture:=true (GESTURE=true in .env); this launch is
for running it by hand, e.g. in `make ros-shell`:

  ros2 launch go2_gesture gesture_teleop.launch.py                       # Go2 front camera
  ros2 launch go2_gesture gesture_teleop.launch.py mode:=tricks          # signs -> tricks
  ros2 launch go2_gesture gesture_teleop.launch.py image_topic:='' camera_device:=0   # webcam
  ros2 launch go2_gesture gesture_teleop.launch.py max_linear_speed:=0.2
"""
import os

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node
from launch_ros.parameter_descriptions import ParameterValue


def generate_launch_description():
    params_file = os.path.join(
        get_package_share_directory('go2_gesture'), 'config', 'gesture_teleop.yaml')

    def arg(name):
        return ParameterValue(LaunchConfiguration(name), value_type=str)

    return LaunchDescription([
        DeclareLaunchArgument('mode', default_value='drive', choices=['drive', 'tricks'],
                              description='drive: signs -> cmd_vel; tricks: signs -> voice/trick'),
        DeclareLaunchArgument('image_topic', default_value='camera/image_raw',
                              description="Camera topic; '' = local webcam (camera_device)"),
        DeclareLaunchArgument('camera_device', default_value='0',
                              description='Webcam index (/dev/videoN) when image_topic is empty'),
        DeclareLaunchArgument('max_linear_speed', default_value='0.3',
                              description='Max forward/backward speed (m/s)'),
        DeclareLaunchArgument('max_angular_speed', default_value='0.6',
                              description='Max turn speed (rad/s)'),

        Node(
            package='go2_gesture',
            executable='gesture_teleop.py',
            name='gesture_teleop',
            output='screen',
            parameters=[
                params_file,
                {
                    'mode':              arg('mode'),
                    'image_topic':       arg('image_topic'),
                    'camera_device':     LaunchConfiguration('camera_device'),
                    'max_linear_speed':  LaunchConfiguration('max_linear_speed'),
                    'max_angular_speed': LaunchConfiguration('max_angular_speed'),
                },
            ],
        ),
    ])
