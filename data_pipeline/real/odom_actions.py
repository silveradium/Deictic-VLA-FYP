"""Odometry path -> NaVILA mid-level actions ("turn left 45 degree", "move forward 75 cm", "stop").

NaVILA was trained on VLN-CE actions: 25 cm forward steps and 15 degree turns, merged into at most
75 cm / 45 degrees per action (navila_go2/common/mid_level.py in the NaVILA fork). A teleoperated
path is continuous and often curved, so it is rounded to the nearest unit as it goes: a 15 degree turn
is emitted once the heading is more than half a unit (7.5 degrees) from the reference heading, which
then moves a whole unit; forward steps the same with 25 cm (from 12.5 cm). A 59.9 degree turn is
"turn left 45 + 15", not 45, so the heading after it is right for the forward steps that follow. Consecutive actions of the same kind are then merged and split
again into chunks NaVILA could have said.

NaVILA has no sideways or backward action: lateral drift and reversing are measured and reported, so
the episode can be dropped or the driving fixed.

Pure Python, no ROS: used by data_pipeline/validate/preview.py and data_pipeline/export/deictic_export.py.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

FORWARD, TURN_LEFT, TURN_RIGHT, STOP = "forward", "turn_left", "turn_right", "stop"
FORWARD_UNIT_M = 0.25
TURN_UNIT_DEG = 15.0
MAX_FORWARD_M = 0.75
MAX_TURN_DEG = 45.0
# NaVILA's training answers (a8cheng/NaVILA-Dataset, R2R / RxR / Human) are exactly these 10 strings
STOP_ANSWER = "I think I should stop because I have finished the instruction."


@dataclass
class Action:
    kind: str
    value: float  # m for FORWARD, degrees for turns
    t: float      # time the action was completed (s, same clock as the poses)

    def text(self) -> str:
        """The action as worded inside NaVILA's training answers: "degree" is singular there."""
        if self.kind == FORWARD:
            return f"move forward {self.value * 100:.0f} cm"
        if self.kind in (TURN_LEFT, TURN_RIGHT):
            return f"{self.kind.replace('_', ' ')} {self.value:.0f} degree"
        return "stop"

    def answer(self) -> str:
        """The training answer, word for word: what fine-tuning data must use, or the model learns a
        new output format. evaluation/vlnce_baselines/navila_trainer.py parses it."""
        return STOP_ANSWER if self.kind == STOP else f"The next action is {self.text()}."


@dataclass
class PathStats:
    max_lateral_m: float = 0.0   # sideways drift off the reference heading (strafing)
    backward_m: float = 0.0      # distance driven in reverse
    warnings: list[str] = field(default_factory=list)


def wrap_deg(a: float) -> float:
    return (a + 180.0) % 360.0 - 180.0


def odom_to_actions(poses, forward_unit=FORWARD_UNIT_M, turn_unit=TURN_UNIT_DEG,
                    max_forward=MAX_FORWARD_M, max_turn=MAX_TURN_DEG,
                    lateral_warn_m=0.3, backward_warn_m=0.2):
    """poses: [(t, x, y, yaw_deg), ...] in time order. Returns ([Action, ..., stop], PathStats)."""
    units, stats = odom_to_units(poses, forward_unit, turn_unit, lateral_warn_m, backward_warn_m)
    if not poses:
        return [], stats
    return merge_units(units, max_forward, max_turn) + [Action(STOP, 0.0, poses[-1][0])], stats


def odom_to_units(poses, forward_unit=FORWARD_UNIT_M, turn_unit=TURN_UNIT_DEG,
                  lateral_warn_m=0.3, backward_warn_m=0.2):
    """poses -> ([one Action per 25 cm / 15 degree unit, t = when it was completed], PathStats).
    NaVILA's history gets one frame per unit: the export takes them at these times."""
    stats = PathStats()
    if not poses:
        stats.warnings.append("no odometry")
        return [], stats

    _, rx, ry, ryaw = poses[0]
    units = []  # one entry per 25 cm / 15 deg unit
    for t, x, y, yaw in poses:
        dyaw = wrap_deg(yaw - ryaw)
        # half-open [-unit/2, unit/2): with abs() >= a heading exactly half a unit off flips sign forever
        while dyaw >= turn_unit / 2 or dyaw < -turn_unit / 2:
            step = math.copysign(turn_unit, dyaw)
            units.append(Action(TURN_LEFT if step > 0 else TURN_RIGHT, turn_unit, t))
            ryaw = wrap_deg(ryaw + step)
            dyaw -= step
        c, s = math.cos(math.radians(ryaw)), math.sin(math.radians(ryaw))
        fwd = (x - rx) * c + (y - ry) * s
        stats.max_lateral_m = max(stats.max_lateral_m, abs(-(x - rx) * s + (y - ry) * c))
        while fwd >= forward_unit / 2:
            units.append(Action(FORWARD, forward_unit, t))
            rx, ry = rx + forward_unit * c, ry + forward_unit * s
            fwd -= forward_unit
        if fwd <= -forward_unit:  # reversing: no NaVILA action for it, restart from here
            stats.backward_m += -fwd
            rx, ry = x, y

    if stats.max_lateral_m > lateral_warn_m:
        stats.warnings.append(f"sideways drift {stats.max_lateral_m:.2f} m (strafing? NaVILA can't strafe)")
    if stats.backward_m > backward_warn_m:
        stats.warnings.append(f"reversed {stats.backward_m:.2f} m (NaVILA can't reverse)")
    return units, stats


def merge_units(units, max_forward=MAX_FORWARD_M, max_turn=MAX_TURN_DEG):
    """Merge runs of units of the same kind, capped at what NaVILA says in one action (no stop added)."""
    actions = []
    for u in units:
        cap = max_forward if u.kind == FORWARD else max_turn
        if actions and actions[-1].kind == u.kind and actions[-1].value + u.value <= cap + 1e-9:
            actions[-1].value += u.value
            actions[-1].t = u.t
        else:
            actions.append(Action(u.kind, u.value, u.t))
    return actions
