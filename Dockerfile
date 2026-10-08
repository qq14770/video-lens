# =============================================================================
#  Video Lens —— 视频内容提取服务（Docker 镜像）
#  ---------------------------------------------------------------------------
#  设计要点：
#    1. **ffmpeg 内置**。m3u8 必须服务端合并成 MP4 才能下载，缺 ffmpeg 会退化成
#       "下载一份打不开的清单"。所以宁可镜像大 ~80MB 也要装进去，
#       否则在 NAS 上用户很难自己补装。
#    2. **非 root 运行**。容器里不需要任何特权；NAS 上的挂载目录若被 root 写脏，
#       宿主机上会很难处理。
#    3. **多阶段构建**。编译期依赖（gcc）只留在 builder 层，
#       运行层保持精简（NAS 上镜像拉取速度比本机慢得多）。
#    4. **健康检查走 /api/health**。飞牛的容器面板会读它来判断服务是否真的可用，
#       而不是只看"进程还活着"。
#    5. **状态与临时产物落在 /data**。这是唯一需要挂载卷的目录，
#       定时计划（state.json）与合并产物都在这里，容器重建也不丢。
# =============================================================================

# ---------- 构建阶段：装编译期依赖，预编译 wheel ----------
FROM python:3.12-slim-bookworm AS builder

# pip 索引源与超时参数（可在构建时覆盖）：
#
#   docker compose build --build-arg PIP_INDEX_URL=https://pypi.org/simple
#
# 为什么要可配：**国内直连 PyPI 下载 yt-dlp 这类几十 MB 的包经常超时**，
# 表现为构建失败在 `pip install` 这一步，报 ReadTimeoutError。
# 这与代码无关，纯粹是网络问题 —— 但足以让「改了代码却看不到变化」
# （构建没成功，compose 复用了旧镜像）。
#
# 默认用清华 TUNA 镜像：国内速度快且稳定。要用官方源就传上面那个 build-arg。
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ARG PIP_TIMEOUT=60
ARG PIP_RETRIES=5

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_INDEX_URL=${PIP_INDEX_URL} \
    PIP_TIMEOUT=${PIP_TIMEOUT} \
    PIP_RETRIES=${PIP_RETRIES}

# curl_cffi 之类的可选依赖需要编译工具；没有它们时这段也是无害的。
# apt 源同样换国内镜像 —— 飞牛上拉 deb.debian.org 也常很慢。
RUN set -eux; \
    if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
        sed -i 's|deb.debian.org|mirrors.tuna.tsinghua.edu.cn|g' \
            /etc/apt/sources.list.d/debian.sources; \
    fi; \
    if [ -f /etc/apt/sources.list ]; then \
        sed -i 's|deb.debian.org|mirrors.tuna.tsinghua.edu.cn|g' /etc/apt/sources.list; \
    fi; \
    apt-get update && apt-get install -y --no-install-recommends \
        gcc \
        python3-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY backend/requirements.txt ./requirements.txt
# 装到独立前缀，方便整包拷进运行层
RUN pip install --prefix=/install -r requirements.txt

# ---------- 运行阶段 ----------
FROM python:3.12-slim-bookworm AS runtime

# ffmpeg：HLS 合并的核心依赖（见上面第 1 条）
# ca-certificates：yt-dlp 访问 https 站点必需，缺失会报证书错误
# 同样换国内 apt 镜像（ffmpeg 有几十 MB，走官方源在 NAS 上会非常慢）
RUN set -eux; \
    if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
        sed -i 's|deb.debian.org|mirrors.tuna.tsinghua.edu.cn|g' \
            /etc/apt/sources.list.d/debian.sources; \
    fi; \
    if [ -f /etc/apt/sources.list ]; then \
        sed -i 's|deb.debian.org|mirrors.tuna.tsinghua.edu.cn|g' /etc/apt/sources.list; \
    fi; \
    apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    VX_HOST=0.0.0.0 \
    VX_PORT=8300 \
    VX_STORE_FILE=/data/state.json \
    VX_REMUX_TEMP_DIR=/data/tmp \
    PATH="/install/bin:$PATH"

COPY --from=builder /install /install

# 让 Python 认识 /install 里的包。详见 docker/link_install.py 的 docstring：
# 用 .pth 按运行时版本动态指向，而不是把 python3.12 写死在 PYTHONPATH 里
# （后者在基础镜像升级时会静默失效）。
COPY docker/link_install.py /tmp/link_install.py
RUN python /tmp/link_install.py && rm /tmp/link_install.py

# 建一个 uid/gid 1000 的用户，**同时保留 root 作为默认运行身份**。
#
# 为什么要两个都准备：
#   * NAS 上最省事的做法是用 root —— 彻底绕开「宿主目录属主与容器 uid 不一致」
#     那一整类问题（Windows 拷过去的文件属主常是 123，容器内是 1000，
#     写 /data 就会 Permission denied）。compose 里默认就是这个模式。
#   * 但有些人更看重隔离（多人共用的 NAS），那时 `user: "1000:1000"` 就能切回非 root。
#     把这个用户预先建好，两种模式才能用同一个镜像。
#
# 先探测再创建：基础镜像将来若预置了 uid 1000，groupadd/useradd 会失败并让构建中止。
RUN set -eux; \
    if ! getent group 1000 >/dev/null; then groupadd -g 1000 videolens; fi; \
    if ! getent passwd 1000 >/dev/null; then \
        useradd -u 1000 -g 1000 -M -s /usr/sbin/nologin videolens; \
    fi; \
    mkdir -p /data/tmp; \
    chmod 1777 /data /data/tmp

WORKDIR /app
# 只拷运行所需：backend（服务）+ frontend（静态托管）+ docker（容器辅助脚本）
COPY backend/ /app/backend/
COPY frontend/ /app/frontend/
COPY docker/ /app/docker/

# 显式修权限，**不依赖源文件的权限位**。
#
# 为什么必须这么做（真踩过，现象如下）：
#   项目从 Windows 打包成 zip 传到 NAS 解压，而 zip **不保存 Unix 权限位**，
#   解压出来的文件权限位可能全空。实测飞牛上的结果就是：
#
#       ---------- 1 1000 1000  1709 run.py      ← 权限位 000
#
#   COPY 会**原样继承**这份权限，而 `--chown` 只改属主、不改权限位 ——
#   于是属主是 1000、进程也是 1000，却连自己都读不了：
#
#       python: can't open file '/app/backend/run.py': [Errno 13] Permission denied
#
#   注意是 EACCES 而不是 ENOENT —— 这个区别很关键：文件在，只是没权限。
#   排查时看到 Errno 13 就该往权限位查，而不是怀疑路径写错。
#
# 所以：目录必须 +x（否则无法进入/遍历），文件必须 +r。
# 属主给 1000，这样 root 与非 root 两种模式都能读。
RUN set -eux; \
    chown -R 1000:1000 /app; \
    find /app -type d -exec chmod 755 {} +; \
    find /app -type f -exec chmod 644 {} +

# 默认**不切 USER**，即以 root 运行（NAS 上最省事，见上面注释）。
# 要改用非 root：在 compose 里写 user: "1000:1000"，或取消下面这行的注释。
# USER 1000:1000

# 声明数据卷：定时计划 + 合并/打包产物。挂载它，容器重建也不丢计划。
#
# 说明：镜像内对 /data 的 chown 只在"匿名卷"场景生效。
# compose 里用**宿主目录**挂载时，挂载会覆盖镜像内的属主 ——
# 所以宿主目录仍需自行 chown 1000:1000（部署文档里写了，服务启动时也会自检并提示）。
VOLUME ["/data"]

EXPOSE 8300

# 健康检查打真实接口：它同时会验证 ffmpeg 能力位，比"端口通"有意义。
# 逻辑放在独立文件里 —— 内联的多行 python -c 要叠三层转义（Dockerfile 续行、
# shell 引号、Python 语句），任何一层出错都会变成"容器永远 unhealthy"的难查故障。
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "/app/docker/healthcheck.py"]

# 启动：先跑自检脚本，再交给应用。
#
# 为什么用 sh -c 串联：容器进入 Restarting 循环时，用户通常只能看到
# "Restarting (1) 2 seconds ago" 这一行，无从下手。自检脚本会先把运行环境
# （依赖能否导入 / ffmpeg / 静态资源 / 目录可写）逐项打印出来，
# 让日志第一屏就说明问题。用 exec 保证 Python 成为 PID 1、信号直达。
ENTRYPOINT ["sh", "-c", "python /app/docker/preflight.py && exec python backend/run.py"]
