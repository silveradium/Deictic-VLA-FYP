"""Robot-side bringup: one launch for both data collection and inference.

  ros2 launch deictic_bringup bringup.launch.py mode:=collect|infer

Both modes run the same stack: the go2_ros2_sdk WebRTC bridge plus joystick teleop through
twist_mux (configs/topics.yaml). The joystick nodes are started here, not by go2's
robot.launch.py: go2 doesn't remap teleop_twist_joy, so it published on cmd_vel at
NaVILA's priority instead of cmd_vel_joy. The joystick (cmd_vel_joy, priority 10) overrides
cmd_vel (priority 5) while its enable button is held, so it stays the safety override in infer
mode too (configs/robots/roxy.yaml, go2_teleop_node). The only
difference between modes is who else runs:
  collect  a human drives with the joystick; the recorder writes the bag (build step 4)
  infer    navila_node publishes cmd_vel from NaVILA's decisions. Dry run unless
           enable_motion:=true. Server URL from NAVILA_SERVER_URL.

Robot IP and AES key come from the environment (GO2_IP / GO2_AES_KEY in .env, passed
in as ROBOT_IP / ROBOT_AES_KEY by docker/compose/ros.yml).

The Orbbec on the Pi (`make pi-up`) sends only its JPEG stream over Wi-Fi; orbbec_decode turns
it back into a raw image here, orbbec/color/image_decoded, which navila_node reads when
NAVILA_CAMERA_TOPIC points at it. Without the Pi it just waits.

stt:=true (both modes) listens to the mic on this machine: stt_node transcribes, and wake_router
sends "Hey Roxy, <instruction>" to navila/instruction and any sentence starting with "stop" at
once (audio_stt). Set STT=false in .env on a machine without the mic.

voice:=true (VOICE=true + GEMINI_API_KEY in .env) adds spoken replies (voice): dialog_node asks
Gemini whether a request is a command (sent to navila/instruction word for word) or a question
(answered aloud), tts_node speaks through the Pi's speaker (`make pi-up`). wake_router then stops
forwarding requests itself.
"""

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, GroupAction, IncludeLaunchDescription
from launch.conditions import IfCondition, LaunchConfigurationEquals
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import (
    EnvironmentVariable,
    LaunchConfiguration,
    PathJoinSubstitution,
    PythonExpression,
)
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

    # Restarts the driver when odometry stops (a silent WebRTC link); the driver itself runs with
    # respawn, patched into go2_robot_sdk's robot.launch.py by docker/ros/Dockerfile
    go2_watchdog = Node(
        package="deictic_bringup",
        executable="go2_watchdog",
        output="screen",
        parameters=[LaunchConfiguration("robot_config")],
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
        # go2's settings, then ours on top: drive only while the enable button is held (roxy.yaml)
        parameters=[PathJoinSubstitution([go2_config, "twist_mux.yaml"]), LaunchConfiguration("robot_config")],
        remappings=[("cmd_vel", "cmd_vel_joy")],  # twist_mux priority 10, above NaVILA
    )

    orbbec_decode = Node(
        package="image_transport",
        executable="republish",
        name="orbbec_decode",
        arguments=["compressed", "raw"],
        remappings=[
            ("in/compressed", "orbbec/color/image_raw/compressed"),
            ("out", "orbbec/color/image_decoded"),
        ],
        condition=IfCondition(LaunchConfiguration("orbbec")),
    )

    stt = IfCondition(LaunchConfiguration("stt"))
    stt_node = Node(
        package="audio_stt",
        executable="stt_node",
        output="screen",
        condition=stt,
        parameters=[LaunchConfiguration("robot_config"), {"mic": LaunchConfiguration("mic")}],
    )
    voice_on = PythonExpression(["'", LaunchConfiguration("voice"), "' == 'true'"])
    wake_router = Node(
        package="audio_stt",
        executable="wake_router",
        output="screen",
        condition=stt,
        parameters=[LaunchConfiguration("robot_config"), {
            # with voice, dialog_node decides what reaches NaVILA
            "forward_to_navila": PythonExpression(["not ", voice_on]),
        }],
    )

    voice = IfCondition(LaunchConfiguration("voice"))
    dialog_node = Node(
        package="voice",
        executable="dialog_node",
        output="screen",
        condition=voice,
        parameters=[LaunchConfiguration("robot_config")],
    )
    tts_node = Node(
        package="voice",
        executable="tts_node",
        output="screen",
        condition=voice,
        parameters=[LaunchConfiguration("robot_config")],
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
        DeclareLaunchArgument("stt", default_value="true",
                              description="Mic speech-to-text + 'Hey Roxy' -> navila/instruction"),
        DeclareLaunchArgument("mic", default_value="dji", choices=["dji", "builtin"],
                              description="stt: DJI Mic Mini receiver or this machine's built-in mic"),
        DeclareLaunchArgument("voice", default_value="false",
                              description="Spoken replies: Gemini dialog + TTS on the Pi speaker (GEMINI_API_KEY)"),
        DeclareLaunchArgument("orbbec", default_value="true",
                              description="Decode the Pi's Orbbec JPEG stream to orbbec/color/image_decoded"),
        DeclareLaunchArgument("camera_topic", default_value="camera/image_raw",
                              description="infer: RGB topic NaVILA sees (Go2 front camera by default, "
                                          "orbbec/color/image_decoded for the Orbbec)"),
        DeclareLaunchArgument("enable_motion", default_value="false",
                              description="infer: actually publish cmd_vel (false = dry run, log only)"),
        GroupAction([go2_bridge]),  # scoped: the include's arguments (joystick:=false) stay inside
        go2_watchdog,
        joy,
        teleop,
        orbbec_decode,
        stt_node,
        wake_router,
        dialog_node,
        tts_node,
        navila,
    ])
