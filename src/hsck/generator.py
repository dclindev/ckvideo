import json
from collections import defaultdict
from pathlib import Path

from hsck.logger import get_logger
from hsck.utils import export_thumb_url, guess_video_type

logger = get_logger(__name__)

#: 是否额外产出 IPTV 用的 all.txt / all.m3u 订阅列表。
#:
#: 关掉的理由:App 只读 dist/all.json,这两个文件只有电视盒子/DIYP/PotPlayer
#: 这类播放器才用得上,却要每次 CI 全量重写(合计约 2.1 MB)。
#: 哪天需要了把它改成 True 就行,生成逻辑原样保留。
WRITE_PLAYLISTS: bool = False


class DistGenerator:
    KEEP_LIMIT = 5000

    def __init__(self, media_dir: Path, dist_dir: Path):
        self.media_dir = media_dir
        self.dist_dir = dist_dir
        dist_dir.mkdir(parents=True, exist_ok=True)

    def generate(self, image_host: str):
        logger.info("正在生成发布文件...")

        all_data = []
        categorized_data = defaultdict(list)

        dirs = [d for d in self.media_dir.iterdir() if d.is_dir()]
        dirs.sort(key=lambda x: int(x.name) if x.name.isdigit() else 0, reverse=True)

        data_count = 0

        for page_dir in dirs:
            if data_count > self.KEEP_LIMIT:
                logger.info(f"删除目录 {page_dir}")
                for f in page_dir.glob("*.json"):
                    f.unlink()
                page_dir.rmdir()
                continue

            json_files = sorted(
                page_dir.glob("*.json"),
                key=lambda f: int(f.stem) if f.stem.isdigit() else 0,
                reverse=True,
            )

            for json_file in json_files:
                try:
                    data = json.loads(json_file.read_text(encoding="utf-8"))
                except Exception:
                    logger.warning(f"跳过无效文件 {json_file}")
                    continue

                if not all(k in data for k in ["title", "thumd", "media"]):
                    logger.warning(f"文件缺少必要字段: {json_file}")
                    continue

                # 老缓存没有 videoType:按 media 后缀补齐,保证 all.json 字段一致
                if not data.get("videoType"):
                    data["videoType"] = guess_video_type(str(data.get("media") or ""))

                data["thumd"] = export_thumb_url(str(data.get("thumd") or ""), image_host)
                data_count += 1
                all_data.append(data)
                type_name = data.get("vod_type_name", "未分类")
                categorized_data[type_name].append(data)

        txt_content = ""
        m3u_content = "#EXTM3U\n"

        for type_name, items in sorted(categorized_data.items()):
            if not WRITE_PLAYLISTS:
                break
            for i, item in enumerate(items):
                group_idx = i // 50 + 1
                group_name = f"{type_name}{group_idx}"

                if i % 50 == 0:
                    txt_content += f"{group_name},#genre#\n"

                txt_content += f"{item['title']},{item['media']}\n"
                m3u_content += (
                    f'#EXTINF:-1 tvg-logo="{item["thumd"]}" '
                    f'group-title="{group_name}", {item["title"]}\n'
                    f"{item['media']}\n"
                )

        (self.dist_dir / "all.json").write_text(
            json.dumps(all_data, ensure_ascii=False), encoding="utf-8"
        )
        if WRITE_PLAYLISTS:
            (self.dist_dir / "all.txt").write_text(txt_content, encoding="utf-8")
            (self.dist_dir / "all.m3u").write_text(m3u_content, encoding="utf-8")

        logger.info(f"生成完成, 共 {len(all_data)} 条记录")

    def generate_source(self, media_dir: Path, out_name: str) -> int:
        """把另一个数据源的 media 目录汇总成 dist/<out_name>.json。

        逻辑与 [generate] 保持一致:
        * 同样按目录名(页码)倒序遍历,超过 [KEEP_LIMIT] 的旧目录直接删掉 ——
          否则附加源长期堆积会把仓库拖垮
        * 同样按 media 后缀补 videoType

        与主源的区别只有两点:
        * 不生成 txt/m3u 订阅列表(那些仍是主源的产物)
        * 不重写缩略图域名 —— 附加源的封面通常已是绝对地址

        :return: 写入的记录条数
        """
        records: list[dict[str, object]] = []
        if not media_dir.exists():
            logger.warning(f"源目录不存在: {media_dir}")
            return 0

        page_dirs = sorted(
            (d for d in media_dir.iterdir() if d.is_dir()),
            key=lambda d: int(d.name) if d.name.isdigit() else 0,
            reverse=True,
        )
        data_count = 0

        for page_dir in page_dirs:
            if data_count > self.KEEP_LIMIT:
                logger.info(f"删除目录 {page_dir}")
                for f in page_dir.glob("*.json"):
                    f.unlink()
                page_dir.rmdir()
                continue

            json_files = sorted(
                page_dir.glob("*.json"),
                key=lambda f: int(f.stem) if f.stem.isdigit() else 0,
                reverse=True,
            )
            for json_file in json_files:
                try:
                    data = json.loads(json_file.read_text(encoding="utf-8"))
                except Exception:
                    logger.warning(f"跳过无效文件 {json_file}")
                    continue
                if not all(k in data for k in ["title", "thumd", "media"]):
                    logger.warning(f"文件缺少必要字段: {json_file}")
                    continue
                if not data.get("videoType"):
                    data["videoType"] = guess_video_type(str(data.get("media") or ""))
                data_count += 1
                records.append(data)

        (self.dist_dir / f"{out_name}.json").write_text(
            json.dumps(records, ensure_ascii=False), encoding="utf-8"
        )
        logger.info(f"生成完成 {out_name}.json, 共 {len(records)} 条记录")
        return len(records)
