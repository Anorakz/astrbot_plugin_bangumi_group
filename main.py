"""Bangumi 群友动态

让群友把自己的 Bangumi 账号绑定到机器人，然后：
  * /bgm我      查看自己今天的追番动态
  * /bgm查      查看某位群友今天的追番动态
  * /bgm群报    汇总全群今天谁在追番

数据来源为 Bangumi 公开 API（默认 https://api.bgm.tv）。
如果所在网络无法直连，在插件配置里把 api_base 指向可用的反向代理地址即可。
判定口径：收藏条目的 updated_at 落在「今天」，即该用户今天在 Bangumi 上有过动作。
接口默认按 updated_at 倒序返回，因此只需拉前若干页即可覆盖当天动态。
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import sqlite3
import time
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

import aiohttp

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import At
from astrbot.api.star import Context, Star, StarTools, register

PLUGIN_NAME = "astrbot_plugin_bangumi_group"
DEFAULT_API_BASE = "https://api.bgm.tv"
DEFAULT_UA = (
    "Anorakz/astrbot_plugin_bangumi_group "
    "(https://github.com/Anorakz/astrbot_plugin_bangumi_group)"
)

COLLECT_LABEL = {1: "看过", 2: "想看", 3: "在看", 4: "搁置", 5: "抛弃"}
SUBJECT_LABEL = {1: "书籍", 2: "动画", 3: "音乐", 4: "游戏", 5: "三次元", 6: "影视"}

PAGE_SIZE = 30
MAX_ITEMS_PER_USER = 20
MAX_ITEMS_PER_USER_IN_REPORT = 6

HELP_TEXT = """Bangumi 群友动态

/bgm绑定 <用户名>   绑定你的 Bangumi 账号
/bgm解绑            解除绑定
/bgm我              看看我今天在追什么
/bgm查 @群友        看某位群友今天的动态
/bgm查 <用户名>     直接查任意账号（无需绑定）
/bgm群报            汇总全群今天谁在追番
/bgm帮助            显示这条帮助

用户名就是 bgm.tv/user/<这一段>，例如 /bgm绑定 sai
只能读取公开收藏，若提示查不到请到 Bangumi 设置里把收藏改为公开。"""


class BangumiError(Exception):
    """上游请求失败。"""


class UserNotFound(BangumiError):
    """Bangumi 用户不存在。"""


@register(
    PLUGIN_NAME,
    "Anorakz",
    "绑定群友 Bangumi 账号，查询今日追番动态与全群汇总",
    "v1.0.0",
)
class BangumiGroupPlugin(Star):
    _CMD_WORDS = {
        "bgm绑定", "bgm_bind", "绑定bgm",
        "bgm解绑", "bgm_unbind", "解绑bgm",
        "bgm我", "bgm_me", "我的bgm",
        "bgm查", "bgm_query",
        "bgm群报", "bgm_group", "群报",
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
        self._session: aiohttp.ClientSession | None = None
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
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (scope_id, user_id)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_bindings_scope ON bindings(scope_id)"
            )

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

    async def _fetch_collections(self, username: str) -> list[dict]:
        """拉取收藏列表（接口按 updated_at 倒序，故只需前面几页）。"""
        now = time.time()
        cached = self._cache.get(username)
        if cached and now - cached[0] < self.cache_ttl:
            return cached[1]

        today_start = self._now().replace(hour=0, minute=0, second=0, microsecond=0)
        items: list[dict] = []
        for page in range(self.max_pages):
            data = await self._api_get(
                f"/v0/users/{quote(username)}/collections",
                {"limit": PAGE_SIZE, "offset": page * PAGE_SIZE},
            )
            batch = (data or {}).get("data") or []
            items.extend(batch)
            if len(batch) < PAGE_SIZE:
                break
            tail = self._parse_iso(batch[-1].get("updated_at"))
            if tail is not None and tail.astimezone(self.tz) < today_start:
                break

        self._cache[username] = (now, items)
        return items

    def _today_items(self, items: list[dict]) -> list[dict]:
        today = self._now().date()
        result = []
        for item in items:
            stamp = self._parse_iso(item.get("updated_at"))
            if stamp is not None and stamp.astimezone(self.tz).date() == today:
                result.append(item)
        return result

    @staticmethod
    def _subject_name(item: dict) -> tuple[str, str]:
        subject = item.get("subject") or {}
        name = subject.get("name_cn") or subject.get("name") or f"#{item.get('subject_id')}"
        return str(name), SUBJECT_LABEL.get(item.get("subject_type"), "")

    def _format_item(self, item: dict) -> str:
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
        clock = f"{stamp.astimezone(self.tz):%H:%M} " if stamp else ""
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

    def _upsert_binding(self, scope_id: str, user_id: str, username: str, display: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO bindings(scope_id, user_id, username, display, created_at) "
                "VALUES(?, ?, ?, ?, ?) "
                "ON CONFLICT(scope_id, user_id) DO UPDATE SET "
                "username = excluded.username, display = excluded.display",
                (scope_id, user_id, username, display, self._now().isoformat()),
            )

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
    def _extract_target_id(event: AstrMessageEvent) -> str:
        for component in event.get_messages():
            if isinstance(component, At):
                qq = getattr(component, "qq", None)
                if qq is not None and str(qq) not in {"all", "0", "-1"}:
                    return str(qq)
        return ""

    # ── 渲染 ────────────────────────────────────────────────

    async def _render_user_today(self, username: str, title: str) -> str:
        try:
            items = await self._fetch_collections(username)
        except UserNotFound:
            return f"❌ 找不到 Bangumi 用户「{username}」，可能已改名或注销。"
        except BangumiError as exc:
            return f"❌ 查询「{username}」失败：{exc}"

        today_items = self._today_items(items)
        if not today_items:
            return f"📭 {title}没有新动态。"

        lines = [f"📖 {title}有 {len(today_items)} 条动态："]
        for item in today_items[:MAX_ITEMS_PER_USER]:
            lines.append("  · " + self._format_item(item))
        if len(today_items) > MAX_ITEMS_PER_USER:
            lines.append(f"  …… 还有 {len(today_items) - MAX_ITEMS_PER_USER} 条")
        return "\n".join(lines)

    # ── 命令 ────────────────────────────────────────────────

    @filter.command("bgm绑定", alias={"bgm_bind", "绑定bgm"})
    async def bind(self, event: AstrMessageEvent):
        args = self._args(event)
        if not args:
            yield event.plain_result(
                "用法：/bgm绑定 <Bangumi 用户名或 UID>\n"
                "用户名就是 bgm.tv/user/<这一段>，例如 /bgm绑定 sai"
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

        username = str(profile.get("username") or keyword)
        display = str(profile.get("nickname") or username)
        self._upsert_binding(
            self._scope_id(event), self._sender_id(event), username, display
        )
        yield event.plain_result(
            f"✅ 绑定成功\n"
            f"   你的 Bangumi：{display}（bgm.tv/user/{username}）\n"
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
        label = binding.get("display") or binding["username"]
        yield event.plain_result(
            await self._render_user_today(binding["username"], f"你（{label}）今天")
        )

    @filter.command("bgm查", alias={"bgm_query"})
    async def query(self, event: AstrMessageEvent):
        scope = self._scope_id(event)
        target = self._extract_target_id(event)
        args = self._args(event)

        if not target and not args:
            yield event.plain_result(
                "用法：/bgm查 @群友   或   /bgm查 <Bangumi 用户名>"
            )
            return

        if not target:
            keyword = args[0].strip().lstrip("@")
            yield event.plain_result(
                await self._render_user_today(keyword, f"{keyword} 今天")
            )
            return

        binding = self._get_binding(scope, target)
        if not binding:
            yield event.plain_result(f"{target} 还没有绑定 Bangumi 账号。")
            return
        label = binding.get("display") or binding["username"]
        yield event.plain_result(
            await self._render_user_today(binding["username"], f"{label} 今天")
        )

    @filter.command("bgm群报", alias={"bgm_group", "群报"})
    async def group_report(self, event: AstrMessageEvent):
        scope = self._scope_id(event)
        bindings = self._list_bindings(scope)
        if not bindings:
            yield event.plain_result(
                "本群还没有人绑定 Bangumi 账号。\n让群友发 /bgm绑定 <用户名> 即可。"
            )
            return

        yield event.plain_result(f"🔍 正在统计 {len(bindings)} 位群友的今日动态…")

        active: list[str] = []
        quiet: list[str] = []
        errors: list[str] = []

        for index, binding in enumerate(bindings):
            label = binding.get("display") or binding["username"]
            try:
                items = await self._fetch_collections(binding["username"])
            except UserNotFound:
                errors.append(f"{label}（用户不存在）")
                continue
            except BangumiError as exc:
                errors.append(f"{label}（{exc}）")
                continue

            today_items = self._today_items(items)
            if not today_items:
                quiet.append(label)
            else:
                block = [f"▸ {label} · {len(today_items)} 条"]
                for item in today_items[:MAX_ITEMS_PER_USER_IN_REPORT]:
                    block.append("    " + self._format_item(item))
                if len(today_items) > MAX_ITEMS_PER_USER_IN_REPORT:
                    block.append(
                        f"    …… 还有 {len(today_items) - MAX_ITEMS_PER_USER_IN_REPORT} 条"
                    )
                active.append("\n".join(block))

            if self.query_delay and index != len(bindings) - 1:
                await asyncio.sleep(self.query_delay)

        now = self._now()
        lines = [f"📊 Bangumi 群报 · {now:%m-%d}", f"本群已绑定 {len(bindings)} 人", ""]
        if active:
            lines.append(f"今天有动态：{len(active)} 人")
            lines.extend(active)
        else:
            lines.append("今天大家都还没有动静。")
        if quiet:
            lines.append("")
            lines.append(f"今天无动态：{'、'.join(quiet)}")
        if errors:
            lines.append("")
            lines.append(f"查询失败：{'、'.join(errors)}")
        yield event.plain_result("\n".join(lines))

    @filter.command("bgm帮助", alias={"bgm_help"})
    async def help_cmd(self, event: AstrMessageEvent):
        yield event.plain_result(HELP_TEXT)

    async def terminate(self):
        if self._session and not self._session.closed:
            await self._session.close()
