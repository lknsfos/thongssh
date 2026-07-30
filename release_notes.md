# Release Notes

### 🆕 What's New in 0.7.1

AI chat panel follow-up — fixes plus model picking and a hard off switch:

* **Fixed the CLI provider hanging** — the default system prompt used to tell the model "you're connected to a remote terminal session," which the Claude Code CLI (a real agentic tool with its own bash/tool-use loop) took as an actual instruction and went off trying to locate/reach that "remote host" itself instead of just answering. The prompt now explicitly says there's no shell, tool, or network access at all — analyze only what's given, never try to run or connect to anything.
* **Fixed the code block Copy button doing nothing on Linux** — it was building the clipboard offer through GDK's automatic string-to-clipboard path, which didn't reach the system clipboard; now hands over the encoded text directly instead.
* **Fixed the last line of a long code block being impossible to select** — GTK's floating (overlay) scrollbar had no reserved space of its own, so it sat right on top of that line instead of below it.
* **Disable AI, completely** — a master switch at the top of Settings → AI. Flip it off and the app doesn't touch AI in any way: no header-bar buttons, no probing PATH for claude/codex, no keyring reads, no requests — for anyone who'd rather the feature not exist at all.
* **Pick a model instead of typing it blind**:
  * **API providers** (Claude, Gemini, ChatGPT, Grok, DeepSeek, custom): a list button next to the Model field turns on the moment you enter a key, fetches whatever models that key actually has access to, and fills the field on click. Manual typing still works exactly as before.
  * **CLI tools**: a Model field (empty by default — no `--model` flag gets sent at all unless you set one). Claude Code gets a quick-pick list (opus/sonnet/haiku); free-text entry works for any tool either way.
  * Custom API providers also got a Model field for the first time — previously there was no way to set one at all.

### 🆕 What's New in 0.7.0

She learned to talk back — the big one, an **AI chat panel** baked right into the app:

* **AI chat panel** — a resizable side panel (opens at ~25% of the window width the first time, then remembers whatever width you drag it to) with a real markdown-rendered chat: bold/italic/inline code, fenced code blocks with their own one-click **Copy** button, and a typing indicator while a reply is on the way. Toggle it from a header-bar button per configured provider; press the active one again to collapse the panel without losing the conversation.
* **Talk to it two ways** — pick whichever fits how you already work:
  * **API providers**: Claude, Gemini, ChatGPT, Grok, DeepSeek, plus any number of custom OpenAI-compatible endpoints (local Ollama/LM Studio included) — just paste a key in Settings → AI → API.
  * **CLI Client tools**: drive locally-installed agentic CLIs (Claude Code, Codex, or any custom command you point at) the same way you'd use them in a terminal — no API key needed, just whatever's already on your PATH.
  * One shared conversation either way — switch providers mid-chat and the history carries over uninterrupted; each header button shows the provider's own icon (or a colored badge if none was available) so it's obvious who's about to answer.
* **Terminal context, only when you ask for it** — an "attach context" button in the chat input pulls in the active terminal's current selection (or the last ~20 lines of output if nothing's selected) as a visibly-quoted block you can edit or remove before sending. Never sent automatically.
* **One shared system prompt** for every provider (Settings → AI), pre-filled with a short "you're looking at a remote terminal, stay concise, answer in the asked language" default, with a one-click reset if you've mangled it. A single configurable request timeout applies to both API and CLI paths.
* **Terminal: fully custom color scheme** — flip on "Custom Colors" under Settings → Terminal → Appearance to hand-pick all 16 ANSI palette colors (normal + bright, laid out the same way every other terminal emulator does it) plus background/foreground, seeded from whichever built-in template you had selected. Useful for colorblind-accessibility fixes a fixed template can't cover (a specific palette color reading as indistinguishable against the background, for instance). Saved to its own file in the config directory, and — unlike before — changing it now recolors every already-open terminal immediately instead of only the next new tab.
* **Host search, cleaned up** — the magnifying-glass icon is gone in favor of a plain dimmed "Search" placeholder that disappears the moment you type; the match-count readout was removed entirely; the up/down step buttons are smaller, flat, circular icon buttons instead of full-size bordered ones.
* Settings dialog reshuffled: **AI** is a single sidebar entry with its own **API** / **CLI Client** sub-tabs (not two separate top-level sections), and a layout bug that squeezed dialog content to a narrow column regardless of window size is fixed.

### 🆕 What's New in 0.6.2

Quiet-console-and-find-bar release:

* **Debug logging, opt-in instead of always-on** — Settings → **General** (the old "Interface" page, renamed) now has an "Enable debug logging" switch, off by default. With it off, the console stays quiet (warnings/errors only); flip it on to get the full verbose trace back, no restart needed.
* **Host search box: pick top or bottom** — same Settings → General page, a "Search bar position" dropdown moves the host panel's search box above or below the tree, live.
* **Host search Up/Down without losing focus** — the search box's up/down match buttons now double as the actual Up/Down arrow keys while you're typing, so cycling through matches doesn't yank focus out of the search field.
* **Host search now forgives a typo** — if what you typed doesn't match anything exactly, it falls back to a fuzzy pass that tolerates one missing, extra, wrong, or swapped character. Type `cus-fs21` and it'll still find `cus-fs021`; type `pmxx013` and it'll still find `pxmx013`.
* **In-terminal Find, several fixes at once**:
  * Fixed a bug where the highlighted match crept forward one hit per keystroke instead of staying put — typing "cadence" letter by letter used to jump from the 1st match to the 2nd to the 3rd as you typed, instead of just refining the same one.
  * "Match case" and "Regular expression" are now real labeled checkboxes instead of cryptic "Aa" / ".*" buttons.
  * The Find bar no longer disappears the moment you click back into the terminal — it now stays open (pinned top-right, right under the header bar) regardless of which split-view layout is active, and follows you when you switch tabs or panes instead of silently searching a terminal you've since left.
* **Closing a tab is instant now** — it used to wait on the underlying process actually exiting before the tab disappeared, which was invisible for a quick SSH/Telnet client but very noticeable for "Local Terminal" (a real login shell, which doesn't always die from a plain signal right away — job control, direnv/nvm hooks, a foreground process inside it). The tab now closes the moment you click, full stop; killing the process (escalating to a hard kill after a few seconds if needed) happens quietly in the background.
* Fixed a `GTK-CRITICAL` that could pop up in the console when right-clicking a terminal (a popover was being re-parented on every click instead of once).

### 🆕 What's New in 0.6.1

Quick touch-up after 0.6.0:

* **Keyboard shortcuts don't care what layout you're typing in anymore** — Ctrl+D (logout/EOF), Ctrl+C, Ctrl+W, Ctrl+F, Ctrl+Shift+F and friends now fire off the physical key regardless of which keyboard layout is active. Switch to a Cyrillic (or any other non-Latin) layout and they still work exactly the same.
* **Host dialog reshuffled** — Protocol, Name, Hostname/IP, and Port are always visible up top; everything else moved into two tabs, **Authentication** (username, password, path to key) and **Options** (session logging, SSH/Telnet-specific settings). Port now shows a real default (22 for SSH, 23 for Telnet) instead of a confusing 0.
* **Session log filenames now use the host's name** — `user@MyServerName`, not `user@172.x.x.x`, matching what's actually in your host list.
* **"Save log" can be turned back off** — the terminal right-click checkbox no longer locks itself once logging starts; untick it to stop recording and close out the file.

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
