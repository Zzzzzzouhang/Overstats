from __future__ import annotations

from dataclasses import dataclass

# 与 overstats 其它模块（DashenMatchQuery / DashenProfileQuery 等）完全一致：
# bnet_id 与 customer_token 二选一，解析完全交给 dashen_match.query_match_list
# 内部的 _resolve_query（customer_token 优先，否则用 bnet_search 解析 bnet_id）。
# 本 dataclass 不做任何 bnet_id 解析/归一化，保持与项目其它 Query 同构。
@dataclass
class ShiquQuery:
    """是区吗查询请求（字段与 overstats DashenMatchQuery 对齐）。

    - bnet_id: 玩家战网 ID（如 Player#12345）；与 customer_token 二选一
    - customer_token: 已是解析后的大神 token，优先级高于 bnet_id
    - match_count: 抓取对局数（默认 12，范围 2~25）

    bnet_id → customer_token 的解析交由 overstats 的
    DashenMatchModule.query_match_list → _resolve_query 完成，
    本类不自行解析。
    """

    bnet_id: str = ""
    customer_token: str = ""
    match_count: int = 12
    use_db: bool = False
