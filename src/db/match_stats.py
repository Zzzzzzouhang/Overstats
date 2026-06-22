from __future__ import annotations

from collections import Counter
import json
import math
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


MATCH_STATS_DB_PATH = Path(__file__).resolve().parent / "match_stats.sqlite3"
PLAYER_IDENTITY_TABLE = "player_identity_map"
HERO_MATCH_DETAIL_TABLE = "hero_match_detail"
COMP_DATA_TABLE = "comp_data"
COMP_DATA_SUMMARY_TABLE = "comp_data_summary"
HERO_PERK_PICK_TABLE = "hero_perk_pick"
HERO_PERK_SUMMARY_TABLE = "hero_perk_summary"
MATCH_STRENGTH_CACHE_TABLE = "match_strength_cache"
PLAYER_COMPETITIVE_RANK_TABLE = "player_competitive_rank"
PLAYER_COMPETITIVE_RANK_FETCH_TABLE = "player_competitive_rank_fetch"
MATCH_LIST_PAGE_CACHE_TABLE = "match_list_page_cache"
MATCH_META_TABLE = "match_meta"
MATCH_PLAYER_TABLE = "match_player"
OVERALL_RANK_BUCKET_KEY = -1

# Field order used when inserting match_meta rows.
MATCH_META_FIELDS = (
    "match_id",
    "match_result",
    "focus_player_side",
    "match_mode",
    "map_guid",
    "start_time",
    "game_time_sec",
    "match_list_json",
    "frozen",
    "last_update",
)

# Field order used when inserting match_player rows.
MATCH_PLAYER_FIELDS = (
    "match_id",
    "player_bnet_id",
    "player_name",
    "side",
    "hero_guid",
    "rank_bucket",
    "role_type",
    "kill",
    "assist",
    "death",
    "hero_damage",
    "healing",
    "damage_blocked",
    "friend_bnet_ids_json",
    "hero_damage_taken",
    "final_hit",
    "solo_kills",
    "target_competing_time",
    "healing_taken",
    "endorse_bnet_ids_json",
    "last_update",
)


class IDPoolDB:
    """Small SQLite adapter used by summary rendering and dashen match highlights.

    The adapter is intentionally lazy and permissive:
    - if the sqlite database file is absent, every query degrades to an empty result
    - if the database schema is missing required tables, callers still get empty results
    """

    _warn_lock = threading.Lock()
    _write_lock = threading.Lock()
    _warned_messages: set[str] = set()

    def __init__(self, db_path: Optional[Path] = None, *args: Any, **kwargs: Any) -> None:
        self.db_path = Path(db_path or MATCH_STATS_DB_PATH)

    @classmethod
    def _warn_once(cls, message: str) -> None:
        with cls._warn_lock:
            if message in cls._warned_messages:
                return
            cls._warned_messages.add(message)
        print(f"[overstats] {message}")

    def _get_connection(self) -> Optional[sqlite3.Connection]:
        if not self.db_path.exists():
            self._warn_once(f"match stats sqlite db not found: {self.db_path}")
            return None
        try:
            connection = sqlite3.connect(str(self.db_path))
            connection.row_factory = None
            return connection
        except Exception as exc:
            self._warn_once(f"match stats sqlite connection failed: {type(exc).__name__}: {exc}")
            return None

    def _get_write_connection(self) -> Optional[sqlite3.Connection]:
        try:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(str(self.db_path), timeout=30)
            connection.row_factory = None
            return connection
        except Exception as exc:
            self._warn_once(f"match stats sqlite write connection failed: {type(exc).__name__}: {exc}")
            return None

    def _initialize_player_identity_table(self, connection: sqlite3.Connection) -> None:
        connection.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {PLAYER_IDENTITY_TABLE} (
                bnetid TEXT PRIMARY KEY,
                battletag TEXT NOT NULL,
                battlename TEXT NOT NULL,
                battlenum TEXT NOT NULL DEFAULT '',
                update_time INTEGER NOT NULL
            )
            """
        )

    def _table_exists(self, connection: sqlite3.Connection, table_name: str) -> bool:
        try:
            row = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                (str(table_name or "").strip(),),
            ).fetchone()
        except Exception:
            return False
        return bool(row)

    def _get_existing_columns(self, connection: sqlite3.Connection, table_name: str) -> set[str]:
        try:
            rows = connection.execute(f"PRAGMA table_info({table_name})").fetchall() or []
        except Exception:
            return set()
        return {str(row[1]) for row in rows if len(row) > 1 and str(row[1])}

    def _ensure_columns(
        self,
        connection: sqlite3.Connection,
        table_name: str,
        column_definitions: Sequence[tuple[str, str]],
    ) -> None:
        if not self._table_exists(connection, table_name):
            return
        existing = self._get_existing_columns(connection, table_name)
        for column_name, column_sql in column_definitions:
            if column_name in existing:
                continue
            try:
                connection.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_sql}")
            except Exception as exc:
                self._warn_once(
                    f"match stats sqlite add column failed table={table_name} column={column_name}: "
                    f"{type(exc).__name__}: {exc}"
                )

    def _initialize_match_detail_tables(self, connection: sqlite3.Connection) -> None:
        connection.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {HERO_MATCH_DETAIL_TABLE} (
                match_id TEXT NOT NULL,
                player_bnet_id TEXT NOT NULL,
                player_name TEXT NOT NULL DEFAULT '',
                hero_guid TEXT NOT NULL,
                rank_score INTEGER,
                rank_bucket INTEGER,
                use_time_sec REAL NOT NULL DEFAULT 0,
                use_time_rate REAL NOT NULL DEFAULT 0,
                map_guid TEXT NOT NULL DEFAULT '',
                start_time INTEGER NOT NULL DEFAULT 0,
                game_time_sec INTEGER NOT NULL DEFAULT 0,
                stat_map_json TEXT NOT NULL DEFAULT '{{}}',
                last_update INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (match_id, player_bnet_id, hero_guid)
            )
            """
        )
        connection.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {COMP_DATA_TABLE} (
                match_id TEXT NOT NULL DEFAULT '',
                player_bnet_id TEXT NOT NULL DEFAULT '',
                hero_guid TEXT NOT NULL,
                statmap_name TEXT NOT NULL,
                statmap_value REAL NOT NULL,
                statmap_raw_value REAL,
                rank_score INTEGER,
                rank_bucket INTEGER,
                use_time_sec REAL NOT NULL DEFAULT 0,
                use_time_rate REAL NOT NULL DEFAULT 0,
                last_update INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        self._ensure_columns(
            connection,
            COMP_DATA_TABLE,
            (
                ("match_id", "match_id TEXT NOT NULL DEFAULT ''"),
                ("player_bnet_id", "player_bnet_id TEXT NOT NULL DEFAULT ''"),
                ("statmap_raw_value", "statmap_raw_value REAL"),
                ("rank_bucket", "rank_bucket INTEGER"),
                ("use_time_sec", "use_time_sec REAL NOT NULL DEFAULT 0"),
                ("use_time_rate", "use_time_rate REAL NOT NULL DEFAULT 0"),
            ),
        )
        connection.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {HERO_PERK_PICK_TABLE} (
                match_id TEXT NOT NULL,
                player_bnet_id TEXT NOT NULL,
                player_name TEXT NOT NULL DEFAULT '',
                hero_guid TEXT NOT NULL,
                perk_guid TEXT NOT NULL,
                perk_level INTEGER NOT NULL DEFAULT 0,
                slot_index INTEGER NOT NULL DEFAULT 0,
                rank_score INTEGER,
                rank_bucket INTEGER,
                start_time INTEGER NOT NULL DEFAULT 0,
                last_update INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (match_id, player_bnet_id, hero_guid, perk_level, slot_index, perk_guid)
            )
            """
        )
        connection.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {COMP_DATA_SUMMARY_TABLE} (
                hero_guid TEXT NOT NULL,
                statmap_name TEXT NOT NULL,
                rank_bucket_key INTEGER NOT NULL,
                sample_count INTEGER NOT NULL DEFAULT 0,
                avg_value REAL,
                median_value REAL,
                top20_value REAL,
                top10_value REAL,
                top5_value REAL,
                top2_value REAL,
                bottom20_value REAL,
                bottom10_value REAL,
                bottom5_value REAL,
                bottom2_value REAL,
                updated_at INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (hero_guid, statmap_name, rank_bucket_key)
            )
            """
        )
        connection.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {HERO_PERK_SUMMARY_TABLE} (
                hero_guid TEXT NOT NULL,
                perk_level INTEGER NOT NULL,
                perk_guid TEXT NOT NULL,
                rank_bucket_key INTEGER NOT NULL,
                pick_count INTEGER NOT NULL DEFAULT 0,
                sample_count INTEGER NOT NULL DEFAULT 0,
                pick_rate REAL NOT NULL DEFAULT 0,
                updated_at INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (hero_guid, perk_level, perk_guid, rank_bucket_key)
            )
            """
        )
        # match_strength_cache: stores per-match avg_score computed by dashen_quick_strength
        connection.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {MATCH_STRENGTH_CACHE_TABLE} (
                match_id TEXT PRIMARY KEY,
                avg_score REAL NOT NULL,
                player_count INTEGER NOT NULL DEFAULT 0,
                score_min INTEGER,
                score_max INTEGER,
                updated_at INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        # player_competitive_rank: stores per-player per-role competitive rank score
        connection.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {PLAYER_COMPETITIVE_RANK_TABLE} (
                player_bnet_id TEXT NOT NULL,
                role_type TEXT NOT NULL,
                rank_score INTEGER NOT NULL,
                season INTEGER,
                source_match_id TEXT,
                cache_week TEXT NOT NULL DEFAULT '',
                checked_at INTEGER NOT NULL DEFAULT 0,
                updated_at INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (player_bnet_id, role_type)
            )
            """
        )
        self._ensure_columns(
            connection,
            PLAYER_COMPETITIVE_RANK_TABLE,
            (
                ("cache_week", "cache_week TEXT NOT NULL DEFAULT ''"),
                ("checked_at", "checked_at INTEGER NOT NULL DEFAULT 0"),
            ),
        )
        connection.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {PLAYER_COMPETITIVE_RANK_FETCH_TABLE} (
                player_bnet_id TEXT NOT NULL,
                cache_week TEXT NOT NULL,
                game_mode TEXT NOT NULL,
                checked_at INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (player_bnet_id, cache_week, game_mode)
            )
            """
        )
        connection.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {MATCH_LIST_PAGE_CACHE_TABLE} (
                source_kind TEXT NOT NULL,
                customer_token TEXT NOT NULL,
                game_mode TEXT NOT NULL,
                season_key TEXT NOT NULL,
                page INTEGER NOT NULL,
                payload_json TEXT NOT NULL DEFAULT '{{}}',
                match_ids_json TEXT NOT NULL DEFAULT '[]',
                entry_count INTEGER NOT NULL DEFAULT 0,
                fetched_at INTEGER NOT NULL DEFAULT 0,
                stop_reason TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (source_kind, customer_token, game_mode, season_key, page)
            )
            """
        )
        # match_meta: stores per-match metadata (one row per match)
        connection.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {MATCH_META_TABLE} (
                match_id TEXT PRIMARY KEY,
                match_result INTEGER,
                focus_player_side TEXT,
                match_mode TEXT,
                map_guid TEXT NOT NULL DEFAULT '',
                start_time INTEGER NOT NULL DEFAULT 0,
                game_time_sec INTEGER NOT NULL DEFAULT 0,
                match_list_json TEXT,
                frozen INTEGER NOT NULL DEFAULT 0,
                last_update INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        # match_player: stores per-player per-match data (10 rows per match)
        connection.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {MATCH_PLAYER_TABLE} (
                match_id TEXT NOT NULL,
                player_bnet_id TEXT NOT NULL,
                player_name TEXT NOT NULL DEFAULT '',
                side TEXT NOT NULL DEFAULT '',
                hero_guid TEXT NOT NULL DEFAULT '',
                rank_bucket INTEGER,
                role_type TEXT NOT NULL DEFAULT '',
                kill INTEGER NOT NULL DEFAULT 0,
                assist INTEGER NOT NULL DEFAULT 0,
                death INTEGER NOT NULL DEFAULT 0,
                hero_damage INTEGER NOT NULL DEFAULT 0,
                healing INTEGER NOT NULL DEFAULT 0,
                damage_blocked INTEGER NOT NULL DEFAULT 0,
                friend_bnet_ids_json TEXT,
                hero_damage_taken INTEGER NOT NULL DEFAULT 0,
                final_hit INTEGER NOT NULL DEFAULT 0,
                solo_kills INTEGER NOT NULL DEFAULT 0,
                target_competing_time REAL NOT NULL DEFAULT 0,
                healing_taken INTEGER NOT NULL DEFAULT 0,
                endorse_bnet_ids_json TEXT,
                last_update INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (match_id, player_bnet_id)
            )
            """
        )
        # Ensure new columns exist on older databases.
        self._ensure_columns(
            connection,
            MATCH_META_TABLE,
            (
                ("frozen", "frozen INTEGER NOT NULL DEFAULT 0"),
            ),
        )
        self._ensure_columns(
            connection,
            MATCH_PLAYER_TABLE,
            (
                ("hero_damage_taken", "hero_damage_taken INTEGER NOT NULL DEFAULT 0"),
                ("final_hit", "final_hit INTEGER NOT NULL DEFAULT 0"),
                ("solo_kills", "solo_kills INTEGER NOT NULL DEFAULT 0"),
                ("target_competing_time", "target_competing_time REAL NOT NULL DEFAULT 0"),
                ("healing_taken", "healing_taken INTEGER NOT NULL DEFAULT 0"),
                ("endorse_bnet_ids_json", "endorse_bnet_ids_json TEXT"),
            ),
        )
        index_statements = (
            f"CREATE UNIQUE INDEX IF NOT EXISTS idx_{COMP_DATA_TABLE}_uniq "
            f"ON {COMP_DATA_TABLE} (match_id, player_bnet_id, hero_guid, statmap_name)",
            f"CREATE INDEX IF NOT EXISTS idx_{COMP_DATA_TABLE}_hero_stat_rank_value "
            f"ON {COMP_DATA_TABLE} (hero_guid, statmap_name, rank_bucket, statmap_value)",
            f"CREATE INDEX IF NOT EXISTS idx_{COMP_DATA_TABLE}_match_player "
            f"ON {COMP_DATA_TABLE} (match_id, player_bnet_id)",
            f"CREATE INDEX IF NOT EXISTS idx_{HERO_MATCH_DETAIL_TABLE}_hero_rank_start "
            f"ON {HERO_MATCH_DETAIL_TABLE} (hero_guid, rank_bucket, start_time)",
            f"CREATE INDEX IF NOT EXISTS idx_{HERO_PERK_PICK_TABLE}_hero_level_rank_perk "
            f"ON {HERO_PERK_PICK_TABLE} (hero_guid, perk_level, rank_bucket, perk_guid)",
            f"CREATE INDEX IF NOT EXISTS idx_{HERO_PERK_PICK_TABLE}_match_player "
            f"ON {HERO_PERK_PICK_TABLE} (match_id, player_bnet_id)",
            f"CREATE INDEX IF NOT EXISTS idx_{COMP_DATA_SUMMARY_TABLE}_hero_rank "
            f"ON {COMP_DATA_SUMMARY_TABLE} (hero_guid, rank_bucket_key, statmap_name)",
            f"CREATE INDEX IF NOT EXISTS idx_{HERO_PERK_SUMMARY_TABLE}_hero_level_rank "
            f"ON {HERO_PERK_SUMMARY_TABLE} (hero_guid, perk_level, rank_bucket_key, perk_guid)",
            f"CREATE INDEX IF NOT EXISTS idx_{PLAYER_COMPETITIVE_RANK_TABLE}_player "
            f"ON {PLAYER_COMPETITIVE_RANK_TABLE} (player_bnet_id)",
            f"CREATE INDEX IF NOT EXISTS idx_{PLAYER_COMPETITIVE_RANK_TABLE}_week_player "
            f"ON {PLAYER_COMPETITIVE_RANK_TABLE} (cache_week, player_bnet_id)",
            f"CREATE INDEX IF NOT EXISTS idx_{MATCH_LIST_PAGE_CACHE_TABLE}_lookup "
            f"ON {MATCH_LIST_PAGE_CACHE_TABLE} (source_kind, customer_token, game_mode, season_key, page)",
            f"CREATE INDEX IF NOT EXISTS idx_{MATCH_PLAYER_TABLE}_bnet "
            f"ON {MATCH_PLAYER_TABLE} (player_bnet_id)",
            f"CREATE INDEX IF NOT EXISTS idx_{MATCH_META_TABLE}_start "
            f"ON {MATCH_META_TABLE} (start_time)",
            f"CREATE INDEX IF NOT EXISTS idx_{MATCH_PLAYER_TABLE}_side "
            f"ON {MATCH_PLAYER_TABLE} (match_id, side)",
        )
        for statement in index_statements:
            try:
                connection.execute(statement)
            except Exception as exc:
                self._warn_once(f"match stats sqlite create index failed: {type(exc).__name__}: {exc}")

    def initialize_match_detail_schema(self) -> bool:
        with self._write_lock:
            conn = self._get_write_connection()
            if conn is None:
                return False
            try:
                self._initialize_player_identity_table(conn)
                self._initialize_match_detail_tables(conn)
                conn.commit()
                return True
            except Exception as exc:
                self._warn_once(f"match stats sqlite initialize schema failed: {type(exc).__name__}: {exc}")
                return False
            finally:
                try:
                    conn.close()
                except Exception:
                    pass

    def initialize_player_identity_schema(self) -> bool:
        with self._write_lock:
            conn = self._get_write_connection()
            if conn is None:
                return False
            try:
                self._initialize_player_identity_table(conn)
                conn.commit()
                return True
            except Exception as exc:
                self._warn_once(f"match stats sqlite initialize player identity schema failed: {type(exc).__name__}: {exc}")
                return False
            finally:
                try:
                    conn.close()
                except Exception:
                    pass

    @staticmethod
    def _escape_like_pattern(text: str) -> str:
        return str(text or "").replace("!", "!!").replace("%", "!%").replace("_", "!_")

    @staticmethod
    def _summary_rows_to_dict(rows: List[Any]) -> Dict[Any, Dict[str, Any]]:
        result: Dict[Any, Dict[str, Any]] = {}
        for row in rows or []:
            (
                statmap_name,
                rank_score,
                sample_count,
                avg_value,
                median_value,
                top20_value,
                top10_value,
                top5_value,
                top2_value,
                bottom20_value,
                bottom10_value,
                bottom5_value,
                bottom2_value,
            ) = row
            rank_key = int(rank_score) if rank_score is not None else None
            result[(str(statmap_name), rank_key)] = {
                "count": int(sample_count or 0),
                "avg": float(avg_value) if avg_value is not None else None,
                "median": float(median_value) if median_value is not None else None,
                "top20": float(top20_value) if top20_value is not None else None,
                "top10": float(top10_value) if top10_value is not None else None,
                "top5": float(top5_value) if top5_value is not None else None,
                "top2": float(top2_value) if top2_value is not None else None,
                "bottom20": float(bottom20_value) if bottom20_value is not None else None,
                "bottom10": float(bottom10_value) if bottom10_value is not None else None,
                "bottom5": float(bottom5_value) if bottom5_value is not None else None,
                "bottom2": float(bottom2_value) if bottom2_value is not None else None,
            }
        return result

    @staticmethod
    def _perk_summary_rows_to_dict(rows: List[Any]) -> Dict[Any, Dict[str, Any]]:
        grouped: Dict[Any, Dict[str, Any]] = {}
        for row in rows or []:
            hero_guid, perk_level, perk_guid, rank_bucket_key, pick_count, sample_count, pick_rate = row
            result_key = None if int(rank_bucket_key) == OVERALL_RANK_BUCKET_KEY else int(rank_bucket_key)
            bucket = grouped.setdefault(
                result_key,
                {"hero_guid": str(hero_guid), "perk_level": int(perk_level or 0), "sample_count": 0, "perks": {}},
            )
            bucket["sample_count"] = max(int(sample_count or 0), int(bucket.get("sample_count") or 0))
            bucket["perks"][str(perk_guid)] = {
                "pick_count": int(pick_count or 0),
                "sample_count": int(sample_count or 0),
                "pick_rate": float(pick_rate or 0.0),
            }
        return grouped

    def get_all_rank_buckets(self) -> List[int]:
        """Read all non-null, non-zero rank_bucket values from hero_match_detail.

        These values are in the 0-5+ range (produced by normalize_hero_rank_score).
        They can be converted to the 1000-5000 scale by the caller.
        """
        conn = self._get_connection()
        if conn is None:
            return []
        try:
            cursor = conn.cursor()
            try:
                cursor.execute(
                    """
                    SELECT rank_bucket FROM hero_match_detail
                    WHERE rank_bucket IS NOT NULL AND rank_bucket > 0
                    """
                )
                rows = cursor.fetchall() or []
            finally:
                cursor.close()
            return [int(row[0]) for row in rows if row[0] is not None and int(row[0]) > 0]
        except Exception as exc:
            self._warn_once(f"match stats sqlite get_all_rank_buckets failed: {type(exc).__name__}: {exc}")
            return []
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def get_match_player_rank_scores(self, match_ids: List[str]) -> List[int]:
        """Read raw rank_score values for all players in the given matches.

        Returns the rank_score values as stored in hero_match_detail (0-5+ range).
        """
        if not match_ids:
            return []
        conn = self._get_connection()
        if conn is None:
            return []
        try:
            placeholders = ",".join(["?"] * len(match_ids))
            cursor = conn.cursor()
            try:
                cursor.execute(
                    f"""
                    SELECT DISTINCT player_bnet_id, rank_score
                    FROM hero_match_detail
                    WHERE match_id IN ({placeholders})
                    AND rank_score IS NOT NULL AND rank_score > 0
                    """,
                    tuple(str(mid) for mid in match_ids),
                )
                rows = cursor.fetchall() or []
            finally:
                cursor.close()
            return [int(row[1]) for row in rows if row[1] is not None and int(row[1]) > 0]
        except Exception as exc:
            self._warn_once(f"match stats sqlite get_match_player_rank_scores failed: {type(exc).__name__}: {exc}")
            return []
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def get_hero_details_by_match_player(
        self, match_id: str, player_bnet_id: str
    ) -> List[Dict[str, Any]]:
        """Return hero_match_detail rows for a specific player in a specific match.

        Each row contains hero_guid, stat_map_json, use_time_sec, use_time_rate,
        rank_score, rank_bucket, map_guid, start_time, game_time_sec.
        Used by _load_detail_from_db to reconstruct heroList for the focus player.
        """
        normalized_match = str(match_id or "").strip()
        normalized_player = str(player_bnet_id or "").strip()
        if not normalized_match or not normalized_player:
            return []
        conn = self._get_connection()
        if conn is None:
            return []
        try:
            cursor = conn.cursor()
            try:
                cursor.execute(
                    f"""
                    SELECT hero_guid, stat_map_json, use_time_sec, use_time_rate,
                           rank_score, rank_bucket, map_guid, start_time, game_time_sec
                    FROM {HERO_MATCH_DETAIL_TABLE}
                    WHERE match_id = ? AND player_bnet_id = ?
                    """,
                    (normalized_match, normalized_player),
                )
                rows = cursor.fetchall() or []
            finally:
                cursor.close()
            result: List[Dict[str, Any]] = []
            for row in rows:
                result.append(
                    {
                        "hero_guid": str(row[0] or ""),
                        "stat_map_json": str(row[1] or "{}"),
                        "use_time_sec": float(row[2] or 0),
                        "use_time_rate": float(row[3] or 0),
                        "rank_score": int(row[4]) if row[4] is not None else 0,
                        "rank_bucket": int(row[5]) if row[5] is not None else 0,
                        "map_guid": str(row[6] or ""),
                        "start_time": int(row[7] or 0),
                        "game_time_sec": int(row[8] or 0),
                    }
                )
            return result
        except Exception as exc:
            self._warn_once(
                f"match stats sqlite get_hero_details_by_match_player failed: "
                f"{type(exc).__name__}: {exc}"
            )
            return []
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def get_all_rank(self) -> List[Dict[str, Any]]:
        conn = self._get_connection()
        if conn is None:
            return []
        try:
            cutoff = int(time.time()) - 15 * 24 * 3600
            cursor = conn.cursor()
            try:
                cursor.execute(
                    """
                    SELECT bnet_id, tank, dps, healer, "open", level, playtime, last_update
                    FROM "rank"
                    WHERE last_update > ?
                    """,
                    (cutoff,),
                )
                rows = cursor.fetchall() or []
            finally:
                cursor.close()
            return [
                {
                    "bnet_id": row[0],
                    "tank": row[1],
                    "dps": row[2],
                    "healer": row[3],
                    "open": row[4],
                    "level": row[5],
                    "playtime": row[6],
                    "last_update": row[7],
                }
                for row in rows
            ]
        except Exception as exc:
            self._warn_once(f"match stats sqlite get_all_rank failed: {type(exc).__name__}: {exc}")
            return []
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def get_group_titles(self, bnet_id: str) -> List[Dict[str, Any]]:
        bnet_id = str(bnet_id or "").strip()
        if not bnet_id:
            return []
        conn = self._get_connection()
        if conn is None:
            return []
        created_at_sql = """
            CASE
                WHEN created_at IS NULL THEN 0
                WHEN typeof(created_at) IN ('integer', 'real') THEN CAST(created_at AS INTEGER)
                ELSE COALESCE(CAST(strftime('%s', created_at) AS INTEGER), 0)
            END
        """
        try:
            cursor = conn.cursor()
            try:
                cursor.execute(
                    f"""
                    SELECT bnet_id, battletag, battlenum, title, color, {created_at_sql} AS created_at_ts
                    FROM group_title
                    WHERE bnet_id = ?
                    ORDER BY created_at_ts ASC, title ASC
                    """,
                    (bnet_id,),
                )
                rows = cursor.fetchall() or []
            finally:
                cursor.close()
            return [
                {
                    "bnet_id": row[0],
                    "battletag": row[1],
                    "battlenum": row[2],
                    "title": row[3],
                    "color": row[4],
                    "create_at": int(row[5] or 0),
                }
                for row in rows
            ]
        except Exception as exc:
            self._warn_once(f"match stats sqlite get_group_titles failed: {type(exc).__name__}: {exc}")
            return []
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def get_statmap_summary(
        self,
        hero_guid: str,
        statmap_names: Optional[List[str]] = None,
        rank_scores: Optional[List[int]] = None,
        ratio_statmap_names: Optional[List[str]] = None,
        group_by_rank: bool = True,
    ) -> Dict[str, Any]:
        hero_guid = str(hero_guid or "").strip()
        if not hero_guid:
            return {}

        statmap_names = [str(item) for item in (statmap_names or []) if str(item)]
        rank_scores = [int(item) for item in (rank_scores or [])]
        ratio_statmap_names = [str(item) for item in (ratio_statmap_names or []) if str(item)]

        conn = self._get_connection()
        if conn is None:
            return {}

        summary_rows = self._get_statmap_summary_preaggregated(
            conn,
            hero_guid=hero_guid,
            statmap_names=statmap_names,
            rank_scores=rank_scores,
            group_by_rank=group_by_rank,
        )
        if summary_rows:
            try:
                conn.close()
            except Exception:
                pass
            return summary_rows

        where_parts = ["hero_guid = ?"]
        params: List[Any] = [hero_guid]
        if statmap_names:
            placeholders = ",".join(["?"] * len(statmap_names))
            where_parts.append(f"statmap_name IN ({placeholders})")
            params.extend(statmap_names)
        if rank_scores:
            placeholders = ",".join(["?"] * len(rank_scores))
            where_parts.append(f"rank_score IN ({placeholders})")
            params.extend(rank_scores)

        where_sql = " AND ".join(where_parts)
        value_expr = "statmap_value"
        query_params = list(params)
        if ratio_statmap_names:
            ratio_placeholders = ",".join(["?"] * len(ratio_statmap_names))
            value_expr = (
                "CASE WHEN statmap_name IN "
                f"({ratio_placeholders}) "
                "THEN MIN(1.0, MAX(0.0, statmap_value)) "
                "ELSE statmap_value END"
            )
            query_params = list(ratio_statmap_names) + query_params

        rank_select = "rank_score" if group_by_rank else "NULL AS rank_score"
        partition_by = "statmap_name, rank_score" if group_by_rank else "statmap_name"

        try:
            cursor = conn.cursor()
            try:
                cursor.execute(
                    f"""
                    WITH filtered AS (
                        SELECT statmap_name, {rank_select}, {value_expr} AS statmap_value
                        FROM comp_data
                        WHERE {where_sql}
                    ),
                    ranked AS (
                        SELECT
                            statmap_name,
                            rank_score,
                            statmap_value,
                            ROW_NUMBER() OVER (
                                PARTITION BY {partition_by}
                                ORDER BY statmap_value
                            ) AS rn,
                            COUNT(*) OVER (
                                PARTITION BY {partition_by}
                            ) AS cnt
                        FROM filtered
                    )
                    SELECT
                        statmap_name,
                        rank_score,
                        MAX(cnt) AS sample_count,
                        AVG(statmap_value) AS avg_value,
                        AVG(
                            CASE
                                WHEN rn IN (
                                    CAST((cnt + 1) / 2 AS INTEGER),
                                    CAST((cnt + 2) / 2 AS INTEGER)
                                )
                                THEN statmap_value
                            END
                        ) AS median_value,
                        MIN(CASE WHEN rn >= CAST(((cnt * 80) + 99) / 100 AS INTEGER) THEN statmap_value END) AS top20_value,
                        MIN(CASE WHEN rn >= CAST(((cnt * 90) + 99) / 100 AS INTEGER) THEN statmap_value END) AS top10_value,
                        MIN(CASE WHEN rn >= CAST(((cnt * 95) + 99) / 100 AS INTEGER) THEN statmap_value END) AS top5_value,
                        MIN(CASE WHEN rn >= CAST(((cnt * 98) + 99) / 100 AS INTEGER) THEN statmap_value END) AS top2_value,
                        MIN(CASE WHEN rn >= CAST(((cnt * 20) + 99) / 100 AS INTEGER) THEN statmap_value END) AS bottom20_value,
                        MIN(CASE WHEN rn >= CAST(((cnt * 10) + 99) / 100 AS INTEGER) THEN statmap_value END) AS bottom10_value,
                        MIN(CASE WHEN rn >= CAST(((cnt * 5) + 99) / 100 AS INTEGER) THEN statmap_value END) AS bottom5_value,
                        MIN(CASE WHEN rn >= CAST(((cnt * 2) + 99) / 100 AS INTEGER) THEN statmap_value END) AS bottom2_value
                    FROM ranked
                    GROUP BY statmap_name, rank_score
                    """,
                    tuple(query_params),
                )
                rows = cursor.fetchall() or []
            finally:
                cursor.close()
            return self._summary_rows_to_dict(list(rows))
        except Exception as exc:
            self._warn_once(f"match stats sqlite get_statmap_summary window query failed: {type(exc).__name__}: {exc}")
            return self._get_statmap_summary_python(
                conn,
                where_sql=where_sql,
                params=params,
                ratio_statmap_names=set(ratio_statmap_names),
                group_by_rank=group_by_rank,
            )
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _get_statmap_summary_preaggregated(
        self,
        conn: sqlite3.Connection,
        *,
        hero_guid: str,
        statmap_names: List[str],
        rank_scores: List[int],
        group_by_rank: bool,
    ) -> Dict[str, Any]:
        if not self._table_exists(conn, COMP_DATA_SUMMARY_TABLE):
            return {}
        where_parts = ["hero_guid = ?"]
        params: List[Any] = [hero_guid]
        if statmap_names:
            placeholders = ",".join(["?"] * len(statmap_names))
            where_parts.append(f"statmap_name IN ({placeholders})")
            params.extend(statmap_names)

        if group_by_rank:
            if rank_scores:
                placeholders = ",".join(["?"] * len(rank_scores))
                where_parts.append(f"rank_bucket_key IN ({placeholders})")
                params.extend(rank_scores)
            else:
                where_parts.append("rank_bucket_key != ?")
                params.append(OVERALL_RANK_BUCKET_KEY)
        else:
            where_parts.append("rank_bucket_key = ?")
            params.append(OVERALL_RANK_BUCKET_KEY)

        try:
            cursor = conn.cursor()
            try:
                cursor.execute(
                    f"""
                    SELECT
                        statmap_name,
                        CASE
                            WHEN rank_bucket_key = ? THEN NULL
                            ELSE rank_bucket_key
                        END AS rank_score,
                        sample_count,
                        avg_value,
                        median_value,
                        top20_value,
                        top10_value,
                        top5_value,
                        top2_value,
                        bottom20_value,
                        bottom10_value,
                        bottom5_value,
                        bottom2_value
                    FROM {COMP_DATA_SUMMARY_TABLE}
                    WHERE {" AND ".join(where_parts)}
                    """,
                    (OVERALL_RANK_BUCKET_KEY, *params),
                )
                rows = cursor.fetchall() or []
            finally:
                cursor.close()
        except Exception as exc:
            self._warn_once(f"match stats sqlite preaggregated statmap summary failed: {type(exc).__name__}: {exc}")
            return {}
        return self._summary_rows_to_dict(list(rows))

    def _get_statmap_summary_python(
        self,
        conn: Any,
        *,
        where_sql: str,
        params: List[Any],
        ratio_statmap_names: set[str],
        group_by_rank: bool,
    ) -> Dict[str, Any]:
        try:
            cursor = conn.cursor()
            try:
                cursor.execute(
                    f"""
                    SELECT statmap_name, rank_score, statmap_value
                    FROM comp_data
                    WHERE {where_sql}
                    ORDER BY statmap_name, rank_score, statmap_value
                    """,
                    tuple(params),
                )
                rows = cursor.fetchall() or []
            finally:
                cursor.close()
        except Exception as exc:
            self._warn_once(f"match stats sqlite get_statmap_summary fallback failed: {type(exc).__name__}: {exc}")
            return {}

        grouped: Dict[Any, List[float]] = {}
        for statmap_name, rank_score, value in rows:
            try:
                normalized_value = float(value)
            except (TypeError, ValueError):
                continue
            if str(statmap_name) in ratio_statmap_names:
                normalized_value = max(0.0, min(1.0, normalized_value))
            rank_key = int(rank_score) if group_by_rank and rank_score is not None else None
            grouped.setdefault((str(statmap_name), rank_key), []).append(normalized_value)

        result: Dict[Any, Dict[str, Any]] = {}
        for key, values in grouped.items():
            count = len(values)
            if count <= 0:
                continue
            values.sort()
            mid_left = (count - 1) // 2
            mid_right = count // 2

            def percentile_index(ratio: float) -> int:
                return max(0, min(count - 1, math.ceil(count * ratio) - 1))

            result[key] = {
                "count": count,
                "avg": sum(values) / count,
                "median": (values[mid_left] + values[mid_right]) / 2,
                "top20": values[percentile_index(0.80)],
                "top10": values[percentile_index(0.90)],
                "top5": values[percentile_index(0.95)],
                "top2": values[percentile_index(0.98)],
                "bottom20": values[percentile_index(0.20)],
                "bottom10": values[percentile_index(0.10)],
                "bottom5": values[percentile_index(0.05)],
                "bottom2": values[percentile_index(0.02)],
            }
        return result

    def get_perk_pick_summary(
        self,
        hero_guid: str,
        perk_level: int,
        rank_scores: Optional[List[int]] = None,
        include_overall: bool = True,
    ) -> Dict[Any, Dict[str, Any]]:
        hero_guid = str(hero_guid or "").strip()
        if not hero_guid:
            return {}
        normalized_perk_level = max(0, int(perk_level or 0))
        normalized_ranks = [int(item) for item in (rank_scores or [])]

        conn = self._get_connection()
        if conn is None:
            return {}
        try:
            if self._table_exists(conn, HERO_PERK_SUMMARY_TABLE):
                where_parts = ["hero_guid = ?", "perk_level = ?"]
                params: List[Any] = [hero_guid, normalized_perk_level]
                rank_filters: List[int] = []
                if include_overall:
                    rank_filters.append(OVERALL_RANK_BUCKET_KEY)
                rank_filters.extend(normalized_ranks)
                if rank_filters:
                    placeholders = ",".join(["?"] * len(rank_filters))
                    where_parts.append(f"rank_bucket_key IN ({placeholders})")
                    params.extend(rank_filters)
                elif not include_overall:
                    where_parts.append("rank_bucket_key != ?")
                    params.append(OVERALL_RANK_BUCKET_KEY)

                cursor = conn.cursor()
                try:
                    cursor.execute(
                        f"""
                        SELECT
                            hero_guid,
                            perk_level,
                            perk_guid,
                            rank_bucket_key,
                            pick_count,
                            sample_count,
                            pick_rate
                        FROM {HERO_PERK_SUMMARY_TABLE}
                        WHERE {" AND ".join(where_parts)}
                        ORDER BY rank_bucket_key ASC, pick_count DESC, perk_guid ASC
                        """,
                        tuple(params),
                    )
                    rows = cursor.fetchall() or []
                finally:
                    cursor.close()
                if rows:
                    return self._perk_summary_rows_to_dict(list(rows))
        except Exception as exc:
            self._warn_once(f"match stats sqlite get_perk_pick_summary failed: {type(exc).__name__}: {exc}")
        finally:
            try:
                conn.close()
            except Exception:
                pass
        return {}

    def get_perk_pick_summary_from_raw(
        self,
        hero_guid: str,
        perk_level: int,
        rank_scores: Optional[List[int]] = None,
        include_overall: bool = True,
    ) -> Dict[Any, Dict[str, Any]]:
        hero_guid = str(hero_guid or "").strip()
        if not hero_guid:
            return {}
        normalized_perk_level = max(0, int(perk_level or 0))
        normalized_ranks = [int(item) for item in (rank_scores or [])]

        conn = self._get_connection()
        if conn is None:
            return {}
        try:
            if not self._table_exists(conn, HERO_PERK_PICK_TABLE):
                return {}
            cursor = conn.cursor()
            try:
                cursor.execute(
                    f"""
                    SELECT
                        perk_guid,
                        rank_bucket,
                        match_id,
                        player_bnet_id
                    FROM {HERO_PERK_PICK_TABLE}
                    WHERE hero_guid = ? AND perk_level = ?
                    ORDER BY rank_bucket ASC, perk_guid ASC
                    """,
                    (hero_guid, normalized_perk_level),
                )
                rows = cursor.fetchall() or []
            finally:
                cursor.close()
        except Exception as exc:
            self._warn_once(
                f"match stats sqlite get_perk_pick_summary_from_raw failed: {type(exc).__name__}: {exc}"
            )
            return {}
        finally:
            try:
                conn.close()
            except Exception:
                pass

        if not rows:
            return {}

        overall_samples: set[tuple[str, str]] = set()
        overall_counts: Counter[str] = Counter()
        rank_samples: Dict[int, set[tuple[str, str]]] = {}
        rank_counts: Dict[int, Counter[str]] = {}
        for perk_guid, rank_bucket, match_id, player_bnet_id in rows:
            normalized_perk_guid = str(perk_guid or "").strip()
            normalized_match_id = str(match_id or "").strip()
            normalized_player_id = str(player_bnet_id or "").strip()
            if not normalized_perk_guid or not normalized_match_id or not normalized_player_id:
                continue
            sample_key = (normalized_match_id, normalized_player_id)
            overall_samples.add(sample_key)
            overall_counts[normalized_perk_guid] += 1
            if rank_bucket is None:
                continue
            normalized_rank_bucket = int(rank_bucket)
            rank_samples.setdefault(normalized_rank_bucket, set()).add(sample_key)
            rank_counts.setdefault(normalized_rank_bucket, Counter())[normalized_perk_guid] += 1

        result: Dict[Any, Dict[str, Any]] = {}
        overall_sample_count = len(overall_samples)
        if include_overall and overall_sample_count > 0:
            result[None] = {
                "hero_guid": hero_guid,
                "perk_level": normalized_perk_level,
                "sample_count": overall_sample_count,
                "perks": {
                    perk_guid: {
                        "pick_count": int(pick_count),
                        "sample_count": overall_sample_count,
                        "pick_rate": float(pick_count / overall_sample_count),
                    }
                    for perk_guid, pick_count in overall_counts.items()
                },
            }

        if normalized_ranks:
            selected_ranks = list(dict.fromkeys(normalized_ranks))
        else:
            selected_ranks = sorted(rank_counts)

        for rank_bucket in selected_ranks:
            sample_count = len(rank_samples.get(rank_bucket) or ())
            if sample_count <= 0:
                continue
            counter = rank_counts.get(rank_bucket) or Counter()
            result[int(rank_bucket)] = {
                "hero_guid": hero_guid,
                "perk_level": normalized_perk_level,
                "sample_count": sample_count,
                "perks": {
                    perk_guid: {
                        "pick_count": int(pick_count),
                        "sample_count": sample_count,
                        "pick_rate": float(pick_count / sample_count),
                    }
                    for perk_guid, pick_count in counter.items()
                },
            }
        return result

    def _expand_comp_summary_keys(
        self,
        keys: Iterable[tuple[str, str, Optional[int]]],
    ) -> List[tuple[str, str, int]]:
        expanded: set[tuple[str, str, int]] = set()
        for hero_guid, statmap_name, rank_bucket in keys or []:
            normalized_hero_guid = str(hero_guid or "").strip()
            normalized_statmap_name = str(statmap_name or "").strip()
            if not normalized_hero_guid or not normalized_statmap_name:
                continue
            expanded.add((normalized_hero_guid, normalized_statmap_name, OVERALL_RANK_BUCKET_KEY))
            if rank_bucket is not None:
                expanded.add((normalized_hero_guid, normalized_statmap_name, int(rank_bucket)))
        return sorted(expanded)

    def _expand_perk_summary_keys(
        self,
        keys: Iterable[tuple[str, int, Optional[int]]],
    ) -> List[tuple[str, int, int]]:
        expanded: set[tuple[str, int, int]] = set()
        for hero_guid, perk_level, rank_bucket in keys or []:
            normalized_hero_guid = str(hero_guid or "").strip()
            normalized_perk_level = int(perk_level or 0)
            if not normalized_hero_guid or normalized_perk_level <= 0:
                continue
            expanded.add((normalized_hero_guid, normalized_perk_level, OVERALL_RANK_BUCKET_KEY))
            if rank_bucket is not None:
                expanded.add((normalized_hero_guid, normalized_perk_level, int(rank_bucket)))
        return sorted(expanded)

    def _refresh_comp_data_summaries(
        self,
        conn: sqlite3.Connection,
        summary_keys: Iterable[tuple[str, str, Optional[int]]],
        *,
        updated_at: int,
    ) -> None:
        expanded_keys = self._expand_comp_summary_keys(summary_keys)
        if not expanded_keys:
            return
        conn.execute("DROP TABLE IF EXISTS temp_comp_summary_key")
        conn.execute(
            """
            CREATE TEMP TABLE temp_comp_summary_key (
                hero_guid TEXT NOT NULL,
                statmap_name TEXT NOT NULL,
                rank_bucket_key INTEGER NOT NULL,
                PRIMARY KEY (hero_guid, statmap_name, rank_bucket_key)
            )
            """
        )
        conn.executemany(
            """
            INSERT OR IGNORE INTO temp_comp_summary_key (
                hero_guid,
                statmap_name,
                rank_bucket_key
            ) VALUES (?, ?, ?)
            """,
            expanded_keys,
        )
        rows = conn.execute(
            f"""
            WITH filtered AS (
                SELECT
                    temp.hero_guid,
                    temp.statmap_name,
                    temp.rank_bucket_key,
                    comp.statmap_value
                FROM temp_comp_summary_key AS temp
                JOIN {COMP_DATA_TABLE} AS comp
                    ON comp.hero_guid = temp.hero_guid
                    AND comp.statmap_name = temp.statmap_name
                    AND (
                        temp.rank_bucket_key = {OVERALL_RANK_BUCKET_KEY}
                        OR comp.rank_bucket = temp.rank_bucket_key
                    )
            ),
            ranked AS (
                SELECT
                    hero_guid,
                    statmap_name,
                    rank_bucket_key,
                    statmap_value,
                    ROW_NUMBER() OVER (
                        PARTITION BY hero_guid, statmap_name, rank_bucket_key
                        ORDER BY statmap_value
                    ) AS rn,
                    COUNT(*) OVER (
                        PARTITION BY hero_guid, statmap_name, rank_bucket_key
                    ) AS cnt
                FROM filtered
            )
            SELECT
                hero_guid,
                statmap_name,
                rank_bucket_key,
                MAX(cnt) AS sample_count,
                AVG(statmap_value) AS avg_value,
                AVG(
                    CASE
                        WHEN rn IN (
                            CAST((cnt + 1) / 2 AS INTEGER),
                            CAST((cnt + 2) / 2 AS INTEGER)
                        )
                        THEN statmap_value
                    END
                ) AS median_value,
                MIN(CASE WHEN rn >= CAST(((cnt * 80) + 99) / 100 AS INTEGER) THEN statmap_value END) AS top20_value,
                MIN(CASE WHEN rn >= CAST(((cnt * 90) + 99) / 100 AS INTEGER) THEN statmap_value END) AS top10_value,
                MIN(CASE WHEN rn >= CAST(((cnt * 95) + 99) / 100 AS INTEGER) THEN statmap_value END) AS top5_value,
                MIN(CASE WHEN rn >= CAST(((cnt * 98) + 99) / 100 AS INTEGER) THEN statmap_value END) AS top2_value,
                MIN(CASE WHEN rn >= CAST(((cnt * 20) + 99) / 100 AS INTEGER) THEN statmap_value END) AS bottom20_value,
                MIN(CASE WHEN rn >= CAST(((cnt * 10) + 99) / 100 AS INTEGER) THEN statmap_value END) AS bottom10_value,
                MIN(CASE WHEN rn >= CAST(((cnt * 5) + 99) / 100 AS INTEGER) THEN statmap_value END) AS bottom5_value,
                MIN(CASE WHEN rn >= CAST(((cnt * 2) + 99) / 100 AS INTEGER) THEN statmap_value END) AS bottom2_value
            FROM ranked
            GROUP BY hero_guid, statmap_name, rank_bucket_key
            """
        ).fetchall()
        conn.executemany(
            f"""
            INSERT INTO {COMP_DATA_SUMMARY_TABLE} (
                hero_guid,
                statmap_name,
                rank_bucket_key,
                sample_count,
                avg_value,
                median_value,
                top20_value,
                top10_value,
                top5_value,
                top2_value,
                bottom20_value,
                bottom10_value,
                bottom5_value,
                bottom2_value,
                updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(hero_guid, statmap_name, rank_bucket_key) DO UPDATE SET
                sample_count = excluded.sample_count,
                avg_value = excluded.avg_value,
                median_value = excluded.median_value,
                top20_value = excluded.top20_value,
                top10_value = excluded.top10_value,
                top5_value = excluded.top5_value,
                top2_value = excluded.top2_value,
                bottom20_value = excluded.bottom20_value,
                bottom10_value = excluded.bottom10_value,
                bottom5_value = excluded.bottom5_value,
                bottom2_value = excluded.bottom2_value,
                updated_at = excluded.updated_at
            """,
            [
                (
                    row[0],
                    row[1],
                    int(row[2]),
                    int(row[3] or 0),
                    row[4],
                    row[5],
                    row[6],
                    row[7],
                    row[8],
                    row[9],
                    row[10],
                    row[11],
                    row[12],
                    row[13],
                    int(updated_at),
                )
                for row in rows
            ],
        )
        conn.execute("DROP TABLE IF EXISTS temp_comp_summary_key")

    def _refresh_hero_perk_summaries(
        self,
        conn: sqlite3.Connection,
        summary_keys: Iterable[tuple[str, int, Optional[int]]],
        *,
        updated_at: int,
    ) -> None:
        expanded_keys = self._expand_perk_summary_keys(summary_keys)
        if not expanded_keys:
            return
        conn.execute("DROP TABLE IF EXISTS temp_perk_summary_key")
        conn.execute(
            """
            CREATE TEMP TABLE temp_perk_summary_key (
                hero_guid TEXT NOT NULL,
                perk_level INTEGER NOT NULL,
                rank_bucket_key INTEGER NOT NULL,
                PRIMARY KEY (hero_guid, perk_level, rank_bucket_key)
            )
            """
        )
        conn.executemany(
            """
            INSERT OR IGNORE INTO temp_perk_summary_key (
                hero_guid,
                perk_level,
                rank_bucket_key
            ) VALUES (?, ?, ?)
            """,
            expanded_keys,
        )
        rows = conn.execute(
            f"""
            WITH sample_source AS (
                SELECT
                    temp.hero_guid,
                    temp.perk_level,
                    temp.rank_bucket_key,
                    pick.match_id,
                    pick.player_bnet_id,
                    pick.hero_guid AS sample_hero_guid
                FROM temp_perk_summary_key AS temp
                JOIN {HERO_PERK_PICK_TABLE} AS pick
                    ON pick.hero_guid = temp.hero_guid
                    AND pick.perk_level = temp.perk_level
                    AND (
                        temp.rank_bucket_key = {OVERALL_RANK_BUCKET_KEY}
                        OR pick.rank_bucket = temp.rank_bucket_key
                    )
                GROUP BY
                    temp.hero_guid,
                    temp.perk_level,
                    temp.rank_bucket_key,
                    pick.match_id,
                    pick.player_bnet_id,
                    pick.hero_guid
            ),
            sample_counts AS (
                SELECT
                    hero_guid,
                    perk_level,
                    rank_bucket_key,
                    COUNT(*) AS sample_count
                FROM sample_source
                GROUP BY hero_guid, perk_level, rank_bucket_key
            ),
            pick_counts AS (
                SELECT
                    temp.hero_guid,
                    temp.perk_level,
                    pick.perk_guid,
                    temp.rank_bucket_key,
                    COUNT(*) AS pick_count
                FROM temp_perk_summary_key AS temp
                JOIN {HERO_PERK_PICK_TABLE} AS pick
                    ON pick.hero_guid = temp.hero_guid
                    AND pick.perk_level = temp.perk_level
                    AND (
                        temp.rank_bucket_key = {OVERALL_RANK_BUCKET_KEY}
                        OR pick.rank_bucket = temp.rank_bucket_key
                    )
                GROUP BY
                    temp.hero_guid,
                    temp.perk_level,
                    pick.perk_guid,
                    temp.rank_bucket_key
            )
            SELECT
                pick_counts.hero_guid,
                pick_counts.perk_level,
                pick_counts.perk_guid,
                pick_counts.rank_bucket_key,
                pick_counts.pick_count,
                sample_counts.sample_count,
                CASE
                    WHEN sample_counts.sample_count > 0
                    THEN CAST(pick_counts.pick_count AS REAL) / CAST(sample_counts.sample_count AS REAL)
                    ELSE 0
                END AS pick_rate
            FROM pick_counts
            JOIN sample_counts
                ON sample_counts.hero_guid = pick_counts.hero_guid
                AND sample_counts.perk_level = pick_counts.perk_level
                AND sample_counts.rank_bucket_key = pick_counts.rank_bucket_key
            """
        ).fetchall()
        conn.executemany(
            f"""
            INSERT INTO {HERO_PERK_SUMMARY_TABLE} (
                hero_guid,
                perk_level,
                perk_guid,
                rank_bucket_key,
                pick_count,
                sample_count,
                pick_rate,
                updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(hero_guid, perk_level, perk_guid, rank_bucket_key) DO UPDATE SET
                pick_count = excluded.pick_count,
                sample_count = excluded.sample_count,
                pick_rate = excluded.pick_rate,
                updated_at = excluded.updated_at
            """,
            [
                (
                    row[0],
                    int(row[1] or 0),
                    row[2],
                    int(row[3]),
                    int(row[4] or 0),
                    int(row[5] or 0),
                    float(row[6] or 0.0),
                    int(updated_at),
                )
                for row in rows
            ],
        )
        conn.execute("DROP TABLE IF EXISTS temp_perk_summary_key")

    def _compute_match_frozen_flag(
        self,
        conn,
        match_id: str,
        match_meta_row: Optional[Dict[str, Any]],
        match_player_rows: Optional[Sequence[Dict[str, Any]]],
    ) -> int:
        """Decide whether a match detail payload is complete enough to freeze."""
        if not match_id:
            return 0
        try:
            cursor = conn.cursor()
            try:
                cursor.execute(
                    f"SELECT frozen FROM {MATCH_META_TABLE} WHERE match_id = ?",
                    (match_id,),
                )
                meta_row = cursor.fetchone()
            finally:
                cursor.close()
        except Exception:
            return 0

        # Frozen is sticky: a match that has settled never reopens.
        if meta_row is not None and int(meta_row[0] or 0) == 1:
            return 1

        now_ts = int(time.time())
        try:
            start_time = int((match_meta_row or {}).get("start_time") or 0)
        except (TypeError, ValueError):
            start_time = 0
        if start_time > 0 and now_ts - start_time > 4 * 3600:
            return 1

        player_signal: Dict[str, bool] = {}
        for row in match_player_rows or []:
            if not isinstance(row, dict):
                continue
            bnet_id = str(row.get("player_bnet_id") or "").strip()
            if not bnet_id:
                continue
            player_signal.setdefault(bnet_id, False)
            try:
                kda_total = int(row.get("kill") or 0) + int(row.get("assist") or 0) + int(row.get("death") or 0)
            except (TypeError, ValueError):
                kda_total = 0
            if kda_total != 0 or bool(row.get("_has_kad_signal")):
                player_signal[bnet_id] = True

        if len(player_signal) < 10:
            return 0
        if sum(1 for has_signal in player_signal.values() if has_signal) >= 10:
            return 1
        return 0

    def write_match_detail_batch(
        self,
        *,
        hero_detail_rows: Sequence[Dict[str, Any]],
        comp_data_rows: Sequence[Dict[str, Any]],
        perk_pick_rows: Sequence[Dict[str, Any]],
        comp_summary_keys: Iterable[tuple[str, str, Optional[int]]],
        perk_summary_keys: Iterable[tuple[str, int, Optional[int]]],
        match_meta_row: Optional[Dict[str, Any]] = None,
        match_player_rows: Optional[Sequence[Dict[str, Any]]] = None,
    ) -> Dict[str, int]:
        if (
            not hero_detail_rows
            and not comp_data_rows
            and not perk_pick_rows
            and not match_meta_row
            and not match_player_rows
        ):
            return {"hero_details": 0, "comp_data": 0, "perk_picks": 0, "match_meta": 0, "match_players": 0}

        with self._write_lock:
            conn = self._get_write_connection()
            if conn is None:
                return {"hero_details": 0, "comp_data": 0, "perk_picks": 0, "match_meta": 0, "match_players": 0}
            try:
                self._initialize_player_identity_table(conn)
                self._initialize_match_detail_tables(conn)

                # 1. INSERT OR REPLACE match_meta
                match_meta_count = 0
                if match_meta_row:
                    match_id_for_meta = str(match_meta_row.get("match_id") or "")
                    # Decide the frozen flag for this match before overwriting.
                    # frozen is sticky: once a match's player roster is observed
                    # to be unchanged across two writes, it stays frozen (the data
                    # has settled). We compute it by comparing the new roster
                    # (from match_player_rows) against the roster currently in DB.
                    new_frozen = self._compute_match_frozen_flag(
                        conn, match_id_for_meta, match_meta_row, match_player_rows
                    )
                    match_meta_row = dict(match_meta_row)
                    match_meta_row["frozen"] = new_frozen
                    conn.execute(
                        f"""
                        INSERT OR REPLACE INTO {MATCH_META_TABLE} (
                            match_id,
                            match_result,
                            focus_player_side,
                            match_mode,
                            map_guid,
                            start_time,
                            game_time_sec,
                            match_list_json,
                            frozen,
                            last_update
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        tuple(match_meta_row.get(k) for k in MATCH_META_FIELDS),
                    )
                    match_meta_count = 1

                # 2. INSERT OR REPLACE match_player (up to 10 rows per match)
                match_player_count = 0
                if match_player_rows:
                    conn.executemany(
                        f"""
                        INSERT OR REPLACE INTO {MATCH_PLAYER_TABLE} (
                            match_id,
                            player_bnet_id,
                            player_name,
                            side,
                            hero_guid,
                            rank_bucket,
                            role_type,
                            kill,
                            assist,
                            death,
                            hero_damage,
                            healing,
                            damage_blocked,
                            friend_bnet_ids_json,
                            hero_damage_taken,
                            final_hit,
                            solo_kills,
                            target_competing_time,
                            healing_taken,
                            endorse_bnet_ids_json,
                            last_update
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        [
                            tuple(row.get(k) for k in MATCH_PLAYER_FIELDS)
                            for row in match_player_rows
                        ],
                    )
                    match_player_count = len(match_player_rows)

                if hero_detail_rows:
                    conn.executemany(
                        f"""
                        INSERT OR IGNORE INTO {HERO_MATCH_DETAIL_TABLE} (
                            match_id,
                            player_bnet_id,
                            player_name,
                            hero_guid,
                            rank_score,
                            rank_bucket,
                            use_time_sec,
                            use_time_rate,
                            map_guid,
                            start_time,
                            game_time_sec,
                            stat_map_json,
                            last_update
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        [
                            (
                                str(row.get("match_id") or ""),
                                str(row.get("player_bnet_id") or ""),
                                str(row.get("player_name") or ""),
                                str(row.get("hero_guid") or ""),
                                row.get("rank_score"),
                                row.get("rank_bucket"),
                                float(row.get("use_time_sec") or 0.0),
                                float(row.get("use_time_rate") or 0.0),
                                str(row.get("map_guid") or ""),
                                int(row.get("start_time") or 0),
                                int(row.get("game_time_sec") or 0),
                                str(row.get("stat_map_json") or "{}"),
                                int(row.get("last_update") or 0),
                            )
                            for row in hero_detail_rows
                        ],
                    )

                if comp_data_rows:
                    conn.executemany(
                        f"""
                        INSERT OR IGNORE INTO {COMP_DATA_TABLE} (
                            match_id,
                            player_bnet_id,
                            hero_guid,
                            statmap_name,
                            statmap_value,
                            statmap_raw_value,
                            rank_score,
                            rank_bucket,
                            use_time_sec,
                            use_time_rate,
                            last_update
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        [
                            (
                                str(row.get("match_id") or ""),
                                str(row.get("player_bnet_id") or ""),
                                str(row.get("hero_guid") or ""),
                                str(row.get("statmap_name") or ""),
                                float(row.get("statmap_value") or 0.0),
                                float(row.get("statmap_raw_value") or 0.0),
                                row.get("rank_score"),
                                row.get("rank_bucket"),
                                float(row.get("use_time_sec") or 0.0),
                                float(row.get("use_time_rate") or 0.0),
                                int(row.get("last_update") or 0),
                            )
                            for row in comp_data_rows
                        ],
                    )

                if perk_pick_rows:
                    conn.executemany(
                        f"""
                        INSERT OR IGNORE INTO {HERO_PERK_PICK_TABLE} (
                            match_id,
                            player_bnet_id,
                            player_name,
                            hero_guid,
                            perk_guid,
                            perk_level,
                            slot_index,
                            rank_score,
                            rank_bucket,
                            start_time,
                            last_update
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        [
                            (
                                str(row.get("match_id") or ""),
                                str(row.get("player_bnet_id") or ""),
                                str(row.get("player_name") or ""),
                                str(row.get("hero_guid") or ""),
                                str(row.get("perk_guid") or ""),
                                int(row.get("perk_level") or 0),
                                int(row.get("slot_index") or 0),
                                row.get("rank_score"),
                                row.get("rank_bucket"),
                                int(row.get("start_time") or 0),
                                int(row.get("last_update") or 0),
                            )
                            for row in perk_pick_rows
                        ],
                    )

                refresh_ts = int(time.time())
                self._refresh_comp_data_summaries(conn, comp_summary_keys, updated_at=refresh_ts)
                self._refresh_hero_perk_summaries(conn, perk_summary_keys, updated_at=refresh_ts)
                conn.commit()
                return {
                    "hero_details": len(hero_detail_rows),
                    "comp_data": len(comp_data_rows),
                    "perk_picks": len(perk_pick_rows),
                    "match_meta": match_meta_count,
                    "match_players": match_player_count,
                }
            except Exception as exc:
                self._warn_once(f"match stats sqlite write_match_detail_batch failed: {type(exc).__name__}: {exc}")
                return {
                    "hero_details": 0,
                    "comp_data": 0,
                    "perk_picks": 0,
                    "match_meta": 0,
                    "match_players": 0,
                }
            finally:
                try:
                    conn.close()
                except Exception:
                    pass

    # ------------------------------------------------------------------
    # match_list batch write (from MatchListRecorder, INSERT OR IGNORE)
    # ------------------------------------------------------------------

    def write_match_list_batch(self, rows: Sequence[tuple]) -> int:
        """Batch INSERT OR IGNORE match_meta rows from queryMatchList.

        Each row is a tuple aligned with MATCH_META_FIELDS.
        Uses INSERT OR IGNORE so queryMatchInfo data (more complete) is preserved.
        """
        if not rows:
            return 0
        with self._write_lock:
            conn = self._get_write_connection()
            if conn is None:
                return 0
            try:
                self._initialize_match_detail_tables(conn)
                placeholders = ",".join(["?"] * len(MATCH_META_FIELDS))
                conn.executemany(
                    f"INSERT OR IGNORE INTO {MATCH_META_TABLE} "
                    f"({','.join(MATCH_META_FIELDS)}) VALUES ({placeholders})",
                    rows,
                )
                conn.commit()
                return len(rows)
            except Exception as exc:
                self._warn_once(
                    f"match stats sqlite write_match_list_batch failed: "
                    f"{type(exc).__name__}: {exc}"
                )
                return 0
            finally:
                try:
                    conn.close()
                except Exception:
                    pass

    def write_match_list_page_cache_batch(self, rows: Sequence[Dict[str, Any]]) -> int:
        """Upsert raw queryMatchList page payloads for observability and reuse."""
        if not rows:
            return 0
        normalized_rows = []
        for row in rows or []:
            source_kind = str(row.get("source_kind") or "normal").strip() or "normal"
            customer_token = str(row.get("customer_token") or "").strip()
            game_mode = str(row.get("game_mode") or "").strip()
            season_key = str(row.get("season_key") or "current").strip() or "current"
            try:
                page = int(row.get("page") or 0)
            except (TypeError, ValueError):
                page = 0
            if not customer_token or not game_mode or page <= 0:
                continue
            normalized_rows.append(
                (
                    source_kind,
                    customer_token,
                    game_mode,
                    season_key,
                    page,
                    str(row.get("payload_json") or "{}"),
                    str(row.get("match_ids_json") or "[]"),
                    int(row.get("entry_count") or 0),
                    int(row.get("fetched_at") or int(time.time())),
                    str(row.get("stop_reason") or ""),
                )
            )
        if not normalized_rows:
            return 0
        with self._write_lock:
            conn = self._get_write_connection()
            if conn is None:
                return 0
            try:
                self._initialize_match_detail_tables(conn)
                conn.executemany(
                    f"""
                    INSERT INTO {MATCH_LIST_PAGE_CACHE_TABLE}
                        (source_kind, customer_token, game_mode, season_key, page,
                         payload_json, match_ids_json, entry_count, fetched_at, stop_reason)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(source_kind, customer_token, game_mode, season_key, page)
                    DO UPDATE SET
                        payload_json = excluded.payload_json,
                        match_ids_json = excluded.match_ids_json,
                        entry_count = excluded.entry_count,
                        fetched_at = excluded.fetched_at,
                        stop_reason = excluded.stop_reason
                    """,
                    normalized_rows,
                )
                conn.commit()
                return len(normalized_rows)
            except Exception as exc:
                self._warn_once(
                    f"match stats sqlite write_match_list_page_cache_batch failed: "
                    f"{type(exc).__name__}: {exc}"
                )
                return 0
            finally:
                try:
                    conn.close()
                except Exception:
                    pass

    def update_match_list_page_stop_reason(
        self,
        *,
        source_kind: str,
        customer_token: str,
        game_mode: str,
        season_key: str,
        page: int,
        stop_reason: str,
    ) -> bool:
        normalized_reason = str(stop_reason or "").strip()
        if not normalized_reason:
            return False
        with self._write_lock:
            conn = self._get_write_connection()
            if conn is None:
                return False
            try:
                self._initialize_match_detail_tables(conn)
                conn.execute(
                    f"""
                    INSERT INTO {MATCH_LIST_PAGE_CACHE_TABLE}
                        (source_kind, customer_token, game_mode, season_key, page,
                         payload_json, match_ids_json, entry_count, fetched_at, stop_reason)
                    VALUES (?, ?, ?, ?, ?, '{{}}', '[]', 0, ?, ?)
                    ON CONFLICT(source_kind, customer_token, game_mode, season_key, page)
                    DO UPDATE SET stop_reason = excluded.stop_reason
                    """,
                    (
                        str(source_kind or "normal"),
                        str(customer_token or ""),
                        str(game_mode or ""),
                        str(season_key or "current"),
                        int(page),
                        int(time.time()),
                        normalized_reason,
                    ),
                )
                conn.commit()
                return True
            except Exception as exc:
                self._warn_once(
                    f"match stats sqlite update_match_list_page_stop_reason failed: "
                    f"{type(exc).__name__}: {exc}"
                )
                return False
            finally:
                try:
                    conn.close()
                except Exception:
                    pass

    def get_match_list_page_cache(
        self,
        *,
        source_kind: str,
        customer_token: str,
        game_mode: str,
        season_key: str,
        page: int,
        max_age_sec: int = 600,
    ) -> Optional[Dict[str, Any]]:
        """Read a cached raw queryMatchList page payload when it is still fresh."""
        normalized_token = str(customer_token or "").strip()
        normalized_mode = str(game_mode or "").strip()
        if not normalized_token or not normalized_mode:
            return None
        try:
            normalized_page = int(page)
        except (TypeError, ValueError):
            return None
        if normalized_page <= 0:
            return None
        conn = self._get_connection()
        if conn is None:
            return None
        try:
            cursor = conn.cursor()
            try:
                cursor.execute(
                    f"""
                    SELECT payload_json, match_ids_json, entry_count, fetched_at, stop_reason
                    FROM {MATCH_LIST_PAGE_CACHE_TABLE}
                    WHERE source_kind = ? AND customer_token = ? AND game_mode = ?
                      AND season_key = ? AND page = ?
                    """,
                    (
                        str(source_kind or "normal").strip() or "normal",
                        normalized_token,
                        normalized_mode,
                        str(season_key or "current").strip() or "current",
                        normalized_page,
                    ),
                )
                row = cursor.fetchone()
            finally:
                cursor.close()
            if not row:
                return None
            fetched_at = int(row[3] or 0)
            if max_age_sec > 0 and fetched_at > 0 and int(time.time()) - fetched_at > int(max_age_sec):
                return None
            try:
                payload = json.loads(str(row[0] or "{}"))
            except Exception:
                payload = {}
            try:
                match_ids = json.loads(str(row[1] or "[]"))
            except Exception:
                match_ids = []
            return {
                "payload": payload,
                "match_ids": match_ids,
                "entry_count": int(row[2] or 0),
                "fetched_at": fetched_at,
                "stop_reason": str(row[4] or ""),
            }
        except Exception as exc:
            self._warn_once(
                f"match stats sqlite get_match_list_page_cache failed: "
                f"{type(exc).__name__}: {exc}"
            )
            return None
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def get_match_list_page_cache_entries(
        self,
        *,
        source_kind: str,
        customer_token: str,
        game_mode: str,
        season_keys: Sequence[str],
        max_age_sec: int = 600,
        max_pages: int = 120,
    ) -> List[Dict[str, Any]]:
        """Return cached queryMatchList entries for one user/mode across pages."""
        normalized_token = str(customer_token or "").strip()
        normalized_mode = str(game_mode or "").strip()
        normalized_source = str(source_kind or "normal").strip() or "normal"
        keys = [str(key or "current").strip() or "current" for key in (season_keys or [])]
        keys = list(dict.fromkeys(keys))
        if not normalized_token or not normalized_mode or not keys:
            return []
        conn = self._get_connection()
        if conn is None:
            return []
        try:
            placeholders = ",".join(["?"] * len(keys))
            cursor = conn.cursor()
            try:
                cursor.execute(
                    f"""
                    SELECT payload_json, fetched_at, page, season_key
                    FROM {MATCH_LIST_PAGE_CACHE_TABLE}
                    WHERE source_kind = ? AND customer_token = ? AND game_mode = ?
                      AND season_key IN ({placeholders})
                    ORDER BY season_key ASC, page ASC
                    LIMIT ?
                    """,
                    (normalized_source, normalized_token, normalized_mode, *keys, int(max_pages)),
                )
                rows = cursor.fetchall() or []
            finally:
                cursor.close()
            now_ts = int(time.time())
            entries: List[Dict[str, Any]] = []
            for row in rows:
                fetched_at = int(row[1] or 0)
                if max_age_sec > 0 and fetched_at > 0 and now_ts - fetched_at > int(max_age_sec):
                    continue
                try:
                    payload = json.loads(str(row[0] or "{}"))
                except Exception:
                    payload = {}
                data = payload.get("data", payload) if isinstance(payload, dict) else {}
                raw_entries: Any = []
                if isinstance(data, list):
                    raw_entries = data
                elif isinstance(data, dict):
                    for key in ("matchList", "recentMatchList"):
                        value = data.get(key)
                        if isinstance(value, list):
                            raw_entries = value
                            break
                for entry in raw_entries or []:
                    if not isinstance(entry, dict):
                        continue
                    item = dict(entry)
                    item.setdefault("gameMode", normalized_mode)
                    item["_dashenSeasonKey"] = str(row[3] or "")
                    item["_matchListCachePage"] = int(row[2] or 0)
                    entries.append(item)
            return entries
        except Exception as exc:
            self._warn_once(
                f"match stats sqlite get_match_list_page_cache_entries failed: "
                f"{type(exc).__name__}: {exc}"
            )
            return []
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def get_match_list_last_fetched_at(
        self,
        *,
        customer_token: str,
        source_kinds: Optional[Sequence[str]] = None,
    ) -> int:
        """Return the most recent cached queryMatchList fetch timestamp for one owner."""
        normalized_token = str(customer_token or "").strip()
        if not normalized_token:
            return 0
        kinds = [str(kind or "").strip() for kind in (source_kinds or []) if str(kind or "").strip()]
        conn = self._get_connection()
        if conn is None:
            return 0
        try:
            if not self._table_exists(conn, MATCH_LIST_PAGE_CACHE_TABLE):
                return 0
            cursor = conn.cursor()
            try:
                if kinds:
                    placeholders = ",".join(["?"] * len(kinds))
                    cursor.execute(
                        f"""
                        SELECT MAX(fetched_at)
                        FROM {MATCH_LIST_PAGE_CACHE_TABLE}
                        WHERE customer_token = ? AND source_kind IN ({placeholders})
                        """,
                        (normalized_token, *kinds),
                    )
                else:
                    cursor.execute(
                        f"""
                        SELECT MAX(fetched_at)
                        FROM {MATCH_LIST_PAGE_CACHE_TABLE}
                        WHERE customer_token = ?
                        """,
                        (normalized_token,),
                    )
                row = cursor.fetchone()
            finally:
                cursor.close()
            if not row or row[0] in (None, ""):
                return 0
            return max(0, int(row[0] or 0))
        except Exception as exc:
            self._warn_once(
                f"match stats sqlite get_match_list_last_fetched_at failed: "
                f"{type(exc).__name__}: {exc}"
            )
            return 0
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def get_all_match_ids(self) -> set[str]:
        """Return a snapshot of all known match ids in match_meta."""
        conn = self._get_connection()
        if conn is None:
            return set()
        try:
            cursor = conn.cursor()
            try:
                cursor.execute(f"SELECT match_id FROM {MATCH_META_TABLE}")
                rows = cursor.fetchall() or []
            finally:
                cursor.close()
            return {str(row[0] or "") for row in rows if str(row[0] or "").strip()}
        except Exception as exc:
            self._warn_once(f"match stats sqlite get_all_match_ids failed: {type(exc).__name__}: {exc}")
            return set()
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def get_player_match_ids(self, bnet_id: str, limit: int = 1000) -> set[str]:
        """Return match_ids for a specific player from match_player table.

        Uses the existing idx_match_player_bnet index for efficient lookup.
        Returns at most *limit* match ids (default 1000).
        """
        normalized = str(bnet_id or "").strip()
        if not normalized:
            return set()
        conn = self._get_connection()
        if conn is None:
            return set()
        try:
            cursor = conn.cursor()
            try:
                cursor.execute(
                    f"SELECT DISTINCT match_id FROM {MATCH_PLAYER_TABLE} "
                    f"WHERE player_bnet_id = ? LIMIT ?",
                    (normalized, int(limit)),
                )
                rows = cursor.fetchall() or []
            finally:
                cursor.close()
            return {str(row[0] or "") for row in rows if str(row[0] or "").strip()}
        except Exception as exc:
            self._warn_once(
                f"match stats sqlite get_player_match_ids failed: "
                f"{type(exc).__name__}: {exc}"
            )
            return set()
        finally:
            try:
                conn.close()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # token → bnet_id resolution (for CountInfoRecorder)
    # ------------------------------------------------------------------

    def resolve_bnet_id_by_token(self, customer_token: str) -> str:
        """Resolve a customer_token to a bnet_id.

        Strategy: the customer_token is the Dashen token associated with a
        specific player account.  We look up the most recent match_meta row
        joined with match_player to find the focus player's bnet_id.
        Falls back to scanning player_identity_map if needed.
        """
        token = str(customer_token or "").strip()
        if not token:
            return ""
        conn = self._get_connection()
        if conn is None:
            return ""
        try:
            cursor = conn.cursor()
            try:
                # Strategy 1: find the most recent match_player entry whose
                # friend_bnet_ids_json or endorse_bnet_ids_json references the
                # token (these JSON blobs contain related player identifiers).
                escaped = self._escape_like_pattern(token)
                cursor.execute(
                    f"SELECT player_bnet_id FROM {MATCH_PLAYER_TABLE} "
                    f"WHERE friend_bnet_ids_json LIKE ? ESCAPE '!' "
                    f"OR endorse_bnet_ids_json LIKE ? ESCAPE '!' "
                    f"ORDER BY rowid DESC LIMIT 1",
                    (f"%{escaped}%", f"%{escaped}%"),
                )
                row = cursor.fetchone()
                if row:
                    return str(row[0] or "")

                # Strategy 2: scan player_identity_map for a matching bnetid
                # (the token value itself is sometimes used as a lookup key).
                cursor.execute(
                    f"SELECT bnetid FROM {PLAYER_IDENTITY_TABLE} "
                    f"WHERE bnetid = ? OR battletag LIKE ? ESCAPE '!' "
                    f"ORDER BY update_time DESC LIMIT 1",
                    (token, f"%{escaped}%"),
                )
                id_row = cursor.fetchone()
                if id_row:
                    return str(id_row[0] or "")
                return ""
            finally:
                cursor.close()
        except Exception as exc:
            self._warn_once(
                f"match stats resolve_bnet_id_by_token failed: "
                f"{type(exc).__name__}: {exc}"
            )
            return ""
        finally:
            try:
                conn.close()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # match_meta / match_player read helpers (for summary DB fallback)
    # ------------------------------------------------------------------

    @staticmethod
    def get_player_result(meta: Dict[str, Any], player_side: str) -> int:
        """Convert match_result (focus-player-relative) to the given player's side.

        If the player is on the same side as the focus player, the result is
        identical; otherwise it is inverted.
        """
        try:
            match_result = int(meta.get("match_result") or 0)
        except (TypeError, ValueError):
            return 0
        focus_side = str(meta.get("focus_player_side") or "team").strip()
        if focus_side == str(player_side or "").strip():
            return match_result
        return -match_result

    def get_match_meta(self, match_ids: Sequence[str]) -> Dict[str, Dict[str, Any]]:
        """Return {match_id: {match_result, focus_player_side, match_mode, ...}}."""
        if not match_ids:
            return {}
        conn = self._get_connection()
        if conn is None:
            return {}
        try:
            placeholders = ",".join(["?"] * len(match_ids))
            cursor = conn.cursor()
            try:
                cursor.execute(
                    f"""
                    SELECT match_id, match_result, focus_player_side, match_mode,
                           map_guid, start_time, game_time_sec, match_list_json,
                           frozen, last_update
                    FROM {MATCH_META_TABLE}
                    WHERE match_id IN ({placeholders})
                    """,
                    tuple(str(mid) for mid in match_ids),
                )
                rows = cursor.fetchall() or []
            finally:
                cursor.close()
            result: Dict[str, Dict[str, Any]] = {}
            for row in rows:
                result[str(row[0])] = {
                    "match_id": str(row[0]),
                    "match_result": row[1],
                    "focus_player_side": str(row[2] or "team"),
                    "match_mode": str(row[3] or ""),
                    "map_guid": str(row[4] or ""),
                    "start_time": int(row[5] or 0),
                    "game_time_sec": int(row[6] or 0),
                    "match_list_json": row[7],
                    "frozen": int(row[8] or 0),
                    "last_update": int(row[9] or 0),
                }
            return result
        except Exception as exc:
            self._warn_once(f"match stats sqlite get_match_meta failed: {type(exc).__name__}: {exc}")
            return {}
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def get_match_players(
        self, match_ids: Sequence[str]
    ) -> Dict[str, List[Dict[str, Any]]]:
        """Return {match_id: [{player_bnet_id, side, hero_guid, kill, death, ...}]}.

        Used by summary DB fallback to reconstruct teammateList / enemyList.
        """
        if not match_ids:
            return {}
        conn = self._get_connection()
        if conn is None:
            return {}
        try:
            placeholders = ",".join(["?"] * len(match_ids))
            cursor = conn.cursor()
            try:
                cursor.execute(
                    f"""
                    SELECT match_id, player_bnet_id, player_name, side, hero_guid,
                           rank_bucket, role_type, kill, assist, death,
                           hero_damage, healing, damage_blocked, friend_bnet_ids_json,
                           hero_damage_taken, final_hit, solo_kills,
                           target_competing_time, healing_taken, endorse_bnet_ids_json
                    FROM {MATCH_PLAYER_TABLE}
                    WHERE match_id IN ({placeholders})
                    ORDER BY match_id, side, player_bnet_id
                    """,
                    tuple(str(mid) for mid in match_ids),
                )
                rows = cursor.fetchall() or []
            finally:
                cursor.close()
            result: Dict[str, List[Dict[str, Any]]] = {str(mid): [] for mid in match_ids}
            for row in rows:
                match_id = str(row[0])
                result.setdefault(match_id, []).append(
                    {
                        "match_id": match_id,
                        "player_bnet_id": str(row[1]),
                        "player_name": str(row[2] or ""),
                        "side": str(row[3] or ""),
                        "hero_guid": str(row[4] or ""),
                        "rank_bucket": row[5],
                        "role_type": str(row[6] or ""),
                        "kill": int(row[7] or 0),
                        "assist": int(row[8] or 0),
                        "death": int(row[9] or 0),
                        "hero_damage": int(row[10] or 0),
                        "healing": int(row[11] or 0),
                        "damage_blocked": int(row[12] or 0),
                        "friend_bnet_ids_json": row[13],
                        "hero_damage_taken": int(row[14] or 0),
                        "final_hit": int(row[15] or 0),
                        "solo_kills": int(row[16] or 0),
                        "target_competing_time": float(row[17] or 0),
                        "healing_taken": int(row[18] or 0),
                        "endorse_bnet_ids_json": row[19],
                    }
                )
            return result
        except Exception as exc:
            self._warn_once(f"match stats sqlite get_match_players failed: {type(exc).__name__}: {exc}")
            return {}
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def get_match_details_by_player(
        self,
        player_bnet_id: str,
        *,
        since_ts: int = 0,
        limit: int = 500,
    ) -> List[Dict[str, Any]]:
        """Return a player's match list from DB (JOIN match_meta + match_player).

        Each entry includes match_result already converted to the player's perspective.
        Used by summary DB fallback to avoid re-fetching from API.
        """
        normalized_bnet = str(player_bnet_id or "").strip()
        if not normalized_bnet:
            return []
        conn = self._get_connection()
        if conn is None:
            return []
        try:
            cursor = conn.cursor()
            try:
                if since_ts > 0:
                    cursor.execute(
                        f"""
                        SELECT m.match_id, m.match_result, m.focus_player_side, m.match_mode,
                               m.map_guid, m.start_time, m.game_time_sec, m.match_list_json,
                               p.side
                        FROM {MATCH_META_TABLE} m
                        JOIN {MATCH_PLAYER_TABLE} p ON m.match_id = p.match_id
                        WHERE p.player_bnet_id = ? AND m.start_time >= ?
                        ORDER BY m.start_time DESC
                        LIMIT ?
                        """,
                        (normalized_bnet, int(since_ts), int(limit)),
                    )
                else:
                    cursor.execute(
                        f"""
                        SELECT m.match_id, m.match_result, m.focus_player_side, m.match_mode,
                               m.map_guid, m.start_time, m.game_time_sec, m.match_list_json,
                               p.side
                        FROM {MATCH_META_TABLE} m
                        JOIN {MATCH_PLAYER_TABLE} p ON m.match_id = p.match_id
                        WHERE p.player_bnet_id = ?
                        ORDER BY m.start_time DESC
                        LIMIT ?
                        """,
                        (normalized_bnet, int(limit)),
                    )
                rows = cursor.fetchall() or []
            finally:
                cursor.close()
            results: List[Dict[str, Any]] = []
            for row in rows:
                meta = {
                    "match_result": row[1],
                    "focus_player_side": str(row[2] or "team"),
                }
                player_side = str(row[8] or "team")
                results.append(
                    {
                        "match_id": str(row[0]),
                        "match_result": self.get_player_result(meta, player_side),
                        "match_mode": str(row[3] or ""),
                        "map_guid": str(row[4] or ""),
                        "start_time": int(row[5] or 0),
                        "game_time_sec": int(row[6] or 0),
                        "match_list_json": row[7],
                        "player_side": player_side,
                    }
                )
            return results
        except Exception as exc:
            self._warn_once(f"match stats sqlite get_match_details_by_player failed: {type(exc).__name__}: {exc}")
            return []
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def get_match_list_entries_by_player(
        self,
        player_bnet_id: str,
        *,
        since_ts_ms: int = 0,
        limit: int = 500,
    ) -> List[Dict[str, Any]]:
        """Rebuild match-list-like entries from DB for a specific player."""
        normalized_bnet = str(player_bnet_id or "").strip()
        if not normalized_bnet:
            return []
        since_sec = int(int(since_ts_ms or 0) / 1000) if int(since_ts_ms or 0) > 0 else 0
        conn = self._get_connection()
        if conn is None:
            return []
        try:
            cursor = conn.cursor()
            try:
                if since_sec > 0:
                    cursor.execute(
                        f"""
                        SELECT m.match_id, m.match_result, m.focus_player_side, m.match_mode,
                               m.map_guid, m.start_time, m.match_list_json, p.side
                        FROM {MATCH_META_TABLE} m
                        JOIN {MATCH_PLAYER_TABLE} p ON m.match_id = p.match_id
                        WHERE p.player_bnet_id = ? AND m.start_time >= ?
                        ORDER BY m.start_time DESC
                        LIMIT ?
                        """,
                        (normalized_bnet, since_sec, int(limit)),
                    )
                else:
                    cursor.execute(
                        f"""
                        SELECT m.match_id, m.match_result, m.focus_player_side, m.match_mode,
                               m.map_guid, m.start_time, m.match_list_json, p.side
                        FROM {MATCH_META_TABLE} m
                        JOIN {MATCH_PLAYER_TABLE} p ON m.match_id = p.match_id
                        WHERE p.player_bnet_id = ?
                        ORDER BY m.start_time DESC
                        LIMIT ?
                        """,
                        (normalized_bnet, int(limit)),
                    )
                rows = cursor.fetchall() or []
            finally:
                cursor.close()
            result: List[Dict[str, Any]] = []
            for row in rows:
                item: Dict[str, Any] = {}
                raw_json = row[6]
                if raw_json:
                    try:
                        parsed = json.loads(str(raw_json))
                    except Exception:
                        parsed = {}
                    if isinstance(parsed, dict):
                        item.update(parsed)
                meta = {
                    "match_result": row[1],
                    "focus_player_side": str(row[2] or "team"),
                }
                start_time = int(row[5] or 0)
                item["matchId"] = str(row[0] or item.get("matchId") or "")
                item["matchRet"] = self.get_player_result(meta, str(row[7] or "team"))
                item["gameMode"] = str(item.get("gameMode") or row[3] or "")
                item["mapGuid"] = str(item.get("mapGuid") or row[4] or "")
                if start_time > 0:
                    item["beginTs"] = int(item.get("beginTs") or start_time * 1000)
                if item.get("matchId") and item.get("beginTs"):
                    result.append(item)
            return result
        except Exception as exc:
            self._warn_once(
                f"match stats sqlite get_match_list_entries_by_player failed: "
                f"{type(exc).__name__}: {exc}"
            )
            return []
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def upsert_player_identity_records(self, rows: Iterable[Dict[str, Any]]) -> int:
        normalized_rows: Dict[str, tuple[str, str, str, str, int]] = {}
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            bnetid = str(row.get("bnetid") or row.get("bnetId") or row.get("bnet_id") or "").strip()
            battletag = str(row.get("battletag") or row.get("battleTag") or "").replace("＃", "#").strip()
            battlename = str(row.get("battlename") or row.get("battleName") or "").strip()
            battlenum = str(row.get("battlenum") or row.get("battleNum") or "").strip()
            if not battletag and battlename:
                battletag = battlename if not battlenum else f"{battlename}#{battlenum}"
            if not battlename and battletag:
                if "#" in battletag:
                    battlename, battlenum = battletag.rsplit("#", 1)
                    battlename = battlename.strip() or battletag
                    battlenum = battlenum.strip()
                else:
                    battlename = battletag
            if not bnetid or not battletag or not battlename:
                continue
            try:
                update_time = int(row.get("update_time") or time.time())
            except (TypeError, ValueError):
                update_time = int(time.time())
            normalized_rows[bnetid] = (bnetid, battletag, battlename, battlenum, update_time)

        if not normalized_rows:
            return 0

        with self._write_lock:
            conn = self._get_write_connection()
            if conn is None:
                return 0
            try:
                self._initialize_player_identity_table(conn)
                conn.executemany(
                    f"""
                    INSERT INTO {PLAYER_IDENTITY_TABLE} (
                        bnetid,
                        battletag,
                        battlename,
                        battlenum,
                        update_time
                    ) VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(bnetid) DO UPDATE SET
                        battletag = excluded.battletag,
                        battlename = excluded.battlename,
                        battlenum = excluded.battlenum,
                        update_time = excluded.update_time
                    """,
                    list(normalized_rows.values()),
                )
                conn.commit()
                return len(normalized_rows)
            except Exception as exc:
                self._warn_once(f"match stats sqlite upsert_player_identity_records failed: {type(exc).__name__}: {exc}")
                return 0
            finally:
                try:
                    conn.close()
                except Exception:
                    pass

    def search_player_identity_by_bnet_id(
        self,
        bnet_id: str,
        *,
        limit: int = 10,
        exact_only: bool = False,
    ) -> List[Dict[str, Any]]:
        normalized_bnet_id = str(bnet_id or "").strip()
        if not normalized_bnet_id:
            return []

        try:
            normalized_limit = max(1, int(limit or 10))
        except (TypeError, ValueError):
            normalized_limit = 10

        conn = self._get_connection()
        if conn is None:
            return []

        escaped_bnet_id = self._escape_like_pattern(normalized_bnet_id)
        prefix_pattern = f"{escaped_bnet_id}%"
        contains_pattern = f"%{escaped_bnet_id}%"

        try:
            cursor = conn.cursor()
            try:
                if exact_only:
                    cursor.execute(
                        f"""
                        SELECT
                            bnetid,
                            battletag,
                            battlename,
                            battlenum,
                            update_time,
                            'exact' AS match_type
                        FROM {PLAYER_IDENTITY_TABLE}
                        WHERE bnetid = ?
                        ORDER BY update_time DESC, bnetid ASC
                        LIMIT ?
                        """,
                        (normalized_bnet_id, normalized_limit),
                    )
                else:
                    cursor.execute(
                        f"""
                        SELECT
                            bnetid,
                            battletag,
                            battlename,
                            battlenum,
                            update_time,
                            CASE
                                WHEN bnetid = ? THEN 'exact'
                                WHEN bnetid LIKE ? ESCAPE '!' THEN 'prefix'
                                ELSE 'contains'
                            END AS match_type
                        FROM {PLAYER_IDENTITY_TABLE}
                        WHERE
                            bnetid = ?
                            OR bnetid LIKE ? ESCAPE '!'
                            OR bnetid LIKE ? ESCAPE '!'
                        ORDER BY
                            CASE
                                WHEN bnetid = ? THEN 0
                                WHEN bnetid LIKE ? ESCAPE '!' THEN 1
                                ELSE 2
                            END ASC,
                            update_time DESC,
                            bnetid ASC
                        LIMIT ?
                        """,
                        (
                            normalized_bnet_id,
                            prefix_pattern,
                            normalized_bnet_id,
                            prefix_pattern,
                            contains_pattern,
                            normalized_bnet_id,
                            prefix_pattern,
                            normalized_limit,
                        ),
                    )
                rows = cursor.fetchall() or []
            finally:
                cursor.close()

            return [
                {
                    "bnetid": row[0],
                    "battletag": row[1],
                    "battlename": row[2],
                    "battlenum": row[3],
                    "update_time": int(row[4] or 0),
                    "match_type": row[5],
                }
                for row in rows
            ]
        except Exception as exc:
            self._warn_once(f"match stats sqlite search_player_identity_by_bnet_id failed: {type(exc).__name__}: {exc}")
            return []
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def get_match_strength(self, match_ids: Sequence[str]) -> Dict[str, float]:
        """Read cached avg_score for given match_ids from match_strength_cache.

        Also attempts to compute from match_player + player_competitive_rank
        when match_strength_cache has no data (backward-compatible).
        Returns {match_id: avg_score}.
        """
        if not match_ids:
            return {}
        conn = self._get_connection()
        if conn is None:
            return {}
        try:
            placeholders = ",".join(["?"] * len(match_ids))
            cursor = conn.cursor()
            try:
                # Tier A: read from match_strength_cache (legacy)
                cursor.execute(
                    f"SELECT match_id, avg_score FROM {MATCH_STRENGTH_CACHE_TABLE} "
                    f"WHERE match_id IN ({placeholders}) AND avg_score > 0",
                    tuple(str(mid) for mid in match_ids),
                )
                rows = cursor.fetchall() or []
                result = {
                    str(r[0]): float(r[1])
                    for r in rows
                    if r[0] and float(r[1] or 0) > 0
                }
                if result:
                    return result

                # Tier B: compute from match_player + player_competitive_rank
                cursor.execute(
                    f"""
                    SELECT mp.match_id,
                           AVG(CAST(pcr.rank_score AS REAL)) AS avg_score
                    FROM {MATCH_PLAYER_TABLE} mp
                    JOIN {PLAYER_COMPETITIVE_RANK_TABLE} pcr
                      ON mp.player_bnet_id = pcr.player_bnet_id
                      AND mp.role_type = pcr.role_type
                    WHERE mp.match_id IN ({placeholders})
                      AND pcr.rank_score > 0
                    GROUP BY mp.match_id
                    HAVING avg_score > 0
                    """,
                    tuple(str(mid) for mid in match_ids),
                )
                computed_rows = cursor.fetchall() or []
            finally:
                cursor.close()
            return {
                str(r[0]): float(r[1])
                for r in computed_rows
                if r[0] and float(r[1] or 0) > 0
            }
        except Exception as exc:
            self._warn_once(
                f"match stats sqlite get_match_strength failed: "
                f"{type(exc).__name__}: {exc}"
            )
            return {}
        finally:
            try:
                conn.close()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # player_competitive_rank: per-player per-role competitive rank score
    # ------------------------------------------------------------------

    def upsert_player_competitive_ranks(
        self,
        records: Sequence[Dict[str, Any]],
        *,
        cache_week: str = "",
        checked_at: Optional[int] = None,
    ) -> int:
        """Batch upsert player competitive rank records. Returns count of rows written."""
        if not records:
            return 0
        with self._write_lock:
            conn = self._get_write_connection()
            if conn is None:
                return 0
            try:
                self._initialize_match_detail_tables(conn)
                now = int(checked_at or time.time())
                rows = [
                    (
                        str(r.get("player_bnet_id") or ""),
                        str(r.get("role_type") or ""),
                        int(r.get("rank_score") or 0),
                        r.get("season"),
                        str(r.get("source_match_id") or ""),
                        str(r.get("cache_week") or cache_week or ""),
                        int(r.get("checked_at") or now),
                        now,
                    )
                    for r in records
                    if r.get("player_bnet_id")
                    and r.get("role_type")
                    and int(r.get("rank_score") or 0) > 0
                ]
                if not rows:
                    return 0
                conn.executemany(
                    f"""
                    INSERT OR REPLACE INTO {PLAYER_COMPETITIVE_RANK_TABLE}
                        (player_bnet_id, role_type, rank_score, season, source_match_id,
                         cache_week, checked_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    rows,
                )
                conn.commit()
                return len(rows)
            except Exception as exc:
                self._warn_once(f"match stats sqlite upsert_player_competitive_ranks failed: {type(exc).__name__}: {exc}")
                return 0
            finally:
                try:
                    conn.close()
                except Exception:
                    pass

    def get_player_competitive_ranks(
        self,
        player_bnet_ids: Sequence[str],
        cache_week: str = "",
    ) -> Dict[str, Dict[str, int]]:
        """Read competitive ranks for given players. Returns {bnet_id: {role_type: rank_score}}."""
        records = self.get_player_competitive_rank_records(player_bnet_ids, cache_week=cache_week)
        return {
            player_id: {
                role: int(data.get("rank_score") or 0)
                for role, data in role_map.items()
                if int(data.get("rank_score") or 0) > 0
            }
            for player_id, role_map in records.items()
        }

    def get_player_competitive_rank_records(
        self,
        player_bnet_ids: Sequence[str],
        *,
        cache_week: str = "",
    ) -> Dict[str, Dict[str, Dict[str, Any]]]:
        """Read competitive rank records with metadata by player and role."""
        if not player_bnet_ids:
            return {}
        conn = self._get_connection()
        if conn is None:
            return {}
        try:
            placeholders = ",".join(["?"] * len(player_bnet_ids))
            cursor = conn.cursor()
            try:
                params: List[Any] = [str(pid) for pid in player_bnet_ids]
                week_clause = ""
                if cache_week:
                    week_clause = " AND cache_week = ?"
                    params.append(str(cache_week))
                cursor.execute(
                    f"""
                    SELECT player_bnet_id, role_type, rank_score, season, source_match_id,
                           cache_week, checked_at, updated_at
                    FROM {PLAYER_COMPETITIVE_RANK_TABLE}
                    WHERE player_bnet_id IN ({placeholders}) AND rank_score > 0{week_clause}
                    """,
                    tuple(params),
                )
                rows = cursor.fetchall() or []
            finally:
                cursor.close()
            result: Dict[str, Dict[str, Dict[str, Any]]] = {}
            for row in rows:
                pid = str(row[0])
                role = str(row[1])
                score = int(row[2])
                if score > 0:
                    result.setdefault(pid, {})[role] = {
                        "rank_score": score,
                        "season": row[3],
                        "source_match_id": str(row[4] or ""),
                        "cache_week": str(row[5] or ""),
                        "checked_at": int(row[6] or 0),
                        "updated_at": int(row[7] or 0),
                    }
            return result
        except Exception as exc:
            self._warn_once(f"match stats sqlite get_player_competitive_rank_records failed: {type(exc).__name__}: {exc}")
            return {}
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def get_player_competitive_rank_fetch_markers(
        self,
        player_bnet_ids: Sequence[str],
        *,
        cache_week: str,
        game_mode: str = "sport",
    ) -> set[str]:
        if not player_bnet_ids or not cache_week:
            return set()
        conn = self._get_connection()
        if conn is None:
            return set()
        try:
            placeholders = ",".join(["?"] * len(player_bnet_ids))
            cursor = conn.cursor()
            try:
                cursor.execute(
                    f"""
                    SELECT player_bnet_id FROM {PLAYER_COMPETITIVE_RANK_FETCH_TABLE}
                    WHERE player_bnet_id IN ({placeholders}) AND cache_week = ? AND game_mode = ?
                    """,
                    tuple(str(pid) for pid in player_bnet_ids) + (str(cache_week), str(game_mode or "sport")),
                )
                rows = cursor.fetchall() or []
            finally:
                cursor.close()
            return {str(row[0] or "") for row in rows if str(row[0] or "").strip()}
        except Exception as exc:
            self._warn_once(
                f"match stats sqlite get_player_competitive_rank_fetch_markers failed: "
                f"{type(exc).__name__}: {exc}"
            )
            return set()
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def upsert_player_competitive_rank_snapshot(
        self,
        player_bnet_id: str,
        *,
        cache_week: str,
        game_mode: str = "sport",
        role_rank_records: Sequence[Dict[str, Any]] = (),
        checked_at: Optional[int] = None,
    ) -> int:
        """Write a player's weekly rank snapshot and fetch marker in one transaction."""
        normalized_player_id = str(player_bnet_id or "").strip()
        normalized_week = str(cache_week or "").strip()
        if not normalized_player_id or not normalized_week:
            return 0
        now = int(checked_at or time.time())
        rows = []
        for record in role_rank_records or []:
            role_type = str(record.get("role_type") or "").strip()
            try:
                rank_score = int(record.get("rank_score") or 0)
            except (TypeError, ValueError):
                rank_score = 0
            if not role_type or rank_score <= 0:
                continue
            rows.append(
                (
                    normalized_player_id,
                    role_type,
                    rank_score,
                    record.get("season"),
                    str(record.get("source_match_id") or ""),
                    normalized_week,
                    now,
                    now,
                )
            )
        with self._write_lock:
            conn = self._get_write_connection()
            if conn is None:
                return 0
            try:
                self._initialize_match_detail_tables(conn)
                if rows:
                    conn.executemany(
                        f"""
                        INSERT OR REPLACE INTO {PLAYER_COMPETITIVE_RANK_TABLE}
                            (player_bnet_id, role_type, rank_score, season, source_match_id,
                             cache_week, checked_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        rows,
                    )
                conn.execute(
                    f"""
                    INSERT OR REPLACE INTO {PLAYER_COMPETITIVE_RANK_FETCH_TABLE}
                        (player_bnet_id, cache_week, game_mode, checked_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (normalized_player_id, normalized_week, str(game_mode or "sport"), now),
                )
                conn.commit()
                return len(rows)
            except Exception as exc:
                self._warn_once(
                    f"match stats sqlite upsert_player_competitive_rank_snapshot failed: "
                    f"{type(exc).__name__}: {exc}"
                )
                return 0
            finally:
                try:
                    conn.close()
                except Exception:
                    pass

    def get_entry_ds_exact_ci_one(self, battletag: str, battlenum: Optional[int] = None) -> Optional[Dict[str, Any]]:
        return None

    def get_entry_ds(self, *args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
        return []

    def get_all_entries_ds2(self) -> List[Dict[str, Any]]:
        return []

    def __getattr__(self, name: str) -> Any:
        def _noop(*args: Any, **kwargs: Any) -> Any:
            if name == "get_entry_ds_exact_ci_one":
                return None
            if name.startswith("get_"):
                return []
            return None

        return _noop

__all__ = [
    "COMP_DATA_SUMMARY_TABLE",
    "COMP_DATA_TABLE",
    "HERO_MATCH_DETAIL_TABLE",
    "HERO_PERK_PICK_TABLE",
    "HERO_PERK_SUMMARY_TABLE",
    "IDPoolDB",
    "MATCH_STATS_DB_PATH",
    "MATCH_STRENGTH_CACHE_TABLE",
    "OVERALL_RANK_BUCKET_KEY",
    "PLAYER_COMPETITIVE_RANK_TABLE",
    "PLAYER_IDENTITY_TABLE",
]
