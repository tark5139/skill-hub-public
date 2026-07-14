"""Apple Silicon launcher used by the standalone PyInstaller build."""

from __future__ import annotations

import platform
import sys

from skillhub_cli.main import app


def main() -> None:
    macos_version = platform.mac_ver()[0]
    major = int(macos_version.split(".", 1)[0]) if macos_version else 0
    if sys.platform != "darwin" or platform.machine() != "arm64" or major < 13:
        raise SystemExit("skillctl 1.0 requires macOS 13+ on Apple Silicon (arm64)")
    app()


if __name__ == "__main__":
    main()
