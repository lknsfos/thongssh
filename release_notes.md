# Release Notes

### 🆕 What's New in 0.6.0

She got organized *and* chatty:

* **Local terminal, front and center** — a "Local Terminal" entry now always sits at the top of the host list, separate from your saved hosts. Double-click it for a plain local shell, no SSH involved.
* **Session logging** — check "Save session log" on any host and every connection gets recorded to a plain-text, human-readable transcript (no ANSI escape-code soup) in real time as the session runs, not just when it ends. Didn't turn it on at connect time? Right-click an open terminal and tick "Save log" to start recording from that point on. Pick where logs land in Settings → Client Options, or leave it alone and it defaults to a tidy `logs/` folder next to your configs.
* **In-terminal Find** — right-click → Find... or Ctrl+Shift+F, from anywhere (even mid-session). Case-sensitive, regex, and wrap-around toggles, with up/down buttons to step through matches.
* **Host search, always on duty** — the host panel's search box is just always there now, no button to hunt for. Click it or hit Ctrl+F from literally anywhere in the app — tree, terminal, wherever — and it grabs focus, ready to type.
* **Host dialog: username finally has its own seat** — it's a separate field under Authentication now, instead of jammed into the address as `user@host`.
* **Window remembers its size** — resize it, close it, reopen it, same size (and maximized state) as you left it. (Position can't be remembered — GTK4 flatly removed window-position APIs, Wayland treats that as the compositor's call, not the app's.)
* **Batch Command got a real text box** — the single-line command field is now a multi-line, auto-expanding box, so a long or multi-line command is actually visible instead of scrolling off-screen. Ctrl+Enter sends.
* **`.config_path` override** — mostly for anyone running from a git checkout rather than a package: drop a `.config_path` file next to the app (it's created for you, pre-filled with the current default) and point it at a different folder to keep two checkouts' hosts/settings completely separate — handy for not mixing a personal setup with a work one on the same machine.
* **Subtler active-div highlight** and an **optional alternating row tint** for the host tree (Settings → Interface), for anyone who wants a little more visual rhythm without a hard outline.
* Assorted GNOME/Linux fixes: switching quickly between two split divs no longer occasionally gets "stuck" on one; dragging a tab between divs shows its actual name and icon instead of a stray number; Ctrl+Shift+C/V copy-paste in the terminal works again.

### 🆕 What's New in 0.5.1

She's getting flexible — splits every which way:
* **Multi-div split view** — three new header bar buttons let you split the tab area vertically (side by side), horizontally (stacked), or into a full 2x2 grid. Each div runs its own independent set of tabs. Hit the same button twice to snap back to one div (everything merges back together). Switch between vertical/horizontal and your tabs just re-orient, no shuffling. Drag a tab from one div straight into another whenever you want to rearrange.
* **Active div highlight** — a subtle accent-colored outline shows which div is currently "listening" for new tabs, so double-clicking a host in the tree always lands where you expect.
* **Batch Command learned to filter by div** — the "Select / Deselect All" row now has a "Divs" dropdown next to it. Split the view and it fills in with Left/Right, Top/Bottom, or all four quadrants, so you can blast a command at just the terminals in one section instead of everything at once.
* **SFTP: New Folder** — right-click (even on empty space) in either the local or remote panel to create a new directory where you're standing.

### 🆕 What's New in 0.4.1

She's been to the gym:
* **Batch Command** — one command, blasted to every open terminal at once, with checkboxes to pick your targets. Header bar button and hamburger menu, your pick.
* **Send File from a terminal tab** — right-click a terminal, pick a local file, it flies over SFTP to the remote host using whatever auth that session already trusts (key or saved password). There's even a cheeky "Detect from terminal" button that runs a real `pwd` over there to guess the remote directory for you (only press it when you're actually at a shell prompt, not mid-`vim`!).
* **Cross-platform password storage** — swapped the GNOME-only libsecret dependency for the `keyring` library, so passwords land in the real native vault on every OS (macOS Keychain, Windows Credential Locker, Linux/BSD Secret Service), with an encrypted local fallback if none is available.
* **A little more native on macOS** — proper Dock icon, About window icon, and a system font that isn't a sad fallback serif.
* **Interface settings** — pick your app icon (Safe vs. Original 👀) right from Settings → Interface.
