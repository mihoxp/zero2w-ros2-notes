#!/bin/bash
# Started by v7rc-car.service: DFRobot motor driver + V7RC bridge (UDP 6188, fed by the BLE relay).
source /opt/ros/humble/setup.bash
source /home/pi/ros2_ws/install/setup.bash

LOG=/tmp/v7rc_car
mkdir -p $LOG
# Pololu Zumo chassis, 75:1 HP motors at 7.4 V; track centres 8.5 cm apart (measured)
# Measured on the car: M1 = right wheel (CW = forward), M2 = left wheel (CW = backward)
ros2 run dfrobot_motor_driver motor_driver --ros-args \
    -p wheel_separation:=0.085 -p max_wheel_speed:=0.5 \
    -p left_motor:=2 -p right_motor:=1 -p invert_left:=true -p invert_right:=false \
    > $LOG/motor_driver.log 2>&1 &
ros2 run v7rc_bridge v7rc_bridge --ros-args \
    -p track_width:=0.085 > $LOG/v7rc_bridge.log 2>&1 &

trap 'kill $(jobs -p) 2>/dev/null; wait; exit 0' TERM INT
echo "motor_driver + v7rc_bridge started, logs in $LOG"
# If either node exits, stop the other so systemd restarts both
wait -n
echo "A node exited, stopping the other"
kill $(jobs -p) 2>/dev/null
wait
exit 1
