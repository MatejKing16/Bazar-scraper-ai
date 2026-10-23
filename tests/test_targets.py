"""
Unit test suite pro TargetManager a REST API pro správu sledovaných cílů.
"""

from pathlib import Path
import pytest
from targets_manager import TargetManager
from web import app


@pytest.fixture
def test_client():
    app.config["TESTING"] = True
    with app.test_client() as client:
        yield client


def test_target_manager_crud(tmp_path: Path):
    target_file = tmp_path / "targets.json"

    # Inicializace s výchozím dotazem
    mgr = TargetManager(
        file_path=target_file,
        default_query="3D tiskárna Ender nebo Prusa",
        default_min_price=1000,
        default_max_price=8000,
        default_score_threshold=8,
    )

    targets = mgr.get_all_targets()
    assert len(targets) == 1
    assert targets[0]["id"] == "target_default"
    assert targets[0]["is_active"] is True

    # Přidání nového cíle (např. Herní notebook)
    new_t = mgr.add_target(
        name="Herní notebook RTX",
        query="Herní notebook RTX 3060 3070 nebo 4060",
        min_price=10000,
        max_price=22000,
        score_threshold=8,
        is_active=True,
    )
    assert new_t["id"].startswith("target_")
    assert new_t["name"] == "Herní notebook RTX"
    assert len(mgr.get_all_targets()) == 2

    # Toggle aktivace
    toggled = mgr.toggle_target(new_t["id"])
    assert toggled is not None
    assert toggled["is_active"] is False
    assert len(mgr.get_active_targets()) == 1

    # Editace cíle
    updated = mgr.update_target(new_t["id"], {"name": "Herní notebooky RTX 4060", "max_price": 25000})
    assert updated["name"] == "Herní notebooky RTX 4060"
    assert updated["max_price"] == 25000

    # Smazání cíle
    deleted = mgr.delete_target(new_t["id"])
    assert deleted is True
    assert len(mgr.get_all_targets()) == 1


def test_web_targets_api(test_client):
    # GET /api/targets
    res = test_client.get("/api/targets")
    assert res.status_code == 200
    data = res.get_json()
    assert data["success"] is True
    assert isinstance(data["targets"], list)

    # POST /api/targets (vytvoření nového)
    payload = {
        "name": "Grafická karta",
        "query": "Grafická karta RTX 4070",
        "min_price": 8000,
        "max_price": 14000,
        "score_threshold": 8,
    }
    create_res = test_client.post("/api/targets", json=payload)
    assert create_res.status_code == 200
    c_data = create_res.get_json()
    assert c_data["success"] is True
    target_id = c_data["target"]["id"]

    # POST /api/targets/<id>/toggle
    toggle_res = test_client.post(f"/api/targets/{target_id}/toggle")
    assert toggle_res.status_code == 200
    assert toggle_res.get_json()["target"]["is_active"] is False

    # PUT /api/targets/<id>
    update_res = test_client.put(f"/api/targets/{target_id}", json={"name": "Nvidia RTX 4070 Super"})
    assert update_res.status_code == 200
    assert update_res.get_json()["target"]["name"] == "Nvidia RTX 4070 Super"

    # DELETE /api/targets/<id>
    del_res = test_client.delete(f"/api/targets/{target_id}")
    assert del_res.status_code == 200
    assert del_res.get_json()["success"] is True
