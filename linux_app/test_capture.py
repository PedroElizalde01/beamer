"""The grabbed pointer's moves, with the X server's queue replayed by hand: events from before a warp
still carry old places, and one of them must never read as a jump. Needs an X display, as
capture.py opens one on import; nothing is grabbed or moved.  python3 linux_app/test_capture.py"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.append(str(Path(__file__).resolve().parent.parent / "win_app"))

import capture  # noqa: E402


def main():
    moves, warps = [], []
    c = capture.Capture("ctrl_r", lambda dx, dy: moves.append((dx, dy)), *(lambda *a: None,) * 4)
    c._warp = lambda x, y: warps.append((x, y))
    # The state a grab leaves: warped to the middle, the warp not yet landed.
    c._center, c._last, c._warping = (960, 540), None, True

    c._grabbed_motion(1919, 600)  # queued before the grab's warp: where the pointer was, not a move
    assert moves == [], moves
    c._grabbed_motion(960, 540)  # the warp lands
    c._grabbed_motion(965, 543)
    c._grabbed_motion(1200, 540)  # wandered off: put back
    assert warps == [(960, 540)], warps
    c._grabbed_motion(1210, 540)  # queued before that warp landed: still measured on
    c._grabbed_motion(960, 540)  # it lands
    c._grabbed_motion(962, 540)
    assert moves == [(5.0, 3.0), (235.0, -3.0), (10.0, 0.0), (2.0, 0.0)], moves
    print("ok")


if __name__ == "__main__":
    main()
