from __future__ import annotations

from dataclasses import dataclass, replace
import re
from typing import Optional

try:
    from overstats.src.client.apiclient import DashenAPIClient, dashen_api_client
    from overstats.src.modules.blizzard_player_search import (
        BlizzardPlayerSearchEntry,
        BlizzardPlayerSearchModule,
        BlizzardPlayerSearchQuery,
        blizzard_player_search_module,
        normalize_blizzard_id,
        normalize_player_search_name,
        validate_blizzard_locale,
    )
    from overstats.src.modules.dashen_profile.render import RenderedImage, render_profile_summary
    from overstats.src.modules.errors import ModuleError
    from overstats.src.modules.query_tool import cache_query_tool_asset_bytes, get_cached_asset_path
    from overstats.src.modules.render_base import run_render
except ModuleNotFoundError:
    from src.client.apiclient import DashenAPIClient, dashen_api_client
    from src.modules.blizzard_player_search import (
        BlizzardPlayerSearchEntry,
        BlizzardPlayerSearchModule,
        BlizzardPlayerSearchQuery,
        blizzard_player_search_module,
        normalize_blizzard_id,
        normalize_player_search_name,
        validate_blizzard_locale,
    )
    from src.modules.dashen_profile.render import RenderedImage, render_profile_summary
    from src.modules.errors import ModuleError
    from src.modules.query_tool import cache_query_tool_asset_bytes, get_cached_asset_path
    from src.modules.render_base import run_render

from .parser import (
    BLIZZARD_PROFILE_FETCH_LOCALE,
    BlizzardParsedProfile,
    build_blizzard_render_context,
    parse_blizzard_profile_html,
)


_BLIZZARD_ID_FROM_URL_RE = re.compile(r"/career/([^/]+)/?$")
_BATTLETAG_WITH_SUFFIX_RE = re.compile(r".+[#-]\d+$")


@dataclass(frozen=True)
class BlizzardProfileQuery:
    player_id: str = ""
    blizzard_id: str = ""
    locale: str = "zh-tw"
    mode: str = "quick"


@dataclass(frozen=True)
class BlizzardProfileOutput:
    query: BlizzardProfileQuery
    resolved_player_id: str
    resolved_blizzard_id: str
    career_url: str
    parsed: BlizzardParsedProfile
    resolved_entry: Optional[BlizzardPlayerSearchEntry] = None
    search_results: tuple[BlizzardPlayerSearchEntry, ...] = ()
    image: Optional[RenderedImage] = None

    @property
    def battletag(self) -> str:
        context = build_blizzard_render_context(self.parsed, resolved_player_label=self.resolved_player_id)
        return context.battletag

    @property
    def battlenum(self) -> str:
        context = build_blizzard_render_context(self.parsed, resolved_player_label=self.resolved_player_id)
        return context.battlenum


class BlizzardProfileModule:
    def __init__(
        self,
        api_client: Optional[DashenAPIClient] = None,
        search_module: Optional[BlizzardPlayerSearchModule] = None,
    ) -> None:
        self.api_client = api_client or dashen_api_client
        self.search_module = search_module or blizzard_player_search_module

    async def query_profile(
        self,
        query: BlizzardProfileQuery,
        *,
        render: bool = False,
    ) -> BlizzardProfileOutput:
        normalized_query = self._normalize_query(query)
        resolved_entry, search_results, fetch_target = await self._resolve_target(normalized_query)
        html, final_url, status_code = await self.api_client.fetch_blizzard_career_page(
            fetch_target,
            locale=BLIZZARD_PROFILE_FETCH_LOCALE,
        )
        if status_code == 404:
            raise ModuleError(
                error="blizzard_profile_not_found",
                message="Blizzard career profile was not found or is not public.",
                status_code=404,
                hint="Try an exact BattleTag like Player#1234 or use Blizzard Player Search first.",
                details={
                    "player_id": normalized_query.player_id,
                    "blizzard_id": normalized_query.blizzard_id,
                    "fetch_target": fetch_target,
                },
            )
        if status_code < 200 or status_code >= 300:
            raise ModuleError(
                error="blizzard_profile_query_failed",
                message="Blizzard career profile request failed.",
                status_code=502,
                details={
                    "player_id": normalized_query.player_id,
                    "blizzard_id": normalized_query.blizzard_id,
                    "fetch_target": fetch_target,
                    "status_code": status_code,
                },
            )

        try:
            parsed = parse_blizzard_profile_html(
                html,
                mode=normalized_query.mode,
                preferred_title=(resolved_entry.title if resolved_entry else ""),
                preferred_last_updated_at=(resolved_entry.last_updated_at if resolved_entry else 0),
            )
        except ValueError as exc:
            raise ModuleError(
                error="blizzard_profile_parse_failed",
                message=str(exc),
                status_code=502,
                details={
                    "player_id": normalized_query.player_id,
                    "blizzard_id": normalized_query.blizzard_id,
                    "final_url": final_url,
                },
            ) from exc

        resolved_blizzard_id = self._extract_blizzard_id(final_url) or normalized_query.blizzard_id or fetch_target
        resolved_player_id = normalized_query.player_id or parsed.summary.display_name
        image = None
        if render:
            await self._warm_hero_icon_cache(parsed)
            avatar_bytes = await self._try_fetch_avatar_bytes(parsed)
            try:
                context = build_blizzard_render_context(parsed, resolved_player_label=resolved_player_id)
                context = replace(context, avatar_bytes=avatar_bytes)
                image = await run_render(render_profile_summary, context)
            except RuntimeError as exc:
                raise ModuleError(
                    error="render_dependency_missing",
                    message=str(exc),
                    status_code=500,
                    hint="Install Pillow in the runtime environment to enable image rendering.",
                ) from exc

        return BlizzardProfileOutput(
            query=normalized_query,
            resolved_player_id=resolved_player_id,
            resolved_blizzard_id=resolved_blizzard_id,
            career_url=self._build_career_url(resolved_blizzard_id, normalized_query.locale),
            parsed=parsed,
            resolved_entry=resolved_entry,
            search_results=search_results,
            image=image,
        )

    async def query_profile_image(self, query: BlizzardProfileQuery) -> BlizzardProfileOutput:
        return await self.query_profile(query, render=True)

    def _normalize_query(self, query: BlizzardProfileQuery) -> BlizzardProfileQuery:
        locale = self._validate_locale(query.locale)
        mode = self._normalize_mode(query.mode)
        player_id = normalize_player_search_name(query.player_id)
        blizzard_id = normalize_blizzard_id(query.blizzard_id)
        if not player_id and not blizzard_id:
            raise ModuleError(
                error="missing_target",
                message="player_id or blizzard_id is required for Blizzard profile query.",
                status_code=400,
                hint='Example: {"player_id":"TeKrop#2217"}',
            )
        return BlizzardProfileQuery(
            player_id=player_id,
            blizzard_id=blizzard_id,
            locale=locale,
            mode=mode,
        )

    async def _resolve_target(
        self,
        query: BlizzardProfileQuery,
    ) -> tuple[Optional[BlizzardPlayerSearchEntry], tuple[BlizzardPlayerSearchEntry, ...], str]:
        if query.blizzard_id:
            return None, (), query.blizzard_id

        if _BATTLETAG_WITH_SUFFIX_RE.fullmatch(query.player_id or ""):
            return None, (), query.player_id

        search_output = await self.search_module.search(
            BlizzardPlayerSearchQuery(
                name=query.player_id,
                locale=query.locale,
                order_by="last_updated_at:desc",
                offset=0,
                limit=10,
            )
        )
        if search_output.total <= 0:
            raise ModuleError(
                error="blizzard_player_not_found",
                message=f"Could not find a public Blizzard player named {query.player_id}.",
                status_code=404,
                details={"player_id": query.player_id},
            )
        if search_output.total > 1:
            raise ModuleError(
                error="blizzard_player_ambiguous",
                message="Multiple public Blizzard profiles matched this name. Please provide an exact BattleTag or Blizzard ID.",
                status_code=409,
                hint='Use /api/v2/blizzard-player-search first, then retry with "blizzard_id".',
                details={
                    "player_id": query.player_id,
                    "count": search_output.total,
                    "candidates": [item.to_dict() for item in search_output.results],
                },
            )
        resolved_entry = search_output.results[0]
        return resolved_entry, tuple(search_output.results), resolved_entry.blizzard_id

    async def _try_fetch_avatar_bytes(self, parsed: BlizzardParsedProfile) -> Optional[bytes]:
        avatar_url = str(parsed.summary.avatar_url or "").strip()
        if not avatar_url:
            return None
        try:
            return await self.api_client.get_icon_proxy(avatar_url)
        except Exception as exc:
            print(f"[overstats] failed to fetch blizzard profile avatar: {exc}")
            return None

    async def _warm_hero_icon_cache(self, parsed: BlizzardParsedProfile) -> None:
        icon_urls = []
        for row in parsed.hero_rows:
            icon_url = str(row.payload.get("heroIconUrl") or "").strip()
            if icon_url and not get_cached_asset_path(icon_url, "heroes"):
                icon_urls.append(icon_url)

        seen = set()
        for icon_url in icon_urls:
            if icon_url in seen:
                continue
            seen.add(icon_url)
            try:
                data = await self.api_client.get_icon_proxy(icon_url)
            except Exception as exc:
                print(f"[overstats] failed to fetch blizzard hero icon url={icon_url}: {exc}")
                continue
            cache_query_tool_asset_bytes(icon_url, data, category="heroes")

    def _validate_locale(self, locale: str) -> str:
        try:
            return validate_blizzard_locale(locale)
        except ValueError as exc:
            raise ModuleError(
                error="invalid_locale",
                message=str(exc),
                status_code=400,
                details={"locale": locale},
            ) from exc

    def _normalize_mode(self, mode: str) -> str:
        normalized = str(mode or "").strip().lower()
        if normalized in {"competitive", "comp", "ranked"}:
            return "competitive"
        return "quick"

    def _extract_blizzard_id(self, final_url: str) -> str:
        match = _BLIZZARD_ID_FROM_URL_RE.search(str(final_url or "").strip())
        return normalize_blizzard_id(str(match.group(1) if match else "").strip())

    def _build_career_url(self, blizzard_id: str, locale: str) -> str:
        return f"https://overwatch.blizzard.com/{locale}/career/{normalize_blizzard_id(blizzard_id)}/"


blizzard_profile_module = BlizzardProfileModule()
