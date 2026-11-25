#!/usr/bin/env python3
"""Recent-advice page builder for alex0708mylife.

Renders scripts/advice_content.json into investing/advice.html from
scripts/advice_template.html. The content JSON is written by the biweekly
portfolio review (a Claude Code run scheduled on Alex's machine) and holds
one verdict card per watchlist stock: verdict + reasoning lines + sources.

Privacy rule (deliberate, agreed with Alex): the public page shows share
names and verdicts ONLY - no share counts, no portfolio weights, no amounts,
no personal plans. The review writes the sanitised content here and keeps
the full private report in ClaudeFiles, outside the repository.

Verdicts map to badge styles in the template: keep / reduce / add / review /
sell. Run locally with:
  python scripts/update_advice.py
"""
import html
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "scripts" / "advice_template.html"
CONTENT = ROOT / "scripts" / "advice_content.json"
OUTPUT = ROOT / "investing" / "advice.html"

VERDICTS = {"Keep", "Reduce", "Add", "Review", "Sell"}


def card(entry):
    """Render one verdict card from a content entry."""
    tick = entry.get("tick", "")
    name = entry.get("name", tick)
    verdict = entry.get("verdict", "Review")
    vcls = verdict.lower()
    lis = "\n".join(
        f"      <li>{html.escape(line)}</li>" for line in entry.get("lines", [])
    )
    srcs = "\n".join(
        f'      <a href="{html.escape(url)}" target="_blank" rel="noopener">{html.escape(label)}</a>'
        for label, url in entry.get("sources", [])
    )
    return f"""    <div class="advice-card">
      <div class="adv-head">
        <h2 class="adv-name">{html.escape(name)} <span class="adv-tick">{html.escape(tick)}</span></h2>
        <span class="adv-verdict v-{vcls}">{html.escape(verdict)}</span>
      </div>
      <ul class="adv-lines">
{lis}
      </ul>
      <div class="adv-src">
        <span>Sources</span>
{srcs}
      </div>
    </div>"""


def main():
    content = json.loads(CONTENT.read_text(encoding="utf-8"))
    entries = content.get("entries") or []
    rows = "\n".join(card(e) for e in entries)
    page = (TEMPLATE.read_text(encoding="utf-8")
                    .replace("{{ROWS}}", rows)
                    .replace("{{STAMP}}", html.escape(content.get("reviewed", "")))
                    .replace("{{NEXT}}", html.escape(content.get("next", ""))))
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(page, encoding="utf-8")
    print(f"Wrote {OUTPUT} with {len(entries)} verdict cards.")


if __name__ == "__main__":
    main()
