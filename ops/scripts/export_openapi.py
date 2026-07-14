from __future__ import annotations

import json
from pathlib import Path

from skillhub.config import Settings
from skillhub.main import create_app


def main() -> None:
    project_root = Path(__file__).resolve().parents[2]
    settings = Settings(
        env="test",
        database_url="sqlite:///:memory:",
        admin_token="openapi-generation-token-32-characters",
        local_storage_path=project_root / "var" / "openapi-storage",
    )
    document = create_app(settings, initialize_schema=False).openapi()
    destination = project_root / "docs" / "api" / "openapi.json"
    destination.write_text(
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(destination)


if __name__ == "__main__":
    main()
