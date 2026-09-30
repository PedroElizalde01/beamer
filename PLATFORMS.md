# Which machines pair with which

Beamer links two machines at a time. In this fork, a Windows PC pairs with a Mac or with a Linux
computer. A Mac and a Linux computer cannot pair with each other yet.

| Pair | Status | Shows the code | Types the code |
|---|---|---|---|
| Windows PC and Mac | Works (the original app) | Windows PC | Mac |
| Windows PC and Linux computer | Works (this fork, `linux_app/`) | Windows PC | Linux computer |
| Mac and Linux computer | Not implemented | Neither can | Both would |

Both working pairs use the same pairing: a six-digit code that lasts a minute and works once,
exchanged with CPace as [PAIRING.md](PAIRING.md) describes. The code never crosses the network.

## Two roles

Every link has one machine in each of two roles. The roles, not the operating systems, decide who
can pair with whom.

| | The PC's role | The Mac's role |
|---|---|---|
| While pairing | Shows the code and announces itself on the network (UDP 24821) | Lists the machines it hears, and types the code into one |
| After pairing | Listens on TCP 24820, learns the other machine's address when it connects, then connects back | Connects to the address pairing gave it, and listens on TCP 24820 for the link back |
| On the wire | Names itself `windows` | Names itself `mac` |
| Apps that can take it | Windows | Mac, Linux |

So there are two links, one each way, and input goes in either direction whichever machine is in
which role. The roles only decide how the two find each other and pair.

A Linux computer takes the Mac's role. To the Windows app it looks like a Mac, except that its
first message says `"platform": "linux"`. The Windows app saves that and names it "Linux computer"
everywhere, and a Mac, which sends no platform, stays "Mac". Only a Linux app and a Windows app
from this fork do this. An official Windows build still says "Mac", and an older Linux build of
this fork never says it is Linux.

## Why a Mac and a Linux computer cannot pair

Both the Mac app and the Linux app only take the Mac's role. Pairing needs one machine to show a
code and the other to type it. Neither of these two can show one, so they never find each other.

### What it would take

1. **A second mode in the Linux app** that takes the PC's role: it shows the code, announces
   itself, and listens. Everything this needs already exists in the shared files (`pairing.py`'s
   `Announcer`, `receiver.py`, the sender's logic). What is missing is a Linux window for the
   code, and a setting that chooses the mode.
2. **The Mac app naming Linux.** The Mac app assumes the other machine is a Windows PC and says
   "PC" and "Windows". It would need the same change the Windows app got: learn the platform
   from the first message and name the other machine by it. The Linux app would then say
   `"platform": "linux"` in the PC's role too.
3. **Testing on a real Mac,** which this fork has not had.

A Mac app built from this fork can only be given to other people once it is signed and
notarised, which needs a paid Apple developer account. Without that, macOS refuses to open it.

## One partner at a time

Each machine keeps one pairing, which is one shared token. A Windows PC paired with a Linux
computer is no longer paired with the Mac it had before, and pairing again switches between them.
Three machines at once, a Mac, a Linux computer and a PC, would need a new design in every app,
where each keeps several tokens and the edges lead to different machines.
