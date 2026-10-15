"""What PyInstaller freezes into scribe-server.exe. See meetbot.server_main."""

from meetbot.server_main import main

if __name__ == "__main__":
    raise SystemExit(main())
