<div align="center">
  <img src="thongssh_gtk/icons/thongssh.png" alt="ThongSSH logo" width="128">

  # ThongSSH

  A lightweight SSH / Telnet / SFTP client for Linux and macOS, built on GTK 4 and Libadwaita.

  [![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
  [![Platform](https://img.shields.io/badge/platform-Linux%20%7C%20macOS-lightgrey.svg)](#installation)
</div>

---

ThongSSH is a terminal client focused on a clean host list, fast tab/split management, and a few
practical extras (session logging, settings sync, an optional AI chat panel) without turning into
a full IDE. It's a native GTK4/libadwaita app, not an Electron wrapper.

## Table of contents

- [Features](#features)
- [Installation](#installation)
  - [Prebuilt packages](#prebuilt-packages)
  - [Running from source](#running-from-source)
- [Configuration and sync](#configuration-and-sync)
- [Tech stack](#tech-stack)
- [Project status](#project-status)
- [License](#license)

## Features

**Connections**
- SSH, Telnet, and SFTP, plus a local terminal entry pinned to the top of the host list (no SSH
  involved, just a shell).
- Cross-platform password storage via the `keyring` library — the native vault on every OS
  (macOS Keychain, Linux/BSD Secret Service), with an encrypted local fallback if none is available.

**Layout**
- Split the tab area vertically, horizontally, or into a 2x2 grid, each pane running its own
  independent set of tabs. Drag a tab from one pane into another to rearrange; the active pane is
  outlined with an accent color.
- Per-tab background colors, set from the right-click menu or defaulted per host in its
  configuration. Text contrast adjusts automatically.

**Terminal**
- In-terminal find with case-sensitive, regex, and wrap-around options, plus a "highlight all"
  mode with a configurable tint color — each split pane has its own independent find bar.
- Session logging to a plain-text transcript, written in real time, with an option to skip noisy
  full-screen TUI redraws (vim, htop, tmux, ...).
- A fully customizable 16-color ANSI palette plus background/foreground, applied to every open
  terminal immediately.
- An optional templated watermark (`$name` / `$host` / `$user`) overlaid on the terminal
  background, with regex-based rules to change its color when the output matches a pattern.
- Batch Command: send one command to every open terminal at once, or just the ones in a chosen
  pane.
- Send a local file to the remote host over SFTP directly from a terminal tab's right-click menu.
- Post-connection and output-triggered commands, configured per host: run once right after
  connecting, or whenever the terminal's output matches a regular expression.

**Quickies**
- A panel of saved terminal snippets next to the host list, with one-click insert or
  insert-and-run, and its own fuzzy search.

**AI assistant (optional)**
- A resizable chat panel, hidden by default, with support for Claude, Gemini, ChatGPT, Grok,
  DeepSeek, OpenAI-compatible custom endpoints (including local Ollama/LM Studio), or a locally
  installed agentic CLI (Claude Code, Codex, or a custom command).
- Terminal context (selection or recent output) can be attached to a message explicitly; nothing
  is sent automatically.

**Configuration**
- Settings sync across machines through a shared folder (Dropbox, iCloud, a network share, or any
  local directory) — no cloud API involved. Real three-way merge, not a blind overwrite; passwords
  and SSH key paths never leave the machine they're on.
- Multiple independent config directories are supported for running more than one checkout side by
  side (see `.config_path`).

A full changelog is kept in [release_notes.md](release_notes.md).

## Installation

### Prebuilt packages

`.deb`, `.rpm`, `.AppImage`, and macOS `.dmg` packages are published on the
[Releases page](https://github.com/lknsfos/thongssh/releases).

### Running from source

Requires libadwaita 1.4+ and GTK 4.10+.

**Ubuntu / Debian** (24.04 LTS or newer):
```bash
sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-4.0 gir1.2-adw-1 gir1.2-vte-3.91 \
  gnome-keyring gir1.2-secret-1 openssh-client telnet sshpass \
  python3-paramiko python3-keyring python3-cryptography
```

**Fedora / RHEL** (Fedora 39+ / RHEL 10+):
```bash
sudo dnf install python3-gobject gtk4 libadwaita vte4 gnome-keyring libsecret \
  openssh-clients telnet sshpass python3-paramiko python3-keyring python3-cryptography
```

**Arch Linux:**
```bash
sudo pacman -S python-gobject python-cairo gtk4 libadwaita vte4 gnome-keyring libsecret \
  openssh inetutils sshpass python-paramiko python-keyring python-cryptography
```

**macOS:**
```bash
brew install gtk4 libadwaita vte3 libsecret gobject-introspection pygobject3 pkg-config sshpass
```

#### Linux

```bash
git clone https://github.com/lknsfos/thongssh.git
cd thongssh
python3 thongssh.py
```

Running from a checkout has no `.desktop` file, so app launchers and docks have no icon to show.
To install one:
```bash
./install-desktop-entry.sh
```
This detects the checkout's path automatically and is safe to re-run after moving it.

#### macOS

Needs a virtualenv that can still see Homebrew's PyGObject/pycairo:
```bash
git clone https://github.com/lknsfos/thongssh.git
cd thongssh

python3 -m venv --system-site-packages venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

python3 thongssh.py
```

If Python can't find the `gi` module, make sure the venv was created from Homebrew's own
`python3` (`/usr/local/bin/python3` or `/opt/homebrew/bin/python3`), not a `pyenv`/`conda`
interpreter that Homebrew's PyGObject wasn't built against.

## Configuration and sync

Hosts, Quickies, AI chat history, and general/terminal settings can be synced across machines by
pointing Settings → Sync at a shared folder — any folder that's kept in sync by something else
(Dropbox, iCloud, Syncthing, a network share) works, since the app only reads and writes plain
files there. Changes merge rather than overwrite: unrelated edits on two machines combine cleanly,
and the most recently synced side wins on a real conflict. Passwords stay in the OS keyring and
SSH key paths stay local; neither is included in the synced files.

Running multiple checkouts on the same machine (e.g. personal and work) is supported by dropping a
`.config_path` file next to the app pointing at a separate config directory for each.

## Tech stack

- Python 3.10+
- GTK 4 and Libadwaita 1
- VTE 3.91 (terminal widget)
- Paramiko (SFTP)
- `keyring` and `cryptography` (native credential storage on every platform)

## Project status

ThongSSH is under active development, built collaboratively by a human maintainer and AI
assistance (Claude). It hasn't had a formal third-party security audit — treat it accordingly for
anything sensitive, and report issues on the [issue tracker](https://github.com/lknsfos/thongssh/issues).

## License

MIT — see [LICENSE](LICENSE).
