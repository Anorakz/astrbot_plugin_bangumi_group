"""Bangumi 群友动态

让群友把自己的 Bangumi 账号绑定到机器人，然后：
  * /bgm我      查看自己今天的追番动态
  * /bgm查      查看某位群友今天的追番动态
  * /bgm群报    汇总全群今天谁在追番

数据来源为 Bangumi 公开 API（默认 https://api.bgm.tv）。
如果所在网络无法直连，在插件配置里把 api_base 指向可用的反向代理地址即可。

★ 口径（重要，勿误解）
  判定依据是收藏条目的 `updated_at` —— 即「这条收藏最后一次被改动的时间」。
  Bangumi 公开接口**不提供单集观看记录**（/v0/users/{u}/collections 只有
  一个笼统的 updated_at 和当前的 ep_status，没有「第 N 集是哪天看的」）。
  因此：
    · 一条番剧只会出现在它最后一次被改动的那一天
    · 之后再次改动它，就会从原来那天「消失」（时间被覆盖）
  这是接口能力边界，不是 bug。要逐集时间线只能看 Bangumi 网页。

  接口按 updated_at 倒序返回，因此查某一天只需拉前几页即可覆盖。

日期参数：所有查询命令都支持在末尾加 `-N` 指定「往前第 N 天」，
  -0（默认）今天、-1 昨天、-2 前天……依次递推。
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import re
import sqlite3
import time
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

import aiohttp

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import At, Node, Nodes, Plain
from astrbot.core.message.message_event_result import MessageChain
from astrbot.api.star import Context, Star, StarTools, register

PLUGIN_NAME = "astrbot_plugin_bangumi_group"
DEFAULT_API_BASE = "https://api.bgm.tv"
DEFAULT_UA = (
    "Anorakz/astrbot_plugin_bangumi_group "
    "(https://github.com/Anorakz/astrbot_plugin_bangumi_group)"
)

COLLECT_LABEL = {1: "想看", 2: "看过", 3: "在看", 4: "搁置", 5: "抛弃"}
SUBJECT_LABEL = {1: "书籍", 2: "动画", 3: "音乐", 4: "游戏", 5: "三次元", 6: "影视"}

PAGE_SIZE = 30
MAX_ITEMS_PER_USER = 20
MAX_ITEMS_PER_USER_IN_REPORT = 6
# 拉取分页的上限。查历史日期时可能需要多拉几页，
# 但仍要留一个硬上限，避免单个用户把整份收藏都翻完。
MAX_PAGES_HARD = 10
SEASON_CACHE_TTL = 6 * 3600  # 当季放送表缓存（秒）

HELP_TEXT = """Bangumi 群友动态

/bgm绑定 <用户名>   绑定你的 Bangumi 账号
/bgm解绑            解除绑定
/bgm我              看看我今天在追什么
/bgm查 @群友        看某位群友今天的动态
/bgm查 <用户名>     直接查任意账号（无需绑定）
/bgm群报            汇总全群近24小时谁在追番（加 -N 按自然日查）
/bgm群报订阅 [小时] 每天定时推送群报（默认23:00；取消加「取消」）
/番剧排名 [0|1|2]    群友追番排行前三（0今天 1本周 2本季新番）
/bgm帮助            显示这条帮助

日期参数：以上查询命令都可加 -N 看更早的动态
  -0 今天（默认）、-1 昨天、-2 前天……依次递推
  例如：/bgm我 -1        /bgm查 @群友 -3      /bgm群报 -7

⚠️ 口径说明（重要）
  本插件读的是「收藏条目的最后修改时间」，不是「第几集是哪天看的」。
  Bangumi 公开接口不提供单集观看记录，所以：
    · 一条番剧只会出现在它「最后一次被改动」的那一天
    · 之后再次改动它（改评分/推进度），就会从原来那天消失
  想要逐集时间线，只能去 Bangumi 网页自己看。

用户名就是个人主页地址里的最后一段，例如 /bgm绑定 sai
只能读取公开收藏，若提示查不到请到 Bangumi 设置里把收藏改为公开。"""


class BangumiError(Exception):
    """上游请求失败。"""


class UserNotFound(BangumiError):
    """Bangumi 用户不存在。"""


@register(
    PLUGIN_NAME,
    "Anorakz",
    "绑定群友 Bangumi 账号，查询追番动态与全群汇总（支持 -N 指定天数）",
    "v1.1.0",
)
class BangumiGroupPlugin(Star):
    _CMD_WORDS = {
        "bgm绑定", "bgm_bind", "绑定bgm",
        "bgm解绑", "bgm_unbind", "解绑bgm",
        "bgm我", "bgm_me", "我的bgm",
        "bgm查", "bgm_query",
        "bgm群报", "bgm_group", "群报",
        "bgm群报订阅", "bgm_report_sub", "群报订阅",
        "番剧排名", "bgm_rank", "追番排名",
        "bgm帮助", "bgm_help",
    }

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.api_base = str(config.get("api_base") or DEFAULT_API_BASE).rstrip("/")
        self.user_agent = str(config.get("user_agent") or "").strip() or DEFAULT_UA
        self.tz = self._resolve_tz(str(config.get("timezone") or "Asia/Shanghai"))
        self.cache_ttl = max(0, int(config.get("cache_ttl") or 300))
        self.max_pages = max(1, int(config.get("max_pages") or 2))
        self.query_delay = max(0.0, float(config.get("query_delay") or 0.5))
        self.admin_uids = {
            str(uid).strip() for uid in (config.get("admin_uids") or []) if str(uid).strip()
        }

        try:
            data_dir = Path(StarTools.get_data_dir(PLUGIN_NAME))
        except Exception:  # 兜底，避免因框架差异导致插件加载失败
            data_dir = Path(__file__).resolve().parent / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = data_dir / "bindings.db"

        self._cache: dict[str, tuple[float, list[dict]]] = {}
        self._season_cache: tuple[float, set[str]] | None = None
        self._session: aiohttp.ClientSession | None = None
        self._report_task: asyncio.Task | None = None
        self._init_db()
        logger.info(f"[bangumi_group] 加载完成，API = {self.api_base}")

    # ── 基础设施 ────────────────────────────────────────────

    @staticmethod
    def _resolve_tz(name: str) -> ZoneInfo:
        try:
            return ZoneInfo(name)
        except Exception:
            logger.warning(f"[bangumi_group] 无法识别时区 {name}，回退 Asia/Shanghai")
            return ZoneInfo("Asia/Shanghai")

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS bindings (
                    scope_id   TEXT NOT NULL,
                    user_id    TEXT NOT NULL,
                    username   TEXT NOT NULL,
                    display    TEXT,
                    qq_name    TEXT,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (scope_id, user_id)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_bindings_scope ON bindings(scope_id)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS report_subs (
                    scope_id   TEXT PRIMARY KEY,
                    umo        TEXT NOT NULL,
                    hour       INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            # 旧库迁移：补 qq_name 列（QQ 群昵称，随该用户使用命令时自动刷新）
            cols = {r[1] for r in conn.execute("PRAGMA table_info(bindings)")}
            if "qq_name" not in cols:
                conn.execute("ALTER TABLE bindings ADD COLUMN qq_name TEXT")

    def _now(self) -> dt.datetime:
        return dt.datetime.now(self.tz)

    @staticmethod
    def _parse_iso(value: str | None) -> dt.datetime | None:
        if not value:
            return None
        try:
            return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={"User-Agent": self.user_agent, "Accept": "application/json"},
                timeout=aiohttp.ClientTimeout(total=20, connect=10),
            )
        return self._session

    async def _api_get(self, path: str, params: dict | None = None):
        session = await self._get_session()
        url = f"{self.api_base}{path}"
        try:
            async with session.get(url, params=params) as resp:
                if resp.status == 404:
                    raise UserNotFound(path)
                if resp.status != 200:
                    raise BangumiError(f"HTTP {resp.status}")
                try:
                    return await resp.json(content_type=None)
                except json.JSONDecodeError as exc:
                    raise BangumiError("上游返回了非法数据") from exc
        except aiohttp.ClientError as exc:
            raise BangumiError(f"网络错误 {exc}") from exc
        except asyncio.TimeoutError as exc:
            raise BangumiError("请求超时") from exc

    # ── Bangumi 数据 ────────────────────────────────────────

    async def _fetch_user(self, username: str) -> dict:
        data = await self._api_get(f"/v0/users/{quote(username)}")
        if not isinstance(data, dict) or not data.get("username"):
            raise UserNotFound(username)
        return data

    async def _fetch_collections(self, username: str, day_offset: int = 0) -> list[dict]:
        """拉取收藏列表（接口按 updated_at 倒序，故只需前面几页）。

        day_offset 表示需要覆盖到「往前第几天」：0 拉到今天为止，
        1 表示需要覆盖到昨天，以此类推（内部统一存正数深度）。
        缓存记录 (拉取时刻, 条目, 已覆盖深度)，只要缓存还新鲜
        且上次覆盖的深度 >= 本次所需深度，就直接复用。
        """
        now = time.time()
        need = max(0, int(day_offset))
        wanted_start = self._day_start(need)

        cached = self._cache.get(username)
        if cached:
            stamp, cached_items, covered_depth = cached
            fresh = now - stamp < self.cache_ttl
            if fresh and covered_depth >= need:
                return cached_items

        items: list[dict] = []
        covered_depth = need
        pages_budget = max(self.max_pages, min(MAX_PAGES_HARD, self.max_pages + need))
        for page in range(pages_budget):
            data = await self._api_get(
                f"/v0/users/{quote(username)}/collections",
                {"limit": PAGE_SIZE, "offset": page * PAGE_SIZE},
            )
            batch = (data or {}).get("data") or []
            items.extend(batch)
            if len(batch) < PAGE_SIZE:
                # 拉完了全部收藏，覆盖范围足够深
                covered_depth = MAX_PAGES_HARD * 10
                break
            tail = self._parse_iso(batch[-1].get("updated_at"))
            if tail is not None and tail.astimezone(self.tz) < wanted_start:
                # 尾条已经早于目标日期，说明所需区间已完整覆盖
                break
        else:
            # 页数用尽仍未触及目标日期，如实记录覆盖不到那么深
            covered_depth = need - 1 if need > 0 else 0

        self._cache[username] = (now, items, covered_depth)
        return items

    def _day_start(self, day_offset: int) -> dt.datetime:
        """返回「往前第 day_offset 天」当天 00:00（带时区）。"""
        base = self._now().replace(hour=0, minute=0, second=0, microsecond=0)
        return base - dt.timedelta(days=max(0, int(day_offset)))

    def _day_items(self, items: list[dict], day_offset: int = 0) -> list[dict]:
        """筛出目标日期当天的条目。"""
        target = self._day_start(day_offset).date()
        result = []
        for item in items:
            stamp = self._parse_iso(item.get("updated_at"))
            if stamp is not None and stamp.astimezone(self.tz).date() == target:
                result.append(item)
        return result

    def _season_start(self) -> dt.datetime:
        """当前季度起始日 00:00（1/4/7/10 月 1 号）。"""
        now = self._now()
        first_month = ((now.month - 1) // 3) * 3 + 1
        return dt.datetime(now.year, first_month, 1, tzinfo=self.tz)

    def _window_start(self, kind: int) -> dt.datetime:
        """统计窗口起点：0=今天 00:00；1=本周一 00:00；2=本季度首日 00:00。"""
        kind = int(kind)
        if kind == 1:
            today = self._day_start(0)
            return today - dt.timedelta(days=today.weekday())
        if kind == 2:
            return self._season_start()
        return self._day_start(0)

    async def _fetch_season_ids(self) -> set[str]:
        """当季放送番剧的 subject id 集合（/calendar，缓存 6 小时）。"""
        now = time.time()
        cache = getattr(self, "_season_cache", None)
        if cache is not None and now - cache[0] < SEASON_CACHE_TTL:
            return cache[1]
        data = await self._api_get("/calendar")
        ids: set[str] = set()
        if isinstance(data, list):
            for day in data:
                if not isinstance(day, dict):
                    continue
                for item in day.get("items") or []:
                    sid = (item or {}).get("id")
                    if sid:
                        ids.add(str(sid))
        if ids:
            self._season_cache = (now, ids)
        return ids

    def _window_items(self, items: list[dict], kind: int = 0) -> list[dict]:
        """筛出窗口内的条目（今天 / 本周 / 本季度）。"""
        start = self._window_start(kind)
        result = []
        for item in items:
            stamp = self._parse_iso(item.get("updated_at"))
            if stamp is not None and stamp.astimezone(self.tz) >= start:
                result.append(item)
        return result

    def _recent_items(self, items: list[dict], hours: int = 24) -> list[dict]:
        """筛出最近 hours 小时内的条目（滚动窗口，用于群报）。

        窗口 = (现在 - hours, 现在]，对应「上次推送时刻 → 本次推送时刻」，
        因此定时群报（每天同一时刻推）拿到的正好是两推之间 24 小时的更新。
        """
        end = self._now()
        start = end - dt.timedelta(hours=max(1, int(hours)))
        result = []
        for item in items:
            stamp = self._parse_iso(item.get("updated_at"))
            if stamp is None:
                continue
            local = stamp.astimezone(self.tz)
            if start < local <= end:
                result.append(item)
        return result

    def _recent_label(self, hours: int = 24) -> str:
        end = self._now()
        start = end - dt.timedelta(hours=max(1, int(hours)))
        return f"近 {hours} 小时（{start:%m-%d %H:%M} → {end:%m-%d %H:%M}）"

    def _day_label(self, day_offset: int) -> str:
        """人类可读的日期标签：今天 / 昨天 / 前天 / MM-DD。"""
        day = self._day_start(day_offset).date()
        mapping = {0: "今天", 1: "昨天", 2: "前天"}
        if day_offset in mapping:
            return mapping[day_offset]
        weekday = "一二三四五六日"[day.weekday()]
        return f"{day:%m-%d}（周{weekday}）"

    @staticmethod
    def _subject_name(item: dict) -> tuple[str, str]:
        subject = item.get("subject") or {}
        name = subject.get("name_cn") or subject.get("name") or f"#{item.get('subject_id')}"
        return str(name), SUBJECT_LABEL.get(item.get("subject_type"), "")

    def _format_item(self, item: dict, with_date: bool = False) -> str:
        name, kind = self._subject_name(item)
        bits: list[str] = []

        collect = COLLECT_LABEL.get(item.get("type"))
        if collect:
            bits.append(f"标记为{collect}")
        episodes = item.get("ep_status") or 0
        if episodes:
            bits.append(f"进度到第 {episodes} 集")
        rate = item.get("rate") or 0
        if rate:
            bits.append(f"评分 {rate}")
        if not bits:
            bits.append("更新了收藏")

        stamp = self._parse_iso(item.get("updated_at"))
        if stamp:
            local = stamp.astimezone(self.tz)
            clock = f"{local:%m-%d %H:%M} " if with_date else f"{local:%H:%M} "
        else:
            clock = ""
        prefix = f"[{kind}] " if kind else ""
        return f"{clock}{prefix}《{name}》 {' · '.join(bits)}"

    # ── 绑定表 ──────────────────────────────────────────────

    @staticmethod
    def _sender_id(event: AstrMessageEvent) -> str:
        return str(event.get_sender_id() or "").strip()

    @staticmethod
    def _scope_id(event: AstrMessageEvent) -> str:
        group_id = event.get_group_id()
        if group_id:
            return f"group:{group_id}"
        return f"private:{event.get_sender_id()}"

    def _upsert_binding(
        self,
        scope_id: str,
        user_id: str,
        username: str,
        display: str,
        qq_name: str | None = None,
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO bindings(scope_id, user_id, username, display, qq_name, created_at) "
                "VALUES(?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(scope_id, user_id) DO UPDATE SET "
                "username = excluded.username, display = excluded.display, "
                "qq_name = COALESCE(excluded.qq_name, bindings.qq_name)",
                (scope_id, user_id, username, display, qq_name, self._now().isoformat()),
            )

    def _refresh_qq_name(self, event: AstrMessageEvent, binding: dict) -> None:
        """用发送者当前的群昵称刷新绑定记录（昵称改了也能被查到）。"""
        try:
            name = str(event.get_sender_name() or "").strip()
        except Exception:
            return
        if not name or name == str(binding.get("qq_name") or ""):
            return
        with self._conn() as conn:
            conn.execute(
                "UPDATE bindings SET qq_name = ? WHERE scope_id = ? AND user_id = ?",
                (name, binding["scope_id"], binding["user_id"]),
            )
        binding["qq_name"] = name

    def _delete_binding(self, scope_id: str, user_id: str) -> int:
        with self._conn() as conn:
            cursor = conn.execute(
                "DELETE FROM bindings WHERE scope_id = ? AND user_id = ?",
                (scope_id, user_id),
            )
            return cursor.rowcount

    def _get_binding(self, scope_id: str, user_id: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM bindings WHERE scope_id = ? AND user_id = ?",
                (scope_id, user_id),
            ).fetchone()
        return dict(row) if row else None

    def _find_binding_by_keyword(self, scope_id: str, keyword: str) -> dict | None:
        """按 群昵称 → Bangumi昵称 → 用户名 的优先级匹配绑定（优先本群 scope）。

        官方平台(qq_official)的群消息不含 @其他群友 的提及信息，
        `/bgm查 @群友` 无法拿到对方 openid；群昵称在绑定/使用命令时记录，
        改了昵称也会在该用户下次使用命令时自动刷新。
        """
        kw = keyword.strip().casefold()
        if not kw:
            return None
        with self._conn() as conn:
            rows = [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM bindings "
                    "ORDER BY (scope_id = ?) DESC, created_at ASC",
                    (scope_id,),
                )
            ]
        for field in ("qq_name", "display", "username"):
            for row in rows:
                value = str(row.get(field) or "").strip().casefold()
                if value == kw:
                    return row
        return None

    def _list_bindings(self, scope_id: str) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM bindings WHERE scope_id = ? ORDER BY created_at",
                (scope_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    # ── 消息解析 ────────────────────────────────────────────

    def _args(self, event: AstrMessageEvent) -> list[str]:
        text = (event.message_str or "").strip()
        if not text:
            return []
        parts = text.split()
        if parts and parts[0].lstrip("/／").lower() in {w.lower() for w in self._CMD_WORDS}:
            parts = parts[1:]
        return parts

    @staticmethod
    def _parse_day_offset(args: list[str]) -> tuple[int, list[str]]:
        """从参数里挑出 -N 天数偏移，返回 (offset, 剩余参数)。

        兼容全角减号、中文破折号，以及 `- 3` 这类被空格分开的写法。
        """
        offset = 0
        rest: list[str] = []
        index = 0
        while index < len(args):
            token = args[index].strip()
            normalized = token.replace("－", "-").replace("—", "-").replace("–", "-")
            value: int | None = None
            if normalized.startswith("-") and len(normalized) > 1 and normalized[1:].isdigit():
                value = int(normalized[1:])
            elif normalized == "-" and index + 1 < len(args) and args[index + 1].strip().isdigit():
                value = int(args[index + 1].strip())
                index += 1
            if value is not None:
                offset = max(0, value)
            else:
                rest.append(token)
            index += 1
        return offset, rest

    @staticmethod
    def _extract_target_id(event: AstrMessageEvent) -> str:
        """取 @ 的目标用户 ID。

        aiocqhttp(NapCat) 平台：目标群友的 At 组件直接带 QQ 号。
        qq_official 官方平台：每条群消息都固定带一个「@机器人」的 At 组件
        （qq = 事件 self_id），必须跳过，否则会误把机器人当查询目标；
        且 @ 其他群友不会生成 At 组件，而是以 `<@!openid>` 文本形式
        残留在 message_str 里，需要自行解析（openid 恰好是绑定表的 user_id）。
        """
        try:
            self_id = str(event.get_self_id() or "")
        except Exception:
            self_id = str(getattr(event, "self_id", "") or "")
        for component in event.get_messages():
            if isinstance(component, At):
                qq = getattr(component, "qq", None)
                if qq is None:
                    continue
                qq = str(qq)
                # "qq_official" 是官方适配器合成的「@机器人」标记
                # （C2C 路径 self_id 是 "unknown_selfid"，与 At.qq 对不上）
                if qq in {"all", "0", "-1", "qq_official"}:
                    continue
                if self_id and qq == self_id:
                    continue  # 官方平台群聊：@机器人自身的组件
                return qq
        m = re.search(r"<@!?(\w+)", getattr(event, "message_str", "") or "")
        if m:
            oid = m.group(1)
            if not (self_id and oid == self_id):
                return oid
        return ""

    # ── 群报订阅存储 ────────────────────────────────────────

    def _get_report_sub(self, scope_id: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM report_subs WHERE scope_id = ?", (scope_id,)
            ).fetchone()
        return dict(row) if row else None

    def _upsert_report_sub(self, scope_id: str, umo: str, hour: int) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO report_subs(scope_id, umo, hour, created_at) "
                "VALUES(?, ?, ?, ?) "
                "ON CONFLICT(scope_id) DO UPDATE SET umo = excluded.umo, hour = excluded.hour",
                (scope_id, umo, int(hour), self._now().isoformat()),
            )

    def _delete_report_sub(self, scope_id: str) -> int:
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM report_subs WHERE scope_id = ?", (scope_id,))
        return cur.rowcount

    def _list_report_subs(self) -> list[dict]:
        with self._conn() as conn:
            rows = [dict(r) for r in conn.execute("SELECT * FROM report_subs")]
        return rows

    # ── 渲染 ────────────────────────────────────────────────

    async def _render_user_day(
        self, username: str, title: str, day_offset: int = 0
    ) -> str:
        day_label = self._day_label(day_offset)
        try:
            items = await self._fetch_collections(username, day_offset)
        except UserNotFound:
            return f"❌ 找不到 Bangumi 用户「{username}」，可能已改名或注销。"
        except BangumiError as exc:
            return f"❌ 查询「{username}」失败：{exc}"

        day_items = self._day_items(items, day_offset)
        if not day_items:
            return f"📭 {title}{day_label}没有新动态。"

        # 查历史日期时附上具体日期，避免同一个钟点看不出是哪天
        with_date = day_offset > 0
        lines = [f"📖 {title}{day_label}有 {len(day_items)} 条动态："]
        for item in day_items[:MAX_ITEMS_PER_USER]:
            lines.append("  · " + self._format_item(item, with_date=with_date))
        if len(day_items) > MAX_ITEMS_PER_USER:
            lines.append(f"  …… 还有 {len(day_items) - MAX_ITEMS_PER_USER} 条")
        return "\n".join(lines)

    # ── 命令 ────────────────────────────────────────────────

    async def _resolve_query_key(self, profile: dict, keyword: str) -> str:
        """选出对该用户真正可用的查询键。

        Bangumi 的查询键因账号而异：自定义过用户名的用字符串，没自定义的
        用数字 uid；两者互不通用（用错那个会 404）。这里按
        username → id → 原始输入 的顺序探测，返回第一个能查到收藏的键，
        保证后续 /bgm我、群报、排名都查得到。
        """
        candidates: list[str] = []
        for value in (profile.get("username"), profile.get("id"), keyword):
            text = str(value or "").strip()
            if text and text not in candidates:
                candidates.append(text)
        for key in candidates:
            try:
                await self._api_get(
                    f"/v0/users/{quote(key)}/collections", {"limit": 1}
                )
                return key
            except BangumiError:
                continue
        return candidates[0] if candidates else keyword

    @filter.command("bgm绑定", alias={"bgm_bind", "绑定bgm"})
    async def bind(self, event: AstrMessageEvent):
        args = self._args(event)
        if not args:
            yield event.plain_result(
                "用法：/bgm绑定 <Bangumi 用户名或 UID>\n"
                "用户名就是个人主页地址里的最后一段，例如 /bgm绑定 sai"
            )
            return

        keyword = args[0].strip().lstrip("@")
        try:
            profile = await self._fetch_user(keyword)
        except UserNotFound:
            yield event.plain_result(
                f"❌ 找不到 Bangumi 用户「{keyword}」。\n"
                "请确认拼写（区分大小写），或直接填数字 UID。"
            )
            return
        except BangumiError as exc:
            yield event.plain_result(f"❌ 查询失败：{exc}")
            return

        username = await self._resolve_query_key(profile, keyword)
        display = str(profile.get("nickname") or username)
        try:
            qq_name = str(event.get_sender_name() or "").strip()
        except Exception:
            qq_name = ""
        self._upsert_binding(
            self._scope_id(event),
            self._sender_id(event),
            username,
            display,
            qq_name,
        )
        yield event.plain_result(
            f"✅ 绑定成功\n"
            f"   你的 Bangumi：{display}（用户名 {username}）\n"
            f"   之后发 /bgm我 就能看今天的追番动态。"
        )

    @filter.command("bgm解绑", alias={"bgm_unbind", "解绑bgm"})
    async def unbind(self, event: AstrMessageEvent):
        scope = self._scope_id(event)
        sender = self._sender_id(event)
        target = self._extract_target_id(event)

        if not target or target == sender:
            if self._delete_binding(scope, sender):
                yield event.plain_result("✅ 已解除你的 Bangumi 绑定。")
            else:
                yield event.plain_result("你还没有绑定账号。可用 /bgm绑定 <用户名> 绑定。")
            return

        if sender not in self.admin_uids:
            yield event.plain_result("只有管理员可以解除他人的绑定。")
            return
        if self._delete_binding(scope, target):
            yield event.plain_result(f"✅ 已解除 {target} 的绑定。")
        else:
            yield event.plain_result(f"{target} 没有绑定记录。")

    @filter.command("bgm我", alias={"bgm_me", "我的bgm"})
    async def me(self, event: AstrMessageEvent):
        binding = self._get_binding(self._scope_id(event), self._sender_id(event))
        if not binding:
            yield event.plain_result(
                "你还没有绑定 Bangumi 账号。\n用法：/bgm绑定 <用户名>"
            )
            return
        self._refresh_qq_name(event, binding)
        day_offset, _ = self._parse_day_offset(self._args(event))
        label = binding.get("display") or binding["username"]
        yield event.plain_result(
            await self._render_user_day(binding["username"], f"你（{label}）", day_offset)
        )

    @filter.command("bgm查", alias={"bgm_query"})
    async def query(self, event: AstrMessageEvent):
        scope = self._scope_id(event)
        target = self._extract_target_id(event)
        day_offset, args = self._parse_day_offset(self._args(event))

        if not target:
            if not args:
                yield event.plain_result(
                    "用法：/bgm查 @群友 [-N]   或   /bgm查 <Bangumi 用户名> [-N]\n"
                    "N 可选：-0 今天（默认）、-1 昨天、-2 前天……依次递推"
                )
                return
            keyword = args[0].strip().lstrip("@")
            # 无 @ 时直接按 Bangumi 用户名/UID 搜索（不要求绑定）
            yield event.plain_result(
                await self._render_user_day(keyword, f"{keyword} ", day_offset)
            )
            return

        binding = self._get_binding(scope, target)
        if not binding:
            yield event.plain_result(f"{target} 还没有绑定 Bangumi 账号。")
            return
        self._refresh_qq_name(event, binding)
        label = binding.get("display") or binding["username"]
        yield event.plain_result(
            await self._render_user_day(binding["username"], f"{label} ", day_offset)
        )

    @filter.command("bgm群报", alias={"bgm_group", "群报"})
    async def group_report(self, event: AstrMessageEvent):
        scope = self._scope_id(event)
        raw_args = self._args(event)
        day_offset, _ = self._parse_day_offset(raw_args)
        legacy = bool(raw_args)  # 显式带参数 → 按自然日（兼容 /bgm群报 -N 历史查询）

        bindings = self._list_bindings(scope)
        if not bindings:
            yield event.plain_result(
                "本群还没有人绑定 Bangumi 账号。\n让群友发 /bgm绑定 <用户名> 即可。"
            )
            return

        window_text = self._day_label(day_offset) if legacy else "近 24 小时"
        yield event.plain_result(
            f"🔍 正在统计 {len(bindings)} 位群友{window_text}的动态…"
        )

        nodes = (
            await self._build_report_nodes(scope, day_offset=day_offset)
            if legacy
            else await self._build_report_nodes(scope)
        )
        yield event.chain_result([Nodes(nodes)])

    async def _build_report_nodes(
        self, scope: str, day_offset: int | None = None, hours: int = 24
    ) -> list[Node]:
        """构建群报节点：每位绑定群友一条，最后合并为转发消息。

        day_offset=None（默认）→ 滚动窗口「近 hours 小时」，
          对应「上次推送时刻 → 本次推送时刻」的更新；
        传 day_offset → 按自然日统计（兼容 /bgm群报 -N）。
        """
        rolling = day_offset is None
        short_label = f"近 {hours} 小时" if rolling else self._day_label(int(day_offset))
        full_label = self._recent_label(hours) if rolling else self._day_label(int(day_offset))
        depth = 1 if rolling else int(day_offset)
        bindings = self._list_bindings(scope)
        if not bindings:
            return []

        nodes: list[Node] = []
        now = self._now()
        nodes.append(
            Node(
                content=[
                    Plain(
                        f"📊 Bangumi 群报 · {now:%m-%d} · {full_label}\n"
                        f"本群已绑定 {len(bindings)} 人，每人一条"
                    )
                ],
                name="群报",
                uin="0",
            )
        )

        for index, binding in enumerate(bindings):
            label = binding.get("display") or binding["username"]
            uin = str(binding.get("user_id") or "0")
            try:
                items = await self._fetch_collections(binding["username"], depth)
            except UserNotFound:
                nodes.append(
                    Node(
                        content=[Plain(f"⚠️ {label} · 用户不存在（可能已改名或注销）")],
                        name=label,
                        uin=uin,
                    )
                )
                continue
            except BangumiError as exc:
                nodes.append(
                    Node(
                        content=[Plain(f"⚠️ {label} · 查询失败：{exc}")],
                        name=label,
                        uin=uin,
                    )
                )
                continue

            day_items = (
                self._recent_items(items, hours)
                if rolling
                else self._day_items(items, int(day_offset))
            )
            if not day_items:
                text = f"📭 {label} · {short_label}没有新动态"
            else:
                lines = [f"📖 {label} · {short_label}有 {len(day_items)} 条动态"]
                for item in day_items[:MAX_ITEMS_PER_USER_IN_REPORT]:
                    lines.append(self._format_item(item))
                if len(day_items) > MAX_ITEMS_PER_USER_IN_REPORT:
                    lines.append(
                        f"…… 还有 {len(day_items) - MAX_ITEMS_PER_USER_IN_REPORT} 条"
                    )
                text = "\n".join(lines)
            nodes.append(Node(content=[Plain(text)], name=label, uin=uin))

            if self.query_delay and index != len(bindings) - 1:
                await asyncio.sleep(self.query_delay)

        return nodes

    # ── 群报订阅（每日定时推送）──────────────────────────────

    @filter.command("bgm群报订阅", alias={"bgm_report_sub", "群报订阅"})
    async def report_subscribe(self, event: AstrMessageEvent):
        scope = self._scope_id(event)
        args = self._args(event)
        if args and args[0].strip().lower() in {"off", "cancel", "取消", "关", "关闭"}:
            if self._delete_report_sub(scope):
                yield event.plain_result("✅ 已取消本群的群报订阅。")
            else:
                yield event.plain_result("本群还没有订阅群报。")
            return

        hour = 23
        if args:
            raw = args[0].strip().rstrip("点:：时")
            if not raw.isdigit() or not (0 <= int(raw) <= 23):
                yield event.plain_result(
                    "用法：/bgm群报订阅 [小时]\n"
                    "  小时为 0-23 的整数，默认 23（每天 23:00 推送）\n"
                    "  取消：/bgm群报订阅 取消"
                )
                return
            hour = int(raw)

        umo = str(getattr(event, "unified_msg_origin", "") or "")
        if not umo:
            yield event.plain_result("❌ 无法确定会话标识，订阅失败。")
            return
        self._upsert_report_sub(scope, umo, hour)
        yield event.plain_result(
            f"✅ 已订阅每日群报\n"
            f"   推送时间：每天 {hour:02d}:00\n"
            f"   取消方式：/bgm群报订阅 取消"
        )

    async def initialize(self) -> None:
        if self._report_task is None or self._report_task.done():
            self._report_task = asyncio.create_task(self._report_loop())
            logger.info("[bangumi_group] 群报订阅调度已启动")

    async def _report_loop(self) -> None:
        """睡到每个整点，检查是否有订阅需要推送。"""
        try:
            while True:
                now = self._now()
                nxt = now.replace(minute=0, second=0, microsecond=0) + dt.timedelta(hours=1)
                await asyncio.sleep(max(5.0, (nxt - now).total_seconds()))
                await self._report_tick()
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # 兜底，避免任务静默死亡
            logger.error(f"[bangumi_group] 群报订阅循环异常退出: {exc}")

    async def _report_tick(self) -> None:
        hour = self._now().hour
        for sub in self._list_report_subs():
            if int(sub.get("hour") or -1) != hour:
                continue
            try:
                nodes = await self._build_report_nodes(sub["scope_id"])  # 滚动近 24 小时
                if not nodes:
                    continue
                await self.context.send_message(
                    sub["umo"], MessageChain([Nodes(nodes)])
                )
                logger.info(f"[bangumi_group] 群报已推送 → {sub['umo']}")
            except Exception as exc:
                logger.error(f"[bangumi_group] 群报推送失败 {sub.get('umo')}: {exc}")

    @filter.command("番剧排名", alias={"bgm_rank", "追番排名"})
    async def rank(self, event: AstrMessageEvent):
        args = self._args(event)
        kind = 0
        if args:
            raw = args[0].strip()
            if raw not in {"0", "1", "2"}:
                yield event.plain_result(
                    "用法：/番剧排名 [0|1|2]\n"
                    "  0 = 今天（默认）\n"
                    "  1 = 本周\n"
                    "  2 = 本季度新番（当季放送表口径）"
                )
                return
            kind = int(raw)

        scope = self._scope_id(event)
        bindings = self._list_bindings(scope)
        if not bindings:
            yield event.plain_result(
                "本群还没有人绑定 Bangumi 账号。\n让群友发 /bgm绑定 <用户名> 即可。"
            )
            return

        window_label = {0: "今天", 1: "本周", 2: "本季度新番"}[kind]
        yield event.plain_result(
            f"🔍 正在统计 {len(bindings)} 位群友的追番排行（{window_label}）…"
        )

        season_ids: set[str] | None = None
        depth = {0: 0, 1: 7}.get(kind, 0)
        if kind == 2:
            try:
                season_ids = await self._fetch_season_ids()
            except BangumiError:
                season_ids = set()
            if not season_ids:
                yield event.plain_result("❌ 取当季放送表失败，请稍后再试。")
                return
            depth = min(130, max(7, (self._now() - self._season_start()).days + 1))

        stats: dict[str, dict] = {}
        failed: list[str] = []
        for index, binding in enumerate(bindings):
            label = binding.get("display") or binding["username"]
            try:
                items = await self._fetch_collections(binding["username"], depth)
            except UserNotFound:
                failed.append(label)
                continue
            except BangumiError:
                failed.append(label)
                continue

            for item in self._window_items(items, kind):
                if (item.get("subject_type") or 2) != 2:  # 只统计动画
                    continue
                if season_ids is not None and str(item.get("subject_id")) not in season_ids:
                    continue  # 本季度新番：只保留当季放送表中的条目
                name, _ = self._subject_name(item)
                key = str(item.get("subject_id") or name)
                stamp = self._parse_iso(item.get("updated_at"))
                row = stats.setdefault(key, {"name": name, "members": set(), "latest": None})
                row["members"].add(label)
                if stamp is not None and (row["latest"] is None or stamp > row["latest"]):
                    row["latest"] = stamp

            if self.query_delay and index != len(bindings) - 1:
                await asyncio.sleep(self.query_delay)

        if not stats:
            yield event.plain_result(
                f"📭 {window_label}大家都没有新的追番动态。"
                if kind != 2
                else "📭 大家本季度还没有追当季新番。"
            )
            return

        ranked = sorted(
            stats.values(),
            key=lambda r: (
                -len(r["members"]),
                -(r["latest"].timestamp() if r["latest"] else 0.0),
                r["name"],
            ),
        )

        medals = ["🥇", "🥈", "🥉"]
        lines = [f"🏆 群友追番排行 · {window_label}（{len(bindings)} 人参与）", ""]
        for i, row in enumerate(ranked[:3]):
            who = "、".join(sorted(row["members"]))
            lines.append(f"{medals[i]} 《{row['name']}》 · {len(row['members'])} 人")
            lines.append(f"      {who}")
        if len(ranked) > 3:
            lines.append("")
            lines.append(f"（另有 {len(ranked) - 3} 部各有动态，未进前三）")
        if failed:
            lines.append(f"（查询失败：{'、'.join(failed)}）")
        yield event.plain_result("\n".join(lines))

    @filter.regex(r"^/?bgm(?:绑定|解绑|群报订阅|群报|查|我|排名|帮助)\S+$")
    async def nospace_dispatch(self, event: AstrMessageEvent):
        """兼容漏打空格的写法（如 /bgm绑定xxx、/bgm查mzfl）。

        AstrBot 的标准指令匹配要求「指令名 + 空格」或整串相等
        （core/star/filter/command.py: startswith(cmd + " ")），
        漏空格时命令不会被调用。这里用正则兜住，补空格后转交对应指令。
        """
        text = (event.message_str or "").strip()
        m = re.match(
            r"^/?(bgm(?:绑定|解绑|群报订阅|群报|查|我|排名|帮助))(\S+)$", text
        )
        if not m:
            return
        cmd_word, rest = m.group(1), m.group(2)
        handler = {
            "bgm绑定": self.bind,
            "bgm解绑": self.unbind,
            "bgm我": self.me,
            "bgm查": self.query,
            "bgm群报": self.group_report,
            "bgm群报订阅": self.report_subscribe,
            "bgm排名": self.rank,
            "bgm帮助": self.help_cmd,
        }.get(cmd_word)
        if handler is None:
            return
        logger.info(f"[bangumi_group] 漏空格兼容：{text!r} → /{cmd_word} {rest}")
        event.message_str = f"/{cmd_word} {rest}"
        async for piece in handler(event):
            yield piece

    @filter.command("bgm帮助", alias={"bgm_help"})
    async def help_cmd(self, event: AstrMessageEvent):
        yield event.plain_result(HELP_TEXT)

    async def terminate(self):
        if self._report_task and not self._report_task.done():
            self._report_task.cancel()
        if self._session and not self._session.closed:
            await self._session.close()
