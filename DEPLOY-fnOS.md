# 部署到飞牛 OS（fnOS）

本文针对飞牛 OS 的实际情况写，不是通用 Docker 教程。飞牛底层是 Debian，
自带 Docker 与 Compose，所以用容器部署最省事：**Python、ffmpeg、依赖全部封装在镜像里**，
不用碰系统环境，也不会因为 fnOS 升级而失效。

---

## 一、为什么在飞牛上推荐 Docker

| 顾虑 | 容器方案如何解决 |
|------|-----------------|
| fnOS 的系统 Python 版本受系统管理，动它有风险 | 镜像自带 Python 3.12，与宿主完全隔离 |
| HLS 合并需要 ffmpeg，飞牛上手动装不方便 | 镜像内置 ffmpeg，开箱即用 |
| NAS 常年开机，服务要能自启/自愈 | `restart: unless-stopped` + 健康检查 |
| 定时任务状态不能因为升级镜像就丢 | `state.json` 落在 `/data` 卷，重建容器也保留 |
| NAS 磁盘空间宝贵 | 多阶段构建，运行层不含编译工具；日志限 10MB×3 |

---

## 二、准备工作

### 1. 确认 Docker 可用

飞牛的「Docker」应用里能进容器列表就说明可用。或者 ssh 进来执行：

```bash
docker --version
docker compose version
```

飞牛默认管理员账号是 `1000`，ssh 登录后请用 `sudo` 执行 docker 命令。

### 2. 建数据目录

**compose 默认以 root 运行容器**，所以这一步**不需要 chown** —— 建个目录就行：

```bash
# 路径按自己的存储空间调整（可能是 /vol1/1000、/vol2/1000 …）
mkdir -p /vol1/1000/video-lens-data
```

就这么多。若想改成**非 root 运行**（更安全，但要处理属主），见本节末尾。

> **为什么默认 root**：NAS 上这是务实选择 —— 彻底绕开「宿主目录属主与容器
> uid 不一致」这一整类问题（Windows 拷过去的文件属主常是 `123`，
> 容器内是 `1000`，写 `/data` 就会 `Permission denied`）。
>
> **代价**：容器若被攻破，攻击者拿到的是 **NAS 的 root**（不只是容器内）。
> 对「局域网自用 + 不暴露公网 + 只解析自己粘的链接」这个场景，风险可控。
> compose 里已加 `no-new-privileges:true`，即使是 root 也去掉了提权路径。

<details>
<summary>想改用非 root 运行？点开看步骤</summary>

```bash
# 1) compose 里取消注释 user: "1000:1000"（约在第 40 行）
#    并确保 volumes 里的数据目录属主是 1000:1000

# 2) 建目录 + 设属主 + 实测写入（一条命令）
cd /vol1/1000/docker/video-lens
sudo bash scripts/prepare-data-dir.sh

# 3) 重启
sudo docker compose up -d
```

`prepare-data-dir.sh` 会自动从 compose 里读出数据目录路径、创建它、
`chown 1000:1000`，并**以 uid 1000 的身份实测能否写入** ——
最后那步很关键，只 chown 不验证的话，权限仍可能因别的原因不对。

</details>

### 2b. 传源码时注意权限位（Windows 用户必看）

**从 Windows 用 zip / SMB / 文件管理上传，会丢掉 Unix 权限位**，
文件变成 `----------`（000），目录变成 `d---------`。这会导致两个症状：

| 症状 | 原因 |
|------|------|
| `docker compose build` 报 `no configuration file provided: not found` | compose 读不到 `docker-compose.yml`（文件权限全空） |
| 容器报 `[Errno 13] Permission denied` | 读不到 `backend/run.py` |

**一条命令修好**（在项目根目录，即含 `docker-compose.yml` 的那一层执行）：

```bash
sudo bash scripts/fix-perms.sh
```

它会：规范化权限（目录 755 / 文件 644）→ 把属主设为 1000:1000 →
打印修复前后对比并逐个验证关键文件可读。它会**先确认自己在项目根**才动手，
定位失败就直接退出，不会在随便某个目录递归改权限。

想手动做也行：

```bash
cd /vol1/1000/video-lens
sudo find . -type d -exec chmod 755 {} +
sudo find . -type f -exec chmod 644 {} +
```

> **怎么确认自己中招了**：`ls -la` 看第一列。正常的目录是 `drwxr-xr-x`、
> 文件是 `-rw-r--r--`；如果是 `d---------` / `----------`，就是权限位丢了。
>
> **根治办法**：别用 zip —— `git clone` 与 `scp -r` 都会保留权限位。

### 3. 生成令牌密钥

```bash
openssl rand -hex 32
```

复制输出，下一步要用。**不设置的话每次重启都会随机生成，导致所有播放/下载链接失效。**

---

## 三、部署

### 方式 A：飞牛图形界面（推荐新手）

1. 把整个项目文件夹传到飞牛（「文件管理」里上传 zip 后解压，或用 ssh `git clone`）。
2. 打开飞牛的 **Docker** 应用 → **Compose** → **新建项目**。
3. 项目名填 `video-lens`；「路径」选到项目目录（含 `docker-compose.yml` 的那一层）。
4. 把 `docker-compose.yml` 内容粘进去，**改两处**：
   - `VX_SECRET:` 换成第 3 步生成的串；
   - `volumes` 左边的路径换成你实际的，例如 `/vol1/1000/video-lens-data:/data`。
5. 点「构建并启动」。首次会拉取基础镜像并编译，视网络情况需要 3~10 分钟。

### 方式 B：ssh 命令行

```bash
cd /vol1/1000/video-lens        # 换成你的项目目录

# 先改配置
sudo nano docker-compose.yml   # 填 VX_SECRET、核对挂载路径

# 构建并启动
sudo docker compose up -d --build

# 看日志（首次启动会打印 ffmpeg 能力位与目录自检结果）
sudo docker compose logs -f
```

看到这两行就说明正常：

```
[Video Lens] 服务正常
video-lens  | [Video Lens] http://0.0.0.0:8300  （API 文档：/api/docs）
```

---

## 四、访问

在浏览器打开：

```
http://<你的飞牛IP>:8300
```

例如 `http://192.168.1.100:8300`。API 文档在 `/api/docs`。

想换个端口（比如 8300 被飞牛其他服务占了），改 compose 里的 `ports` 左边那个数字：

```yaml
ports:
  - "18300:8300"     # 右边别改，那是容器内的端口
```

### 部署后想改 VX_SECRET 或 挂载路径？（容器已经创建的情况）

`VX_SECRET`（环境变量）和 `volumes`（目录挂载）都是**容器创建那一刻固定进去**的。
**只改 `docker-compose.yml` 文件不会生效，必须重建容器**。两种做法任选其一：

**做法 A —— SSH 进 NAS（最干净，推荐）：**

```bash
cd /vol1/1000/docker/video-lens      # 换成你的项目目录
sudo nano docker-compose.yml          # 改 VX_SECRET 那行 / volumes 左边路径
sudo docker compose up -d --force-recreate   # 关键：强制用新配置重建容器
sudo docker compose logs -f           # 看是否出现「[Video Lens] 服务正常」
```

**做法 B —— 飞牛 Docker 图形界面：**

1. 打开「Docker → Compose（或"项目"）」，找到 `video-lens`。
2. 点「编辑 / 修改配置」，在编辑框里把 `VX_SECRET:` 改成新串、把 `volumes` 左边路径改好。
3. 点「更新 / 重新部署」——它用新配置重建容器，等同于做法 A 的 `--force-recreate`。

> **验证新值确实进去了**：`sudo docker exec video-lens env | grep VX_SECRET`
> （能看到你填的串即生效；注意这行会回显密钥，事后清一下终端历史即可。）

**两个副作用必须知道：**

- **改 VX_SECRET → 旧的播放/下载直链全部失效**（签名校验不过）。这是预期行为，重新生成直链即可；新链接用新密钥。
- **改 volumes 左边路径 → 旧目录里的 `state.json`（你的定时计划）不会自动迁移**。
  - 想保留数据：先把旧目录 `state.json` 拷到新目录，或保持原路径不动。
  - 想换路径：先用 `sudo bash scripts/prepare-data-dir.sh <新路径>` 建好目录并验证可写，再改 compose 重建。
  - 注意：数据在**宿主目录**不在容器里，所以重建容器本身不会丢数据，丢数据只发生在你**改了左边路径**且没迁移时。

---

## 五、部署后自检

打开页面后，确认这四件事：

| 检查项 | 怎么看 |
|--------|--------|
| 服务在跑 | 顶栏右侧显示「服务正常 · v1.0.0」 |
| ffmpeg 可用 | 浏览器访问 `http://<IP>:8300/api/health`，`"ffmpeg_available": true` |
| 提取可用 | 粘一条普通视频链接，能出标题+播放器 |
| HLS 合并可用 | 粘一个 m3u8，点「下载 MP4」应看到进度条爬升 |

命令行自检（等价于上述）：

```bash
curl -s http://127.0.0.1:8300/api/health | python3 -m json.tool
```

---

## 六、飞牛上的常见问题

### `docker compose build` 报 `no configuration file provided: not found`

`docker compose` 只认这四种文件名（区分大小写）：
`compose.yaml` / `compose.yml` / `docker-compose.yaml` / `docker-compose.yml`。

按可能性排序的原因：

```bash
# ① 当前目录不对（最常见：cd 到了别的路径，或解压多套了一层目录）
pwd
ls -la

# ② 找一下文件到底在哪
find /vol1 -maxdepth 4 -name "docker-compose.yml" 2>/dev/null

# ③ 文件在，但权限位全空导致读不到（见下一节）
ls -la docker-compose.yml
```

确认后在**含 `docker-compose.yml` 的那一层**执行构建。

### 文件权限位全空（`----------` / `d---------`）

从 Windows 拷过来的典型症状。一条命令修好：

```bash
cd <项目根>          # 含 docker-compose.yml 的那一层
sudo bash scripts/fix-perms.sh
```

细节见第二节的 2b。

### 容器反复重启，日志报 `[Errno 13] Permission denied`

```
video-lens | python: can't open file '/app/backend/run.py': [Errno 13] Permission denied
video-lens exited with code 2 (restarting)
```

**先判断你现在跑的是不是旧镜像**——两个一眼可辨的特征，中一个就说明该重建：

| 症状 | 含义 |
|------|------|
| 日志里**没有** `[preflight] ---- 容器启动自检 ----` 这一行 | 你的镜像太旧（旧 Dockerfile 还没有 preflight 自检） |
| 报错直接落在 `run.py` 上（而不是先报 `preflight.py`） | 同上：旧的 ENTRYPOINT 直接 `python backend/run.py` |

只要用**当前版本**的 Dockerfile 重新构建就不会再出现 —— 镜像里已用显式
`chmod` 把权限位规范化（不依赖源文件权限位），并在启动时先跑 `preflight.py` 自检。

```bash
cd <项目根>                      # 含 docker-compose.yml 的那一层
sudo bash scripts/check-deploy.sh        # 一把排雷：文件是否齐全 / Dockerfile 是否新版 / 镜像新旧

# 关键：必须重新 build，并且带 --no-cache 与 --force-recreate
sudo docker compose build --no-cache
sudo docker compose up -d --force-recreate
sudo docker compose logs -f
```

也可以先确认 NAS 上的 Dockerfile 确实是新版（两行都应打印出来）：

```bash
grep -n "preflight.py" Dockerfile
grep -n "chmod 644" Dockerfile
```

若两条都为空 ⇒ **NAS 上的 Dockerfile 是旧的**，先把最新的 `Dockerfile`、
`docker/`、`backend/`、`frontend/` 同步上去，再执行上面的重建命令。

> **为什么必须带 `--no-cache` 与 `--force-recreate`**：
> `up -d` 未必重建镜像，`--no-cache` 绕过构建缓存让 Dockerfile 的 chmod 真的生效；
> `--force-recreate` 才会丢弃仍在用旧镜像的那个容器。
> 重建成功的标志：日志第一行是 `[preflight] ---- 容器启动自检 ----`。

> **排查要诀**：错误码是 **EACCES（Errno 13）而不是 ENOENT**。
> 看到 Errno 13 就往权限位查；看到 "No such file" 才是路径问题。
> 根因见第二节 2b（zip/SMB 不保留 Unix 权限位）。

### 启动日志报「目录不可写」

```
ERROR 目录不可写（定时计划与批次状态）：/data —— Permission denied
  → Docker/飞牛：确认挂载的宿主目录属主与容器内 uid 一致（容器内是 1000）
```

这与上面的权限问题**不是一回事**：这是**宿主挂载目录**的属主问题。
`fix-perms.sh` 会一并处理；也可手动：

```bash
sudo chown -R 1000:1000 /vol1/1000/video-lens
sudo docker compose restart
```

### 容器一直重启，但日志是空的

用自检脚本直接把环境打出来。镜像的 ENTRYPOINT 已内置自检，
正常会看到这样一屏：

```
[preflight] ---- 容器启动自检 ----
[preflight] python 3.12.x @ /usr/local/bin/python
[preflight] OK   import fastapi  0.11x
[preflight] OK   import yt_dlp
[preflight] OK   ffmpeg  /usr/bin/ffmpeg
[preflight] OK   前端静态目录  /app/frontend
[preflight] OK   状态目录可写  /data
[preflight] ---- 自检结束，交给应用 ----
```

若某项 FAIL，它同时会打印**具体怎么修**。也可以手动跑自检：

```bash
sudo docker run --rm --entrypoint python video-video-lens /app/docker/preflight.py
```

### 端口被占用

```bash
# 看谁占了 8300
sudo ss -tlnp | grep 8300
```

换成别的宿主端口即可（容器内保持 8300）。

### 项目别放在飞牛的 docker 目录下

`/vol1/1000/docker/` 是飞牛 Docker 应用自己的管理目录。
把项目放进去，将来飞牛升级或清理时可能被误伤。建议放到普通数据目录：

```bash
sudo mv /vol1/1000/docker/video-lens /vol1/1000/video-lens
cd /vol1/1000/video-lens
sudo bash scripts/fix-perms.sh
sudo docker compose up -d --build
```

### 想让容器抓内网地址（比如 NAS 上另一个媒体服务的 m3u8）

默认禁止访问内网（防 SSRF，这是正确的默认值）。确认只在纯内网使用时，
可在 compose 里加：

```yaml
environment:
  VX_BLOCK_PRIVATE_NET: "false"
```

**不要**在会暴露到公网的环境里这么设。

### 下载大文件时 NAS 卡顿

NAS 的 CPU/IO 通常比 PC 弱，建议在 compose 里收紧并发：

```yaml
environment:
  VX_REMUX_CONCURRENCY: "1"      # 同时只合并一个
  VX_BATCH_CONCURRENCY: "1"      # 批量也只跑一条
```

### 想看容器里的临时产物占了多大

合并产物与打包的 zip 都在 `/data/tmp`，超过任务 TTL（默认 1 小时）会被自动回收。
手动看一眼：

```bash
sudo du -sh /vol1/1000/video-lens-data/tmp
```

### 升级到新版本

```bash
cd /vol1/1000/video-lens
sudo docker compose down
sudo docker compose up -d --build
```

`state.json` 在卷里，**定时计划不会丢**。

---

## 七、关于安全（重要）

本项目定位是**个人自用**。部署到局域网没问题，但请注意：

- **不要直接暴露到公网**。它没有登录鉴权，等于给外人一个"任意 URL 转发器"，
  可能被用来探测你的内网。确有公网需求的话，请套一层带认证的反向代理
  （飞牛上可用 Nginx Proxy Manager 之类），并启用 HTTPS。
- **SSRF 防护默认开启**（`VX_BLOCK_PRIVATE_NET=true`），别随手关掉。
- **限制提取频率**：`VX_RATE_LIMIT_RPM` 默认 30/分钟，多人共用时按需调整，
  别设得过大。
- **遵守站点条款与版权**：页脚那句合规提示不是装饰。

---

## 八、不用 Docker 的话（原生部署）

不推荐，但飞牛既然有 Debian 底子，也是可行的：

```bash
# 1. 系统依赖
sudo apt update
sudo apt install -y python3 python3-venv python3-pip ffmpeg

# 2. 建虚拟环境（别污染系统 Python）
cd /vol1/1000/video-lens
python3 -m venv .venv
.venv/bin/pip install -r backend/requirements.txt

# 3. 先手动跑一次确认没问题
VX_HOST=0.0.0.0 VX_PORT=8300 \
VX_SECRET=$(openssl rand -hex 32) \
.venv/bin/python backend/run.py
```

确认可用后做成 systemd 服务 `/etc/systemd/system/video-lens.service`：

```ini
[Unit]
Description=Video Lens 视频内容提取
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=1000
Group=1000
WorkingDirectory=/vol1/1000/video-lens
Environment=VX_HOST=0.0.0.0
Environment=VX_PORT=8300
Environment=VX_SECRET=换成你的固定密钥
Environment=VX_STORE_FILE=/vol1/1000/video-lens/state.json
Environment=VX_REMUX_TEMP_DIR=/vol1/1000/video-lens/tmp
ExecStart=/vol1/1000/video-lens/.venv/bin/python backend/run.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now video-lens
sudo systemctl status video-lens
```

**原生部署的两个额外注意点**：

1. **yt-dlp 要定期升级**，站点改版后旧版会解析失败：
   `.venv/bin/pip install -U yt-dlp && sudo systemctl restart video-lens`
2. **fnOS 系统升级可能重置 apt 源或 Python**，虚拟环境法受影响较小，
   但 ffmpeg 可能需要在升级后重装。
