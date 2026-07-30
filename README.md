# 👙 ThongSSH

**Minimalist. Sexy. Secure?**

Welcome to **ThongSSH** — the lightest and hottest SSH/Telnet/SFTP client, now flashing a little skin on macOS too.

Just like the perfect pair of thongs, this client is barely there, but it holds everything together (your connections!) perfectly. No bulky "granny panty" interface here, just pure functionality and pleasure.

## ✨ About the Look

ThongSSH is for those who love minimalism. We've got a cute host panel so you never lose track of your servers.

## 💋 Features

**Connections**
* SSH, Telnet, and SFTP, plus a plain **local terminal** entry pinned to the top of the host list (separate from your saved hosts — no SSH involved, just a shell).
* Cross-platform password storage via the `keyring` library — lands in the real native vault on every OS (macOS Keychain, Linux/BSD Secret Service), with an encrypted local fallback if none is available.

**AI Assistant**
* A resizable **AI chat panel**, tucked away until you want it — toggle it from a header-bar button per configured provider, markdown replies (bold/italic/code, fenced code blocks with a one-click Copy button), typing indicator while a reply is in flight.
* **API providers**: Claude, Gemini, ChatGPT, Grok, DeepSeek, or any number of custom OpenAI-compatible endpoints (local Ollama/LM Studio included) — paste a key in Settings → AI → API.
* **CLI Client tools**, for talking to a locally-installed agentic CLI (Claude Code, Codex, or any custom command) exactly the way you'd drive it from a terminal, no API key required.
* One shared conversation regardless of which provider answers — switch mid-chat and history carries over; each header button carries the provider's own icon (or a colored badge as a fallback).
* **Terminal context, opt-in only** — an "attach context" button in the chat input pulls in the active terminal's current selection (or the last ~20 lines of output) as an editable, visibly-quoted block. Never sent automatically.

**Layout**
* **Multi-div split view** — split the tab area vertically, horizontally, or into a full 2x2 grid, each div running its own independent set of tabs. Switch between vertical/horizontal and your tabs just re-orient, no shuffling. Drag a tab from one div straight into another to rearrange. A subtle accent-colored outline shows which div is currently active.

**Terminal**
* **In-terminal Find** — right-click → Find..., or **Ctrl+Shift+F** from anywhere. Case-sensitive, regex, and wrap-around toggles, with up/down buttons to step through matches.
* **Session logging** — check "Save session log" on a host and every connection is recorded to a clean, human-readable transcript in real time (not just at disconnect). Didn't turn it on up front? Right-click an open terminal and tick "Save log" to start recording from that point on. Log location is configurable in Settings → Client Options.
* **Fully custom color scheme** — flip on "Custom Colors" (Settings → Terminal → Appearance) to hand-edit all 16 ANSI palette colors plus background/foreground, seeded from whichever built-in template you had selected. Applies to every open terminal immediately, no reconnect needed.
* **Batch Command** — one command, sent to every open terminal at once (or just the ones in a chosen div), via a multi-line, auto-expanding input box so a long command is actually visible instead of scrolling off-screen. Ctrl+Enter sends.
* **Send File from a terminal tab** — right-click a terminal, pick a local file, it flies over SFTP to the remote host using whatever auth that session already trusts. A "Detect from terminal" button runs a real `pwd` over there to guess the remote directory for you.

**Configuration**
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
2.  **Security:** We tried, but the code audit was done by electric sheep. So... you get the picture.

## 📦 Packages & Updates

Prebuilt `.deb`, `.rpm`, and macOS `.dmg` packages are up on the [Releases page](https://github.com/lknsfos/thongs-gtk4.dev/releases) — grab one if you'd rather not run from source.

## 💅 What You'll Need (System Dependencies)

Only needed if you're running from a git checkout instead of a prebuilt package above. The UI needs libadwaita 1.4+ / GTK 4.10+ (for `Adw.SwitchRow`/`Adw.SpinRow`/`Adw.NavigationSplitView` and `Gtk.ColorDialogButton`), so the distro version matters:

### For Ubuntu / Debian Babes 💃 (Ubuntu 24.04 LTS or newer):
```bash
sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-4.0 gir1.2-adw-1 gir1.2-vte-3.91 gnome-keyring gir1.2-secret-1 openssh-client telnet sshpass python3-paramiko python3-keyring python3-cryptography
```

### For Fedora (39 or newer) / RHEL (10 or newer) Cuties 🎩 (it's literally a hat, come on):
```bash
sudo dnf install python3-gobject gtk4 libadwaita vte4 gnome-keyring libsecret openssh-clients telnet sshpass python3-paramiko python3-keyring python3-cryptography
```

### For Arch Hotties 🔥:
Rolling release, so whatever's current should already be new enough:
```bash
sudo pacman -S python-gobject python-cairo gtk4 libadwaita vte4 gnome-keyring libsecret openssh inetutils sshpass python-paramiko python-keyring python-cryptography
```

### For macOS Mermaids 🧜‍♀️
```bash
brew install gtk4 libadwaita vte3 libsecret gobject-introspection pygobject3 pkg-config sshpass
```

## 🚀 How to Run (For the brave)

### Linux
```bash
git clone https://github.com/lknsfos/thongs-gtk4.dev.git
cd thongs-gtk4.dev
python3 thongssh.py
```

### macOS
Needs a venv that can still see Homebrew's PyGObject/pycairo:
```bash
git clone https://github.com/lknsfos/thongs-gtk4.dev.git
cd thongs-gtk4.dev

python3 -m venv --system-site-packages venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

python3 thongssh.py
```

If that complains about a missing `gi` module, make sure the venv was created from Homebrew's own `python3` (`/usr/local/bin/python3` or `/opt/homebrew/bin/python3`) — not some other `pyenv`/`conda` python that Homebrew's PyGObject was never built against.

_Made with 💖, Python & AI hallucinations._
