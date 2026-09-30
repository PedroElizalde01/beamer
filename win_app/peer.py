"""Who is at the other end of the link, and what the window calls it.

The peer says which platform it is in its hello (`platform`); a Beamer from before that field
says nothing, and every one of those is a Mac. The answer is saved, so the window names the peer
right from the next start rather than only once it has connected. The wire keeps its own words
("mac", "windows"), which name the two roles, not the machines.
"""

from dataclasses import dataclass

PLATFORMS = ("mac", "linux")
DEFAULT = "mac"
# While pairing, the machine typing the code has not said what it is yet.
EITHER = "Mac or Linux computer"


@dataclass(frozen=True)
class Peer:
    platform: str
    # After "your", "the" or "a": "Send input to your Mac", "Where your Linux computer is".
    name: str
    # Where there is no room for more: the arrangement drawing.
    short: str
    # The switch on the peer's own Overview that lets this PC drive it, as that app words it.
    drive_switch: str

    def say(self, template: str) -> str:
        """`template` with {name}, {short} and {drive_switch} filled in."""
        return template.format(name=self.name, short=self.short, drive_switch=self.drive_switch)


MAC = Peer("mac", "Mac", "Mac", "Windows drives this Mac")
LINUX = Peer("linux", "Linux computer", "Linux", "“Your PC drives this machine”")
_BY_PLATFORM = {peer.platform: peer for peer in (MAC, LINUX)}


def of(platform) -> Peer:
    """The peer for a saved or announced platform; anything unknown is a Mac, as before."""
    return _BY_PLATFORM.get(platform, MAC)


def platform_of(hello) -> str:
    """The platform a hello's data names, or the default for one that names none."""
    value = hello.get("platform") if isinstance(hello, dict) else None
    return value if value in PLATFORMS else DEFAULT
