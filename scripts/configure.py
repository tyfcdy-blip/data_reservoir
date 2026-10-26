"""生成独立本机配置；已有配置不覆盖，不输出任何密码。"""

from pathlib import Path
import secrets
import base64


def new_config_key() -> str:
    # Fernet要求32字节随机密钥的URL安全Base64编码，初始化脚本保持仅标准库。
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    config = root / "config"
    config.mkdir(exist_ok=True)
    # Git 不保存运行配置。首次克隆补齐空文件，重复执行不覆盖已有接入设置。
    for name, content in (("datasets.json", "[]\n"), ("sources.json", '{"sources": []}\n')):
        try:
            with (config / name).open("x", encoding="utf-8") as target_config:
                target_config.write(content)
        except FileExistsError:
            pass
    target = root / ".env"
    if target.exists():
        original = target.read_text(encoding="utf-8").splitlines()
        keys = [line.split("=", 1)[1].strip().strip("\"'") for line in original
                if line.split("=", 1)[0].strip() == "DS_CONFIG_ENCRYPTION_KEY"]
        if not any(keys):
            lines = [line for line in original if line.split("=", 1)[0].strip() != "DS_CONFIG_ENCRYPTION_KEY"]
            lines.append("DS_CONFIG_ENCRYPTION_KEY=" + new_config_key())
            target.write_text("\n".join(lines) + "\n", encoding="utf-8")
            print("保留已有账号和数据库配置，已生成管理配置加密密钥，请备份.env。")
        else:
            print("已有.env及配置加密密钥，保留原配置。")
        return
    database_password = secrets.token_urlsafe(32)
    admin_password = secrets.token_urlsafe(24)
    values = {
        "POSTGRES_PASSWORD": database_password,
        "DS_DATABASE_URL": f"postgresql://data_service:{database_password}@127.0.0.1:15433/data_service",
        "DS_DOCKER_DATABASE_URL": f"postgresql://data_service:{database_password}@db:5432/data_service",
        "DS_ADMIN_PASSWORD": admin_password,
        "DS_CONFIG_ENCRYPTION_KEY": new_config_key(),
    }
    template = (root / ".env.example").read_text(encoding="utf-8")
    lines = [line.split("=", 1)[0] + "=" + values[line.split("=", 1)[0]]
             if line.split("=", 1)[0] in values else line for line in template.splitlines()]
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    private = root / ".local"
    private.mkdir(exist_ok=True)
    (private / "admin-credentials.txt").write_text(
        "管理API账号：admin\n密码：" + admin_password + "\n仅本机保管，不打入镜像。\n", encoding="utf-8")
    print("已生成独立配置，管理员凭据保存于.local/admin-credentials.txt。")


if __name__ == "__main__":
    main()
