from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

try:
    from overstats.src.modules.errors import ModuleError
    from overstats.src.modules.bnet_search import BnetSearchModule, BnetSearchResult, bnet_search_module
except ModuleNotFoundError:
    from src.modules.errors import ModuleError
    from src.modules.bnet_search import BnetSearchModule, BnetSearchResult, bnet_search_module

from .requests import DashenSummaryQuery, DashenSummaryRequests, decode_summary_image_base64

try:
    from overstats.src.modules.dashen_summary.engine import (
        fetch_summary_payload,
        render_summary_image_from_payload,
    )
except ModuleNotFoundError:
    from src.modules.dashen_summary.engine import (
        fetch_summary_payload,
        render_summary_image_from_payload,
    )


@dataclass(frozen=True)
class DashenSummaryOutput:
    scope: str
    title: str
    customer_token: str
    full_id: str
    bnet_id: str
    worker_url: str
    match_count: int
    all_match_count: int
    payload_kb: int
    timings: List[Dict[str, Any]]
    image_base64: str
    image_bytes: bytes
    image_media_type: str
    resolved_bnet: Optional[BnetSearchResult] = None


@dataclass(frozen=True)
class DashenSummaryFetched:
    """总结抓取阶段产物：渲染阶段只消费 payload，不再发起上游请求。"""

    query: DashenSummaryQuery
    resolved_bnet: Optional[BnetSearchResult]
    payload: Dict[str, Any]


class DashenSummaryModule:
    def __init__(self, search_module: Optional[BnetSearchModule] = None) -> None:
        self.requests = DashenSummaryRequests()
        self.search_module = search_module or bnet_search_module

    async def fetch_summary(self, query: DashenSummaryQuery) -> DashenSummaryFetched:
        """抓取阶段：解析 + 对局数据 + 详情 + 奖项 + stats。只含上游请求，可放入抓取槽位。"""
        query, resolved_bnet = await self._resolve_query(query)
        payload = await fetch_summary_payload(query)
        return DashenSummaryFetched(query=query, resolved_bnet=resolved_bnet, payload=payload)

    async def render_summary(self, fetched: DashenSummaryFetched) -> DashenSummaryOutput:
        """渲染阶段：PIL 渲染 + 编码，在事件循环上直接执行（与上游一致），不占用抓取槽位。"""
        payload = await render_summary_image_from_payload(fetched.payload)
        image_bytes, image_media_type = decode_summary_image_base64(payload.get("image_base64") or "")
        full_id = str(payload.get("full_id") or fetched.query.full_id or fetched.query.bnet_id).strip()
        bnet_id = str((fetched.resolved_bnet.bnet_id if fetched.resolved_bnet else fetched.query.bnet_id) or "").strip()
        return DashenSummaryOutput(
            scope=payload.get("scope") or fetched.query.scope,
            title=payload.get("title") or "",
            customer_token=fetched.query.customer_token,
            full_id=full_id,
            bnet_id=bnet_id,
            worker_url=payload.get("worker_url") or "local-module",
            match_count=int(payload.get("match_count") or 0),
            all_match_count=int(payload.get("all_match_count") or 0),
            payload_kb=int(payload.get("payload_kb") or 0),
            timings=list(payload.get("timings") or []),
            image_base64=payload.get("image_base64") or "",
            image_bytes=image_bytes,
            image_media_type=image_media_type,
            resolved_bnet=fetched.resolved_bnet,
        )

    async def query_summary(self, query: DashenSummaryQuery) -> DashenSummaryOutput:
        fetched = await self.fetch_summary(query)
        return await self.render_summary(fetched)

    async def _resolve_query(self, query: DashenSummaryQuery) -> tuple[DashenSummaryQuery, Optional[BnetSearchResult]]:
        if query.customer_token:
            resolved_full_id = str(query.full_id or query.bnet_id).strip()
            return DashenSummaryQuery(
                customer_token=query.customer_token,
                bnet_id=query.bnet_id,
                full_id=resolved_full_id,
                icon_url=query.icon_url,
                scope=query.scope,
            ), None

        lookup_text = str(query.bnet_id or query.full_id or "").strip()
        if not lookup_text:
            raise ModuleError(
                error="missing_target",
                message="Missing query target: bnet_id, full_id or customer_token is required.",
                status_code=400,
                hint='Example: {"bnet_id":"Player#12345"}',
            )

        search_output = await self.search_module.search(lookup_text, render=False)
        customer_token = search_output.result.customer_token
        if not customer_token:
            payload: Dict[str, Any] = search_output.result.payload
            data = payload.get("data") if isinstance(payload, dict) else None
            raise ModuleError(
                error="bnet_not_found",
                message=f"Could not resolve customerToken from bnet_id: {lookup_text}",
                status_code=404,
                hint=(
                    "Check exact letter case and the number after '#'. "
                    "Dashen search is often case-sensitive. "
                    "If you already have customer_token, query with customer_token directly."
                ),
                details={
                    "query": search_output.result.query,
                    "upstream_code": payload.get("code") if isinstance(payload, dict) else None,
                    "upstream_msg": payload.get("msg") if isinstance(payload, dict) else None,
                    "has_data": isinstance(data, dict),
                    "has_customer_token": bool(customer_token),
                    "resolved_name": search_output.result.full_id,
                    "resolved_bnet_id": search_output.result.bnet_id,
                },
            )

        resolved_query = DashenSummaryQuery(
            customer_token=customer_token,
            bnet_id=search_output.result.bnet_id or lookup_text,
            full_id=search_output.result.full_id or lookup_text,
            icon_url=search_output.result.icon_url,
            scope=query.scope,
        )
        return resolved_query, search_output.result


dashen_summary_module = DashenSummaryModule()
