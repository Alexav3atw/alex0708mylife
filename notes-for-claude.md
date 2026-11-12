# Notes for Claude

Alex's scratchpad for requests and remarks that don't need a chat session.
**Claude: read this file whenever you pull this repo, before making any changes.**

## Current notes

<!-- Add your notes here, one per line, with a date. Example:
- 2026-11-12: please fix the typo on space/nasa-history.html
-->

## Editing workflow (agreed with Alex, 2026-11-12)

- Alex edits pages **directly on GitHub** — always `git pull` before working in the local copy.
- Free to edit on GitHub: `index.html`, `space/`, `kuifje/`, `pc-network/`, `time/`.
- Never edit `space/news.html` or `investing/index.html` directly — the daily GitHub Actions regenerate them and would silently overwrite Alex's changes. Edits go in `scripts/news_template.html`, `scripts/investing_template.html`, `scripts/update_investing.py` instead.
- Stock list and descriptions live in the `STOCKS` list in `scripts/update_investing.py` — code-like, Claude territory.
- Small text edits: Alex does them on GitHub. Structure, new pages, nav links, layout, scripts: Claude.
