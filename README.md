# Claude Archive

A local web UI to browse every conversation you've ever had with [Claude Code](https://claude.com/claude-code) — plus **The Almanac**, a stats page that audits your prompting habits without mercy.

Everything runs on your machine. Nothing is uploaded anywhere; the app just reads the conversation logs Claude Code already keeps in `~/.claude/projects/`.

## What you get

**The Archive** — a card-catalog style browser:
- Sidebar with a real directory tree built from where each conversation actually happened (`~/Desktop/my-project/…`)
- Every conversation listed with its AI-generated title, your opening message, date, and prompt count, grouped by month
- Click any conversation to read the full transcript — your prompts, Claude's replies rendered as markdown, and tool calls (Bash, Edit, Read…) shown as compact chips
- Search across titles and opening messages
- Deep links: every conversation has a shareable-with-yourself URL hash

**The Almanac** (`⁂` button in the sidebar) — analytics over your entire history:
- Prompts typed, words written, tokens Claude wrote back, tool calls, sub-agents spawned
- The confessional: how many times Claude said *"You're absolutely right."*
- Manners audit: please/thanks count, swear words, ALL-CAPS rage prompts
- A 24-hour clock of when you summon Claude (night-owl certification included)
- Most-used tools, top bash commands, most-rewritten files, git commits made
- Your verbal tics and the words you can't stop typing
- Records: marathon session, chattiest session, longest prompt ever

## Requirements

- macOS (Linux works too — anywhere Claude Code keeps `~/.claude/projects/`)
- Python 3.9+ (preinstalled on recent macOS)
- You've used Claude Code at least once

No dependencies. No `pip install`. Two files.

## Run it

```bash
git clone https://github.com/rajbeer1/claude-archive.git
cd claude-archive
python3 server.py
```

Open **http://localhost:4747** — done.

The first Almanac load reads your full history (can take a few seconds if you have hundreds of MB of logs); after that everything is cached and only changed files are re-read. Refresh the page after new conversations to pick them up.

## How it works

Claude Code stores every session as a JSONL file under `~/.claude/projects/<project-dir>/<session-id>.jsonl`. `server.py` (stdlib only) scans those files, extracts titles / messages / tool calls / token usage, and serves a tiny JSON API plus the single-page UI in `index.html`.

- Everything stays local: the server binds to `127.0.0.1` only.
- Read-only: the app never modifies or deletes your conversation files.

## Privacy note

Your conversation logs may contain anything you've ever typed into Claude Code — API keys you pasted, personal details, that 90,000-character prompt. This tool makes them *visible*, not *public*. Keep it that way: don't deploy this to a server.
