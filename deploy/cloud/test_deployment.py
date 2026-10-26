"""云配置离线结构验证；不连接Docker守护进程或任何数据库。"""

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parent


class CloudDeploymentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory(prefix="data-reservoir-compose-test-")
        cls.addClassCleanup(cls.directory.cleanup)
        cls.work = Path(cls.directory.name)
        shutil.copyfile(ROOT / "compose.yml", cls.work / "compose.yml")
        (cls.work / ".env").write_text(
            "DS_APP_IMAGE=example.invalid/data-service:subpath-test\n"
            "POSTGRES_USER=data_service\nPOSTGRES_PASSWORD=isolated-placeholder\n"
            "DS_DOCKER_DATABASE_URL=postgresql://data_service:isolated-placeholder@db:5432/ds_restore_test\n"
            "DS_TRUSTED_PROXY_IPS=172.28.0.9\n",
            encoding="utf-8",
        )
        cls.environment = {key: value for key, value in os.environ.items()
                           if not key.startswith(("DS_", "POSTGRES_", "COMPOSE_"))}
        result = subprocess.run(
            ["docker", "compose", "--env-file", ".env", "-f", "compose.yml",
             "--profile", "serve", "--profile", "sync", "config", "--format", "json"],
            cwd=cls.work, env=cls.environment, capture_output=True, text=True, check=True,
        )
        cls.configuration = json.loads(result.stdout)

    def test_independent_project_and_named_database_volume(self):
        self.assertEqual(self.configuration["name"], "muyaju-data-reservoir")
        self.assertEqual(self.configuration["volumes"]["database"]["name"],
                         "muyaju-data-reservoir-pg17")
        self.assertNotIn("init", self.configuration["services"])

    def test_no_host_published_ports_and_database_not_on_ingress(self):
        for service in self.configuration["services"].values():
            self.assertFalse(service.get("ports"))
        self.assertEqual(set(self.configuration["services"]["db"]["networks"]), {"database"})

    def test_implicit_start_cannot_start_api_or_worker(self):
        services = self.configuration["services"]
        self.assertEqual(services["api"]["profiles"], ["serve"])
        self.assertEqual(services["worker"]["profiles"], ["sync"])
        self.assertFalse(services["db"].get("profiles"))

    def test_worker_can_reach_approved_external_source(self):
        self.assertFalse(self.configuration["networks"]["database"].get("internal", False))
        self.assertEqual(set(self.configuration["services"]["worker"]["networks"]), {"database"})

    def test_proxy_and_root_path_are_explicit_and_consistent(self):
        api = self.configuration["services"]["api"]
        command = api["command"]
        self.assertEqual(command[command.index("--root-path") + 1],
                         api["environment"]["DS_ROOT_PATH"])
        self.assertEqual(command[command.index("--forwarded-allow-ips") + 1], "172.28.0.9")
        self.assertEqual(self.configuration["networks"]["ingress"]["name"], "basic_basic_net")
        self.assertTrue(self.configuration["networks"]["ingress"]["external"])
        self.assertIn("muyaju-data-reservoir-api", api["networks"]["ingress"]["aliases"])

    def test_nginx_strips_prefix_and_overwrites_forwarded_headers(self):
        nginx = (ROOT / "nginx-location.conf").read_text(encoding="utf-8")
        self.assertIn("location = /data-service", nginx)
        self.assertIn("return 308 /data-service/;", nginx)
        self.assertIn("proxy_pass http://muyaju-data-reservoir-api:8000/;", nginx)
        self.assertIn("proxy_set_header X-Forwarded-For $remote_addr;", nginx)
        self.assertIn("proxy_set_header X-Forwarded-Proto $scheme;", nginx)
        self.assertNotIn("$proxy_add_x_forwarded_for", nginx)


if __name__ == "__main__":
    unittest.main()
