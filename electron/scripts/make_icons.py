"""Draw the shell's icons from the same code that draws the page's mark.

One drawing, everywhere: the installer, the window, the tray in each of its
states. Generated rather than committed as artwork so a change to the mark in
meetbot/desktop.py reaches the app on the next build.

Run from the repository root (``npm run icons`` does): writes electron/resources/
icon.ico for the installer and executables, and electron/assets/tray-*.png
for the notification area at 1x and 2x.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from meetbot.desktop import spark_image, write_icon  # noqa: E402

BUILD = ROOT / "electron" / "resources"
ASSETS = ROOT / "electron" / "assets"

#: Tray states, and how each is drawn.
STATES = {
    "idle": {},
    "recording": {"recording": True},
    "muted": {"recording": True, "muted": True},
}


def main() -> int:
    BUILD.mkdir(parents=True, exist_ok=True)
    ASSETS.mkdir(parents=True, exist_ok=True)
    if not write_icon(BUILD / "icon.ico"):
        print("Pillow is needed to draw the icons: pip install pillow")
        return 1
    spark_image(512).save(BUILD / "icon.png")
    spark_image(128).save(ASSETS / "logo.png")
    for name, style in STATES.items():
        # Electron picks the @2x file on a high-DPI display by its name.
        spark_image(16, **style).save(ASSETS / f"tray-{name}.png")
        spark_image(32, **style).save(ASSETS / f"tray-{name}@2x.png")
    print(f"icons written to {BUILD} and {ASSETS}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
