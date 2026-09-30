"""The clipboard through xclip, with the interface receiver.py and link.py expect:
changed_contents(), set_contents(text, image) and forget_sync().

ponytail: xclip rather than owning the X selection here; serving selection requests ourselves is
a few hundred lines of Xlib for the same result.
"""

import hashlib
import logging
import subprocess
import threading

LOGGER = logging.getLogger(__name__)
TIMEOUT_SECONDS = 1.0
PNG = "image/png"

_lock = threading.Lock()
_last = [None]  # digest of what was last sent or set, so an unchanged clipboard is not sent again


def _xclip(*args, data=None):
    return subprocess.run(
        ["xclip", "-selection", "clipboard", *args],
        input=data, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=TIMEOUT_SECONDS, check=False,
    )


def _digest(text, image):
    return hashlib.sha256((text or "").encode("utf-8") + b"\0" + (image or b"")).digest()


def read():
    """(text or None, PNG bytes or None) on the clipboard now."""
    try:
        targets = _xclip("-t", "TARGETS", "-o").stdout.decode("utf-8", "replace").split()
        image = _xclip("-t", PNG, "-o").stdout if PNG in targets else None
        text = None
        if "UTF8_STRING" in targets or "STRING" in targets or "text/plain" in targets:
            text = _xclip("-o").stdout.decode("utf-8", "replace") or None
        return text, image or None
    except (OSError, subprocess.TimeoutExpired) as exc:
        LOGGER.warning("could not read the clipboard: %s", exc)
        return None, None


def changed_contents():
    text, image = read()
    with _lock:
        digest = _digest(text, image)
        if digest == _last[0]:
            return None, None
        _last[0] = digest
    return text, image


def set_contents(text, image):
    """Text wins when both come: xclip serves one type at a time, and text is what gets pasted."""
    try:
        # xclip forks to serve the selection and keeps its end of any pipe open, so its output
        # goes nowhere and only stdin is ours.
        if text is not None:
            subprocess.run(["xclip", "-selection", "clipboard", "-i"], input=text.encode("utf-8"),
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=TIMEOUT_SECONDS, check=True)
        elif image is not None:
            subprocess.run(["xclip", "-selection", "clipboard", "-t", PNG, "-i"], input=image,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=TIMEOUT_SECONDS, check=True)
        else:
            return False
    except (OSError, subprocess.SubprocessError) as exc:
        LOGGER.warning("could not set the clipboard: %s", exc)
        return False
    with _lock:
        _last[0] = _digest(*read())
    return True


def forget_sync():
    with _lock:
        _last[0] = None
