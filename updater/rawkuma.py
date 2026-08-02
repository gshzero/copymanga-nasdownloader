import logging
import re
from typing import Any, Dict, List, Tuple
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from utils.request import RequestHandler
from .base import BaseUpdater

log = logging.getLogger(__name__)

BASE_URL = "https://rawkuma.net"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/137.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8,ja;q=0.7",
}
request = RequestHandler(headers=HEADERS)


def _absolute_url(url: str) -> str:
    return urljoin(BASE_URL, url or "")


def _chapter_sort_key(chapter: Dict[str, Any]) -> float:
    raw_number = chapter.get("number")
    if raw_number not in (None, ""):
        try:
            return float(raw_number)
        except (TypeError, ValueError):
            pass

    match = re.search(r"(\d+(?:\.\d+)?)", chapter.get("title", ""))
    return float(match.group(1)) if match else 0


class RawkumaUpdater(BaseUpdater):
    SITE_NAME = "rawkuma"
    ID_FIELD = "manga_url"
    REQUIRED_FIELDS = BaseUpdater.REQUIRED_FIELDS + [
        "name"
    ]

    @classmethod
    def get_field_meta(cls) -> Dict[str, Dict[str, str]]:
        meta = super().get_field_meta()
        meta["manga_url"] = {
            "label": "Rawkuma 漫画详情页 URL",
            "type": "text",
            "placeholder": "https://rawkuma.net/...",
            "cols": 12,
            "required": True,
            "primary": True,
        }
        return meta

    def get_chapters(self, record: Dict) -> List[Dict]:
        manga_url = record["manga_url"]
        log.info(f"获取 Rawkuma 漫画页面: {record['name']} ({manga_url})")

        response = request.get(manga_url)
        if not response:
            log.error("无法获取 Rawkuma 漫画页面")
            return []

        soup = BeautifulSoup(response.text, "html.parser")
        chapters = []

        for row in soup.select("#chapter-list div[data-chapter-number]"):
            title_el = row.select_one(".font-medium")
            link_el = row.select_one("a[href]")
            drive_el = row.select_one('a[href*="drive.google.com"]')

            title = title_el.get_text(strip=True) if title_el else ""
            read_url = link_el.get("href", "") if link_el else ""
            dl_url = drive_el.get("href", "") if drive_el else ""
            number = row.get("data-chapter-number", "")

            if title and read_url:
                chapters.append({
                    "uuid": _absolute_url(read_url),
                    "title": title,
                    "dl_url": dl_url,
                    "number": number,
                })

        if not chapters:
            # Fallback for layout changes. Keep it conservative to avoid
            # pulling unrelated navigation links into subscriptions.
            for link in soup.select('a[href*="/chapter"], a[href*="/read"]'):
                title = link.get_text(" ", strip=True)
                href = link.get("href", "")
                if title and href:
                    item = {
                        "uuid": _absolute_url(href),
                        "title": title,
                        "dl_url": "",
                        "number": "",
                    }
                    if item not in chapters:
                        chapters.append(item)

        chapters = sorted(chapters, key=_chapter_sort_key)
        log.info(f"Rawkuma 解析到 {len(chapters)} 个章节")
        return chapters

    def find_subsequent_uuids(self, chapters: List[Dict], target_chapter: str) -> List[Tuple[str, str]]:
        if not chapters:
            return []

        if not target_chapter:
            return [(chap["uuid"], chap["title"]) for chap in chapters]

        target_index = -1
        for index, chapter in enumerate(chapters):
            if chapter["title"] == target_chapter:
                target_index = index
                break

        if target_index != -1 and target_index < len(chapters) - 1:
            return [(chap["uuid"], chap["title"]) for chap in chapters[target_index + 1:]]

        if target_index == -1:
            log.warning(f"未找到 Rawkuma 上次下载章节 '{target_chapter}'，可能是标题变更")

        return []

    def create_download_task(self, record: Dict, chapter_infos: List[Tuple[str, str]]) -> Dict[str, Any]:
        chapters = self.get_chapters(record)
        drive_links = {chapter["uuid"]: chapter.get("dl_url", "") for chapter in chapters}

        starting_index = 0
        if record.get("latest_chapter"):
            for chapter in chapters:
                starting_index += 1
                if chapter["title"] == record["latest_chapter"]:
                    break

        return {
            "site": self.SITE_NAME,
            "manga_url": record["manga_url"],
            "name": record["name"],
            "chapter_infos": chapter_infos,
            "drive_links": drive_links,
            "latest_chapter": record.get("latest_chapter", ""),
            "starting_index": starting_index,
        }
