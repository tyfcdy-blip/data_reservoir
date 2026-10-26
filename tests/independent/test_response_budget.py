"""UTF-8真实大文本回归：单查询和批量总响应分别受限。"""

from uuid import uuid4

from fastapi.testclient import TestClient

from data_service.api import create_app
from data_service.auth import login
from data_service.datasets import register_dataset
from data_service.ingest import append_chunk, commit_batch, create_batch
from data_service.models import DatasetDefinition


def test_large_single_and_batch_responses_return_413_without_partial_data(independent_settings, day):
    slug = "ind_large_" + uuid4().hex[:12]
    definition = DatasetDefinition(slug=slug, name="独立UTF8响应边界", date_field="dt", key_fields=["id"], fields=[
        {"name": "dt", "type": "date", "required": True},
        {"name": "id", "type": "integer", "required": True},
        {"name": "text", "type": "string", "max_length": 10000},
    ])
    register_dataset(independent_settings, definition)
    batch = create_batch(independent_settings, slug, day, 350, None)
    for chunk in range(14):
        rows = [{"dt": str(day), "id": sequence, "text": "汉" * 10000} for sequence in range(chunk * 25, (chunk + 1) * 25)]
        append_chunk(independent_settings, batch["id"], chunk, rows)
    commit_batch(independent_settings, batch["id"])
    token = login(independent_settings, independent_settings.admin_username, independent_settings.admin_password, "independent-response-budget")
    headers = {"Authorization": "Bearer " + token}
    with TestClient(create_app(independent_settings), raise_server_exceptions=False) as client:
        request = {"start_date": str(day), "end_date": str(day), "limit": 350}
        excessive = client.post(f"/api/v1/datasets/{slug}/query", headers=headers, json=request)
        assert excessive.status_code == 413 and "data" not in excessive.json()
        narrow = client.post(f"/api/v1/datasets/{slug}/query", headers=headers, json={**request, "fields": ["id"]})
        assert narrow.status_code == 200 and len(narrow.json()["data"]["rows"]) == 350
        partial = {**request, "limit": 190}
        single = client.post(f"/api/v1/datasets/{slug}/query", headers=headers, json=partial)
        assert single.status_code == 200 and 5 * 1024 * 1024 < len(single.content) < 8 * 1024 * 1024
        grouped = client.post("/api/v1/query/batch", headers=headers,
                              json={"queries": [{"dataset": slug, "query": partial}] * 3})
        assert grouped.status_code == 413 and "data" not in grouped.json()
