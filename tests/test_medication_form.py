from fastapi.testclient import TestClient

from system_app.main import app


def test_medication_form_accepts_preset_schedule_and_choices():
    client = TestClient(app)

    response = client.post(
        "/medications",
        data={
            "medication_choice": "혈압약",
            "dosage_choice": "1정",
            "schedule_template": "morning_evening",
            "start_date": "2026-04-20",
            "end_date": "2026-04-20",
            "instructions": "테스트 등록",
        },
    )

    assert response.status_code == 204

    page = client.get("/")
    assert page.status_code == 200
    assert "혈압약" in page.text
    assert "아침 08:00" in page.text
    assert "야간 21:00" in page.text


def test_medication_form_accepts_custom_name_and_dosage():
    client = TestClient(app)

    response = client.post(
        "/medications",
        data={
            "medication_choice": "__custom__",
            "medication_name_custom": "맞춤약",
            "dosage_choice": "__custom__",
            "dosage_custom": "반정",
            "schedule_template": "morning_lunch_evening",
            "start_date": "2026-04-20",
            "end_date": "2026-04-20",
            "instructions": "커스텀 등록",
        },
    )

    assert response.status_code == 204

    page = client.get("/")
    assert page.status_code == 200
    assert "맞춤약" in page.text
    assert "반정" in page.text
