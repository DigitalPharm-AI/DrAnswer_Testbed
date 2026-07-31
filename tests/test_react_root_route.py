from __future__ import annotations

import re

from fastapi.testclient import TestClient

from system_app.main import create_app


def test_react_app_is_served_at_root_and_legacy_routes_are_absent() -> None:
    client = TestClient(create_app())

    root = client.get("/")

    assert root.status_code == 200
    assert 'id="root"' in root.text

    asset_paths = re.findall(
        r'(?:src|href)="(/static/react/[^"]+)"',
        root.text,
    )
    assert len(asset_paths) == 2
    for path in asset_paths:
        asset = client.get(path)
        assert asset.status_code == 200
        assert asset.content

    assert client.get("/react").status_code == 404
    assert client.get("/static/styles.css").status_code == 404
    assert client.get("/partials/chat").status_code == 404
    client.close()
