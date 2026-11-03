"""Camera and odometry on one clock.

The recorder stamps every message with its arrival time, and the two streams arrive with different
delays: odometry (Go2 over WebRTC) reaches the recording machine later than the Orbbec frames (Pi
over DDS). In session S1-R1 the odometry was about 2.6 s late (most episodes 2.35-2.85 s, a few early
ones under 1.5 s). Uncorrected, the frame taken "when a 15 degree unit was completed" shows the robot
seconds later, and the bag's first seconds of odometry show the robot being put in place before the
episode.

The lag is measured per episode. Turning the robot slides the whole picture sideways, so the
horizontal image shift between consecutive frames (phase correlation) follows the odometry's yaw rate;
the delay at which the two curves agree best is the lag. Walking forward barely shifts the picture
sideways, and a person moving in view is a small part of it, so turning dominates. Episodes that never
turn give a weak correlation: estimate_lag reports it, and the caller decides.

Pure numpy + OpenCV, no ROS.
"""

from __future__ import annotations

import cv2
import numpy as np

SIZE = (160, 120)          # frames are compared at this size (px)
STEP_S = 0.05              # resampling step of both curves
SMOOTH_S = 0.3             # moving average over both curves
SEARCH_S = (-2.0, 5.0)     # lags tried: odometry time = camera time + lag
MIN_CORR = 0.5             # below this the measured lag is not trusted


def shift_rate(frames) -> list[tuple[float, float]]:
    """[(t, horizontal image shift in px/s)] between consecutive JPEG frames [(t, bytes)]."""
    win = cv2.createHanningWindow(SIZE, cv2.CV_32F)
    out, prev = [], None
    for t, jpeg in frames:
        gray = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_GRAYSCALE)
        if gray is None:
            continue
        gray = cv2.resize(gray, SIZE).astype(np.float32)
        if prev is not None and t > prev[0]:
            (dx, _), _ = cv2.phaseCorrelate(prev[1], gray, win)
            out.append(((prev[0] + t) / 2, dx / (t - prev[0])))
        prev = (t, gray)
    return out


def _smooth(x: np.ndarray) -> np.ndarray:
    k = max(1, round(SMOOTH_S / STEP_S))
    return np.convolve(x, np.ones(k) / k, "same")


def estimate_lag(frames, odom) -> tuple[float | None, float]:
    """(lag_s, corr) with odometry time = camera time + lag; corr is the correlation at that lag
    (positive: the image shifts the way the robot turns). (None, 0.0) without enough data.

    frames: [(t, jpeg bytes)]; odom: [(t, x, y, yaw_deg)], both in time order, same clock.
    """
    rate = shift_rate(frames)
    if len(rate) < 10 or len(odom) < 10:
        return None, 0.0
    cam_t = np.arange(rate[0][0], rate[-1][0], STEP_S)
    cam = _smooth(np.interp(cam_t, [r[0] for r in rate], [r[1] for r in rate]))
    ot = np.array([o[0] for o in odom])
    yaw = np.degrees(np.unwrap(np.radians([o[3] for o in odom])))
    odo_t = np.arange(ot[0], ot[-1], STEP_S)
    if len(odo_t) < 10 or np.std(cam) < 1e-6:
        return None, 0.0
    odo = _smooth(np.gradient(np.interp(odo_t, ot, yaw), STEP_S))
    best = (None, 0.0)
    for lag in np.arange(SEARCH_S[0], SEARCH_S[1] + 1e-9, STEP_S):
        shifted = np.interp(cam_t + lag, odo_t, odo, left=0.0, right=0.0)
        if np.std(shifted) < 1e-6:
            continue
        c = float(np.corrcoef(cam, shifted)[0, 1])
        if best[0] is None or c > best[1]:
            best = (round(float(lag), 2), c)
    return best[0], round(best[1], 3)


def to_camera_clock(odom, lag: float) -> list:
    """Odometry poses moved onto the camera's clock."""
    return [(t - lag, *rest) for t, *rest in odom]
