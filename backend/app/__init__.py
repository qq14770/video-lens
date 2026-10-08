"""Video Lens —— 聚焦视频本身的提取服务。

模块划分（单向依赖，便于替换与测试）：

    config        运行期配置（全部来自环境变量，带安全默认值）
    security      输入校验：URL 白名单/SSRF 防护 + HMAC 令牌签发
    providers     站点识别（用于 UI 展示「来源」标签）
    extractor     yt-dlp 调用 + 广告/冗余过滤 + 播放源择优
    stream_proxy  直链反向代理（Range 透传、HLS 清单改写、签名令牌）
    jobs          异步解析任务表（前端轮询进度，避免长请求超时）
    main          FastAPI 装配层（路由 / 中间件 / 静态托管）
"""

__version__ = "1.0.0"
__all__ = ["__version__"]
