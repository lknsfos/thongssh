# Release Notes

### 🌍 What's New in 0.9.3

ThongSSH now speaks 13 languages, and a handful of Quickies-panel rough edges got sanded down:

* **Real internationalization, not a stub** — a standard GNU gettext setup (the same one every Linux/Unix desktop app uses), following the system language by default. Settings → General → Language lets you force a specific one instead ("Takes effect the next time ThongSSH starts"): English, Spanish, French, German, Italian, Portuguese, Russian, Ukrainian, Hebrew, Arabic, Simplified Chinese, Traditional Chinese, and Japanese, all professionally translated (feature names like "Quickies" translated by meaning, not word-for-word — e.g. "Шпаргалки" in Russian, "Rapidini" in Italian, "サクコマ" in Japanese). Arabic and Hebrew also correctly flip the whole UI to right-to-left.
* **Fixed focus not returning to the terminal after using a Quicky's Run button** — you had to click back into the terminal by hand to keep typing; Run (and Send, and double-click, and the right-click menu) all hand focus back to the terminal now.
* **New: configurable keyboard shortcuts for the first 10 Quickies** — Settings → Shortcuts gained a compact 2×10 grid, Paste and Run side by side. Defaults are Ctrl+1–9/0 to paste a Quicky and Ctrl+Shift+1–9/0 to send-and-run it (slot 10 = the 0 key); any of them can be rebound or cleared like the other shortcuts.
* **New: a "Clear shortcut" button** next to every keyboard shortcut in Settings — previously the only way to remove one was to rebind it to something else.
* **New: reorder your Quickies** — drag rows in Settings' Quickies editor (or use "Move Up"/"Move Down" from the panel's right-click menu) to change their order; both surfaces now show each Quicky's position number, since that position is what determines which hotkey (1–10) triggers it.
* **Quickies panel decluttered** — removed the redundant "Quickies" title row; the "+" (add) button now lives in the search row instead, which itself carries a "Quickies search" placeholder so it doesn't read as the host search box.
* **Added a clear divider between the host list and the Quickies panel** — a thin line-gap-line separator, so the add/remove host buttons no longer look like they belong to Quickies (or vice versa, depending on which panel is on top).
* **Fixed a missing bottom margin on the Quickies search box** when Quickies' search row is positioned at the bottom of the panel — it was sitting flush against the panel's edge instead of matching the spacing every other search box gets.
* **About → Legal's copyright year is dynamic now** instead of hardcoded — it'll say the right year on its own from here on.

### 🆕 What's New in 0.9.2

A real AppImage crash fixed, a quicker way to spin up a local terminal, and the project is now officially MIT-licensed:

* **Fixed the AppImage segfaulting on right-click** — the bundled PyGObject (whatever Ubuntu 22.04's own `python3-gi` happened to be) mismarshaled `Gdk.Event` — a GTK4 *boxed* type, not a GObject — when returned from a right-click gesture, corrupting memory badly enough to crash later, deep inside CPython itself. The AppImage now bundles a pinned, known-good PyGObject instead of whatever the build container's system package happened to be.
* **Fixed Settings never actually working inside the AppImage** — discovered while chasing the crash above: the AppImage's bundled libadwaita was a version older than what `Adw.SwitchRow`/`Adw.SpinRow` (used throughout Settings) require, so opening Settings from an AppImage build has apparently never worked at all. The whole bundled GTK4/libadwaita/VTE stack is rebuilt against a current libadwaita now.
* **New: a "+" button in the tab strip** opens a fresh local terminal without needing the sidebar's "Local Terminal" entry — it starts in whichever directory the currently-active local tab is actually in (read live, not just wherever that tab itself started), named `Local:<dir>` (`Local:~`, `Local:.config`, …), and keeps that name **live-updated** as you `cd` around, no reconnect needed. Toggle it off in Settings → Terminal → Behavior ("New local terminal opens in current directory", on by default) to always start fresh at `$HOME` instead.
* **ThongSSH is now MIT-licensed** — a `LICENSE` file plus a license header in every source file, so pulling this into a company's own toolset doesn't need a legal detour first.

### 🔒 What's New in 0.9.1

Two security fixes, both reported from reading the source rather than found in the wild — thank you.

* **Saved SSH passwords no longer appear in plain text in `ps aux`/`/proc/<pid>/cmdline`** — connecting with a password saved in the keyring shelled out to `sshpass -p <password> ssh ...`, putting the password directly in the process's argument list for any local user to read for the whole life of the session. It's passed via the `SSHPASS` environment variable now (`sshpass -e`), which isn't visible in `ps aux` at all.
* **Fixed saved-password connections silently disabling host key verification** — the same code path also always added `-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null` "because sshpass can't handle host key prompts." Both are gone: sshpass was never asked to answer that prompt anyway (it only intercepts the password prompt), so a known host behaves identically without them, and a genuinely new or changed host key now correctly stops and asks — right there in the same terminal — instead of being silently accepted.
* **Fixed SFTP and Send File trusting *any* server's host key with no verification at all** — both used `paramiko.AutoAddPolicy()`, which accepts whatever key a server presents on first connect and never checks it again later, so a machine-in-the-middle was never detected either way. Both now load `~/.ssh/known_hosts` like a real `ssh` client would; an unrecognized host now shows a confirmation dialog with its fingerprint before trusting and saving it (same first-connect trust prompt OpenSSH itself shows), and a *known* host whose key has since changed correctly stops the connection instead of accepting it.

### 🆕 What's New in 0.9.0

Disconnected sessions are easier to spot and reconnect to, hosts/tabs gained a quick copy-to-clipboard menu, and the watermark's position is now a visual picker instead of a dropdown:

* **Disconnected tabs are now struck through** — if "Close tab on disconnect" is off, a tab whose session has ended used to look completely identical to a live one until you actually clicked into it. Its name gets a strikethrough now, and clears again the moment you reconnect.
* **New: "Ask for username when reconnecting"** (Settings → Terminal, appears once "Close tab on disconnect" is off) — reconnecting to a disconnected tab used to silently reuse whatever username it last connected with, forever. Turning this on prompts again on reconnect, pre-filled with the previous username so confirming without changes still reproduces the old behavior.
* **New: "Copy to Clipboard" on hosts and tabs** — right-click a host (tree) or a tab and copy its Server name, Hostname/IP, or user@hostname directly; the menu shows the actual value for whichever host you clicked, not a generic label. "user@hostname" is greyed out for hosts with no username configured. Copies to both the Clipboard and Primary selections, so Ctrl+V *and* Shift+Insert/right-click-paste both get the same thing (previously only Ctrl+V did — Shift+Insert/right-click-paste read the Primary selection, which this didn't touch, and pasted whatever had last been selected elsewhere instead).
* **Fixed the host right-click menu being cut off (needing a scroll) the first time you opened it, if you had any User Commands configured** — the menu's User Commands section was rebuilt on every single right-click, and a `GtkPopoverMenu` doesn't reliably re-measure its size in time for its own very next popup the first time a bound menu grows from empty to populated. It's built once at startup (and again only when the list itself changes in Settings) instead of on every click.
* **Watermark position is now a 3x3 grid you click, not a dropdown** — a small attached button next to the watermark toggle (same style as the Sync button) opens the same grid Settings uses, so the position can be changed without a trip through the full Settings dialog; both stay in sync with each other.
* **New: configurable keyboard shortcuts** (Settings → Shortcuts) — Close Tab, Focus Host Search, Find in Terminal, Copy, and Paste no longer have their key combinations hardcoded. Click a shortcut, press the new combination (Escape cancels), Apply — and it syncs across machines like other general settings. Close Tab's default is now Ctrl+Shift+W instead of plain Ctrl+W, which is also bash/zsh's own "delete last word" binding — every terminal session was fighting the app for it.

### 🆕 What's New in 0.8.3

Terminal/tab regressions from the watermark work fixed, plus a CLI provider and a launcher-icon bug:

* **Fixed the terminal scrollbar disappearing** — the watermark's overlay ended up wrapping the terminal *inside* its `Gtk.ScrolledWindow`, which quietly broke the scrollbar's connection to the terminal's real scroll position (keyboard scrolling still worked, since that bypasses the scrollbar entirely). The overlay now wraps the ScrolledWindow instead of sitting inside it.
* **Fixed tab drag-to-reorder only working between split panes, not within one** — the drag support added for moving tabs between panes was quietly swallowing every same-pane reorder attempt too, since a custom drag handler and the notebook's own built-in reordering can't both claim the same drag gesture. Reordering within a pane is handled by hand now, same as moving between panes.
* **Tab headers are more compact** — less padding around each tab, a smaller close button, tighter spacing — most noticeable once there are enough tabs to scroll the tab strip.
* **Fixed the Codex CLI provider failing immediately** ("Not inside a trusted directory…") — it refuses to run outside a directory it trusts, and every CLI provider call runs in a throwaway scratch directory by design (so a plain chat question is never mistaken for "look at my codebase"). Skips that check now; any CLI tool that falls back to an interactive prompt instead of erroring also fails fast now rather than hanging.
* **Fixed the dock/taskbar icon disappearing after a restart** (seen on elementary OS, likely affects other Linux DEs too) — a bug in the app's own icon-preference handling was deleting a dev checkout's installed `.desktop` launcher on every single startup, mistaking it for an internal override file it manages itself. It now only ever touches files it actually created.
* **Fixed switching to the "Original" icon doing nothing for a dev checkout** — the same code only knew how to swap icons via an override file layered on top of a proper *system* install (`/usr/share/applications/...`); a checkout installed with `install-desktop-entry.sh` has no such system file to layer over, so the switch silently no-opped. It now edits that checkout's own `.desktop` file directly, and nudges the desktop database to pick up the change immediately instead of waiting for its own schedule.
* **App ID renamed** from the leftover placeholder `com.example.thongssh` to `terminal.thongssh`. If you have an old `.desktop` file installed under the previous name, remove it and re-run `install-desktop-entry.sh`.

### 🆕 What's New in 0.8.2

Settings dialog polish plus two Sync/Watermark fights removed:

* **Fixed the Settings window opening absurdly wide** — a long sync error message sat in a plain, unwrapped label; `Adw.ViewStack` sizes itself by the widest of *all* its pages, not just the visible one, so that one label was enough to blow out the whole dialog even while looking at an unrelated page. The label is capped and ellipsized now, with the full message still available on hover.
* **Fixed User Commands and Quickies looking narrower than every other Settings page** — a plain `Gtk.Box` dropped into an `Adw.PreferencesGroup` doesn't get the automatic full-row-width treatment real Adwaita rows do, and unlike what you'd expect, nothing there was actually *asking* for more width either — Adwaita's page clamp only ever caps width, it never stretches undersized content to fill the room. Both pages now request enough width to match the rest, and the "Text"/"Command" columns actually expand to use it.
* **Terminal font no longer synced** — a font installed on one machine is routinely missing on another (macOS vs. Ubuntu vs. Arch), so this was a permanent tug-of-war with no winner. It's purely per-machine now, like the SSH/Telnet binary paths already are.
* **Watermark font is now choosable — and also not synced**, for the identical reason as the terminal font above. Family only (size stays its own field); defaults to "Sans".
* **Watermark split-view shrink is now a percentage, off by default** — was a fixed "halve it" toggle; it's a proper dropdown now (Off / 90% / 80% / … / 10%), and starts at Off instead of silently halving your watermark the first time you split the view.

### 🆕 What's New in 0.8.1

A single, important sync fix:

* **Fixed Sync wiping hosts/settings when the sync folder wasn't actually mounted yet** — if the configured folder (a Google Drive/Dropbox mount, etc.) hadn't finished mounting at app start, it looked to Sync like an ordinary, accessible, *empty* folder — indistinguishable from "everything was deleted on every other machine." The three-way merge believed it and deleted local hosts and reset settings to match. Sync now checks whether this machine has synced successfully before; if so and the folder/sync file has since gone missing, it refuses to run and reports the error instead of guessing.
* **New: "Reset Sync State"** — for the legitimate opposite case (you've pointed Sync at a genuinely new/empty folder and want to seed it from this machine). Lives in a small dropdown on the sync button (next to the normal "sync now" click), behind a confirmation dialog since it deliberately makes local data authoritative for the next pass.

### 🆕 What's New in 0.8.0

The big one — Quickies grew up, settings can now follow you across machines, and watermarks got smart (if experimental):

* **Quickies, rebuilt** — the snippet row now has three purpose-built actions instead of one risky one:
  * **Send** (▶) inserts the snippet into the active terminal without running it (same as double-click always did); **Send and Run** (⏩) inserts *and* executes immediately. The old one-click **Delete** button is gone entirely — a stray misclick used to silently drop a saved Quicky with zero confirmation — deleting now lives in a **right-click context menu** alongside Send/Send and Run/Edit and a new **Send to Batch Command**, which opens the Batch Command dialog with the (template-rendered) snippet already sitting in the command field.
  * Rows are genuinely compact now: tiny icon buttons sized to the text line instead of stretching it, minimal padding, no more accidental full-height suffix buttons.
  * **Search box** for the snippet list — same look as the host search (no magnifying-glass icon, dimmed placeholder), position configurable (Settings → Quickies → top/bottom), and forgives one typo/missing/extra/swapped character exactly like host search already did, falling back to fuzzy matching only when nothing matches exactly.
  * The divider between the host list and the Quickies panel is properly draggable now and remembers its ratio — across toggling Quickies off/on *and* across restarts.
* **Settings Sync** — keep hosts, Quickies, AI chat history, user commands, and general/terminal settings in step across machines via nothing fancier than a shared folder (Dropbox, iCloud, a network share, or just another local directory — the app never talks to any cloud API itself, only reads/writes files wherever you point it).
  * Real three-way merge (a local-only "what did I last see" snapshot, compared against current local state and the shared file), not a blind overwrite: unrelated changes on both sides merge cleanly, deletions propagate to every machine, and a genuine same-item conflict is resolved by whichever side has the newer synced file — never silent data loss, never duplicate entries.
  * Passwords never leave the keyring and never touch the shared file (they never touched `hosts.json` either, so nothing new here); each host's SSH key path is stripped on the way out and left untouched locally on the way in, since it's always machine-specific.
  * The shared file is versioned by timestamp and sha256-hashed for integrity — a corrupted or mid-write file aborts that sync pass cleanly instead of merging against garbage.
  * Settings → Sync: enable switch, folder picker, sync interval (minimum 60 seconds, enforced), a checkbox per category, and a Force Sync Now button. A sync icon appears in the header bar next to AI whenever sync is enabled — click it to sync on demand.
* **Adaptive Watermarks (experimental)** — Settings → Terminal → Adaptive Watermarks lets you define ordered regex rules that override the watermark's color/opacity when its text matches — e.g. red whenever it contains "root", blue whenever it contains a specific host prefix. The topmost matching rule always wins (so `root@arbe-svc053` stays red if a "root" rule sits above an "arbe" rule, even though both match), with add/remove and up/down reordering. Marked experimental — the matching/priority behavior is new and hasn't seen much real-world mileage yet.
* **Smaller fixes bundled in**: the terminal panel's bottom corners now round to match native macOS window chrome instead of showing whatever's behind the app through the corners; the host search box lost its magnifying-glass icon in favor of a plain dimmed "Search" placeholder; the AI panel's first-ever open now sizes to ~25% of the window instead of a fixed pixel guess; terminal color schemes gained a full "Custom" mode with all 16 ANSI palette colors editable, not just background/foreground; the header bar's watermark/Quickies/sync buttons now sit grouped next to Batch Command instead of scattered among the split-view buttons.

### 🆕 What's New in 0.7.3

Chat history for the AI panel, plus a real code-block rendering bug fixed:

* **AI chats now have history** — conversations save to disk (`~/.cache/thongssh/ai_chats/`) as you go, no provider involved. **New chat**/**Delete chat** buttons replace the old single "Clear chat"; a history picker under the provider row lists past chats (title + plain regexp search, no AI involved in the search itself) and reopens any of them. Each chat gets an auto-generated title after its first exchange, via a quiet one-off request to whichever provider just answered.
* **Fixed replies landing in the wrong chat** — switching to a different chat (or starting a new one) while a reply was still in flight could deliver that reply into whatever chat happened to be on screen when it arrived, or lose track of it entirely. Every in-flight request now stays tied to the chat it was actually sent from, wherever you've since navigated.
* **Fixed not being able to get back to a chat that was still "Thinking…"** — a brand-new chat wasn't saved to disk until its first reply landed, so navigating away before that and trying to return via history found nothing to open. It's saved the moment you send, and reopening it while still pending shows the "Thinking…" indicator again instead of looking like nothing was asked.
* **Fixed code blocks losing content** — a code block's text could end up shorter than its actual line count, with the missing lines sitting below the visible area with no scrollbar to reach them (worst case, a block could look completely empty). Two compounding causes: a non-wrapping text view mis-measures its own height once the panel is narrower than its widest line, and separately, reserving room for a horizontal scrollbar was subtracting that space from the content twice over. Both fixed at the source instead of papering over the symptom.
* **Fixed the provider buttons not centering** — Settings changes intended to center them (in 0.7.2) didn't actually take effect: a plain `Gtk.Box` doesn't honor a child's centering along its own axis, and separately, the row's "how many fit before wrapping" setting was making it always claim the full row width regardless. Both fixed; the buttons now sit centered as their own tight group.

### 🆕 What's New in 0.7.2

AI chat panel, round three — layout rework plus a nasty bug squashed:

* **Fixed the app hanging on quit and pegging a CPU core** — a background timer meant to size the chat input on first open had no retry limit, so if the AI panel was never opened in a session it just spun forever instead of giving up. Closing the window wouldn't cleanly end the process (Ctrl+C in the terminal didn't help either) until it was killed by hand. Capped now, same as the other places in the app that poll for a widget's real size.
* **One AI button instead of a strip of them** — a single "AI" toggle now lives next to the system menu, opening and closing the panel; it's hidden entirely whenever no provider is configured or "Disable AI" is on. Picking *which* provider answers moved into the panel's own header as a row of buttons — plenty of providers configured wraps them onto a second row automatically instead of overflowing the main window's header bar.
* **Chat input is resizable, not auto-growing** — drag the handle above it for more room; typing a long message no longer pushes the transcript around on its own.
* **Fixed two invisible/mismatched buttons in the chat panel** — Send and Attach-context were rendering oversized (Send was outright invisible on some themes, a flat + accent-color combination gone wrong) — both now match the plain icon buttons used everywhere else in the app (same ones as Add Host / Add Group / Remove).
* **Fixed broken-looking icons**:
  * The new AI button briefly shipped with an icon name that doesn't exist in most icon themes — it's a plain "AI" text label now.
  * The split-view buttons' icon rendered as a literal open book on some icon themes (Yaru, notably) — replaced with two icons drawn in-house to match the existing 4-way split icon's style (a couple of bordered squares side by side, or stacked, instead of 4).

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
