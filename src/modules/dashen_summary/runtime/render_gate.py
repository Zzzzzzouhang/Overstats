"""渲染并发闸门（兼容层）。

实现已上移到 src/modules/render_base.py（全局共享的渲染/AI 二段闸门，
容量 = 抓取槽位 × 3）。本文件仅保留转发，供既有
`from .render_gate import get_global_render_limit, get_global_render_semaphore`
的调用方使用。
"""

try:
    from overstats.src.modules.render_base import (
        get_global_render_limit,
        get_global_render_semaphore,
    )
except ModuleNotFoundError:  # 直接以 src.* 路径运行
    from src.modules.render_base import (  # type: ignore[no-redef]
        get_global_render_limit,
        get_global_render_semaphore,
    )

__all__ = ["get_global_render_limit", "get_global_render_semaphore"]
