cmake_minimum_required(VERSION 3.8)
project(deictic_bringup)

find_package(ament_cmake REQUIRED)

install(DIRECTORY launch DESTINATION share/${PROJECT_NAME})
install(PROGRAMS scripts/go2_watchdog DESTINATION lib/${PROJECT_NAME})

ament_package()
