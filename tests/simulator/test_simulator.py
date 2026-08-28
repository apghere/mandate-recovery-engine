from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from simulator.app import _in_permitted_window, create_app
from simulator.chaos import ChaosConfig
from simulator.store import AttemptRecord, SequenceCapViolation, Store

ALLOWED_HOUR = next(h for h in range(24) if _in_permitted_window(h))
DISALLOWED_HOUR = next(h for h in range(24) if not _in_permitted_window(h))


def _slot(hour: int) -> str:
    return datetime(2026, 9, 1, hour, 0, tzinfo=UTC).isoformat()


def _execute_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "cycle_id": "CYC-1",
        "sequence_no": 1,
        "idempotency_key": "KEY-1",
        "mandate_id": "MAND-1",
        "amount": 500.0,
        "scheduled_for": _slot(ALLOWED_HOUR),
    }
    payload.update(overrides)
    return payload


@pytest.fixture
def client() -> TestClient:
    app = create_app(db_path=":memory:")
    return TestClient(app)


def test_health(client: TestClient) -> None:
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_execute_within_window_returns_success_or_failure(client: TestClient) -> None:
    resp = client.post("/execute", json=_execute_payload())
    assert resp.status_code == 200
    body = resp.json()
    assert body["outcome"] in ("success", "failure")
    assert (body["raw_reason"] is None) == (body["outcome"] == "success")
    assert body["idempotent_replay"] is False


def test_execute_outside_window_is_denied_independently(client: TestClient) -> None:
    resp = client.post(
        "/execute", json=_execute_payload(scheduled_for=_slot(DISALLOWED_HOUR))
    )
    assert resp.status_code == 409
    assert resp.json()["detail"]["denial_reason"] == "OUTSIDE_EXECUTION_WINDOW"


def test_duplicate_idempotency_key_replays_without_new_attempt(client: TestClient) -> None:
    first = client.post("/execute", json=_execute_payload())
    second = client.post("/execute", json=_execute_payload())
    assert first.status_code == second.status_code == 200
    assert first.json()["outcome"] == second.json()["outcome"]
    assert first.json()["raw_reason"] == second.json()["raw_reason"]
    assert first.json()["idempotent_replay"] is False
    assert second.json()["idempotent_replay"] is True


def test_five_distinct_idempotency_keys_same_sequence_no_rejected(client: TestClient) -> None:
    """A buggy caller retries sequence_no=1 under a fresh idempotency_key.
    The DB-level UNIQUE(cycle_id, sequence_no) must reject it even though
    the caller thinks it's issuing a valid new attempt."""
    client.post("/execute", json=_execute_payload(idempotency_key="KEY-A"))
    resp = client.post("/execute", json=_execute_payload(idempotency_key="KEY-B"))
    assert resp.status_code == 409
    assert resp.json()["detail"]["denial_reason"] == "NPCI_ATTEMPT_CAP_EXCEEDED"


def test_all_four_sequence_numbers_accepted_for_one_cycle(client: TestClient) -> None:
    for n in range(1, 5):
        resp = client.post(
            "/execute",
            json=_execute_payload(idempotency_key=f"KEY-{n}", sequence_no=n),
        )
        assert resp.status_code == 200, resp.json()


def test_sequence_no_above_four_rejected_by_request_validation(client: TestClient) -> None:
    resp = client.post(
        "/execute", json=_execute_payload(idempotency_key="KEY-5", sequence_no=5)
    )
    assert resp.status_code == 422


def test_store_rejects_a_fifth_attempt_even_bypassing_the_api() -> None:
    """CLAUDE.md Phase 2 definition of done: 'simulator rejects a 5th
    attempt' — proven at the storage layer directly, so no caller-side
    validation (Pydantic's le=4) is doing the real enforcement work."""
    store = Store(":memory:")
    for n in range(1, 5):
        store.insert_attempt(
            AttemptRecord(
                cycle_id="CYC-X",
                sequence_no=n,
                idempotency_key=f"K{n}",
                mandate_id="M",
                payer_id=None,
                amount=100.0,
                scheduled_for=_slot(ALLOWED_HOUR),
                outcome="failure",
                raw_reason="INSUFFICIENT FUNDS",
                executed_at=_slot(ALLOWED_HOUR),
            )
        )
    with pytest.raises(SequenceCapViolation):
        store.insert_attempt(
            AttemptRecord(
                cycle_id="CYC-X",
                sequence_no=5,
                idempotency_key="K5",
                mandate_id="M",
                payer_id=None,
                amount=100.0,
                scheduled_for=_slot(ALLOWED_HOUR),
                outcome="failure",
                raw_reason=None,
                executed_at=_slot(ALLOWED_HOUR),
            )
        )
    assert store.count_attempts("CYC-X") == 4


def test_chaos_5xx_does_not_consume_the_attempt_slot(client: TestClient) -> None:
    client.post("/admin/chaos", json={"error_5xx_rate": 1.0, "timeout_rate": 0.0})
    resp = client.post("/execute", json=_execute_payload())
    assert resp.status_code == 503

    client.post("/admin/chaos", json={"error_5xx_rate": 0.0, "timeout_rate": 0.0})
    retry = client.post("/execute", json=_execute_payload())
    assert retry.status_code == 200


def test_chaos_timeout_returns_unknown_and_consumes_the_slot(client: TestClient) -> None:
    client.post("/admin/chaos", json={"error_5xx_rate": 0.0, "timeout_rate": 1.0})
    resp = client.post("/execute", json=_execute_payload())
    assert resp.status_code == 200
    assert resp.json()["outcome"] == "unknown"

    client.post("/admin/chaos", json={"error_5xx_rate": 0.0, "timeout_rate": 0.0})
    retry = client.post(
        "/execute", json=_execute_payload(idempotency_key="KEY-2")
    )
    assert retry.status_code == 409
    assert retry.json()["detail"]["denial_reason"] == "NPCI_ATTEMPT_CAP_EXCEEDED"


def test_chaos_config_rejects_out_of_range_rate() -> None:
    with pytest.raises(ValueError, match=r"\[0, 1\]"):
        ChaosConfig(error_5xx_rate=1.5)
