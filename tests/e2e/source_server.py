"""仅隔离Docker测试使用的两个异构HTTP源，不连接任何真实第三方。"""

import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from urllib.parse import parse_qs, urlsplit


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        return

    def send(self, document, status=200):
        body = json.dumps(document).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path != "/vendor":
            return self.send({}, 404)
        timestamp = self.headers.get("timestamp", "")
        expected = hashlib.md5(("timestamp" + timestamp + os.environ["E2E_VENDOR_SECRET"]).encode(),
                               usedforsecurity=False).hexdigest().upper()
        if self.headers.get("sign") != expected or self.headers.get("appKey") != os.environ["E2E_VENDOR_KEY"]:
            return self.send({}, 401)
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        day = payload["batchQueryParams"][0]["openFieldValue"]
        records = [{"dateTime": day + " 00:00:00", "id": "0001", "shop": " 自营 ", "paid": "100.10", "bill": "30.00"},
                   {"dateTime": day + " 00:00:00", "id": "0002", "shop": " 自营 ", "paid": "-20.10", "bill": "-5.00"},
                   {"dateTime": day + " 00:00:00", "id": "0003", "shop": None, "paid": None, "bill": None}]
        start = payload["pageIndex"] * payload["pageSize"]
        return self.send({"success": True, "code": "200", "data": records[start:start+payload["pageSize"]]})

    def do_GET(self):
        url = urlsplit(self.path)
        if url.path != "/inventory":
            return self.send({}, 404)
        if self.headers.get("Authorization") != os.environ["E2E_INVENTORY_TOKEN"]:
            return self.send({}, 401)
        params = parse_qs(url.query)
        day = params["snapshot"][0]
        records = [{"asOf": day, "skuCode": "007", "warehouseName": "仓库A", "available": 7, "active": True},
                   {"asOf": day, "skuCode": "008", "warehouseName": "仓库B", "available": 3, "active": False}]
        start = (int(params["page"][0])-1) * int(params["limit"][0])
        return self.send({"ok": True, "payload": {"rows": records[start:start+int(params["limit"][0])]}})


HTTPServer(("0.0.0.0", 8001), Handler).serve_forever()
