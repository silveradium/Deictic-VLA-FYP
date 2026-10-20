"""Odometry path -> a plain summary of how Roxy was driven. No action labels.

Episodes are recorded raw (the typed instruction, the Orbbec frames, odometry and the rest of the
bag). The output labels are not decided yet -- "turn left 15 degrees" vs "I think I should turn left
15 degrees" vs something else -- so nothing here turns the path into text. Labels are derived later
from the same odometry that is already in the bag, so no episode has to be recorded again.

What is worth knowing while collecting is whether the driving was clean: Roxy should turn and walk
forward, because the policies this data is for have no sideways or backward action. Lateral drift
and reversing are measured so an episode can be dropped or the driving fixed.

Measuring that from raw odometry is noisier than it looks, so two things are done about it:

* The path is accumulated into chunks of a quarter metre, and only a chunk that is *mostly*
  sideways or backward counts as such. Odometry jitter has no direction, so adding up every
  sample's sideways component would report metres of imaginary strafing over a minute of straight
  walking; jitter is a few percent of a 25 cm chunk, real strafing is most of it.
* A chunk is compared against every heading Roxy held during it, not just the first one, and the
  closest counts. Roxy turns and walks at the same time, so a chunk often starts facing one way and
  ends facing another; walking a curve is forward motion, and only motion that is sideways to every
  heading in the chunk is really sideways.

Pure Python, no ROS: used by data_pipeline/validate/preview.py.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

CHUNK_M = 0.25        # displacement accumulated before a move is classified forward / sideways / back
TURN_FLOOR_DEG = 2.0  # heading change accumulated before it counts as turning
MIN_FRACTION = 0.4    # of a chunk that must be sideways / backward for the chunk to count as that


@dataclass
class PathStats:
    distance_m: float = 0.0      # length of the path driven
    net_turn_deg: float = 0.0    # final heading minus the first, -180..180
    total_turn_deg: float = 0.0  # all turning added up, left and right alike
    lateral_m: float = 0.0       # sideways motion added up (strafing)
    max_lateral_m: float = 0.0   # the largest single sideways move
    backward_m: float = 0.0      # distance driven in reverse
    warnings: list[str] = field(default_factory=list)


def wrap_deg(a: float) -> float:
    return (a + 180.0) % 360.0 - 180.0


def path_stats(poses, chunk_m=CHUNK_M, turn_floor_deg=TURN_FLOOR_DEG, min_fraction=MIN_FRACTION,
               lateral_warn_m=0.3, backward_warn_m=0.2) -> PathStats:
    """poses: [(t, x, y, yaw_deg), ...] in time order."""
    stats = PathStats()
    if len(poses) < 2:
        stats.warnings.append("no odometry")
        return stats

    _, cx, cy, cyaw = poses[0]  # start of the open chunk
    ryaw = cyaw                 # reference heading for the turn accumulator
    yaws = [cyaw]               # every heading held during the open chunk
    for _, x, y, yaw in poses[1:]:
        yaws.append(yaw)
        dyaw = wrap_deg(yaw - ryaw)
        if abs(dyaw) >= turn_floor_deg:
            stats.total_turn_deg += abs(dyaw)
            ryaw = yaw
        dx, dy = x - cx, y - cy
        step = math.hypot(dx, dy)
        if step < chunk_m:
            continue
        # how far off the heading this chunk went, taking the heading it fits best
        err = min((abs(wrap_deg(math.degrees(math.atan2(dy, dx)) - h)) for h in yaws))
        lat, fwd = step * math.sin(math.radians(err)), step * math.cos(math.radians(err))
        stats.distance_m += step
        if lat > min_fraction * step:
            stats.lateral_m += lat
            stats.max_lateral_m = max(stats.max_lateral_m, lat)
        if -fwd > min_fraction * step:
            stats.backward_m += -fwd
        cx, cy, cyaw = x, y, yaw
        yaws = [yaw]

    stats.net_turn_deg = wrap_deg(poses[-1][3] - poses[0][3])
    if stats.lateral_m > lateral_warn_m:
        stats.warnings.append(f"sideways drift {stats.lateral_m:.2f} m (strafing?)")
    if stats.backward_m > backward_warn_m:
        stats.warnings.append(f"reversed {stats.backward_m:.2f} m")
    return stats
