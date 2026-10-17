"""The tricks Roxy does on request: one place for trick_node (what to send) and dialog_node (what
Gemini may pick). Pure Python, no ROS imports.

API ids are Unitree's sport API (go2_robot_sdk/domain/constants/robot_commands.py). Jumps and flips
(FrontJump, FrontFlip, FrontPounce, Handstand, Bound, ...) are left out on purpose, and so is Damp,
which drops the robot where it stands.
"""

from dataclasses import dataclass

SPORT_TOPIC = "rt/api/sport/request"

BALANCE_STAND = 1002
STOP_MOVE = 1003
STAND_UP = 1004
STAND_DOWN = 1005
SIT = 1009
RISE_SIT = 1010
HELLO = 1016
STRETCH = 1017
CONTENT = 1020
DANCE1 = 1022
DANCE2 = 1023
SCRAPE = 1029
WIGGLE_HIPS = 1033
FINGER_HEART = 1036


@dataclass(frozen=True)
class Trick:
    hint: str              # for Gemini: what it is and how people ask for it
    api_id: int | None     # one sport command; None: trick_node handles it specially
    posture: str = "standing"  # where Roxy ends up; every trick but these starts from standing


TRICKS = {
    "hello": Trick("wave a front paw: hi, hello, say hi, wave, shake hands, give me a paw", HELLO),
    "sit": Trick("sit down", SIT, posture="sitting"),
    "stand_up": Trick("stand up, get up (from sitting or lying down)", None),
    "lie_down": Trick("lie down, lay down, down, rest", STAND_DOWN, posture="lying"),
    "stretch": Trick("stretch", STRETCH),
    "dance": Trick("dance", DANCE1),
    "dance2": Trick("a different dance: another dance, dance two, the other dance", DANCE2),
    "wiggle_hips": Trick("wiggle your hips, shake your butt, wag", WIGGLE_HIPS),
    "finger_heart": Trick("make a heart with the front paws: show some love, make a heart", FINGER_HEART),
    "scrape": Trick("take a bow, greet the audience (Unitree's New Year greeting)", SCRAPE),
    "happy": Trick("show you are happy: are you happy, good dog, who's a good dog", CONTENT),
    "come_forward": Trick("walk straight ahead about one metre: come forward, step forward, come closer; "
                          "not for going to a place or something pointed at (that is navigate)", None),
}
