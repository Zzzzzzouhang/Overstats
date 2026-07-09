from __future__ import annotations

from dataclasses import dataclass


@dataclass
class CourtQuery:
    """电竞法庭查询请求（字段与 overstats DashenMatchQuery 对齐）。

    - bnet_id: 玩家战网 ID（如 Player#12345）；与 customer_token 二选一
    - customer_token: 已是解析后的大神 token，优先级高于 bnet_id
    - index: 对局索引（0-based，0 = 最近一局）
    """

    bnet_id: str = ""
    customer_token: str = ""
    index: int = 0
    use_db: bool = False
