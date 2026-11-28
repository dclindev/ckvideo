"""xhamster(zh.xhamster.com) 采集器。

与前一个源(ahtck)的根本区别:**这里不存播放地址,只存详情页地址**。

原因:xhamster 的 m3u8 路径里带签名和时间戳,实测 **约 4 小时失效**
(`xxx==,1792695600/media=hls4/...`,其中 1792695600 就是过期时刻)。每天抓一次
把流地址写进 JSON,用户晚上打开时已经全是死链。所以:

* 采集端只负责「有哪些片子 + 元数据 + 详情页 URL」;
* App 在点播放时现场去详情页解析出 m3u8(见 App 侧 XhamsterResolver)。

记录里的约定(App 靠它分支):
* `media`      = 详情页 URL(不是流地址)
* `videoType`  = `"hls-page"` —— 表示「这是页面, 播放前需解析」

数据来源:
* `/newest/N`                最新,逐页倒序
* `/categories/<slug>/N`     分类,用于撑起 App 的分类滑动手势

这两种页面都把条目直接写在 `window.initials` 这个 16 万字符的 JSON 里
(`layoutPage.videoListProps.videoThumbProps`,分类页则是
`pagesCategoryComponent.*.videoThumbProps`),所以**不用解析 DOM**:
截出 JSON 后递归找「同时含 duration 与 pageURL 的对象」即可,
站点改版换个组件名也不会崩。
"""

import json
import re
import time
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import cast

from hsck.http_client import HttpClient
from hsck.logger import get_logger
from hsck.models import VideoItem

logger = get_logger(__name__)

BASE_URL = "https://zh.xhamster.com"

#: 采集哪些分类。键是站点 slug,值是 (写进 JSON 的分类 id, App 里显示的分类名)。
#: id 全是固定值,改顺序或增删都不会错位。
CATEGORIES: dict[str, tuple[int, str]] = {
    "japanese": (2001, "日本"),
    "jav": (2002, "JAV"),
    "asian": (2003, "亚洲"),
    "amateur": (2004, "素人"),
    "homemade": (2005, "自拍"),
    "wife": (2006, "人妻"),
    "creampie": (2007, "内射"),
    "uncensored": (2008, "无码"),
    "mature": (2009, "熟女"),
    "lesbian": (2010, "女同"),
    "teen": (2011, "少女"),
    "hentai": (2012, "动漫"),
}

#: /newest 流落入的分类
NEWEST_TYPE_ID = 2000
NEWEST_TYPE_NAME = "最新"

#: 记录里的 videoType 值:告诉 App「media 是页面, 播放前要解析」
VIDEO_TYPE_PAGE = "hls-page"

#: 两次列表页请求之间的间隔(秒)。站点没有公开限流说明, 自己控节奏。
FETCH_INTERVAL = 0.4

_INITIALS_MARK = re.compile(r"window\.initials\s*=\s*")

Item = dict[str, object]


def _as_int(value: object) -> int | None:
    """把 JSON 里的数字字段转成 int;转不了返回 None。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _extract_initials(html: str) -> Item | None:
    """截出 `window.initials = {...}` 那段 JSON。

    不能用正则贪婪匹配:这段 JSON 有 16 万字符且内含成对括号,
    必须做括号配平(同时跳过字符串里的括号与转义)。
    """
    mark = _INITIALS_MARK.search(html)
    if mark is None:
        return None
    start = html.find("{", mark.end())
    if start < 0:
        return None

    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(html)):
        ch = html[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    parsed = json.loads(html[start : i + 1])
                except json.JSONDecodeError:
                    return None
                return cast(Item, parsed) if isinstance(parsed, dict) else None
    return None


def _collect_items(node: object, out: list[Item], depth: int = 0) -> None:
    """递归收集所有「同时含 duration 与 pageURL」的对象。

    不写死组件路径:列表页与分类页的组件名不同, 站点一改版写死的路径就废了。
    """
    if depth > 9:
        return
    if isinstance(node, dict):
        if "duration" in node and "pageURL" in node and "id" in node:
            out.append(node)
        for value in node.values():
            _collect_items(value, out, depth + 1)
    elif isinstance(node, list):
        for value in node:
            _collect_items(value, out, depth + 1)


def _duration_text(seconds: object) -> str:
    total = _as_int(seconds)
    if total is None or total <= 0:
        return ""
    return f"{total // 60}:{total % 60:02d}"


def _uptime_text(created: object) -> str:
    """列表给的是 unix 时间戳, 转成与主站一致的 MM-DD。"""
    ts = _as_int(created)
    if ts is None or ts <= 0:
        return ""
    return datetime.fromtimestamp(ts).strftime("%m-%d")


class XhamsterScraper:
    """xhamster 采集器:只抓元数据 + 详情页地址,不抓流地址。"""

    def __init__(self, client: HttpClient, media_dir: Path):
        self.client = client
        self.media_dir = media_dir
        self.max_seen_created = 0

    # ── 列表页 ────────────────────────────────────────────────
    @staticmethod
    def newest_url(page: int) -> str:
        return f"{BASE_URL}/newest/{page}"

    @staticmethod
    def category_url(slug: str, page: int) -> str:
        return f"{BASE_URL}/categories/{slug}/{page}"

    def fetch_items(self, url: str) -> list[Item]:
        """抓一页并取出条目。失败返回空列表, 不抛异常。"""
        html = self.client.get_html(url)
        if not html:
            logger.warning(f"xhamster 列表页抓取失败: {url}")
            return []

        data = _extract_initials(html)
        if data is None:
            logger.warning(f"xhamster 页面里找不到 window.initials: {url}")
            return []

        time.sleep(FETCH_INTERVAL)

        found: list[Item] = []
        _collect_items(data, found)

        # 同一页可能既有主列表又有推荐位, 按 id 去重
        unique: dict[int, Item] = {}
        for item in found:
            vid = _as_int(item.get("id"))
            if vid is not None:
                unique.setdefault(vid, item)
        return list(unique.values())

    # ── 条目 -> VideoItem ─────────────────────────────────────
    def to_video_item(self, item: Item, type_id: int, type_name: str) -> VideoItem | None:
        vod_id = _as_int(item.get("id"))
        if vod_id is None:
            return None

        page_url = str(item.get("pageURL") or "").strip()
        if not page_url.startswith("http"):
            return None

        title = str(item.get("title") or "").strip()
        if not title:
            return None

        thumb = str(item.get("thumbURL") or item.get("imageURL") or "").strip()
        views = _as_int(item.get("views")) or 0
        created_ts = _as_int(item.get("created")) or 0
        if created_ts > self.max_seen_created:
            self.max_seen_created = created_ts

        return VideoItem(
            vod_type_id=type_id,
            vod_type_name=type_name,
            vod_id=vod_id,
            title=title,
            thumd=thumb,
            # 关键:存页面地址, 不存流地址(流地址 4 小时就过期)
            media=page_url,
            video_type=VIDEO_TYPE_PAGE,
            time=_duration_text(item.get("duration")),
            heart=0,
            eye=views,
            uptime=_uptime_text(item.get("created")),
        )

    # ── 对外主流程 ────────────────────────────────────────────
    def _emit(
        self,
        items: list[Item],
        type_id: int,
        type_name: str,
        emitted: dict[int, VideoItem],
        since_created: int,
        target: int | None,
    ) -> Iterator[VideoItem]:
        for item in items:
            if target is not None and len(emitted) >= target:
                return
            vid = _as_int(item.get("id"))
            created_ts = _as_int(item.get("created")) or 0
            if vid is None or vid in emitted:
                continue
            if since_created and created_ts <= since_created:
                continue
            video = self.to_video_item(item, type_id, type_name)
            if video is None:
                continue
            emitted[vid] = video
            yield video

    def scrape(
        self,
        *,
        since_created: int = 0,
        target: int | None = None,
        max_pages: int = 120,
    ) -> Iterator[VideoItem]:
        """抓取并产出条目。

        :param since_created: 上次抓到的最大 created 时间戳(增量游标)
        :param target: 本次最多产出多少条;None 表示只要新的
        :param max_pages: 本次最多请求多少页(分类页 + 最新页), 防止单次跑飞
        """
        emitted: dict[int, VideoItem] = {}
        pages = 0

        def done() -> bool:
            return target is not None and len(emitted) >= target

        # 先抓分类页:App 首页每一屏对应一个分类, 没有分类就只剩一个"最新"
        depth = 3 if target is not None else 1
        for slug, (type_id, type_name) in CATEGORIES.items():
            if done() or pages >= max_pages:
                break
            for page in range(1, depth + 1):
                if done() or pages >= max_pages:
                    break
                pages += 1
                items = self.fetch_items(self.category_url(slug, page))
                if not items:
                    break
                before = len(emitted)
                yield from self._emit(items, type_id, type_name, emitted, since_created, target)
                added = len(emitted) - before
                logger.info(
                    f"xhamster 分类 {slug} 第 {page} 页: {len(items)} 条, "
                    f"新增 {added} 条, 累计 {len(emitted)}"
                )
                if added == 0:
                    break

        # 再抓 /newest 补足数量(与分类会有重叠, 按 id 去重)
        page = 1
        while not done() and pages < max_pages:
            pages += 1
            items = self.fetch_items(self.newest_url(page))
            if not items:
                break
            before = len(emitted)
            yield from self._emit(
                items, NEWEST_TYPE_ID, NEWEST_TYPE_NAME, emitted, since_created, target
            )
            added = len(emitted) - before
            logger.info(
                f"xhamster 最新 第 {page} 页: {len(items)} 条, 新增 {added} 条, 累计 {len(emitted)}"
            )
            if added == 0 and since_created:
                logger.info("xhamster 已追平游标, 停止翻页")
                break
            page += 1
