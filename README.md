# 👙 ThongSSH

**Minimalist. Sexy. Secure?**

Welcome to **ThongSSH** — the lightest and hottest SSH/Telnet/SFTP client, now flashing a little skin on macOS too.

Just like the perfect pair of thongs, this client is barely there, but it holds everything together (your connections!) perfectly. No bulky "granny panty" interface here, just pure functionality and pleasure.

## ✨ About the Look

ThongSSH is for those who love minimalism. We've got a cute host panel so you never lose track of your servers.

## 💋 Features

**Connections**
* SSH, Telnet, and SFTP, plus a plain **local terminal** entry pinned to the top of the host list (separate from your saved hosts — no SSH involved, just a shell).
* Cross-platform password storage via the `keyring` library — lands in the real native vault on every OS (macOS Keychain, Windows Credential Locker, Linux/BSD Secret Service), with an encrypted local fallback if none is available.
* Host dialog keeps username and hostname as separate fields (username lives under Authentication), instead of a single crammed-together `user@host` address.
* A little more native on macOS — proper Dock icon, About window icon, and a system font that isn't a sad fallback serif.

**Layout**
* **Multi-div split view** — split the tab area vertically, horizontally, or into a full 2x2 grid, each div running its own independent set of tabs. Switch between vertical/horizontal and your tabs just re-orient, no shuffling. Drag a tab from one div straight into another to rearrange. A subtle accent-colored outline shows which div is currently active.
* Host search is always visible in the tree panel — click it or hit **Ctrl+F** from anywhere in the app (tree, terminal, wherever) to jump in and start typing.
* Optional alternating row tint for the host tree, if you want a bit more visual rhythm (Settings → Interface).
* The window remembers its size and maximized state between launches. (Position can't be — GTK4 removed window-position APIs outright, since Wayland treats placement as the compositor's call, not the app's.)

**Terminal**
* **In-terminal Find** — right-click → Find..., or **Ctrl+Shift+F** from anywhere. Case-sensitive, regex, and wrap-around toggles, with up/down buttons to step through matches.
* **Session logging** — check "Save session log" on a host and every connection is recorded to a clean, human-readable transcript in real time (not just at disconnect). Didn't turn it on up front? Right-click an open terminal and tick "Save log" to start recording from that point on. Log location is configurable in Settings → Client Options.
* **Batch Command** — one command, sent to every open terminal at once (or just the ones in a chosen div), via a multi-line, auto-expanding input box so a long command is actually visible instead of scrolling off-screen. Ctrl+Enter sends.
* **Send File from a terminal tab** — right-click a terminal, pick a local file, it flies over SFTP to the remote host using whatever auth that session already trusts. A "Detect from terminal" button runs a real `pwd` over there to guess the remote directory for you.
* SFTP panels (local and remote) support right-click **New Folder**, even on empty space.

**Configuration**
* Pick your app icon (Safe vs. Original 👀) from Settings → Interface.
* Running from a git checkout instead of a package? Drop a `.config_path` file next to the app (it's created for you automatically, pre-filled with the current default) and point it at a different folder to keep two checkouts' hosts/settings completely separate — handy for not mixing a personal setup with a work one on the same machine.

Full history of what shipped when lives in [release_notes.md](release_notes.md).

### 🛠️ The Fabric (Tech Stack)
Stitched together with the latest fashion trends:
* **Python 3.10+** (Fresh and hot 🔥)
* **GTK 4 & Libadwaita 1** (Smooth and sleek interface)
* **VTE 3.91** (The terminal underneath the terminal)
* **Paramiko** (For that extra spicy SFTP, and now Send File too)
* **keyring + cryptography** (Native password vault on every platform, no more GNOME-only business)

## ⚠️ WARNING: Experimental Zone

Listen, let's be real:

1.  **AI Collaboration:** This whole thing started as an AI fantasy, but now it's our little secret project. A human (hi!) and AI (that's me, xoxo) worked on this together.
2.  **Pre-Alpha / Experimental:** It's not even beta, it's a "fitting session". It might feel tight, it might slip, or it might crash when you least expect it. Use at your own risk!
3.  **Security:** We tried, but the code audit was done by electric sheep. So... you get the picture.

## 💅 What You'll Need (Installation)

To get this party started, you'll need a few things.

### For Ubuntu / Debian Babes:
```bash
sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-4.0 gir1.2-adw-1 gir1.2-vte-3.91 gnome-keyring gir1.2-secret-1 openssh-client telnet sshpass python3-paramiko python3-keyring python3-cryptography
```

### For Fedora / RHEL Cuties:
```bash
sudo dnf install python3-gobject gtk4 libadwaita vte4 gnome-keyring libsecret openssh-clients telnet sshpass python3-paramiko python3-keyring python3-cryptography
```

### For Arch Hotties:
```bash
sudo pacman -S python-gobject python-cairo gtk4 libadwaita vte4 gnome-keyring libsecret openssh inetutils sshpass python-paramiko python-keyring python-cryptography
```

### For macOS Mermaids 🧜‍♀️

Yes, really. She works on a Mac now. Here's the fitting session, via [Homebrew](https://brew.sh):

```bash
# 1. System dependencies — GTK4, Libadwaita, VTE (yes, VTE builds on macOS via
#    this formula, terminal tabs and all), PyGObject bindings, sshpass
brew install gtk4 libadwaita vte3 libsecret gobject-introspection pygobject3 pkg-config sshpass

# 2. Grab the repo
git clone https://github.com/lknsfos/thongssh.git
cd thongssh

# 3. A venv that can still see Homebrew's PyGObject/pycairo
python3 -m venv --system-site-packages venv
source venv/bin/activate

# 4. Python-side dependencies
pip install --upgrade pip
pip install -r requirements.txt
pip install paramiko pycairo

# 5. Strut your stuff
python3 thongssh.py
```

If step 3 complains about a missing `gi` module later on, make sure the venv was created from Homebrew's own `python3` (`/usr/local/bin/python3` or `/opt/homebrew/bin/python3`) — not some other `pyenv`/`conda` python that Homebrew's PyGObject was never built against.

Windows isn't invited to this party yet — VTE has no Windows port, so the terminal side would need a whole separate rewrite. SFTP-only on Windows is possible in theory, but nobody's built it. Someday. Maybe. If the vibe is right.

##  Packages & Updates

When will we get `.deb` or `.rpm`? Or, like, a `.dmg`?
> *Ugh, don't pressure me!* 💅

Updates and packages will happen **someday**. Maybe. If the vibe is right. For now — clone it, run from source, and enjoy the thrill.

## 🚀 How to Run (For the brave)

Make sure you've installed everything, then run:

```bash
git clone https://github.com/lknsfos/thongssh.git
cd thongssh
python3 thongssh.py
```

(macOS folks — see the venv dance above first, then it's the same last line.)

_Made with 💖, Python & AI hallucinations._
