"""仅本机浏览器验收的合成HTTP来源；不读取业务接口或正式配置。"""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from urllib.parse import parse_qs, urlsplit


class SyntheticSource(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        return

    def do_GET(self):
        parsed = urlsplit(self.path)
        if parsed.path != "/sales" or self.headers.get("Authorization") != os.environ["UI_SOURCE_AUTH"]:
            self.send_error(403)
            return
        args = parse_qs(parsed.query)
        business_date = args.get("dt", [""])[0]
        page = args.get("pageIndex", ["0"])[0]
        rows = [
            {"dt": business_date, "id": "001", "shop": "杭州店", "amount": "123.45", "units": 2},
            {"dt": business_date, "id": "002", "shop": "杭州店", "amount": "6.55", "units": 1},
            {"dt": business_date, "id": "003", "shop": "上海店", "amount": "20.00", "units": 4},
        ] if page == "0" else []
        body = json.dumps({"data": rows}, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", 18093), SyntheticSource).serve_forever()
