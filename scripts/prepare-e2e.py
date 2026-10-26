"""创建专用容器测试配置，不改主项目配置或数据。"""

import json
from pathlib import Path
import secrets


def main():
    root = Path(__file__).resolve().parents[1]
    private = root / ".local"
    config = private / "e2e-config"
    config.mkdir(parents=True, exist_ok=True)
    target = private / "e2e.env"
    if not target.exists():
        password = secrets.token_urlsafe(32)
        values = {
            "POSTGRES_PASSWORD": password,
            "DS_DOCKER_DATABASE_URL": f"postgresql://data_service:{password}@db:5432/standalone_e2e_test",
            "DS_ADMIN_USERNAME": "e2e_admin", "DS_ADMIN_PASSWORD": secrets.token_urlsafe(24),
            "E2E_VENDOR_KEY": secrets.token_urlsafe(16), "E2E_VENDOR_SECRET": secrets.token_urlsafe(24),
            "E2E_INVENTORY_TOKEN": secrets.token_urlsafe(24),
        }
        target.write_text("\n".join(key + "=" + value for key, value in values.items()) + "\n", encoding="utf-8")
    datasets = [
        {"slug": "product_daily", "name": "隔离测试商品", "date_field": "dt", "key_fields": ["product_id"],
         "fields": [{"name": "dt", "type": "date", "required": True},
                    {"name": "product_id", "type": "string", "required": True},
                    {"name": "shop_name", "type": "string", "dimension": True},
                    {"name": "pay_amount", "type": "decimal"}, {"name": "profit_amount", "type": "decimal"}]},
        {"slug": "inventory_daily", "name": "隔离测试库存", "date_field": "snapshot_date", "key_fields": ["sku", "warehouse"],
         "fields": [{"name": "snapshot_date", "type": "date", "required": True},
                    {"name": "sku", "type": "string", "required": True},
                    {"name": "warehouse", "type": "string", "required": True, "dimension": True},
                    {"name": "available_qty", "type": "integer"}, {"name": "active", "type": "boolean"}]},
    ]
    sources = {"sources": [
        {"dataset_slug": "product_daily", "protocol": "vendor_batch", "url": "http://source:8001/vendor",
         "vendor_api_id": "synthetic-products", "vendor_app_key_env": "E2E_VENDOR_KEY", "vendor_app_secret_env": "E2E_VENDOR_SECRET",
         "page_size": 2, "requests_per_second": 10,
         "field_mapping": {"dt": "dateTime", "product_id": "id", "shop_name": "shop", "pay_amount": "paid", "profit_amount": "bill"},
         "date_fields_from_datetime": ["dt"]},
        {"dataset_slug": "inventory_daily", "protocol": "http_json", "url": "http://source:8001/inventory",
         "method": "GET", "parameter_location": "query", "date_param": "snapshot", "page_param": "page", "page_size_param": "limit",
         "page_start": 1, "page_size": 2, "requests_per_second": 10,
         "rows_path": "payload.rows", "success_field": "ok", "success_value": True,
         "header_env": {"Authorization": "E2E_INVENTORY_TOKEN"},
         "field_mapping": {"snapshot_date": "asOf", "sku": "skuCode", "warehouse": "warehouseName", "available_qty": "available", "active": "active"}},
    ]}
    (config / "datasets.json").write_text(json.dumps(datasets, ensure_ascii=False, indent=2), encoding="utf-8")
    (config / "sources.json").write_text(json.dumps(sources, ensure_ascii=False, indent=2), encoding="utf-8")
    print("独立E2E配置已生成，仅合成数据源。")


if __name__ == "__main__":
    main()
