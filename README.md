<p align="center">
  <a href="https://claudebuddy.januscaler.com/">
    <picture>
      <source media="(prefers-color-scheme: dark)" srcset="docs/assets/lockup-dark.png">
      <img src="docs/assets/lockup.png" alt="Claude Buddy" width="300">
    </picture>
  </a>
</p>

<p align="center">
  <b>Move your Claude Code sessions to another Claude account or organization.</b><br>
  Full history and sub-sessions included. Keep sessions in sync to carry on past usage limits.
</p>

<p align="center">
  <a href="https://github.com/shivanshtalwar0/claude-buddy/releases/latest"><img alt="Download" src="https://img.shields.io/github/v/release/shivanshtalwar0/claude-buddy?label=download&color=f17448"></a>
  <img alt="Windows, macOS, Linux" src="https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-14263a">
  <a href="LICENSE"><img alt="MIT license" src="https://img.shields.io/badge/license-MIT-14263a"></a>
  <a href="https://claudebuddy.januscaler.com/"><img alt="Website" src="https://img.shields.io/badge/website-claude--buddy-f17448"></a>
</p>

<p align="center">
  <img src="docs/assets/screenshot.png" alt="Claude Buddy copying 6 Claude Code sessions, including 4 sub-sessions, from a Team organization to a Personal organization" width="820">
</p>

## Download

| Windows | macOS (Apple Silicon) | macOS (Intel) | Linux |
| :---: | :---: | :---: | :---: |
| [**ClaudeBuddy.exe**](https://github.com/shivanshtalwar0/claude-buddy/releases/latest/download/ClaudeBuddy-windows-x64.exe) | [**Claude Buddy.app**](https://github.com/shivanshtalwar0/claude-buddy/releases/latest/download/ClaudeBuddy-macos-apple-silicon.zip) | [**Claude Buddy.app**](https://github.com/shivanshtalwar0/claude-buddy/releases/latest/download/ClaudeBuddy-macos-intel.zip) | [**claude-buddy**](https://github.com/shivanshtalwar0/claude-buddy/releases/latest/download/ClaudeBuddy-linux-x64.tar.gz) |

Free, open source, no sign-up. The downloads aren't code-signed yet, so the first time you open one:
on **macOS**, right-click the app → **Open**; on **Windows**, click **More info → Run anyway**.

## How to use it

1. **Sign in once** to the account or organization you're moving to, in the Claude desktop app.
2. **Open Claude Buddy**, choose **From** and **To**, and tick the sessions you want.
3. **Click Copy**, reopen the Claude desktop app and switch organization. Your sessions are in the sidebar.

## What it does

- **Copies Claude Code sessions** from the Claude desktop app's Code tab to another account or organization, with the full conversation history, project folder, model and settings.
- **Brings sub-sessions along.** Sessions started from another session are shown under their parent, and copying the parent copies all of them in the same structure.
- **Keeps sessions in sync** (optional). Both organizations share one conversation, so when you hit a usage limit in one, you continue in the other and switch back later. Work in one at a time.
- **Live sync** (command line). `watch` keeps an eye on an organization and mirrors every new session, and every new sub-session, into the other one as it appears, in sync. Nothing to copy by hand any more.
- **Shows real names.** Organizations appear as Claude shows them, such as *Developers · Team* or *Personal · Max*, with the account email.
- **Moves claude.ai chats too.** Claude can't import chats, so Claude Buddy turns your claude.ai data export into Markdown files you can upload to a Project in the new account.
- **Stays on your computer.** Originals are never changed. No network requests, and no passwords, tokens or cookies are read.

<p align="center">
  <img src="docs/assets/chats.png" alt="Claude Buddy converting a claude.ai data export into Markdown" width="620">
</p>

## FAQ

<details>
<summary><b>Can I move Claude Code sessions to a different Claude account or organization?</b></summary>

Yes, on the same computer. Sign in to the destination once in the Claude desktop app, then pick it as **To** in Claude Buddy.
</details>

<details>
<summary><b>I hit my Claude usage limit. Can I continue the same session in another account?</b></summary>

Yes. Turn on **Keep in sync** when copying. Both organizations then share the conversation, so you can continue in the other and later switch back to the latest progress. Use one organization at a time. Rewinding a synced session splits it off into its own copy.
</details>

<details>
<summary><b>Can I transfer claude.ai chats to another account?</b></summary>

Not as native chat history, because Claude has no import feature. Export your data from claude.ai (Settings → Privacy → Export data), open the **claude.ai chats** tab in Claude Buddy, and convert it. Then upload the Markdown files to a Project in the new account.
</details>

<details>
<summary><b>What about sessions from the <code>claude</code> terminal command?</b></summary>

Those aren't tied to an account. `claude --resume` already works after you switch accounts.
</details>

<details>
<summary><b>Command line</b></summary>

```bash
git clone https://github.com/shivanshtalwar0/claude-buddy && cd claude-buddy
python3 -m claude_buddy list                                  # accounts and orgs, with names
python3 -m claude_buddy sessions --from a575/a117              # sessions as a tree
python3 -m claude_buddy copy --from a575/a117 --to a575/fc1a --session 02d2dd1a        # copy (with sub-sessions)
python3 -m claude_buddy copy --from a575/a117 --to a575/fc1a --session 02d2dd1a --sync # keep in sync
python3 -m claude_buddy watch --from a575/a117 --to a575/fc1a                          # live sync: new sessions appear in the other org
python3 -m claude_buddy chats-to-md ~/Downloads/claude-export.zip                    # claude.ai export → Markdown
```

`ACCOUNT/ORG` take unique prefixes of the IDs shown by `list`. More options: `--all`, `--include-archived`, `--no-children`, `--title-suffix`, `--dry-run`. For `watch`: `--both` (watch both directions), `--interval` (poll seconds, default 2), `--once` (one pass, for cron), `--state` (state file).
</details>

<details>
<summary><b>How it works</b></summary>

The Claude desktop app stores Code sessions in `<app data>/claude-code-sessions/<account>/<org>/local_<id>.json`, and their conversations in `~/.claude/projects/<folder>/<transcript-id>.jsonl`. `<app data>` is `~/Library/Application Support/Claude` on macOS, `%APPDATA%\Claude` on Windows, and `~/.config/Claude` on Linux.

- **Copy:** clones a session's settings into the target organization under a new ID, drops settings tied to the old organization, and clones the transcript under a new ID.
- **Sync:** both entries point at the same transcript. The Claude app resumes a session with `--resume <transcript id>`, which continues from the latest message, so progress made in either organization shows up in both.
- **Sub-sessions** record their parent in `spawnedFrom.sessionId` (or `forkedFromSessionId` for forks). Those links are rewritten to the copied parent.
- **Live sync** polls the source organization and keeps a hash tree in `<app data>/claude-buddy/watch-state.json`: one sha256 per session file and one per organization over the sorted session hashes, so an unchanged organization costs a single compare. A session that is new to the source is copied in sync; a sub-session that arrives later still points at its parent's copy. Copies made by the watcher (and by `copy`) are never used as sources, so watching both directions cannot loop, and a split that rewinding a synced copy creates is left alone. No dependencies, no network.
- **Organization names** are read from the Claude app's local cache of your claude.ai account (Snappy-compressed V8 data, decoded in pure Python).
</details>

<details>
<summary><b>Build from source</b></summary>

The app is a [Vue 3](https://vuejs.org) + [PrimeVue 4](https://primevue.dev) interface (`frontend/`) in a native window via [pywebview](https://pywebview.flowrl.com). All the file work happens in Python (`claude_buddy/`).

```bash
npm ci --prefix frontend && npm run build --prefix frontend   # builds into claude_buddy/web
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m claude_buddy                              # run the app
npm run dev --prefix frontend                                 # UI with sample data in a browser
.venv/bin/python -m unittest discover -s tests                # tests
```

GitHub Actions builds Windows, macOS and Linux executables on every push and publishes a release for `v*` tags. PrimeVue is kept on 4.x, which is MIT-licensed. Version 5 requires a license key.
</details>

---

<sub>Made by the team behind <a href="https://voxmin.com/?utm_source=claudebuddy&utm_medium=referral&utm_campaign=readme">Voxmin</a>: AI songs, karaoke tracks and lyric videos you can edit.</sub><br>
<sub>Claude Buddy is an unofficial community tool and isn't affiliated with or endorsed by Anthropic. "Claude" is a trademark of Anthropic. MIT licensed.</sub>
