"""Rebuild the inline icon sprite in app/static/index.html from Lucide.

    npm pack lucide-static@<version> && tar -xzf lucide-static-*.tgz
    python scripts/build_icons.py package/

Icons are referenced in the UI as `#i-<name>`; ICONS maps those names to
Lucide icon names (https://lucide.dev/icons).
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ICONS = {
    "archive": "archive",
    "back": "arrow-left",
    "check": "check",
    "chevron": "chevron-down",
    "copy": "copy",
    "disc": "disc-3",
    "download": "download",
    "external": "external-link",
    "folder": "folder",
    "forget": "eraser",
    "inbox": "inbox",
    "info": "info",
    "keyboard": "keyboard",
    "left": "chevron-left",
    "link": "link",
    "list": "list-music",
    "log": "scroll-text",
    "mic": "mic-vocal",
    "more": "ellipsis",
    "open": "arrow-right",
    "refresh": "refresh-cw",
    "right": "chevron-right",
    "search": "search",
    "send": "send",
    "skip": "skip-forward",
    "stop": "square",
    "trash": "trash-2",
    "undo": "undo-2",
    "upload": "upload",
    "warn": "triangle-alert",
    "x": "x",
}

ROOT = Path(__file__).resolve().parent.parent
INDEX = ROOT / "app" / "static" / "index.html"
START, END = "<!-- icons:start -->", "<!-- icons:end -->"


def main(package: Path) -> None:
    version = json.loads((package / "package.json").read_text())["version"]
    symbols = []
    for name, lucide in ICONS.items():
        svg = (package / "icons" / f"{lucide}.svg").read_text()
        body = re.search(r"<svg[^>]*>(.*)</svg>", svg, re.S).group(1)
        body = " ".join(line.strip() for line in body.strip().splitlines())
        symbols.append(
            f'      <symbol id="i-{name}" viewBox="0 0 24 24">{body}</symbol>'
        )
    sprite = "\n".join([
        START,
        f"  <!-- Lucide v{version} (ISC, https://lucide.dev); see static/LICENSE-lucide.txt -->",
        '  <svg xmlns="http://www.w3.org/2000/svg" hidden aria-hidden="true">',
        "    <defs>",
        *symbols,
        "    </defs>",
        "  </svg>",
        "  " + END,
    ])
    html = INDEX.read_text()
    html = re.sub(re.escape(START) + ".*?" + re.escape(END), lambda _: sprite, html, flags=re.S)
    INDEX.write_text(html)
    (ROOT / "app" / "static" / "LICENSE-lucide.txt").write_text(
        (package / "LICENSE").read_text()
    )
    print(f"{len(symbols)} icons from Lucide v{version}")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
