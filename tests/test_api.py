import pytest

from credrot.app import create_app
from credrot.store import get_batch


@pytest.fixture
def client(db, clock, policy):
    app = create_app(db_path=db.path, clock=clock, policy=policy)
    app.config.update(TESTING=True)
    return app.test_client()


def test_home_and_local_css_are_offline(client):
    page = client.get("/")
    assert page.status_code == 200
    body = page.get_data(as_text=True)
    assert "CRED-ALPHA-READER-01" in body
    assert "127.0.0.1" in body
    assert "cdn." not in body and "https://" not in body
    css = client.get("/static/app.css")
    assert css.status_code == 200
    assert b"--accent" in css.data


def test_credentials_api_reports_effective_state(client):
    response = client.get("/api/credentials?now=2025-01-20T00:00:00Z")
    assert response.status_code == 200
    payload = response.get_json()
    by_id = {row["id"]: row for row in payload["credentials"]}
    assert by_id["CRED-ALPHA-READER-01"]["effective_state"] == "expired"
    assert by_id["CRED-GAMMA-OLD-01"]["state"] == "revoked"
    assert len(by_id) == 10


def test_plan_api_persists_batch(client, db):
    response = client.get("/api/plan?now=2025-01-01T00:00:00Z")
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["batch_id"].startswith("BATCH-20250101T000000Z-")
    assert payload["stored"] is True
    assert get_batch(db, payload["batch_id"]) is not None
    assert client.get("/api/plan?now=bad").status_code == 400


def test_plan_api_surfaces_post_rotation_gate(client, db, policy):
    """The API plans from persisted evidence, same as the CLI."""
    from credrot.clock import FixedClock
    from credrot.model import Verification
    from credrot.rotator import rotate_batch
    from credrot.verifier import verify_all

    class Client:
        def check(self, credential, checked_at, batch_id=None):
            outcome = "invalid" if credential.id == "CRED-GAMMA-SPARE-01" else "valid"
            return Verification(
                credential_id=credential.id, outcome=outcome,
                code=outcome.upper(), checked_at=checked_at,
                batch_id=batch_id, latency_ms=0,
            )

    plan = client.get("/api/plan?now=2025-01-01T00:00:00Z").get_json()
    fixed = FixedClock("2025-01-01T00:00:00Z")
    rotate_batch(db, plan["batch_id"], fixed, policy)
    verify_all(
        db, fixed, policy, batch_id=plan["batch_id"], client=Client()
    )
    gated = client.get("/api/plan?now=2025-01-01T00:10:00Z")
    assert gated.status_code == 200
    assert "POST_ROTATION_UNVERIFIED" in gated.get_data(as_text=True)
    payload = gated.get_json()
    assert payload["summary"]["post_rotation_gated"] == [
        "CRED-ALPHA-READER-01", "CRED-BETA-READER-01",
    ]


def test_rotate_api_accepts_only_known_batch(client):
    plan = client.get("/api/plan?now=2025-01-01T00:00:00Z").get_json()
    response = client.post("/api/rotate", json={"batch_id": plan["batch_id"]})
    assert response.status_code == 200
    assert response.get_json()["status"] == "completed"
    again = client.post("/api/rotate", json={"batch_id": plan["batch_id"]})
    assert again.status_code == 200
    assert again.get_json()["skipped"] == 3
    assert client.post("/api/rotate", json={"batch_id": "missing"}).status_code == 404
    assert client.post("/api/rotate", json={}).status_code == 400
    assert client.post("/api/rotate", data="x").status_code == 400


def test_report_and_health_api(client):
    plan = client.get("/api/plan?now=2025-01-01T00:00:00Z").get_json()
    client.post("/api/rotate", json={"batch_id": plan["batch_id"]})
    health = client.get("/api/health").get_json()
    assert health["status"] == "ok"
    assert health["latest_batch"]["batch_id"] == plan["batch_id"]
    report = client.get(f"/api/report?batch_id={plan['batch_id']}")
    assert report.status_code == 200
    assert report.content_type.startswith("application/json")
    markdown = client.get(
        f"/api/report?batch_id={plan['batch_id']}&format=markdown"
    )
    assert markdown.status_code == 200
    assert "# credrot report" in markdown.get_data(as_text=True)
    assert client.get("/api/report?format=xml").status_code == 400
    assert client.get("/api/report?batch_id=nope").status_code == 400
