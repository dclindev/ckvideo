"""详情页 → 播放地址(优先 m3u8)的分层提取模块。

单独成文件,主爬虫只调用 `VideoExtractor.extract()`,不把逻辑塞进循环
(这样以后换站点,加线路都只改这里)。

分层顺序(命中即停,前置层最便宜):

1. `html-json`  —— `data-*` 属性里的 JSON(dplayer / videojs 常见)。
   例:`<div class="dplayer" data-config='{"video":{"url":"…m3u8","type":"hls"}}'>`
2. `html-tag`   —— `<video src>` / `<source src>` / `<iframe src>`
3. `meta`       —— `og:video` / `twitter:player:stream` 一类元信息
4. `inline-js`  —— 内联脚本,播放器配置变量里的地址
5. `api`        —— 站点自己的取流接口(通过 `api_hints` 注入,见 `playdata`)
6. `browser`    —— 浏览器自动化,默认关闭,只有显式开启才会走

设计约束:

* **永不抛异常**:失败返回 `ExtractResult(ok=False, error=...)`,调用方继续下一条。
* HTTP 走调用方传入的 `HttpClient`(httpx + 忽略证书 + 跟随跳转),不额外引依赖。
* 只依赖标准库 `html.parser` 解析 HTML,不需要 bs4/lxml —— GitHub Actions 上更轻。
* 同一页面可能有多个候选流(例如 `video` 与 `video_h265`),按键路径打分选主线路。
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import override
from urllib.parse import urljoin

from hsck.logger import get_logger
from hsck.utils import guess_video_type

logger = get_logger(__name__)

# 只读前若干字节判断是不是播放列表,避免把整条 m3u8 拉下来
PROBE_BYTES = 256

_URL_IN_TEXT = re.compile(
    r"""https?://[^\s"'<>\\]+?(?:\.m3u8|\.mp4)(?:\?[^\s"'<>\\]*)?""",
    re.IGNORECASE,
)


@dataclass
class ExtractResult:
    """一次提取的结果。失败时 `video_url` 为 None,`error` 说明原因。"""

    video_url: str | None = None
    video_type: str | None = None  # "hls" / "mp4"
    method: str = "none"  # html-json / html-tag / meta / inline-js / api / browser
    elapsed_ms: int = 0
    error: str | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.video_url)


def _unescape(text: str) -> str:
    """JSON 里的 `\\/`,`\\u0026` 与 HTML 实体还原成能直接播放的 URL。"""
    return text.replace("\\/", "/").replace("\\u0026", "&").replace("&amp;", "&")


class _PageScanner(HTMLParser):
    """一次遍历收集四类线索:data-* JSON,媒体标签,meta,内联脚本。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.json_attrs: list[tuple[str, str]] = []
        self.media_tags: list[tuple[str, str]] = []
        self.metas: list[tuple[str, str]] = []
        self.scripts: list[str] = []
        self._in_script = False

    @override
    def handle_starttag(self, tag, attrs):
        d = {k.lower(): str(v or "") for k, v in attrs}
        if tag == "script":
            self._in_script = True
        if tag in ("video", "source", "iframe", "embed"):
            src = d.get("src") or d.get("data-src") or ""
            if src:
                self.media_tags.append((tag, src))
        if tag == "meta":
            key = d.get("property") or d.get("name") or ""
            content = d.get("content") or ""
            if key and content:
                self.metas.append((key.lower(), content))
        for k, v in d.items():
            if k.startswith("data-") and v[:1] in "{[":
                self.json_attrs.append((k, v))

    @override
    def handle_startendtag(self, tag, attrs):
        # <source /> 这类自闭合标签不会走 handle_endtag,单独处理
        self.handle_starttag(tag, attrs)

    @override
    def handle_endtag(self, tag):
        if tag == "script":
            self._in_script = False

    @override
    def handle_data(self, data):
        if self._in_script and data.strip():
            self.scripts.append(data)


def _walk_json(
    node,
    path: tuple[str, ...],
    out: list[tuple[str, str, tuple[str, ...]]],
    depth: int = 0,
):
    """递归找 JSON 里的播放地址,并记录键路径(用来判断主流 / 备用流)。"""
    if depth > 8:
        return
    if isinstance(node, dict):
        for k, v in node.items():
            key = str(k).lower()
            if isinstance(v, str) and _URL_IN_TEXT.fullmatch(_unescape(v)):
                out.append((_unescape(v), key, (*path, key)))
            else:
                _walk_json(v, (*path, key), out, depth + 1)
    elif isinstance(node, list):
        for item in node:
            _walk_json(item, path, out, depth + 1)


def _score_by_path(path: tuple[str, ...]) -> int:
    """主流优先:`video.url` > 其它 url > h265/hevc 备用流。"""
    joined = ".".join(path)
    if joined.endswith("video.url"):
        return 120
    if "h265" in joined or "hevc" in joined:
        return 40
    if joined.endswith(("url", "src", "playurl", "m3u8")):
        return 90
    return 70


class VideoExtractor:
    """从详情页提取播放地址。

    :param client: `hsck.http_client.HttpClient` 实例(复用连接与证书设置)
    :param verify: 是否真的 GET 一下候选地址,确认能播(线上建议 True)
    """

    def __init__(self, client, *, verify: bool = True):
        self.client = client
        self.verify = verify

    # ── 内部:HTTP ────────────────────────────────────────────
    def _get_text(self, url: str) -> str:
        try:
            return str(self.client.get_html(url) or "")
        except Exception as e:  # HttpClient 自己也会兜,但这里再兜一层
            logger.debug(f"取页失败 {url}: {e}")
            return ""

    def _probe(self, url: str) -> tuple[bool, str]:
        """确认地址真的能播:m3u8 应返回 #EXTM3U;mp4 看 content-type。"""
        try:
            resp = self.client.get(url)
            status = getattr(resp, "status_code", 0)
            if status != 200:
                return False, f"HTTP {status}"
            body = getattr(resp, "content", b"")[:PROBE_BYTES] or b""
            text = body.decode("utf-8", "replace")
            if text.lstrip().startswith("#EXTM3U"):
                return True, "hls-ok"
            ctype = resp.headers.get("content-type", "") if hasattr(resp, "headers") else ""
            if "video" in ctype:
                return True, "mp4-ok"
            return False, "内容不像播放列表"
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"

    # ── 各层候选 ──────────────────────────────────────────────
    def _layer_html_json(self, scanner: _PageScanner) -> list[tuple[int, str, str, str]]:
        cands: list[tuple[int, str, str, str]] = []
        for attr, raw in scanner.json_attrs:
            try:
                obj = json.loads(_unescape(raw))
            except Exception:
                continue
            found: list[tuple[str, str, tuple[str, ...]]] = []
            _walk_json(obj, (), found)
            for url, _key, path in found:
                vtype = guess_video_type(url)
                if vtype:
                    cands.append(
                        (_score_by_path(path), url, vtype, f"html-json:{attr}:{'.'.join(path)}")
                    )
        return cands

    def _layer_html_tags(self, scanner: _PageScanner) -> list[tuple[int, str, str, str]]:
        cands: list[tuple[int, str, str, str]] = []
        for tag, src in scanner.media_tags:
            url = _unescape(src)
            vtype = guess_video_type(url)
            if tag == "iframe":
                # iframe 本身不是流,但说明播放器在另一个页面里
                cands.append((10, url, "iframe", f"html-tag:{tag}"))
            elif vtype:
                cands.append((100 if tag == "video" else 95, url, vtype, f"html-tag:{tag}"))
        return cands

    def _layer_meta(self, scanner: _PageScanner) -> list[tuple[int, str, str, str]]:
        cands: list[tuple[int, str, str, str]] = []
        for key, content in scanner.metas:
            if not re.search(r"(video|player|stream)", key):
                continue
            url = _unescape(content)
            vtype = guess_video_type(url)
            if vtype:
                cands.append((85, url, vtype, f"meta:{key}"))
        return cands

    def _layer_inline_js(self, scanner: _PageScanner) -> list[tuple[int, str, str, str]]:
        cands: list[tuple[int, str, str, str]] = []
        for idx, body in enumerate(scanner.scripts):
            for m in _URL_IN_TEXT.finditer(body):
                url = _unescape(m.group(0))
                vtype = guess_video_type(url)
                if not vtype:
                    continue
                near = body[max(0, m.start() - 80) : m.start()].lower()
                score = 60 if ("h265" in near or "hevc" in near) else 80
                cands.append((score, url, vtype, f"inline-js#{idx}"))
        return cands

    def _layer_api(
        self, page_url: str, api_hints: Callable[[str], list[tuple[str, str]]] | None
    ) -> list[tuple[int, str, str, str]]:
        """第 5 层:站点取流接口。由调用方把「怎么问接口」注入进来。"""
        if api_hints is None:
            return []
        try:
            out: list[tuple[int, str, str, str]] = []
            for url, note in api_hints(page_url):
                full = urljoin(page_url, _unescape(url))
                vtype = guess_video_type(full) or "hls"
                out.append((110, full, vtype, f"api:{note}"))
            return out
        except Exception as e:  # 接口层的任何问题都不能拖垮提取
            logger.debug(f"api_hints 失败 page={page_url}: {e}")
            return []

    # ── 对外入口 ──────────────────────────────────────────────
    def extract(
        self,
        page_url: str,
        *,
        html: str | None = None,
        api_hints: Callable[[str], list[tuple[str, str]]] | None = None,
    ) -> ExtractResult:
        """提取播放地址。**不抛异常**,失败时返回 ok=False 的结果。"""
        started = time.time()

        def finish(res: ExtractResult) -> ExtractResult:
            res.elapsed_ms = int((time.time() - started) * 1000)
            if res.ok:
                logger.info(
                    f"提取成功 page={page_url} method={res.method} "
                    f"type={res.video_type} {res.elapsed_ms}ms"
                )
            else:
                logger.warning(
                    f"提取失败 page={page_url} method={res.method} "
                    f"{res.elapsed_ms}ms err={res.error}"
                )
            return res

        try:
            text = html if html is not None else self._get_text(page_url)
            if not text:
                return finish(ExtractResult(method="none", error="详情页为空或抓取失败"))

            scanner = _PageScanner()
            try:
                scanner.feed(text)
            except Exception as e:
                logger.debug(f"HTMLParser 异常,转正则兜底 page={page_url}: {e}")

            cands = [
                *self._layer_html_json(scanner),
                *self._layer_html_tags(scanner),
                *self._layer_meta(scanner),
                *self._layer_inline_js(scanner),
                *self._layer_api(page_url, api_hints),
            ]

            if not cands:
                for m in _URL_IN_TEXT.finditer(text):
                    url = _unescape(m.group(0))
                    vtype = guess_video_type(url)
                    if vtype:
                        cands.append((50, url, vtype, "raw-regex"))

            if not cands:
                return finish(ExtractResult(method="none", error="页面里没有 m3u8/mp4 线索"))

            # 去重 + 打分排序(同分保持出现顺序)
            seen: dict[str, tuple[int, str, str, str]] = {}
            for score, url, vtype, method in cands:
                if url not in seen or score > seen[url][0]:
                    seen[url] = (score, url, vtype, method)
            ordered = sorted(seen.values(), key=lambda x: -x[0])
            notes = [f"{m}={u[:60]}" for _, u, _, m in ordered[:5]]

            last_err = "候选全部不可播"
            for _score, url, vtype, method in ordered:
                if self.verify:
                    ok, why = self._probe(url)
                    if not ok:
                        last_err = f"{method} 候选不可播({why})"
                        continue
                    if why == "hls-ok":
                        vtype = "hls"
                return finish(
                    ExtractResult(video_url=url, video_type=vtype, method=method, notes=notes)
                )

            return finish(ExtractResult(method="none", error=last_err, notes=notes))

        except Exception as e:  # 最后一道防线
            return finish(ExtractResult(method="error", error=f"{type(e).__name__}: {e}"))


if __name__ == "__main__":
    # 独立自测:python -m hsck.scraper.video_extractor <详情页URL>...
    import sys

    from hsck.http_client import HttpClient

    extractor = VideoExtractor(HttpClient())
    for target in sys.argv[1:]:
        r = extractor.extract(target)
        print(
            json.dumps(
                {
                    "page": target,
                    "videoUrl": r.video_url,
                    "videoType": r.video_type,
                    "method": r.method,
                    "elapsed_ms": r.elapsed_ms,
                    "error": r.error,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
