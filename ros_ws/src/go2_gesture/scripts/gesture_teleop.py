#!/usr/bin/env python3
"""gesture_teleop: live hand gestures -> cmd_vel.

Reads the Go2's front camera (camera/image_raw, from the go2_ros2_sdk driver) or, with
image_topic:='', a local webcam. MediaPipe HandLandmarker finds the hand, a TFLite MLP
classifies the hand sign, the most common sign over a short history window is the command.

Gesture -> command mapping:
  palm / stop          -> hold still
  stop_inverted / fist -> forward
  call / mute          -> backward
  one                  -> turn left  (angular.z > 0)
  two_up               -> turn right (angular.z < 0)

Publishes geometry_msgs/Twist on cmd_vel (twist_mux priority 5, below the joystick) at
control_rate, but ONLY while one of those gestures is in view. When the hand leaves (or shows
any other sign) for no_hand_timeout seconds it sends one zero Twist and goes quiet, so
twist_mux times the input out and voice tricks (trick_node, webrtc_req) are not interrupted.

The annotated frame is published on gesture/image (view it in Foxglove on :8765) and, with a
display, shown in a window; ESC in the window quits.

  ros2 launch go2_gesture gesture_teleop.launch.py
  ros2 launch go2_gesture gesture_teleop.launch.py image_topic:='' camera_device:=0   # webcam
"""
import copy
import csv
import itertools
import os
import threading
import time
from collections import Counter, deque

import cv2 as cv
import mediapipe as mp
import numpy as np
from mediapipe.tasks import python as mp_python
from mediapipe.tasks.python import vision as mp_vision

import rclpy
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data
from geometry_msgs.msg import Twist
from sensor_msgs.msg import Image
from ament_index_python.packages import get_package_share_directory

try:  # small LiteRT wheel first; full TensorFlow only as a fallback
    from ai_edge_litert.interpreter import Interpreter
except ImportError:
    try:
        from tflite_runtime.interpreter import Interpreter
    except ImportError:
        from tensorflow.lite import Interpreter


# ---------------------------------------------------------------------------
# Gesture -> velocity fractions (multiplied by max speeds at publish time).
# Label strings must match keypoint_classifier_label.csv exactly.
# ---------------------------------------------------------------------------
GESTURE_CMD: dict = {
    # label          : (linear_x_frac, angular_z_frac)
    'palm'           : ( 0.0,  0.0),   # stop
    'stop'           : ( 0.0,  0.0),   # stop
    'stop_inverted'  : ( 1.0,  0.0),   # forward
    'fist'           : ( 1.0,  0.0),   # forward
    'call'           : (-1.0,  0.0),   # backward
    'mute'           : (-1.0,  0.0),   # backward
    'one'            : ( 0.0,  1.0),   # turn left
    'two_up'         : ( 0.0, -1.0),   # turn right
}
_STOP = (0.0, 0.0)


# ---------------------------------------------------------------------------
# Inlined TFLite classifier (from the gesture repo's model/keypoint_classifier/).
# ---------------------------------------------------------------------------
class _KeyPointClassifier:
    def __init__(self, model_path: str, num_threads: int = 1):
        interp = Interpreter(model_path=model_path, num_threads=num_threads)
        interp.allocate_tensors()
        self._interp  = interp
        self._in_idx  = interp.get_input_details()[0]['index']
        self._out_idx = interp.get_output_details()[0]['index']

    def __call__(self, landmark_list: list) -> int:
        self._interp.set_tensor(
            self._in_idx, np.array([landmark_list], dtype=np.float32))
        self._interp.invoke()
        return int(np.argmax(np.squeeze(
            self._interp.get_tensor(self._out_idx))))


def _landmark_list(image: np.ndarray, landmarks) -> list:
    h, w = image.shape[:2]
    return [
        [min(int(lm.x * w), w - 1), min(int(lm.y * h), h - 1)]
        for lm in landmarks
    ]


def _preprocess(landmark_list: list) -> list:
    """Relative normalised coordinates, same preprocessing as training."""
    tmp = copy.deepcopy(landmark_list)
    bx, by = tmp[0]
    for pt in tmp:
        pt[0] -= bx
        pt[1] -= by
    flat = list(itertools.chain.from_iterable(tmp))
    mx = max(map(abs, flat)) or 1.0
    return [v / mx for v in flat]


_CHANNELS = {'bgr8': 3, 'rgb8': 3, 'bgra8': 4, 'rgba8': 4, 'mono8': 1}
_TO_BGR = {'rgb8': cv.COLOR_RGB2BGR, 'rgba8': cv.COLOR_RGBA2BGR,
           'bgra8': cv.COLOR_BGRA2BGR, 'mono8': cv.COLOR_GRAY2BGR}


def _image_to_bgr(msg: Image):
    """sensor_msgs/Image -> BGR ndarray without cv_bridge. None for unsupported encodings."""
    channels = _CHANNELS.get(msg.encoding)
    if channels is None:
        return None
    buf = np.frombuffer(msg.data, dtype=np.uint8).reshape(msg.height, msg.step)
    img = buf[:, :msg.width * channels].reshape(msg.height, msg.width, channels)
    if msg.encoding in _TO_BGR:
        return cv.cvtColor(img, _TO_BGR[msg.encoding])
    return img.copy()


# ---------------------------------------------------------------------------
# ROS 2 node: owns the publishers, the camera subscription and the publish timer.
# Gesture state is written by the detection loop (main thread), read by the timer.
# ---------------------------------------------------------------------------
class GestureTeleop(Node):
    def __init__(self):
        super().__init__('gesture_teleop')

        self.declare_parameter('image_topic',              'camera/image_raw')
        self.declare_parameter('camera_device',            0)
        self.declare_parameter('cap_width',                640)
        self.declare_parameter('cap_height',               480)
        self.declare_parameter('mirror',                   True)
        self.declare_parameter('show_window',              True)
        self.declare_parameter('max_linear_speed',         0.3)
        self.declare_parameter('max_angular_speed',        0.6)
        self.declare_parameter('control_rate',             20.0)
        self.declare_parameter('min_detection_confidence', 0.7)
        self.declare_parameter('min_tracking_confidence',  0.5)
        self.declare_parameter('gesture_history_len',      10)
        self.declare_parameter('no_hand_timeout',          0.5)

        p = self.get_parameter
        self.image_topic = str(p('image_topic').value)
        self.cam_device  = int(p('camera_device').value)
        self.cap_width   = int(p('cap_width').value)
        self.cap_height  = int(p('cap_height').value)
        self.mirror      = bool(p('mirror').value)
        self.show_window = bool(p('show_window').value) and bool(os.environ.get('DISPLAY'))
        self.max_linear  = float(p('max_linear_speed').value)
        self.max_angular = float(p('max_angular_speed').value)
        self.det_conf    = p('min_detection_confidence').value
        self.trk_conf    = p('min_tracking_confidence').value
        self.hist_len    = int(p('gesture_history_len').value)
        self.timeout     = p('no_hand_timeout').value

        pkg = get_package_share_directory('go2_gesture')
        self.landmarker_model = os.path.join(
            pkg, 'model', 'hand_landmarker', 'hand_landmarker.task')
        self.kp_model = os.path.join(
            pkg, 'model', 'keypoint_classifier', 'keypoint_classifier.tflite')
        self.kp_labels_path = os.path.join(
            pkg, 'model', 'keypoint_classifier', 'keypoint_classifier_label.csv')

        self._cmd_pub   = self.create_publisher(Twist, 'cmd_vel', 10)
        self._image_pub = self.create_publisher(Image, 'gesture/image', qos_profile_sensor_data)
        self.create_timer(1.0 / p('control_rate').value, self._publish)

        # Latest camera frame, handed from the subscription to the detection loop.
        self._frame = None
        self._frame_header = None
        self._frame_ready = threading.Condition()
        if self.image_topic:
            self.create_subscription(Image, self.image_topic, self._on_image, qos_profile_sensor_data)

        self._gesture   = 'stop'
        self._last_seen = 0.0     # monotonic time of the last commanding gesture
        self._active    = False   # publishing cmd_vel right now

        source = self.image_topic or f'/dev/video{self.cam_device}'
        self.get_logger().info(
            f'gesture_teleop up: camera={source}  '
            f'v_max={self.max_linear} m/s  w_max={self.max_angular} rad/s  '
            f'timeout={self.timeout}s  window={self.show_window}')

    def _on_image(self, msg: Image) -> None:
        frame = _image_to_bgr(msg)
        if frame is None:
            self.get_logger().warn(f'unsupported image encoding {msg.encoding}',
                                   throttle_duration_sec=5.0)
            return
        with self._frame_ready:
            self._frame, self._frame_header = frame, msg.header
            self._frame_ready.notify()

    def next_frame(self, timeout: float):
        """Block until a new camera frame arrives; (None, None) on timeout."""
        with self._frame_ready:
            if self._frame is None:
                self._frame_ready.wait(timeout)
            frame, header = self._frame, self._frame_header
            self._frame = None
            return frame, header

    def update_gesture(self, label: str) -> None:
        """Called from the detection loop each time a commanding gesture is seen."""
        self._gesture   = label
        self._last_seen = time.monotonic()

    def command(self):
        """(vx, wz) being published right now, None while idle."""
        if not self._active:
            return None
        lx, wz = GESTURE_CMD.get(self._gesture, _STOP)
        return lx * self.max_linear, wz * self.max_angular

    def publish_debug(self, image: np.ndarray, header) -> None:
        msg = Image()
        if header is not None:
            msg.header = header
        else:
            msg.header.stamp = self.get_clock().now().to_msg()
        msg.height, msg.width = image.shape[:2]
        msg.encoding = 'bgr8'
        msg.step = msg.width * 3
        msg.data = image.tobytes()
        self._image_pub.publish(msg)

    def _publish(self) -> None:
        if time.monotonic() - self._last_seen > self.timeout:
            if self._active:  # one stop, then leave cmd_vel to twist_mux / tricks
                self._active = False
                self._cmd_pub.publish(Twist())
                self.get_logger().info('no gesture: stopped, cmd_vel released')
            return
        if not self._active:
            self._active = True
            self.get_logger().info('gesture control active')
        vx, wz = self.command()
        twist = Twist()
        twist.linear.x  = vx
        twist.angular.z = wz
        self._cmd_pub.publish(twist)


# ---------------------------------------------------------------------------
# Detection loop: runs on the MAIN thread (cv.imshow requirement).
# ---------------------------------------------------------------------------
def _camera_loop(node: GestureTeleop) -> None:
    with open(node.kp_labels_path, encoding='utf-8-sig') as f:
        labels = [row[0] for row in csv.reader(f)]

    clf = _KeyPointClassifier(node.kp_model)

    options = mp_vision.HandLandmarkerOptions(
        base_options=mp_python.BaseOptions(
            model_asset_path=node.landmarker_model),
        running_mode=mp_vision.RunningMode.VIDEO,
        num_hands=1,
        min_hand_detection_confidence=node.det_conf,
        min_tracking_confidence=node.trk_conf,
    )
    landmarker = mp_vision.HandLandmarker.create_from_options(options)

    cap = None
    if not node.image_topic:
        cap = cv.VideoCapture(node.cam_device)
        cap.set(cv.CAP_PROP_FRAME_WIDTH,  node.cap_width)
        cap.set(cv.CAP_PROP_FRAME_HEIGHT, node.cap_height)
        if not cap.isOpened():
            node.get_logger().error(f'Cannot open camera device {node.cam_device}')
            rclpy.shutdown()
            return

    history: deque = deque(maxlen=node.hist_len)
    last_ts = 0

    while rclpy.ok():
        if node.show_window and cv.waitKey(1) == 27:   # ESC -> clean shutdown
            break

        header = None
        if cap is not None:
            ret, frame = cap.read()
            if not ret:
                node.get_logger().warn('Camera read failed, retrying',
                                       throttle_duration_sec=2.0)
                time.sleep(0.05)
                continue
        else:
            frame, header = node.next_frame(timeout=0.5)
            if frame is None:
                node.get_logger().warn(f'waiting for {node.image_topic} ...',
                                       throttle_duration_sec=5.0)
                continue

        if node.mirror:
            frame = cv.flip(frame, 1)   # mirror so left/right feel natural facing the camera
        debug = frame.copy()

        # MediaPipe VIDEO mode needs strictly increasing timestamps
        ts = max(time.monotonic_ns() // 1_000_000, last_ts + 1)
        last_ts = ts
        rgb    = cv.cvtColor(frame, cv.COLOR_BGR2RGB)
        mp_img = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
        result = landmarker.detect_for_video(mp_img, ts)

        if result.hand_landmarks:
            pts_list  = _landmark_list(frame, result.hand_landmarks[0])
            sign_id   = clf(_preprocess(pts_list))

            history.append(labels[sign_id])
            current_label = Counter(history).most_common(1)[0][0]
            if current_label in GESTURE_CMD:
                node.update_gesture(current_label)

            color = (0, 220, 0) if current_label in GESTURE_CMD else (160, 160, 160)
            pts = np.array(pts_list, dtype=np.int32)
            x, y, w, h = cv.boundingRect(pts)
            cv.rectangle(debug, (x, y), (x + w, y + h), color, 2)
            cv.putText(debug, current_label, (x, max(y - 8, 20)),
                       cv.FONT_HERSHEY_SIMPLEX, 0.9, color, 2, cv.LINE_AA)
        else:
            # Hand left the frame: reset history so the next gesture starts a fresh vote.
            history.clear()
            cv.putText(debug, 'no hand', (10, 60),
                       cv.FONT_HERSHEY_SIMPLEX, 0.9, (0, 80, 255), 2, cv.LINE_AA)

        # HUD: what is actually being published
        cmd = node.command()
        hud = f'vx={cmd[0]:+.2f}  wz={cmd[1]:+.2f}' if cmd else 'idle (cmd_vel released)'
        cv.putText(debug, hud, (10, 30), cv.FONT_HERSHEY_SIMPLEX, 0.7,
                   (255, 255, 255), 2, cv.LINE_AA)

        node.publish_debug(debug, header)
        if node.show_window:
            try:
                cv.imshow('Gesture Teleop', debug)
            except cv.error as e:
                node.get_logger().warn(f'no window ({e}); see gesture/image instead')
                node.show_window = False

    landmarker.close()
    if cap is not None:
        cap.release()
    if node.show_window:
        cv.destroyAllWindows()


def main() -> None:
    rclpy.init()
    node = GestureTeleop()

    # ROS spin (timers, camera subscription, DDS) runs in a background thread so the main
    # thread can own the OpenCV display loop.
    spin_thread = threading.Thread(target=rclpy.spin, args=(node,), daemon=True)
    spin_thread.start()

    try:
        _camera_loop(node)
    except KeyboardInterrupt:
        pass
    finally:
        if rclpy.ok():
            node._cmd_pub.publish(Twist())   # leave the robot stopped
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == '__main__':
    main()
