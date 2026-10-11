"""Robot-side bringup: one launch for both data collection and inference.

  ros2 launch deictic_bringup bringup.launch.py mode:=collect|infer

Both modes run the same stack: the go2_ros2_sdk WebRTC bridge plus joystick teleop through
twist_mux (configs/topics.yaml). The joystick nodes are started here, not by go2's
robot.launch.py: go2 doesn't remap teleop_twist_joy, so it published on cmd_vel at
NaVILA's priority instead of cmd_vel_joy. The joystick (cmd_vel_joy, priority 10) always overrides
cmd_vel (priority 5), so it stays the safety override in infer mode too. The only
difference between modes is who else runs:
  collect  a human drives with the joystick; the recorder writes the bag (build step 4)
  infer    navila_node publishes cmd_vel from NaVILA's decisions. Dry run unless
           enable_motion:=true. Server URL from NAVILA_SERVER_URL.

Robot IP and AES key come from the environment (GO2_IP / GO2_AES_KEY in .env, passed
in as ROBOT_IP / ROBOT_AES_KEY by docker/compose/ros.yml).
"""

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, GroupAction, IncludeLaunchDescription
from launch.conditions import IfCondition, LaunchConfigurationEquals
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import EnvironmentVariable, LaunchConfiguration, PathJoinSubstitution
from launch_ros.actions import Node
from launch_ros.substitutions import FindPackageShare


def generate_launch_description():
    go2_bridge = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(
            PathJoinSubstitution([FindPackageShare("go2_robot_sdk"), "launch", "robot.launch.py"])
        ),
        launch_arguments={
            "rviz2": LaunchConfiguration("rviz"),
            "foxglove": LaunchConfiguration("foxglove"),
            "joystick": "false",  # started below, remapped to cmd_vel_joy
            "teleop": "true",  # twist_mux: the driver only listens to its output, cmd_vel_out
            "nav2": "false",
            "slam": "false",
        }.items(),
    )

    go2_config = PathJoinSubstitution([FindPackageShare("go2_robot_sdk"), "config"])
    joystick = IfCondition(LaunchConfiguration("joystick"))
    joy = Node(
        package="joy",
        executable="joy_node",
        condition=joystick,
        parameters=[PathJoinSubstitution([go2_config, "joystick.yaml"])],
    )
    teleop = Node(
        package="teleop_twist_joy",
        executable="teleop_node",
        name="go2_teleop_node",  # matches the parameter block in go2's twist_mux.yaml
        condition=joystick,
        parameters=[PathJoinSubstitution([go2_config, "twist_mux.yaml"])],
        remappings=[("cmd_vel", "cmd_vel_joy")],  # twist_mux priority 10, above NaVILA
    )

    navila = Node(
        package="navila_node",
        executable="navila_node",
        output="screen",
        condition=LaunchConfigurationEquals("mode", "infer"),
        parameters=[LaunchConfiguration("robot_config"), {
            "server_url": EnvironmentVariable("NAVILA_SERVER_URL", default_value="http://127.0.0.1:8000"),
            "camera_topic": LaunchConfiguration("camera_topic"),
            "enable_motion": LaunchConfiguration("enable_motion"),
        }],
    )

    return LaunchDescription([
        DeclareLaunchArgument("mode", default_value="collect", choices=["collect", "infer"]),
        DeclareLaunchArgument("rviz", default_value="false", description="RViz2 (needs a display)"),
        DeclareLaunchArgument("foxglove", default_value="true", description="Foxglove bridge on :8765"),
        DeclareLaunchArgument("joystick", default_value="true", description="joy_node + teleop_twist_joy"),
        DeclareLaunchArgument("robot_config", default_value="/deictic/configs/robots/roxy.yaml",
                              description="Robot parameters (configs/robots/, mounted in the container)"),
        DeclareLaunchArgument("camera_topic", default_value="camera/image_raw",
                              description="infer: RGB topic NaVILA sees (Go2 front camera by default)"),
        DeclareLaunchArgument("enable_motion", default_value="false",
                              description="infer: actually publish cmd_vel (false = dry run, log only)"),
        GroupAction([go2_bridge]),  # scoped: the include's arguments (joystick:=false) stay inside
        joy,
        teleop,
        navila,
    ])
