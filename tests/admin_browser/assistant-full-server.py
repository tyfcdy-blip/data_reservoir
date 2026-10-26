"""端到端验收专用：真实 API + PostgreSQL + 受控 HTTP 模型，不连接供应商。"""
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import tempfile
import threading
from urllib.parse import urlsplit, urlunsplit

from cryptography.fernet import Fernet
from dotenv import dotenv_values
import uvicorn

from data_service import auth, db, ingest
from data_service.api import create_app
from data_service.config import Settings
from data_service.datasets import register_dataset
from data_service.models import DatasetDefinition

DATABASE = "standalone_api_test"
SLUG = "assistant_full_sales"
KEY = "synthetic-assistant-full-model-key"
ADMIN = "assistant_full"
PASSWORD = "synthetic-assistant-full-password"
ROW_MARKER = "SYNTHETIC_FULL_ROW_NEVER_SEND"
DAY = datetime.now(ingest.BEIJING).date() - timedelta(days=1)
ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = ROOT / "evidence/assistant-full-server.json"


def verify_database(settings: Settings) -> None:
    if urlsplit(settings.database_url).path != "/" + DATABASE:
        raise RuntimeError("禁止访问白名单以外的数据库")
    with db.connect(settings) as connection:
        actual = connection.execute("SELECT current_database() AS name").fetchone()["name"]
        if actual != DATABASE:
            raise RuntimeError("数据库身份与隔离白名单不符")


def prepare(directory: str) -> Settings:
    if os.environ.get("ASSISTANT_FULL_CONTAINER") != "1" or str(ROOT) != "/work":
        raise RuntimeError("只允许在专用验收容器执行")
    values = dotenv_values(ROOT / ".local/test-api.env")
    configured = values.get("DS_TEST_DATABASE_URL") or values.get("DS_DATABASE_URL")
    if not configured:
        raise RuntimeError("缺少隔离测试库配置")
    # 同一文件中任何数据库设置都必须命中精确白名单，不能回退到应用库。
    for name in ("DS_DATABASE_URL", "DS_TEST_DATABASE_URL"):
        if values.get(name) and urlsplit(values[name]).path != "/" + DATABASE:
            raise RuntimeError("测试配置含非白名单数据库")
    parsed = urlsplit(configured)
    if parsed.path != "/" + DATABASE or parsed.hostname != "127.0.0.1" or parsed.port != 15433:
        raise RuntimeError("隔离测试库地址不符合预期")
    network = urlunsplit(parsed._replace(netloc=parsed.netloc.rsplit("@", 1)[0] + "@db:5432"))
    source_path = Path(directory) / "sources.json"
    source_path.write_text('{"sources":[]}', encoding="utf-8")
    settings = Settings(_env_file=None, database_url=network, admin_username=ADMIN,
                        admin_password=PASSWORD, config_encryption_key=Fernet.generate_key().decode(),
                        source_config_path=str(source_path), schedule_enabled=False)
    verify_database(settings)
    with db.connect(settings) as connection:
        connection.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
    db.migrate(settings)
    auth.initialize_admin(settings)
    definition = DatasetDefinition(slug=SLUG, name="完整链路合成销售表", date_field="dt", key_fields=["id"],
        fields=[{"name": "dt", "type": "date", "required": True, "description": "业务日期"},
                {"name": "id", "type": "string", "required": True, "description": "合成行标识"},
                *[{"name": name, "type": "string", "dimension": True, "description": label}
                  for name, label in [("shop", "店铺"), ("category", "类目"), ("channel", "渠道"), ("owner", "负责人")]],
                {"name": "paid", "type": "decimal", "description": "支付金额"},
                {"name": "refund", "type": "decimal", "description": "成功退款金额"},
                {"name": "private_marker", "type": "string", "description": "不应发送模型的业务行标记"}])
    register_dataset(settings, definition)
    rows = [{"dt": DAY.isoformat(), "id": str(index), "shop": shop, "category": category,
             "channel": channel, "owner": owner, "paid": paid, "refund": refund, "private_marker": ROW_MARKER}
            for index, (shop, category, channel, owner, paid, refund) in enumerate([
                ("A店", "寝具", "天猫", "甲", "1", "1"),
                ("A店", "寝具", "天猫", "甲", "99", "9"),
                ("B店", "寝具", "天猫", "甲", "100", "25"),
                ("B店", "家具", "天猫", "甲", "50", "5"),
                ("B店", "家具", "京东", "甲", "20", "10"),
                ("B店", "家具", "京东", "乙", "0", "1")])]
    batch = ingest.create_batch(settings, SLUG, DAY, len(rows), None)
    ingest.append_chunk(settings, batch["id"], 0, rows)
    ingest.commit_batch(settings, batch["id"])
    return settings


def draft() -> dict:
    return {"status": "ready", "name": "四维退款率汇总", "slug": "assistant_full_summary",
            "definition": {"fields": None, "group_by": ["shop", "category", "channel", "owner"],
                "aggregates": [{"field": "paid", "op": "sum", "alias": "paid_total"},
                               {"field": "refund", "op": "sum", "alias": "refund_total"}],
                "formulas": [{"alias": "refund_rate", "expression": "refund_total / paid_total", "decimal_places": 6}],
                "filters": [], "result_filters": [], "order_by": [{"field": "refund_rate", "direction": "desc"}],
                "require_complete": True}, "allowed_filter_fields": ["shop", "category", "channel", "owner"],
            "max_limit": 100, "questions": [], "notes": ["支付金额和成功退款金额按四个维度求和，再以退款合计除以支付合计。"]}


def main() -> None:
    requests = []
    report = {"database": DATABASE, "database_identity_verified": False, "seed_rows": 6,
              "business_day": DAY.isoformat(), "provider": "controlled HTTP protocol boundary, not a real model",
              "provider_requests": requests, "cleanup": "not_started"}
    with tempfile.TemporaryDirectory(prefix="assistant-full-") as directory:
        settings = prepare(directory)
        report["database_identity_verified"] = True
        class Provider(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                document = json.loads(raw)
                correct_auth = self.headers.get("Authorization") == "Bearer " + KEY
                messages = document.get("messages", [])
                is_connection = bool(messages and '"connection":"ok"' in messages[-1].get("content", ""))
                only_metadata = ROW_MARKER.encode() not in raw and KEY.encode() not in raw
                valid_path = self.path == "/v1/chat/completions"
                requests.append({"kind": "connection_test" if is_connection else "draft", "path": self.path,
                                 "authorization_valid": correct_auth, "business_rows_absent": only_metadata,
                                 "model": document.get("model")})
                if not (correct_auth and only_metadata and valid_path):
                    self.send_error(400, "Controlled provider rejected invalid protocol")
                    return
                content = {"connection": "ok"} if is_connection else draft()
                body = json.dumps({"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(content, ensure_ascii=False)}}]}, ensure_ascii=False).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                EVIDENCE.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        provider = ThreadingHTTPServer(("127.0.0.1", 18095), Provider)
        thread = threading.Thread(target=provider.serve_forever, daemon=True)
        thread.start()
        EVIDENCE.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        try:
            uvicorn.run(create_app(settings), host="0.0.0.0", port=18094, access_log=False, log_level="warning")
        finally:
            provider.shutdown()
            provider.server_close()
            thread.join(timeout=2)
            verify_database(settings)
            with db.connect(settings) as connection:
                report["saved_services_before_cleanup"] = connection.execute("SELECT COUNT(*) AS total FROM query_services").fetchone()["total"]
                connection.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
            db.migrate(settings)
            report["cleanup"] = "isolated_schema_reset_after_database_identity_verification"
            EVIDENCE.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()
