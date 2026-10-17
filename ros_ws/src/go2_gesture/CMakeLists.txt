cmake_minimum_required(VERSION 3.8)
project(go2_gesture)

find_package(ament_cmake REQUIRED)

# Install the executable node script (same pattern as go2_brain/stub_brain.py).
install(
  PROGRAMS scripts/gesture_teleop.py
  DESTINATION lib/${PROJECT_NAME}
)

# Install launch files, config, and the bundled model files.
install(
  DIRECTORY launch config model
  DESTINATION share/${PROJECT_NAME}
)

ament_package()
