# 阿里云数据服务迁移说明

本方案只部署独立数据服务，不包含经营看板。正式入口为 `https://ai.muyaju.net/data-service/`。这是分阶段操作说明，不是已经完成云端部署的证明；每个阶段验收后再执行下一阶段。当前不要求用户一次复制执行所有命令。

## 已确认的服务器和容量

2026-10-26 用户提供：CentOS 7、3.10 内核、x86_64、Docker 26.1.4、Compose 2.27.1；8 核、约 15GB 内存，可用约 12GB。数据盘 `/data` 可用 48GB，系统盘可用 14GB；DockerRootDir 为 `/data/docker`。本机数据服务数据库约 1903MB，未计全部 WAL、镜像、备份和未来增长。

48GB 可作为本次迁移起点，不代表已证明 180 天持续增长容量足够。上传前计算镜像包、备份包、数据库恢复空间和至少一份回滚备份的总需求，并预留 WAL、日志和同事项目的增长空间。出现磁盘快速增长或不足时先停止本项目迁移；不执行全局镜像清理。Docker 显示的“可回收”镜像可能是同事的回滚版本。

PostgreSQL 使用名为 `muyaju-data-reservoir-pg17` 的独立 Docker 数据卷，通常位于当前 Docker 数据目录 `/data/docker/volumes`。无需另外安装系统 PostgreSQL，也无需为它单独购买一块磁盘。部署前用 `docker volume inspect` 核对是否已存在同名卷；若存在未知旧卷，停止核对归属，不能直接复用。Docker 卷使容器替换后保留数据，但不代替数据库备份。禁止 `docker compose down -v`。

CentOS 7 和旧内核是当前环境事实；新镜像必须在此服务器独立试启动后才可认定兼容。本方案不升级共享服务器系统或 Docker。

## 文件、网络与保留内容

源码使用 GitHub 私有仓库管理；源码、配置模板可以提交，实际 `.env`、运行配置、数据库备份和用户提供的部署文档不得提交。应用镜像必须使用新的唯一发布标签或 digest，不能覆盖本机旧 `1.6.0` 标签。`DS_APP_IMAGE` 在实际发布镜像确定后填写。公司现有仓库为 `test.muyaju.net:80`；沿用已有认证方式，但不能从文档复制其他项目的密钥，也不擅改全局 Docker 配置。

部署目录建议 `/mnt/docker/data-reservoir`，仅保存小体积配置和脚本；镜像由 Docker 放在 `/data/docker`。备份和临时大包放 `/data/data-reservoir-backups`，限制为管理员可读。上传以下文件：

- `deploy/cloud/compose.yml`、`database-transfer.sh`、由 `.env.example` 填写的实际 `.env`。
- `config/datasets.json`、`config/sources.json`，复制原本机实际运行配置，不能用示例替换。即使主要配置已存数据库，也需要核对文件中的兼容配置。
- 经校验的数据库 dump 和镜像（若采用离线镜像包方式）。

数据库完整备份保留业务数据、表和字段配置、映射、同步任务和历史、调度设置、开放接口规则、管理员及调用密钥的服务端记录。调用密钥通常只保存校验信息，不会恢复已丢失的明文；已有客户端继续使用原明文密钥，受原到期和撤销规则约束。必须保留原 `DS_CONFIG_ENCRYPTION_KEY`，否则无法解密原数据源和模型配置。数据库用户密码可以另设，但连接地址要一致，URL 密码须正确编码。不要将敏感值发到聊天。

Compose 项目名为 `muyaju-data-reservoir`，独立于同事的 `/mnt/docker/muyaju/docker-compose.yml`。数据库无宿主机公开端口；API 同时接内部数据库网络与现有 `basic_basic_net`，仅以唯一别名 `muyaju-data-reservoir-api` 供 Nginx 访问。worker 的普通桥接网络允许出网。不会给数据库加入 Nginx 网络。

## 第一阶段：准备和隔离恢复演练

1. 先在本地完成子路径测试、恢复演练和独立复核，准备唯一镜像版本、文件清单和 SHA-256。备份必须用 `pg_dump -Fc`，不能复制运行中的 PostgreSQL 文件目录。演练期间本机可以继续使用，演练备份不能作为最后切换快照。
2. 用户确认本项目服务器目录和卷名未被使用，再建立目录、上传文件、设置 `.env` 和备份文件为 600。不要在终端打印 `.env`，不要发送 `docker compose config` 的完整输出；它会展开敏感值。
3. 将现有 Nginx 在 `basic_basic_net` 上的真实容器 IP 填入 `DS_TRUSTED_PROXY_IPS`。可只读获取：

```bash
docker inspect nginx --format '{{(index .NetworkSettings.Networks "basic_basic_net").IPAddress}}'
```

该值不能使用 `*` 或整个共享网段。Nginx 重建后 IP 可能变化，需重新核对后更新本项目 API；普通 reload 不重建容器。不要把客户端来源头当作可信身份。

4. 填好镜像、数据库用户密码、计划恢复库连接和原加密密钥，进入独立部署目录。仅检查配置有效性，不输出完整配置：

```bash
cd /mnt/docker/data-reservoir
docker compose --env-file .env -f compose.yml config --quiet
docker compose --env-file .env -f compose.yml up -d db
docker compose --env-file .env -f compose.yml ps db
```

首次只启动数据库，不运行 `init`。Compose 故意没有 init 服务；旧数据库应整体恢复，不能用初始化流程替代恢复。`api` 和 `worker` 有 profile 防止普通 `up -d` 意外启动；后续仍只使用明确的服务名。

5. 云端数据库健康且磁盘空间充足后，校验上传 dump 的 SHA-256 与本机一致。按已确定的唯一库名恢复（示例日期只是命名示例，每次必须新名字）：

```bash
bash database-transfer.sh restore /data/data-reservoir-backups/rehearsal.dump ds_restore_rehearsal_20261026
```

脚本拒绝覆盖同名库；失败保留新建库供排查，不删除任何旧库，也不自动切换应用。恢复成功后将 `.env` 的 `DS_DOCKER_DATABASE_URL` 指向该演练库，再仅启动 API。应核对 schema 版本 5、数据集和接口配置数量、已发布日期覆盖、关键聚合结果、管理员登录和既有只读密钥行为。不要启动 worker、触发源请求或测试真实写入。

```bash
docker compose --env-file .env -f compose.yml up -d api
docker compose --env-file .env -f compose.yml ps api
```

通过健康检查不代表全部迁移正确。配置、加密解密、权限、聚合结果和日期覆盖都要对账；使用现有应用接口验证，不能将明文密钥放在共享命令历史或截图中。

## 第二阶段：Nginx 接入和地址验收

先查看现有 `ai.muyaju.net` HTTPS server 的必要片段，确认没有 `/data-service/` 路由冲突。备份实际配置文件到 `/data/data-reservoir-backups`，不要在 `conf.d` 留以 `.conf` 结尾的备份，避免被重复加载。

把 `nginx-location.conf` 的两个 location 块合并到该 HTTPS server 内。此文件不能直接作为顶层配置丢入 `conf.d`，不能覆盖同事的整个 server。`proxy_pass` 的尾斜杠会剥离 `/data-service/`，程序通过 `DS_ROOT_PATH` 生成对外链接。代理覆盖客户端伪造的转发头；API 只信任上一阶段确认的 Nginx IP。

```bash
docker exec nginx nginx -t
```

测试成功后才执行：

```bash
docker exec nginx nginx -s reload
```

若失败，不 reload，恢复刚才备份的配置并再次验证。API 重建后容器 IP 可能变化，当前静态 upstream 需要重新执行 Nginx 测试和 reload 以刷新解析；此时可能短暂重连。

验收 `/data-service` 跳转到 `/data-service/`，后台 CSS/JS、登录、退出、文档、OpenAPI servers、复制出的查询地址都包含前缀；响应不跳到 HTTP，不访问网站根 `/api/v1/`。另外抽测同事原有入口仍能访问。不能仅以一个页面能打开判断迁移完成。

## 第三阶段：最终快照和单端切换

由用户确认切换窗口后，等待本机全部 pending/running 同步任务结束，并检查没有新任务排队。停止本机 worker 后再次检查批次状态；同步刚好入队或仍在执行时停止切换、处理后再继续。随后暂停本机 API 写入口，保证最后备份期间不再生成新配置、任务或数据。

1. 生成最终一致快照、SHA-256 和对账基线，安全上传。保留本机数据库、配置和原镜像。
2. 云端停止本项目 API；云端 worker 此时仍未启动。将最终快照恢复到另一个唯一新库，保留演练库便于核查，空间不足时由用户决定清理或扩容，不自动删除。
3. 将云端连接切到最终恢复库，仅启 API，重复关键对账和权限验收；检查所有 pending/running 均为空。如不是空，先报告原因，不让 worker 自动处理遗留任务。
4. 用户完成最终确认后才显式启动云端 worker：

```bash
docker compose --env-file .env -f compose.yml up -d worker
docker compose --env-file .env -f compose.yml ps
```

同一时刻只有一端 worker 运行。数据库锁不能跨本机和云端阻止双端同步。`DS_SCHEDULE_ENABLED=false` 不能替代停止 worker，数据库里已有的表级计划仍可能执行，worker 还会执行保留期清理。

5. 观察工作进程心跳、第一次实际同步结果、请求错误、磁盘和内存增长；核验备份可恢复。前端同事只需要正式开放接口地址和对应授权密钥，不需要数据库账号或管理员账号。

## 后续备份、更新与回滚

日常备份使用当前真实恢复库名，目标文件必须不存在：

```bash
bash database-transfer.sh backup /data/data-reservoir-backups/after-cutover.dump ds_restore_final_20261026
```

正式备份计划和保留数量需按磁盘增长决定，保留至少一份服务器之外的安全备份。`.partial` 为未成功完成的文件，不能用于恢复；只有脚本成功且 SHA-256 校验后的 `.dump` 才进入恢复演练。

后续修改遵循“源码提交 → 唯一版本镜像 → 隔离验证 → 数据库备份 → 仅替换本项目服务 → 验收”。数据卷不随镜像更新而删除；含数据库结构变更的版本需要另外的迁移、兼容及回滚批准。本次子路径适配不修改 schema。

回滚分界：云端尚未接受真实写入时，可停止云端 API/worker、恢复本次 Nginx 配置备份并测试后 reload，确认云端全部停写后恢复本机 API/worker。云端已经接受新数据或配置修改后，不能直接恢复旧本机服务，否则会丢掉切换后的变化；应先停写、备份云端、确认增量如何保留，再决定反向迁移或前向修复。不能同时开放两端写入，不能以旧镜像启动不兼容的新 schema。

## 验证边界

仓库中的配置模板和脚本不等于云端已执行。Docker 配置渲染、脚本参数和恢复拒绝覆盖、代理前缀和端到端访问均需记录实际命令、退出码和证据。旧内核兼容、共享服务器负载、现有 Nginx 真实配置、云端磁盘最终占用及真实数据迁移在服务器执行前均为未验证。Codex 自检不等于用户最终确认、人工代码评审、CI 或发布审批。

2026-10-26 本地隔离验证记录（不代表服务器验证）：

| 验证 | 命令 | 结果 |
| --- | --- | --- |
| 配置结构与网络边界 | `python deploy/cloud/test_deployment.py` | 6 项通过，退出码 0；仅离线渲染，使用占位值 |
| Bash 语法 | `bash -n deploy/cloud/database-transfer.sh` | 退出码 0 |
| Python 静态检查 | `ruff check deploy/cloud/test_deployment.py deploy/cloud/verify-isolated-transfer.py` | 退出码 0 |
| 真实 PostgreSQL 备份恢复 | `python deploy/cloud/verify-isolated-transfer.py` | 5 项通过，退出码 0；随机命名临时容器、tmpfs、无宿主端口；结束后删除该测试容器和网络 |

恢复演练使用 2 行可识别合成数据（`001=12.34`、`002=56.78`），核验恢复后行数及合计 `69.12`，并验证重复备份文件、已有恢复库和非恢复库名均拒绝覆盖。首轮测试遇到 Windows 默认 GBK 解码异常，不计通过；明确 UTF-8 后重新执行通过。测试环境为 Windows、Python 3.12、Docker Desktop、Compose 5.3.0、PostgreSQL 17；服务器 Compose 2.27.1 和旧内核仍需现场验证。
