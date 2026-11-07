from dataclasses import dataclass


@dataclass
class VideoItem:
    """视频数据模型(与 PHP 原代码字段一致)

    `media` 是播放地址(本站是 m3u8);`video_type` 是它的类型标记(hls/mp4),
    落盘时以 camelCase `videoType` 写入 JSON,其余字段名保持不变。
    """

    vod_type_id: int
    vod_type_name: str
    vod_id: int
    title: str
    thumd: str
    media: str
    video_type: str = ""
    time: str = ""
    heart: int = 0
    eye: int = 0
    uptime: str = ""
