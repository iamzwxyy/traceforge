from fastapi.testclient import TestClient

from tenant_cache_api.main import app


def test_profile_endpoint_returns_tenant_data() -> None:
    client = TestClient(app)
    response = client.get("/profiles/42", headers={"x-tenant-id": "acme"})

    assert response.status_code == 200
    assert response.json() == {"id": "42", "name": "Ada @ Acme"}
