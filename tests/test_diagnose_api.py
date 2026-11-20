"""API tests for POST /diagnose: validation, verdicts, conflict, /docs."""

from fastapi.testclient import TestClient

from app.main import (
    app,
    EXAMPLE_DIAG_ALL_DECIDED,
    EXAMPLE_DIAG_CONFLICT,
    EXAMPLE_DIAG_UNDETERMINED,
)

client = TestClient(app)


def _post(payload):
    return client.post("/diagnose", json=payload)


def test_openapi_and_docs_include_diagnose():
    assert client.get("/docs").status_code == 200
    spec = client.get("/openapi.json")
    assert spec.status_code == 200
    assert "/diagnose" in spec.json()["paths"]


def test_all_decided_example():
    r = _post(EXAMPLE_DIAG_ALL_DECIDED["value"])
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["consistent_tables"] == 1
    assert body["detail"] is None
    verdicts = {v["prefix"]: v["verdict"] for v in body["verdicts"]}
    assert verdicts == {
        "10.1.0.0/16": "must_be_present",
        "10.2.0.0/16": "must_be_present",
        "10.3.0.0/16": "must_be_missing",
    }


def test_undetermined_example():
    r = _post(EXAMPLE_DIAG_UNDETERMINED["value"])
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["consistent_tables"] == 2
    assert [v["verdict"] for v in body["verdicts"]] == [
        "undetermined",
        "undetermined",
    ]


def test_conflict_example_reports_only_conflict():
    r = _post(EXAMPLE_DIAG_CONFLICT["value"])
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "conflict"
    assert body["consistent_tables"] == 0
    assert body["verdicts"] is None
    assert body["detail"]


def test_route_status_defaults_to_confirmed():
    payload = {
        "routes": [{"prefix": "10.0.0.0/8", "action": "NH1"}],
        "snapshot": {"installed_count": 1, "observations": []},
    }
    r = _post(payload)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["verdicts"] == []


def test_prefix_and_address_normalization():
    payload = {
        "routes": [
            {"prefix": "10.1.2.3/8", "action": "NH1", "status": "confirmed"},
            {"prefix": "10.1.0.0/16", "action": "NH2", "status": "pending"},
        ],
        "snapshot": {
            "installed_count": 2,
            "observations": [{"address": " 10.1.2.3 ", "result": "NH2"}],
        },
    }
    r = _post(payload)
    assert r.status_code == 200
    assert r.json()["verdicts"][0]["prefix"] == "10.1.0.0/16"


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------

def test_validation_129_routes_rejected():
    payload = {
        "routes": [
            {"prefix": f"10.{i}.0.0/16", "action": "NH1"} for i in range(129)
        ],
        "snapshot": {"installed_count": 129, "observations": []},
    }
    assert _post(payload).status_code == 422


def test_validation_17_pending_rejected():
    payload = {
        "routes": [
            {"prefix": f"10.{i}.0.0/16", "action": "NH1", "status": "pending"}
            for i in range(17)
        ],
        "snapshot": {"installed_count": 17, "observations": []},
    }
    assert _post(payload).status_code == 422


def test_validation_65_observations_rejected():
    payload = {
        "routes": [{"prefix": "10.0.0.0/8", "action": "NH1"}],
        "snapshot": {
            "installed_count": 1,
            "observations": [
                {"address": f"10.0.{i // 256}.{i % 256}", "result": "NH1"}
                for i in range(65)
            ],
        },
    }
    assert _post(payload).status_code == 422


def test_validation_boundaries_accepted():
    payload = {
        "routes": [
            {"prefix": f"10.{i}.0.0/16", "action": "NH1", "status": "pending"}
            for i in range(16)
        ]
        + [
            {"prefix": f"172.{16 + i}.0.0/16", "action": "NH2"}
            for i in range(112)
        ],
        "snapshot": {
            "installed_count": 120,
            "observations": [
                {"address": f"10.0.{i // 256}.{i % 256}", "result": "no-match"}
                for i in range(64)
            ],
        },
    }
    r = _post(payload)
    assert r.status_code == 200
    # 10.0.0.0/16 is pending: no-match observed -> it must be missing
    verdicts = {v["prefix"]: v["verdict"] for v in r.json()["verdicts"]}
    assert verdicts["10.0.0.0/16"] == "must_be_missing"


def test_validation_duplicate_prefixes():
    payload = {
        "routes": [
            {"prefix": "10.0.0.0/8", "action": "NH1"},
            {"prefix": "10.0.0.0/8", "action": "NH2", "status": "pending"},
        ],
        "snapshot": {"installed_count": 1, "observations": []},
    }
    assert _post(payload).status_code == 422


def test_validation_bad_prefix_and_bad_address():
    base = {
        "routes": [{"prefix": "999.0.0.0/8", "action": "NH1"}],
        "snapshot": {"installed_count": 1, "observations": []},
    }
    assert _post(base).status_code == 422
    base["routes"] = [{"prefix": "10.0.0.0/8", "action": "NH1"}]
    base["snapshot"]["observations"] = [{"address": "10.0.0.256", "result": "NH1"}]
    assert _post(base).status_code == 422


def test_validation_reserved_words():
    # 'no-match' is not a valid route action
    payload = {
        "routes": [{"prefix": "10.0.0.0/8", "action": "no-match"}],
        "snapshot": {"installed_count": 1, "observations": []},
    }
    assert _post(payload).status_code == 422
    # empty observation result
    payload = {
        "routes": [{"prefix": "10.0.0.0/8", "action": "NH1"}],
        "snapshot": {
            "installed_count": 1,
            "observations": [{"address": "10.1.1.1", "result": "  "}],
        },
    }
    assert _post(payload).status_code == 422


def test_validation_installed_count_range():
    payload = {
        "routes": [{"prefix": "10.0.0.0/8", "action": "NH1"}],
        "snapshot": {"installed_count": -1, "observations": []},
    }
    assert _post(payload).status_code == 422
    payload["snapshot"]["installed_count"] = 129
    assert _post(payload).status_code == 422


def test_impossible_count_is_conflict_not_422():
    payload = {
        "routes": [{"prefix": "10.0.0.0/8", "action": "NH1"}],
        "snapshot": {"installed_count": 5, "observations": []},
    }
    r = _post(payload)
    assert r.status_code == 200
    assert r.json()["status"] == "conflict"
