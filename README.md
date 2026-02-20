# EasyTransfer

基于 TUS 协议的高性能文件传输工具，支持断点续传、切片上传下载、多 IP 负载均衡、存储配额管理和灵活的文件缓存策略。

## 特性

- **TUS 协议** — 基于开放标准的断点续传上传协议，可靠传输大文件
- **切片传输** — 大文件自动切片上传/下载，支持部分下载（文件未上传完成也可下载已有部分）
- **断点续传** — 网络中断、客户端崩溃后自动从上次位置恢复传输
- **多 IP 负载均衡** — 服务端广播多个 IP，客户端自动探测连通性和流量负载，选择最优节点
- **流量监控** — 实时监控各网卡上传/下载速率和负载百分比
- **存储配额** — 服务端可设置最大存储限额，超限时客户端自动轮询等待并恢复上传
- **文件缓存策略** — 支持永久保存、阅后即焚（下载后自动删除）、定时过期（TTL）三种策略，可按 Token 鉴权设置不同默认策略
- **多状态后端** — 支持 memory（测试）、file（默认）、redis（多 Worker 生产环境）
- **OIDC 用户系统** — 集成任意 OIDC 提供者（Casdoor、Keycloak、Auth0 等）登录，基于角色/群组的配额和速度限制
- **双数据库后端** — 用户数据库支持 SQLite（默认）和 MySQL
- **Token 鉴权** — 基于 API Key 的认证机制 + OIDC Session Token
- **客户端** — CLI（Rich 美化进度条 + login/whoami/logout）+ GUI（Tkinter）
- **pip 安装** — `pip install etransfer` 一键安装

## 项目结构

```
etransfer/
├── client/                     # 客户端
│   ├── cli.py                  # CLI 命令行工具 (Typer + Rich)
│   ├── gui.py                  # GUI 图形界面 (Tkinter/ttkbootstrap)
│   ├── tus_client.py           # TUS 上传客户端（继承 tus-py-client）
│   ├── downloader.py           # 切片下载器
│   ├── cache.py                # 本地切片缓存
│   └── server_info.py          # 服务端信息查询
├── server/                     # 服务端
│   ├── main.py                 # FastAPI 入口
│   ├── config.py               # 服务端配置（pydantic-settings）
│   ├── tus/                    # TUS 协议实现
│   │   ├── handler.py          # TUS 协议路由（POST/PATCH/HEAD/DELETE/OPTIONS）
│   │   ├── storage.py          # 文件存储 + 状态管理
│   │   └── models.py           # TUS 数据模型 + 缓存策略枚举
│   ├── routes/                 # API 路由
│   │   ├── files.py            # 文件管理（列表/详情/下载/删除/清理）
│   │   ├── info.py             # 服务端信息（健康检查/端点/流量/存储状态）
│   │   └── auth.py             # 鉴权验证
│   ├── auth/                   # 用户系统（OIDC）
│   │   ├── oauth.py            # OIDC 客户端（自动发现端点）
│   │   ├── db.py               # 用户数据库（SQLModel + SQLAlchemy async，支持 SQLite/MySQL/PostgreSQL）
│   │   ├── models.py           # User/Group/Session 模型
│   │   └── routes.py           # 用户 API 路由（login/callback/me/groups）
│   ├── middleware/
│   │   └── auth.py             # Token + Session 认证中间件
│   └── services/               # 后台服务
│       ├── state.py            # 状态管理器
│       ├── traffic.py          # 流量监控（psutil）
│       ├── ip_mgr.py           # 网卡/IP 管理
│       └── backends/           # 可插拔状态后端
│           ├── interface.py    # 抽象接口
│           ├── memory.py       # 内存后端
│           ├── file.py         # 文件后端（默认）
│           └── redis.py        # Redis 后端
├── common/                     # 公共模块
│   ├── constants.py            # 常量定义
│   ├── config.py               # 配置管理
│   └── models.py               # Pydantic 数据模型
scripts/                        # 测试与部署脚本
tests/                          # 单元测试
config/                         # 部署配置（git-ignored，密钥安全）
├── README.md                   # 配置流程说明（已跟踪）
└── config.yaml                 # 实际部署配置（不提交）
config.example.yaml             # 配置模板（已跟踪，安全提交）
```

## 快速开始

### 安装

```bash
pip install -e .

# 可选：Redis 后端支持
pip install -e ".[redis]"

# 可选：MySQL 用户数据库
pip install -e ".[mysql]"

# 可选：开发依赖
pip install -e ".[dev]"
```

### 启动服务端

配置文件会自动发现（`./config.yaml` → `./config/config.yaml` → `~/.etransfer/server.yaml`），也可通过 `$ETRANSFER_CONFIG` 环境变量或 `--config` 指定。

```bash
# 方式一：CLI（自动发现 config/config.yaml）
et server start

# 方式二：显式指定配置
et server start --config /path/to/config.yaml

# 方式三：uvicorn 直接启动（同样自动发现配置）
uvicorn etransfer.server.main:app --host 0.0.0.0 --port 8765

# 方式四：环境变量指定配置文件
ETRANSFER_CONFIG=/path/to/config.yaml uvicorn etransfer.server.main:app
```

### 客户端配置（只需一次）

```bash
# 配置服务器地址（域名+端口 或 IP+端口）
etransfer setup 192.168.1.100:8765

# 如果服务器要求登录（OIDC），执行登录
etransfer login
# CLI 会输出一个 URL，手动在浏览器中打开完成认证

# 查看当前配置和登录状态
etransfer status
```

### 上传文件

```bash
# CLI 上传（自动使用已配置的服务器和缓存的 token）
etransfer upload ./myfile.zip

# 指定缓存策略
etransfer upload ./secret.pdf --retention download_once

# 指定 TTL（1小时后过期）
etransfer upload ./temp.dat --retention ttl --retention-ttl 3600

# 临时使用其他 token（覆盖缓存）
etransfer upload ./myfile.zip --token other-api-token
```

### 下载文件

```bash
etransfer download <file_id> -o ./downloads/
```

### 查看文件列表

```bash
etransfer list
```

### 查看服务器信息

```bash
etransfer info
```

### 启动 GUI

```bash
etransfer gui
```

## Python API

### 上传

```python
from etransfer.client.tus_client import EasyTransferClient

client = EasyTransferClient(
    server_url="http://localhost:8765",
    token="my-secret-token",
    chunk_size=4 * 1024 * 1024,  # 4MB
)

# 基本上传
uploader = client.create_uploader("./largefile.zip")
uploader.upload()

# 阅后即焚上传
uploader = client.create_uploader(
    "./secret.pdf",
    retention="download_once",
)
uploader.upload()

# TTL 上传（1 小时后过期）
uploader = client.create_uploader(
    "./temp.dat",
    retention="ttl",
    retention_ttl=3600,
)
uploader.upload()

# 带进度回调和配额等待
uploader = client.create_uploader(
    "./huge.bin",
    progress_callback=lambda uploaded, total: print(f"{uploaded}/{total}"),
)
uploader.upload(wait_on_quota=True, poll_interval=5)

# 获取文件 ID
file_id = uploader.url.split("/")[-1]
```

### 下载

```python
from etransfer.client.downloader import ChunkDownloader

downloader = ChunkDownloader("http://localhost:8765", token="my-secret-token")

# 获取文件信息
info = downloader.get_file_info(file_id)
print(f"文件名: {info.filename}, 大小: {info.size}")

# 下载文件
downloader.download_file(file_id, "./downloads/output.zip")
```

### 服务端信息

```python
client = EasyTransferClient("http://localhost:8765", token="my-secret-token")

# 服务器信息
info = client.get_server_info()

# 文件列表
files = client.list_files()

# 可用端点（含流量负载）
endpoints = client.get_endpoints()

# 存储状态
storage = client.get_storage_status()
```

## 服务端配置

### 环境变量

| 变量                                 | 说明                                | 默认值                     |
| ------------------------------------ | ----------------------------------- | -------------------------- |
| `ETRANSFER_HOST`                     | 绑定地址                            | `0.0.0.0`                  |
| `ETRANSFER_PORT`                     | 端口                                | `8765`                     |
| `ETRANSFER_STORAGE_PATH`             | 文件存储路径                        | `./storage`                |
| `ETRANSFER_STATE_BACKEND`            | 状态后端 (`memory`/`file`/`redis`)  | `file`                     |
| `ETRANSFER_REDIS_URL`                | Redis 地址                          | `redis://localhost:6379/0` |
| `ETRANSFER_AUTH_ENABLED`             | 启用鉴权                            | `true`                     |
| `ETRANSFER_AUTH_TOKENS`              | Token 列表（JSON 数组）             | `[]`                       |
| `ETRANSFER_MAX_UPLOAD_SIZE`          | 单文件大小限制                      | 不限                       |
| `ETRANSFER_MAX_STORAGE_SIZE`         | 总存储配额（支持 `100MB`/`1GB` 等） | 不限                       |
| `ETRANSFER_CHUNK_SIZE`               | 默认切片大小                        | `4194304` (4MB)            |
| `ETRANSFER_ADVERTISED_ENDPOINTS`     | 广播 IP 列表（JSON 数组或逗号分隔） | 自动检测                   |
| `ETRANSFER_DEFAULT_RETENTION`        | 全局默认缓存策略                    | `permanent`                |
| `ETRANSFER_DEFAULT_RETENTION_TTL`    | 全局默认 TTL（秒，仅 `ttl` 策略）   | 不限                       |
| `ETRANSFER_TOKEN_RETENTION_POLICIES` | 按 Token 设置缓存策略（JSON）       | `{}`                       |
| `ETRANSFER_CLEANUP_INTERVAL`         | 清理任务间隔（秒）                  | `3600`                     |
| `ETRANSFER_UPLOAD_EXPIRATION_HOURS`  | 未完成上传过期时间（小时）          | `24`                       |
| `ETRANSFER_CORS_ORIGINS`             | CORS 允许来源                       | `["*"]`                    |

### 配置文件

支持 YAML 配置文件，启动时**自动发现**（无需 `--config`）。项目提供了一个配置模板：

```bash
# 将模板复制到 config/ 文件夹（已 git-ignored，不会提交密钥）
cp config.example.yaml config/config.yaml

# 编辑配置
vim config/config.yaml

# 启动（自动发现 config/config.yaml）
et server start
```

> 详细的配置流程说明见 `config/README.md`。

### 配置热重载

部分配置项（`role_quotas`、`auth_tokens`、`max_storage_size`、`advertised_endpoints`、`retention` 相关）支持不重启即时生效：

```bash
# CLI 命令（推荐）
et server reload

# 或直接调用管理员 API
curl -X POST http://localhost:8765/api/admin/reload-config \
  -H "X-API-Token: your-token"
```

> **无法在线变更**：`host`/`port`/`workers`（uvicorn 绑定在启动时确定）、OIDC 配置、数据库后端等需重启。群组配额存在数据库中，通过 API 管理，天然即时生效。

也可开启自动监听：在 `config.yaml` 中设置 `server.config_watch: true`，服务端会定期检查配置文件变更并自动重载。

### 文件缓存策略

三种策略：

| 策略            | 说明                               | 适用场景                 |
| --------------- | ---------------------------------- | ------------------------ |
| `permanent`     | 永久保存，手动删除                 | 一般文件存储（默认）     |
| `download_once` | 首次完整下载后自动删除（阅后即焚） | 一次性分享、敏感文件传输 |
| `ttl`           | 上传完成后按设定时间自动过期清理   | 临时文件、限时分享       |

**优先级**：客户端显式指定 > Token 级策略 > 全局默认

Token 级策略配置示例：

```bash
export ETRANSFER_TOKEN_RETENTION_POLICIES='{
    "guest-token": {
        "default_retention": "download_once"
    },
    "temp-token": {
        "default_retention": "ttl",
        "default_ttl": 86400
    }
}'
```

下载响应头：

| Header                | 说明         |
| --------------------- | ------------ |
| `X-Retention-Policy`  | 当前策略     |
| `X-Retention-Expires` | TTL 到期时间 |
| `X-Retention-Warning` | 阅后即焚提醒 |
| `X-Download-Count`    | 下载次数     |

### 存储配额

设置服务端最大存储容量：

```bash
export ETRANSFER_MAX_STORAGE_SIZE=10GB
```

超限时：

- 服务端返回 `HTTP 507 Storage Quota Exceeded`
- 客户端自动轮询 `/api/storage` 等待空间释放
- 空间释放后自动从断点恢复上传

## API 端点

### TUS 协议

| 方法      | 路径             | 说明                                    |
| --------- | ---------------- | --------------------------------------- |
| `OPTIONS` | `/tus`           | 获取 TUS 服务端能力                     |
| `POST`    | `/tus`           | 创建上传（支持 `creation-with-upload`） |
| `HEAD`    | `/tus/{file_id}` | 获取上传进度                            |
| `PATCH`   | `/tus/{file_id}` | 上传切片                                |
| `DELETE`  | `/tus/{file_id}` | 终止上传                                |

### 文件管理

| 方法     | 路径                                 | 说明                   |
| -------- | ------------------------------------ | ---------------------- |
| `GET`    | `/api/files`                         | 文件列表（分页）       |
| `GET`    | `/api/files/{file_id}`               | 文件详情（含缓存策略） |
| `GET`    | `/api/files/{file_id}/download`      | 下载文件（支持 Range） |
| `GET`    | `/api/files/{file_id}/info/download` | 下载信息               |
| `DELETE` | `/api/files/{file_id}`               | 删除文件               |
| `POST`   | `/api/files/cleanup`                 | 手动触发清理过期文件   |

### 服务端信息

| 方法  | 路径             | 说明                       |
| ----- | ---------------- | -------------------------- |
| `GET` | `/api/health`    | 健康检查                   |
| `GET` | `/api/info`      | 服务器信息                 |
| `GET` | `/api/stats`     | 详细统计                   |
| `GET` | `/api/endpoints` | 可用端点列表（含流量负载） |
| `GET` | `/api/storage`   | 存储配额与使用状态         |
| `GET` | `/api/traffic`   | 实时流量数据               |

### 认证

| 方法   | 路径               | 说明           |
| ------ | ------------------ | -------------- |
| `POST` | `/api/auth/verify` | 验证 API Token |

所有需认证的请求在 Header 中携带 `X-API-Token: <token>` 或 `Authorization: Bearer <session_token>`。

### 用户系统（OIDC）

| 方法     | 路径                              | 说明                                |
| -------- | --------------------------------- | ----------------------------------- |
| `GET`    | `/api/users/login-info`           | 获取登录配置（客户端用）            |
| `POST`   | `/api/users/login/start`          | CLI 登录流程：返回 state 和授权 URL |
| `GET`    | `/api/users/login/poll/{state}`   | CLI 轮询登录结果                    |
| `GET`    | `/api/users/login`                | 浏览器直接跳转 OIDC 登录            |
| `GET`    | `/api/users/callback`             | OIDC 回调（code 换 token）          |
| `GET`    | `/api/users/me`                   | 当前用户信息 + 有效配额             |
| `GET`    | `/api/users/me/quota`             | 当前用户配额使用情况                |
| `POST`   | `/api/users/logout`               | 注销（失效 Session）                |
| `GET`    | `/api/users`                      | 用户列表（Admin）                   |
| `PUT`    | `/api/users/{id}/role`            | 设置角色（Admin）                   |
| `PUT`    | `/api/users/{id}/active`          | 启用/禁用用户（Admin）              |
| `GET`    | `/api/groups`                     | 群组列表                            |
| `POST`   | `/api/groups`                     | 创建群组 + 配额（Admin）            |
| `PUT`    | `/api/groups/{id}/quota`          | 更新群组配额（Admin）               |
| `DELETE` | `/api/groups/{id}`                | 删除群组（Admin）                   |
| `POST`   | `/api/groups/{gid}/members/{uid}` | 添加成员（Admin）                   |
| `DELETE` | `/api/groups/{gid}/members/{uid}` | 移除成员（Admin）                   |

**CLI 登录流程：**

```bash
# 1. 先配置服务器地址
etransfer setup your-server:8765

# 2. 登录（CLI 输出 URL，手动在浏览器打开）
etransfer login

# 3. 查看当前用户
etransfer whoami

# 4. 注销
etransfer logout
```

**配额优先级：** 群组配额（最大值）> 角色配额 > 全局默认。`None` 表示不限。

## 测试脚本

| 脚本                            | 说明                                            |
| ------------------------------- | ----------------------------------------------- |
| `scripts/test_multi_ip.py`      | 多 IP 负载均衡测试                              |
| `scripts/test_large_partial.py` | 大文件/部分上传下载测试                         |
| `scripts/test_quota_resume.py`  | 存储配额 + 自动恢复上传测试                     |
| `scripts/test_retention.py`     | 文件缓存策略测试（permanent/download_once/ttl） |
| `scripts/test_user_system.py`   | 用户系统测试（OIDC + 角色 + 群组 + 配额）       |
| `scripts/test_hot_reload.py`    | 配置自动发现 + 热重载集成测试                   |
| `scripts/run_client_demo.py`    | 客户端演示                                      |

运行测试：

```bash
# 单元测试（含配置/热重载 45 项）
python -m pytest tests/

# 缓存策略测试（自动启动服务端）
python scripts/test_retention.py

# 存储配额测试
python scripts/test_quota_resume.py

# 大文件测试
python scripts/test_large_partial.py

# 配置热重载测试
python scripts/test_hot_reload.py
```

## 技术栈

- **服务端**: FastAPI + Uvicorn + aiofiles + psutil
- **客户端**: tus-py-client + httpx + Typer + Rich + Tkinter
- **协议**: TUS 1.0.0（creation, creation-with-upload, termination, checksum, expiration）
- **认证**: 通用 OIDC（Casdoor、Keycloak、Auth0 等）+ API Token
- **用户数据库**: SQLModel + SQLAlchemy async（SQLite 默认 / MySQL / PostgreSQL）
- **状态管理**: 可插拔后端（Memory / File / Redis）
- **数据模型**: Pydantic v2 + pydantic-settings

## License

MIT
