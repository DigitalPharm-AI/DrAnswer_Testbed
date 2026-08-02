from __future__ import annotations

import json
from datetime import timedelta

from shared.settings import get_settings
from system_app.models import Notification
from system_app.services.clock_service import ensure_clock
from tests.support.ui import build_ui_app


def _notification(
    *,
    title: str,
    visible_at,
    delivery_channel: str | None = None,
) -> Notification:
    metadata: dict[str, str] = {"severity": "reminder"}
    if delivery_channel is not None:
        metadata["delivery_channel"] = delivery_channel
    return Notification(
        patient_id=get_settings().patient_id,
        notification_type="medication_alert",
        title=title,
        body=f"{title} 본문",
        visible_at=visible_at,
        acknowledged=False,
        metadata_json=json.dumps(metadata),
    )


def test_incremental_cursor_drains_more_visible_rows_than_limit(
    tmp_path,
) -> None:
    client, sessions = build_ui_app(tmp_path)
    with sessions() as session:
        now = ensure_clock(session).current_time
        baseline = _notification(
            title="기준 알림",
            visible_at=now - timedelta(minutes=10),
        )
        incremental = [
            _notification(
                title=f"증분 알림 {index}",
                visible_at=now - timedelta(minutes=5 - index),
            )
            for index in range(5)
        ]
        session.add_all([baseline, *incremental])
        session.flush()
        baseline_id = baseline.public_id
        incremental_ids = [row.public_id for row in incremental]
        session.commit()

    first = client.get(
        f"/api/ui/v1/notifications?after_id={baseline_id}&limit=2"
    ).json()["data"]
    assert [row["id"] for row in first["notifications"]] == [
        incremental_ids[1],
        incremental_ids[0],
    ]
    assert first["last_seen_id"] == incremental_ids[1]

    second = client.get(
        "/api/ui/v1/notifications"
        f"?after_id={first['last_seen_id']}&limit=2"
    ).json()["data"]
    assert [row["id"] for row in second["notifications"]] == [
        incremental_ids[3],
        incremental_ids[2],
    ]
    assert second["last_seen_id"] == incremental_ids[3]

    third = client.get(
        "/api/ui/v1/notifications"
        f"?after_id={second['last_seen_id']}&limit=2"
    ).json()["data"]
    assert [row["id"] for row in third["notifications"]] == [
        incremental_ids[4]
    ]
    assert third["last_seen_id"] == incremental_ids[4]

    exhausted = client.get(
        "/api/ui/v1/notifications"
        f"?after_id={third['last_seen_id']}&limit=2"
    ).json()["data"]
    assert exhausted["notifications"] == []
    assert exhausted["last_seen_id"] == incremental_ids[4]


def test_incremental_cursor_advances_across_internal_only_rows(
    tmp_path,
) -> None:
    client, sessions = build_ui_app(tmp_path)
    with sessions() as session:
        now = ensure_clock(session).current_time
        baseline = _notification(
            title="기준 알림",
            visible_at=now - timedelta(minutes=10),
        )
        hidden_before = _notification(
            title="앞 내부 알림",
            visible_at=now - timedelta(minutes=4),
            delivery_channel="internal_only",
        )
        first_visible = _notification(
            title="첫 공개 알림",
            visible_at=now - timedelta(minutes=3),
        )
        hidden_after = _notification(
            title="뒤 내부 알림",
            visible_at=now - timedelta(minutes=2),
            delivery_channel="chat_only",
        )
        second_visible = _notification(
            title="둘째 공개 알림",
            visible_at=now - timedelta(minutes=1),
        )
        session.add_all(
            [
                baseline,
                hidden_before,
                first_visible,
                hidden_after,
                second_visible,
            ]
        )
        session.flush()
        baseline_id = baseline.public_id
        first_visible_id = first_visible.public_id
        second_visible_id = second_visible.public_id
        hidden_ids = {
            hidden_before.public_id,
            hidden_after.public_id,
        }
        session.commit()

    first = client.get(
        f"/api/ui/v1/notifications?after_id={baseline_id}&limit=1"
    ).json()["data"]
    assert [row["id"] for row in first["notifications"]] == [
        first_visible_id
    ]
    assert first["last_seen_id"] == first_visible_id

    second = client.get(
        "/api/ui/v1/notifications"
        f"?after_id={first['last_seen_id']}&limit=1"
    ).json()["data"]
    assert [row["id"] for row in second["notifications"]] == [
        second_visible_id
    ]
    assert second["last_seen_id"] == second_visible_id
    assert hidden_ids.isdisjoint(
        {
            row["id"]
            for page in (first, second)
            for row in page["notifications"]
        }
    )


def test_incremental_cursor_releases_earlier_created_future_notification(
    tmp_path,
) -> None:
    client, sessions = build_ui_app(tmp_path)
    with sessions() as session:
        now = ensure_clock(session).current_time
        baseline = _notification(
            title="기준 알림",
            visible_at=now - timedelta(minutes=20),
        )
        future = _notification(
            title="예약 알림",
            visible_at=now + timedelta(minutes=10),
        )
        visible_later = _notification(
            title="나중에 생성된 현재 알림",
            visible_at=now - timedelta(minutes=1),
        )
        session.add_all([baseline, future, visible_later])
        session.flush()
        baseline_id = baseline.public_id
        future_public_id = future.public_id
        visible_public_id = visible_later.public_id
        assert future.id < visible_later.id
        session.commit()

    current = client.get(
        f"/api/ui/v1/notifications?after_id={baseline_id}&limit=10"
    ).json()["data"]
    assert [row["id"] for row in current["notifications"]] == [
        visible_public_id
    ]
    assert current["last_seen_id"] == visible_public_id

    with sessions() as session:
        clock = ensure_clock(session)
        clock.current_time += timedelta(minutes=11)
        session.commit()

    released = client.get(
        "/api/ui/v1/notifications"
        f"?after_id={current['last_seen_id']}&limit=10"
    ).json()["data"]
    assert [row["id"] for row in released["notifications"]] == [
        future_public_id
    ]
    assert released["last_seen_id"] == future_public_id
