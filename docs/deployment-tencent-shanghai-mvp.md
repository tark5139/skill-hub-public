# 腾讯云上海 Lighthouse + COS 个人 MVP 部署

本文档是一套可重复执行的单机部署基线，适配仓库根目录现有 `compose.yaml`。它不会自动创建或删除腾讯云资源，不会修改腾讯云安全组，也不包含任何真实凭据。该方案接受单机故障和维护窗口，不应被视为团队生产高可用架构。

## 1. 边界与拓扑

- 地域固定为腾讯云上海 `ap-shanghai`。
- 一台 Ubuntu/Debian Lighthouse 运行 API、Worker、PostgreSQL 和可选 Caddy。
- PostgreSQL 不映射宿主机端口；API 默认只绑定 `127.0.0.1:8080`。
- 两个上海 COS 私有桶分别保存隔离对象和已发布的内部不可变对象；第三个独立私有桶保存数据库备份。
- GitHub 只接收经逐版本明确授权的公开派生包，不是事实源，也没有反向同步路径。

```mermaid
flowchart LR
    Agent["macOS Agent / CLI"] -->|HTTPS| Caddy["Caddy :443"]
    Caddy --> API["Registry API :8080"]
    API --> PG[("PostgreSQL :5432")]
    API --> COS[("Tencent COS · ap-shanghai")]
    Worker["Scanner / Publisher Worker"] --> PG
    Worker --> COS
    Worker -->|"逐版本审批的单向发布"| GitHub["GitHub public repository"]
```

建议从 2 核 4 GB 起步；扫描大压缩包出现内存压力时优先升至 4 核 8 GB。个人 MVP 的初始目标为每日备份、RPO 24 小时、RTO 2 小时。

## 2. 随仓库提供的部署资产

所有腾讯云专用资产位于 `ops/tencent/`：

| 文件 | 用途 |
|---|---|
| `bootstrap-lighthouse.sh` | 幂等安装 Docker/Compose、创建非 root 部署用户和目录、检出指定 Git 版本 |
| `.env.example` | 与现有 Compose 兼容的生产环境变量模板 |
| `deploy.sh` | 精确版本部署、部署前备份、迁移、启动和就绪检查 |
| `healthcheck.sh` | 重试本地或 HTTPS 的 live/ready 探针 |
| `rollback.sh` | 仅回滚应用代码，不自动执行危险的数据库降级 |
| `backup.sh`、`backup.env.example` | 不执行应用目录代码，按 Docker label 直接备份当前 PostgreSQL 并可复制到独立 COS 桶 |
| `systemd/skill-hub-backup.service`、`systemd/skill-hub-backup.timer` | 每日 02:15（Asia/Shanghai）强制执行本地与 COS 备份的 systemd 模板 |
| `restore.sh` | 校验备份、重建数据库、恢复并迁移成功后才启动服务 |
| `common.sh` | 安全读取指定 `.env` 字段、可移植 checksum 与独占运维锁 |
| `trusted-tag-signers.example` | root-owned 发布签名者完整 OpenPGP 指纹白名单示例；不包含公钥材料 |
| `cam-policy-application.json` | 应用访问隔离桶和发布桶的最小 CAM 策略模板 |
| `cam-policy-backup.json` | 备份身份仅写指定备份前缀的 CAM 策略模板 |

所有脚本使用 Bash、`set -euo pipefail`，失败即返回非零退出码。脚本不会打印配置文件内容、SecretId、SecretKey、数据库密码或 GitHub token。

当前 `Dockerfile`、PostgreSQL 与 Caddy 使用的是版本标签而非镜像 digest，因此这里的“可重复”指固定代码 commit、固定配置契约和固定操作顺序，不承诺跨时间的逐字节相同镜像。每次上线应保存 `docker compose images` 输出；需要供应链级可复现构建时，再通过独立变更把基础镜像固定到审核过的 digest。

## 3. 云资源人工准备

1. 在上海购买 Ubuntu/Debian Lighthouse，推荐至少 2 核 4 GB，并启用磁盘与内存告警。
2. 正式 HTTPS 上线时，准备已完成 ICP 备案的 API 域名，将 A/AAAA 记录指向 Lighthouse 公网地址；暂时没有域名时使用第 5 节的 SSH 隧道过渡方案，不以公网 IP 暴露 API。
3. 在 `ap-shanghai` 创建三个**私有** COS 桶，名称均须包含 APPID：
   - `skill-hub-quarantine-<APPID>`：隔离区；
   - `skill-hub-release-<APPID>`：内部发布区；
   - `skill-hub-backup-<APPID>`：数据库异机备份。
4. 发布桶与备份桶开启版本控制和服务端加密；三个桶全部拒绝公共读。隔离桶按安全与审计期限配置生命周期清理。
5. 创建两个 CAM 子用户/身份：应用身份与备份身份。不要使用主账号密钥，也不要让两组身份共用 SecretKey。
6. 如启用 GitHub 公开发布，准备仓库级 GitHub App，权限仅授予目标仓库 `Contents: write`，运行时使用短时 installation token。
7. 初始化脚本会从系统 apt 仓库安装 AWS CLI 与 `flock`；若系统仓库无法提供，初始化会失败并要求先修复软件源，而不会降级成无异机备份或无互斥锁的部署。

## 4. 发布信任根、Lighthouse 初始化与网络基线

初始化只接受**精确的 OpenPGP 签名 tag**。40 位 commit、短 commit、分支和未签名 tag 均会被拒绝。不要把 GitHub 下载页面本身同时当作代码来源和唯一信任来源：应通过独立渠道从发布负责人取得发布公钥，并通过另一个已认证渠道核对完整 fingerprint。

公钥材料与授权白名单必须分离：

- 公钥 bundle 只让 GnuPG 获得验证签名所需的 key material，不表示该 key 已获发布授权；
- fingerprint allowlist 每行仅放一个 40 或 64 位完整指纹，决定哪些签名者获授权；
- 两者在上传后都必须为 root 所有、不可被组或其他用户写入；bootstrap 会把白名单安装为 `/etc/skill-hub/trusted-tag-signers`，把公钥安装为 `/etc/skill-hub/release-tag-public-keys.asc`；
- bootstrap 会先证明公钥 bundle 至少有一个 fingerprint 与 allowlist 精确匹配，再把公钥导入实际运行 deploy/rollback 的 `skillhub` 用户 GnuPG keyring。仅写 allowlist、不导入公钥会导致 `git verify-tag` 必然失败。

先在独立可信工作站显示公钥指纹，并与发布负责人提供的指纹逐字符核对：

```sh
gpg --batch --with-colons --show-keys release-tag-public-keys.asc \
  | awk -F: '$1 == "fpr" { print toupper($10) }'
cp ops/tencent/trusted-tag-signers.example trusted-tag-signers
# 编辑 trusted-tag-signers，只保留核对通过的完整 fingerprint。
```

再从本地已审阅的仓库副本上传初始化脚本、白名单和公钥。以下文件由 root 接收，避免执行或信任未经固定审核的远程 bootstrap：

```sh
scp ops/tencent/bootstrap-lighthouse.sh trusted-tag-signers \
  release-tag-public-keys.asc root@SERVER_IP:/root/
ssh root@SERVER_IP
chown root:root /root/bootstrap-lighthouse.sh /root/trusted-tag-signers \
  /root/release-tag-public-keys.asc
chmod 0700 /root/bootstrap-lighthouse.sh
chmod 0600 /root/trusted-tag-signers /root/release-tag-public-keys.asc
REPO_TAG=v0.1.0 \
  TRUSTED_TAG_SIGNERS_SOURCE=/root/trusted-tag-signers \
  TRUSTED_TAG_PUBLIC_KEY_FILE=/root/release-tag-public-keys.asc \
  /root/bootstrap-lighthouse.sh
```

脚本默认：

- 把仓库放在 `/opt/skill-hub`；
- 创建或使用 `skillhub` 部署用户；
- 把数据库备份目录设为 `/var/backups/skill-hub/postgres`；
- 安装 Docker Engine、Compose v2、AWS CLI 与 `flock`；
- 创建 `/var/lock/skill-hub/ops.lock`，并通过 systemd-tmpfiles 在重启后重建正确权限；部署、回滚、备份、恢复同一时间只允许执行一个；
- 创建仅 root 与部署组可访问的 `/etc/skill-hub`；
- 把已审阅的运维脚本安装为 root 所有的 `/usr/local/lib/skill-hub-ops/` 稳定副本，应用代码回滚不会删除恢复工具；
- 将已核对的 signer allowlist 固定为 root-owned 信任根，并使用 `skillhub` 用户 keyring 中的公钥验证 tag；
- **不修改** UFW 和腾讯云安全组。

只有正式 HTTPS 路径显式提供下列参数时才会启用 UFW；SSH 来源必须是明确的管理 CIDR。无域名过渡期不要使用这组会开放 80/443 的参数，应保持脚本默认不改 UFW，并由腾讯云安全组仅放行受限 SSH：

```sh
CONFIGURE_UFW=1 ADMIN_CIDR=203.0.113.10/32 \
  REPO_TAG=v0.1.0 \
  TRUSTED_TAG_SIGNERS_SOURCE=/root/trusted-tag-signers \
  TRUSTED_TAG_PUBLIC_KEY_FILE=/root/release-tag-public-keys.asc \
  /root/bootstrap-lighthouse.sh
```

正式 HTTPS 上线时，腾讯云安全组人工设置为：

| 端口 | 来源 | 用途 |
|---|---|---|
| 22/TCP | 固定管理 IP 或 VPN 网段 | 运维 SSH；禁止全网开放 |
| 80/TCP | `0.0.0.0/0`、`::/0` | Caddy ACME 与 HTTPS 跳转 |
| 443/TCP、443/UDP | `0.0.0.0/0`、`::/0` | HTTPS / HTTP/3 |

禁止在安全组或 UFW 开放 5432、8080。出站至少允许 DNS、NTP、GitHub 和 COS 上海端点所需的 HTTPS。

应用部署不会自我覆盖这组 root-owned 运维脚本。需要升级运维控制面时，必须从本地已审阅副本重新上传 `bootstrap-lighthouse.sh`，并以 allowlist 授权签名者签署的精确 tag 再运行初始化；raw commit 永远不是发布信任身份。对已有部署重跑 bootstrap 时，它会先取得与 deploy/rollback/backup/restore 相同的独占锁，并一直持有到 bootstrap 完全退出；稳定版 `backup.sh` 在 apt、Git fetch 和 checkout 前创建恢复点，失败即停止。首次空机在锁建立后还会确认不存在同项目 PostgreSQL 容器或 volume，才允许 clone。这使运维工具升级与普通应用发布保持显式分离。

bootstrap 已自动导入公钥。轮换或诊断时，可显式重新导入并查看 `skillhub` 用户实际 keyring 的完整指纹；输出必须与 root-owned allowlist 对账：

```sh
sudo -iu skillhub gpg --batch --import \
  /etc/skill-hub/release-tag-public-keys.asc
sudo -iu skillhub gpg --with-colons --fingerprint \
  | awk -F: '$1 == "fpr" { print toupper($10) }'
sudo awk '!/^[[:space:]]*(#|$)/ { print toupper($0) }' \
  /etc/skill-hub/trusted-tag-signers
```

## 5. 配置、部署与健康检查

重新登录使 Docker 用户组生效，然后用模板创建只对部署用户可读的配置。以下示例使用默认 `skillhub` 用户；如果初始化脚本输出了其他部署用户，应相应替换：

```sh
sudo -iu skillhub
cd /opt/skill-hub
cp ops/tencent/.env.example .env
chmod 0600 .env
```

编辑 `.env` 并替换每个 `REPLACE_*`。数据库密码在 `POSTGRES_PASSWORD` 中使用原值，在 `SKILLHUB_DATABASE_URL` 中必须 URL 编码。关键字段包括：

```dotenv
POSTGRES_DB=skillhub
POSTGRES_USER=skillhub
POSTGRES_PASSWORD=REPLACE_WITH_RANDOM_DATABASE_PASSWORD
SKILLHUB_DATABASE_URL=postgresql+psycopg://skillhub:REPLACE_WITH_URL_ENCODED_DATABASE_PASSWORD@postgres:5432/skillhub

SKILLHUB_ADMIN_TOKEN=REPLACE_WITH_AT_LEAST_32_RANDOM_CHARACTERS
SKILLHUB_PUBLIC_BASE_URL=https://skills.example.cn
SKILLHUB_DOMAIN=skills.example.cn

SKILLHUB_S3_ENDPOINT_URL=https://cos.ap-shanghai.myqcloud.com
SKILLHUB_S3_REGION=ap-shanghai
SKILLHUB_S3_QUARANTINE_BUCKET=REPLACE_QUARANTINE_BUCKET_WITH_APPID
SKILLHUB_S3_RELEASE_BUCKET=REPLACE_RELEASE_BUCKET_WITH_APPID
SKILLHUB_S3_ACCESS_KEY_ID=REPLACE_WITH_APPLICATION_CAM_SECRET_ID
SKILLHUB_S3_SECRET_ACCESS_KEY=REPLACE_WITH_APPLICATION_CAM_SECRET_KEY

SKILLHUB_REQUIRE_SIGNATURE=true
SKILLHUB_TRUSTED_PUBLIC_KEYS_JSON={"REPLACE_OWNER:REPLACE_KEY_ID":"base64:REPLACE_PUBLIC_KEY"}
```

### 当前无域名的安全过渡部署

无域名时不要把 API 的 8080 端口开放到公网，也不要以自签名公网 HTTPS 冒充正式入口。其余数据库、COS、签名和管理员密钥仍按生产要求填写，只把以下三项设为：

```dotenv
SKILLHUB_PUBLIC_BASE_URL=http://127.0.0.1:8080
SKILLHUB_DOMAIN=localhost
SKILLHUB_CORS_ORIGINS=[]
```

`SKILLHUB_DOMAIN=localhost` 仅用于满足 Compose 对 inactive Caddy profile 仍会执行的变量插值；它不授权启动 Caddy。应用对 `http://127.0.0.1:8080` 的配置校验可通过，Compose 又只把 API 映射到服务器 loopback，因此公网无法直接访问。

腾讯云安全组在过渡期只保留 `22/TCP`，来源限定为固定管理 IP 或 VPN CIDR；不得开放 80、443、5432 或 8080。首次发布明确关闭 HTTPS：

```sh
docker compose config --quiet
CONFIRM_EMPTY_INITIAL_DEPLOY=yes ENABLE_HTTPS=0 \
  /usr/local/lib/skill-hub-ops/deploy.sh v0.1.0
```

在 macOS 管理机保持下面的 SSH 会话运行；`ADMIN_USER` 是已配置公钥登录的受限运维账号：

```sh
ssh -N -L 8080:127.0.0.1:8080 ADMIN_USER@SERVER_IP
```

随后本机浏览器、CLI 或 Agent 使用 `http://127.0.0.1:8080`，并可先验证：

```sh
curl --fail http://127.0.0.1:8080/health/live
curl --fail http://127.0.0.1:8080/health/ready
```

该过渡入口只服务于建立隧道的这台管理 Mac 及其本地 Codex、Claude、Trae、WorkBuddy、OpenClaw、Hermes 等进程。飞书 Aily 之类运行在云端的连接器无法访问管理 Mac 的 `127.0.0.1`，不得为迁就云端 Agent 临时开放 8080 或使用未经批准的公网隧道；这类集成必须等待备案域名和正式公网 HTTPS 入口。

域名、ICP 备案和 DNS 全部就绪后，先把 `.env` 改为正式的 `SKILLHUB_PUBLIC_BASE_URL=https://skills.example.cn` 与 `SKILLHUB_DOMAIN=skills.example.cn`，再按第 4 节放行 80/443，并以 `ENABLE_HTTPS=1` 部署同一已验签 tag 或更新版本。只有外部 HTTPS healthcheck 成功，发布才完成；之后可停止 SSH 隧道。不要先开放端口再等待备案或 DNS。

先检查模板插值，不输出完整配置：

```sh
docker compose config --quiet
```

第一次部署前必须先完成第 6 节 CAM 策略和第 7 节 `/etc/skill-hub/backup.env` 配置。部署与回滚默认要求迁移前备份成功复制到 COS；只有经书面批准的紧急操作才可显式设置 `REQUIRE_COS_BACKUP_BEFORE_CHANGE=0`，且本地备份仍不能省略。

部署时必须传入已审阅的精确签名 tag；任意 commit ID、分支、未签名 tag，以及由 allowlist 外签名者签署的 tag 均被拒绝。脚本先停止当前 Caddy/API/Worker，再调用 root-owned 稳定版 `backup.sh` 直接定位当前 PostgreSQL 容器；本地 checksum 与所要求的 COS 副本全部成功后，才允许 `git fetch`、tag 验证、checkout、Dockerfile build 或目标版本 Compose 命令。随后脚本执行 Alembic 向前迁移、删除旧 Worker 心跳、重建服务并等待新 Worker 心跳。启用 Caddy 时，外部 HTTPS 探针也是部署成功条件。从目标 checkout 切换点开始，任一构建、迁移、内部/外部健康检查或状态落盘失败都会由 EXIT trap 再次停止 Caddy/API/Worker；只有所有检查与状态写入成功后才解除 fail-closed 保护。

首次空主机没有数据库可备份，需人工核对不存在同项目 PostgreSQL 容器和 volume，并作一次显式确认：

```sh
CONFIRM_EMPTY_INITIAL_DEPLOY=yes \
  /usr/local/lib/skill-hub-ops/deploy.sh v0.1.0
```

后续发布不再需要该空主机确认：

```sh
/usr/local/lib/skill-hub-ops/deploy.sh v0.2.0
```

内部检查与外部 HTTPS 检查分别执行：

```sh
/usr/local/lib/skill-hub-ops/healthcheck.sh
HEALTHCHECK_BASE_URL=https://skills.example.cn \
  /usr/local/lib/skill-hub-ops/healthcheck.sh
docker compose ps
```

`healthcheck.sh` 默认从 `.env` 读取 `SKILLHUB_API_PORT`，也可显式提供 `HEALTHCHECK_BASE_URL`。`/health/ready` 同时依赖 PostgreSQL 和部署后新写入的 Worker 心跳；旧心跳在启动前被删除。失败时脚本只输出服务状态，不自动输出可能含敏感上下文的应用日志。通过批准的安全日志渠道人工查看 `docker compose logs api worker`。

部署状态保存在 `.git/skillhub-deploy/`：

- `current_revision`：上次成功部署的 commit；
- `previous_revision`：可供代码回滚的上一个 commit；
- `current_ref`：上次成功部署、且已通过 allowlist 验证的精确签名 tag；
- `previous_ref`：可供回滚且需再次验签的上一个精确签名 tag；
- `images.txt`：该次成功部署后的 Compose 镜像 ID 清单；
- `pending_revision`、`pending_ref`：部署中断时保留，供事件调查与回到最后成功版本。

## 6. COS 与 CAM 最小权限

把策略模板中的 APPID、桶名和前缀全部替换后，在 CAM 控制台创建自定义策略并分别绑定到应用身份和备份身份。模板刻意不授予列出账号全部桶、修改桶 ACL、删除发布对象或管理其他云资源的权限。

应用模板 `ops/tencent/cam-policy-application.json` 仅允许：

- 隔离桶对象：Get、Head、Put、Delete；
- 发布桶对象：Get、Head、Put；
- 显式拒绝删除发布对象，不允许桶级管理。

备份模板 `ops/tencent/cam-policy-backup.json` 仅允许向备份桶的 `postgres/*` 前缀执行简单或分块上传所需动作。不要再为该身份附加宽泛的 COS 系统策略；灾难恢复下载应使用单独、临时批准的 break-glass 身份。Action 和资源写法遵循腾讯云的 [COS API 授权策略指引](https://cloud.tencent.com/document/product/436/30580)。

上线前用测试对象验证允许和拒绝路径。尤其要验证当前 S3 客户端的条件写入语义与 COS 行为一致：相同 key、不同 SHA-256 的第二次写入必须被应用拒绝。发布桶的版本控制/保留策略是第二道保护，不能替代应用层校验。

凭据落点：

- 应用 CAM 凭据只放 `/opt/skill-hub/.env`，权限 `0600`；
- 备份 CAM 凭据只放 `/etc/skill-hub/backup.env`，root 所有、部署组只读，权限 `0640`；
- 两者均不得进入 Git、镜像、命令行参数、日志、工单或聊天记录；
- 定期轮换，轮换时先添加新凭据、验证，再撤销旧凭据。

## 7. PostgreSQL 备份与恢复

首次配置独立备份身份：

```sh
DEPLOY_GROUP=$(id -gn)
sudo install -d -m 0750 -o root -g "$DEPLOY_GROUP" /etc/skill-hub
sudo install -m 0640 -o root -g "$DEPLOY_GROUP" \
  ops/tencent/backup.env.example /etc/skill-hub/backup.env
sudoedit /etc/skill-hub/backup.env
```

运行备份：

```sh
sudo BACKUP_ENV_FILE=/etc/skill-hub/backup.env \
  /usr/local/lib/skill-hub-ops/backup.sh
```

稳定版备份脚本不 source、调用或执行目标 Git 工作树中的任何脚本或 Compose 文件；它按固定 Compose label 找到唯一运行中的 PostgreSQL 容器，执行 `pg_dump` custom format，先写 `.partial`，完成后原子改名。checksum 仅记录备份文件 basename，便于移动；校验成功后才将 dump 与 checksum 以服务端加密方式复制到独立 COS 桶。默认本地保留 14 天，直接运行时可设置 `REQUIRE_COS_BACKUP=1` 强制异机复制成功。

先以与定时任务完全相同的强制 COS 模式人工验证一次；只有本地 checksum 和 COS 上传都成功，才安装并启动随仓库提供的 systemd 单元：

```sh
sudo REQUIRE_COS_BACKUP=1 BACKUP_ENV_FILE=/etc/skill-hub/backup.env \
  /usr/local/lib/skill-hub-ops/backup.sh
sudo install -m 0644 -o root -g root \
  ops/tencent/systemd/skill-hub-backup.service \
  ops/tencent/systemd/skill-hub-backup.timer \
  /etc/systemd/system/
sudo systemd-analyze verify \
  /etc/systemd/system/skill-hub-backup.service \
  /etc/systemd/system/skill-hub-backup.timer
sudo systemctl daemon-reload
sudo systemctl enable --now skill-hub-backup.timer
systemctl list-timers skill-hub-backup.timer --all
```

定时器按 `Asia/Shanghai` 每日 02:15 触发，`Persistent=true` 会在主机错过计划时间后补跑，`RandomizedDelaySec=10m` 避免固定时刻争用资源。服务设置 `UMask=0077`，固定调用 root-owned `/usr/local/lib/skill-hub-ops/backup.sh`，并强制 `REQUIRE_COS_BACKUP=1`；它不会从 Git 工作树执行脚本，也不会把凭据写进 systemd 单元。首次启用后应手动启动并核对 journal，随后把非零退出状态接入告警系统：

```sh
sudo systemctl start skill-hub-backup.service
sudo systemctl status skill-hub-backup.service --no-pager
sudo journalctl -u skill-hub-backup.service --since today --no-pager
```

不要用 cron 重复调度同一脚本。修改模板后必须通过已签名发布和运维控制面升级流程重新安装，再执行 `systemctl daemon-reload`；不要直接在线编辑 `/etc/systemd/system` 中的审计基线。

至少每月执行一次异机恢复演练。下载备份后先独立核对 checksum，再在维护窗口显式确认恢复：

```sh
CONFIRM_RESTORE=skillhub CONFIRM_DATABASE=skillhub \
  /usr/local/lib/skill-hub-ops/restore.sh \
  /var/backups/skill-hub/postgres/skillhub-YYYYMMDDTHHMMSSZ.dump
```

必须使用稳定副本 `/usr/local/lib/skill-hub-ops/restore.sh`，不要直接调用旧的低层恢复脚本。安全恢复会强制要求 checksum，停止 Caddy/API/Worker，彻底重建目标数据库后 `pg_restore`，再执行当前代码的 Alembic migration；从停止服务开始，EXIT trap 会在任一步失败（包括内部健康检查）时再次按 Docker label 停止 Caddy/API/Worker。只有恢复、迁移、新 Worker 心跳、内部探针和外部 HTTPS 探针全部通过才解除 fail-closed 保护。旧版生成且 checksum 中记录绝对路径的备份，应先在隔离环境重新计算为 `SHA256  basename.dump` 格式并演练后才能进入正式恢复窗口。

恢复前应创建 Lighthouse 云盘快照并记录当前 COS release key 水位。恢复后随机抽查至少三个 manifest 与对应对象 SHA-256，再恢复外部流量。

## 8. 回滚流程

代码回滚默认使用上一次成功部署记录，并在切换代码前再次备份数据库：

```sh
CONFIRM_ROLLBACK=yes /usr/local/lib/skill-hub-ops/rollback.sh
```

也可明确指定已审阅且由 allowlist 授权签名者签署的精确 tag；commit ID 与分支不被接受：

```sh
CONFIRM_ROLLBACK=yes /usr/local/lib/skill-hub-ops/rollback.sh \
  v0.1.0
```

回滚脚本只重建并重启 API/Worker，**不会运行 Alembic downgrade，也不会自动恢复数据库**。从目标 checkout 起，任一步失败或健康检查失败都会停止 Caddy/API/Worker。数据库迁移必须保持向后兼容；如果旧代码无法读取当前 schema，进入维护状态后使用与目标代码匹配的部署前备份执行第 7 节的显式恢复流程。

数据库恢复不会删除后来写入的 COS 内容寻址对象。不要在事故期间批量删除 COS 对象；先完成元数据与对象清单对账，再按批准的生命周期流程处理孤立对象。

## 9. GitHub 公开发布安全

- GitHub Release 只保存经过公开审批、脱敏和重新扫描后的派生包；COS 发布桶仍是内部事实源。
- 每次授权必须绑定 Skill、精确版本、包 SHA-256、目标 owner/repository、审批人和审批记录 ID。
- tag 固定为 `<skill>-v<semver>`；tag 或 Release 已存在时拒绝覆盖。
- 发布顺序为 Draft Release、上传 ZIP/manifest/checksums/signature/LICENSE/README/attestation、回读校验，最后公开。
- 公开发布现实上不可撤回。发现问题时在 Hub 标记撤销、停止推荐旧版并发布修复版，不能把删除远端当作可靠召回机制。

GitHub token 只使用仓库级 GitHub App 的短时 installation token。当前 Worker 只在进程启动时从环境变量读取 token，尚未集成 installation token 自动刷新器，因此**无人值守 GitHub 发布必须保持关闭**。单次发布时由批准的运维人员获取新 token，原子更新 `.env`、重建 Worker、完成已批准发布，随后清空 token 并再次重建 Worker。不要用长期个人 PAT 掩盖这个缺口。

“不可变”有明确边界：应用与 CAM 策略阻止发布身份覆盖/删除 release 对象，COS 版本控制与保留策略提供第二层保护，GitHub 还需人工开启 Release immutability；但腾讯云主账号、另一个高权限 CAM 身份、GitHub 仓库所有者或平台级删除仍位于该控制边界之外。这不是合规 WORM 声明。数据库、COS 与 GitHub 之间也没有分布式事务，部分失败必须通过审批记录、artifact SHA-256 和外部发布状态进行对账，禁止自动反向覆盖。

## 10. 深圳与广州 P95 探针

探针必须分别运行在物理位于深圳和广州的独立主机；仅修改 location 标签不会改变网络路径。每个地点至少 30 个有效样本，推荐连续三个时段无失败且 P95 不高于 250 ms：

```sh
python3 ops/scripts/p95_probe.py \
  --location shenzhen \
  --url https://skills.example.cn/health/ready \
  --samples 30 \
  --p95-threshold-ms 250

python3 ops/scripts/p95_probe.py \
  --location guangzhou \
  --url https://skills.example.cn/health/ready \
  --samples 30 \
  --p95-threshold-ms 250
```

结果应连同时间、运营商和探针公网 IP 保存，不得只保留一次最好成绩。

## 11. 上线检查与退出条件

上线前必须确认：

- 三个 COS 桶均为私有；应用与备份 CAM 身份均无法列出或管理无关资源；
- 5432/8080 未对公网开放，22 仅允许管理网段；
- HTTPS 证书、重定向和 HSTS 正常；
- `.env`、`backup.env`、GitHub App 私钥和 CAM 密钥未进入镜像、Git 历史或日志；
- PostgreSQL 备份、checksum、COS 复制与异机恢复演练均通过；
- Alembic migration job 完成，API 与 Worker 就绪，停掉 Worker 后 ready 探针能在超时窗口内失败；
- 生产配置缺少可信 Ed25519 公钥或关闭签名时，应用拒绝启动；
- COS 条件写入测试证明既有 key 不会被不同内容覆盖；
- 深圳和广州探针满足约定阈值；
- 每个公开 GitHub 版本均可追溯到唯一审批记录及相同 artifact SHA-256。

当持续负载、恢复目标或协作人数超过个人 MVP 边界时，应迁移到独立托管 PostgreSQL、至少两个 API 副本和独立任务队列，不应在单机 Compose 上叠加隐性高可用承诺。
