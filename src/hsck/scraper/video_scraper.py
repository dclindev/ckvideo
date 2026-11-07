import json
import re
import time
from collections.abc import Iterator
from pathlib import Path
from typing import TypedDict
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from hsck.http_client import HttpClient
from hsck.logger import get_logger
from hsck.models import VideoItem
from hsck.scraper.video_extractor import VideoExtractor
from hsck.utils import get_page_dir, guess_video_type, is_absolute_url, normalize_thumb_url

logger = get_logger(__name__)


class VideoInfo(TypedDict):
    vod_id: int
    title: str
    thumd: str
    time: str
    heart: int
    eye: int
    uptime: str
    # 详情页相对路径(如 /v5/280743-1-1.html),接口失败时用它做第二层提取
    detail_path: str


class VideoScraper:
    def __init__(self, client: HttpClient, host: str, media_dir: Path):
        self.client = client
        self.host = host
        self.media_dir = media_dir
        self.thumbs_updated = False
        # 详情页提取器:接口拿不到地址时的第二层兜底(见 _fetch_media_url)
        self.extractor = VideoExtractor(client)

    def _parse_list_page(self, html: str) -> list[VideoInfo]:
        soup = BeautifulSoup(html, "lxml")
        videos: list[VideoInfo] = []

        vodlist = soup.find("ul", class_="stui-vodlist")
        if not vodlist:
            return videos

        for li in vodlist.find_all("li"):
            thumb = li.find(
                "a",
                class_="stui-vodlist__thumb lazyload",
                href=re.compile(r"^/[^/]+/\d+-1-1\.html$"),
            )

            if not thumb or not thumb.get("href") or not thumb.get("title"):
                continue

            vod_id_match = re.search(r"/[^/]+/(\d+)-1-1\.html", str(thumb["href"]))
            if not vod_id_match:
                continue
            vod_id = int(vod_id_match.group(1))

            time_span = thumb.find("span", class_="pic-text text-right")
            video_time = time_span.text.strip() if time_span else ""

            heart = 0
            eye = 0
            uptime = ""

            sub_p = li.find("p", class_="sub")
            if sub_p:
                heart_span = sub_p.find("span", class_="number pull-right")
                if heart_span:
                    heart_match = re.search(r"(\d+)", heart_span.text)
                    if heart_match:
                        heart = int(heart_match.group(1))

                eye_span = None
                eye_spans = sub_p.find_all("span", class_="pull-right")
                for span in eye_spans:
                    if span.find("i", class_="fa-eye"):
                        eye_span = span
                        break

                if eye_span:
                    eye_match = re.search(r"(\d+)", eye_span.text)
                    if eye_match:
                        eye = int(eye_match.group(1))

                uptime_text = ""
                for content in sub_p.contents:
                    if isinstance(content, str):
                        uptime_text += content.strip()
                uptime = uptime_text.strip()

            title_str = str(thumb.get("title", "") or "")
            clean_title = title_str.strip()
            thumd_url = normalize_thumb_url(str(thumb.get("data-original", "") or ""), self.host)
            videos.append(
                {
                    "vod_id": vod_id,
                    "title": clean_title,
                    "thumd": thumd_url,
                    "time": video_time,
                    "heart": heart,
                    "eye": eye,
                    "uptime": uptime,
                    # 记下详情页路径:接口失败时才需要它,但此时还拿得到
                    "detail_path": str(thumb.get("href", "") or ""),
                }
            )

        return videos

    def _fetch_media_from_api(self, vod_id: int) -> str | None:
        """站点取流接口。本站详情页里没有流地址,接口是唯一来源,也最快。"""
        timestamp = int(time.time() * 1000)
        api_url = f"{self.host}/playdata.php?id={vod_id}&sid=1&nid=1&_={timestamp}&r=0"

        raw = self.client.get_html(api_url)
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return None
        if not data.get("ok"):
            return None
        url = (data.get("p") or {}).get("url")
        return url.replace("\\/", "/") if url else None

    def _fetch_media_url(self, vod_id: int, detail_path: str = "") -> tuple[str | None, str, str]:
        """取播放地址:先接口,接口拿不到再回详情页做分层提取。

        :return: (播放地址, 命中方式, videoType)。失败时地址为 None。
        """
        url = self._fetch_media_from_api(vod_id)
        if url:
            return url, "api:playdata", guess_video_type(url) or "hls"

        if not detail_path:
            logger.warning(f"接口无地址且缺少详情页路径: {vod_id}")
            return None, "none", ""

        page_url = urljoin(self.host, detail_path)
        result = self.extractor.extract(page_url)
        if result.ok:
            return result.video_url, result.method, result.video_type or "hls"

        logger.warning(f"详情页也未提取到播放地址: {vod_id} {page_url} -> {result.error}")
        return None, result.method, ""

    def scrape_videos(self, vod_type_id: int, vod_type_name: str, page: int) -> Iterator[VideoItem]:
        url = f"{self.host}/vodtype/{vod_type_id}-{page}.html"
        logger.debug(f"正在采集: {vod_type_id} {vod_type_name} 第 {page} 页")

        html = self.client.get_html(url)
        video_infos = self._parse_list_page(html)

        for info in video_infos:
            page_dir = self.media_dir / get_page_dir(info["vod_id"])
            cache_file = page_dir / f"{info['vod_id']}.json"

            if cache_file.exists():
                try:
                    cached = json.loads(cache_file.read_text(encoding="utf-8"))
                    changed = False
                    if cached.get("heart") != info["heart"] or cached.get("eye") != info["eye"]:
                        cached["heart"] = info["heart"]
                        cached["eye"] = info["eye"]
                        changed = True
                    live_thumb = info["thumd"]
                    cached_thumb = str(cached.get("thumd") or "")
                    if is_absolute_url(live_thumb) and live_thumb != cached_thumb:
                        cached["thumd"] = live_thumb
                        changed = True
                        self.thumbs_updated = True
                    if changed:
                        cache_file.write_text(
                            json.dumps(cached, ensure_ascii=False, indent=2),
                            encoding="utf-8",
                        )
                        logger.debug(f"更新缓存: {info['vod_id']} {info['title']}")
                    else:
                        logger.debug(f"跳过已存在: {info['vod_id']} {info['title']}")
                except Exception:
                    pass
                continue

            media_url, method, video_type = self._fetch_media_url(
                info["vod_id"], info.get("detail_path", "")
            )
            if not media_url:
                # 单条失败只记日志,继续下一条,绝不中断整批(原行为保持一致)
                logger.warning(
                    f"无法获取媒体地址: {info['vod_id']} {info['title']} (方式={method})"
                )
                continue
            logger.debug(f"地址来源 {info['vod_id']}: {method} type={video_type}")

            yield VideoItem(
                vod_id=info["vod_id"],
                vod_type_id=vod_type_id,
                vod_type_name=vod_type_name,
                title=info["title"],
                thumd=info["thumd"],
                media=media_url,
                video_type=video_type,
                time=info["time"],
                heart=info["heart"],
                eye=info["eye"],
                uptime=info["uptime"],
            )
