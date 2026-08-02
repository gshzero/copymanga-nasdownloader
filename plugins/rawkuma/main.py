import logging
import os
import re
import time
import zipfile
from pathlib import Path
from typing import Any, Dict, List
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from downloader import postprocess
from updater import updater
from utils import config
from utils.notify import notifier
from utils.request import RequestHandler

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


def clean_filename(name: str) -> str:
    if not name:
        return "Unnamed"
    name = re.sub(r'[\\/:*?"<>|]', "_", str(name))
    name = name.strip(". ")
    return name or "Unnamed"


def absolute_url(url: str) -> str:
    return urljoin(BASE_URL, url or "")


def _safe_extract_zip(zip_ref: zipfile.ZipFile, target_dir: Path):
    target_root = target_dir.resolve()
    for member in zip_ref.infolist():
        dest = (target_dir / member.filename).resolve()
        if target_root not in dest.parents and dest != target_root:
            raise RuntimeError(f"Unsafe ZIP path: {member.filename}")
    zip_ref.extractall(target_dir)


def _download_google_drive_zip(dl_url: str, chapter_dir: Path) -> bool:
    try:
        response = request.get(dl_url)
        if not response:
            return False

        content_type = response.headers.get("content-type", "")
        if "text/html" in content_type:
            log.warning("Google Drive 返回 HTML 页面，无法直接作为 ZIP 下载")
            return False

        zip_path = chapter_dir / f"{chapter_dir.name}.zip"
        with open(zip_path, "wb") as f:
            for chunk in response.iter_bytes(chunk_size=1024 * 1024):
                if chunk:
                    f.write(chunk)

        if not zipfile.is_zipfile(zip_path):
            zip_path.unlink(missing_ok=True)
            log.warning("Google Drive 下载结果不是有效 ZIP")
            return False

        with zipfile.ZipFile(zip_path, "r") as zip_ref:
            _safe_extract_zip(zip_ref, chapter_dir)

        zip_path.unlink(missing_ok=True)
        return True
    except Exception as e:
        log.warning(f"Google Drive ZIP 下载失败: {e}")
        return False


def extract_image_urls(html: str) -> List[str]:
    soup = BeautifulSoup(html, "html.parser")
    urls = []

    selectors = [
        "section[data-image-data] img",
        "section.w-full.flex-col img",
        "article img",
        ".reader img",
        "img",
    ]

    for selector in selectors:
        for img in soup.select(selector):
            candidates = [
                img.get("src"),
                img.get("data-src"),
                img.get("data-lazy-src"),
                img.get("data-original"),
            ]

            srcset = img.get("srcset") or img.get("data-srcset")
            if srcset:
                candidates.extend(part.strip().split(" ")[0] for part in srcset.split(","))

            for candidate in candidates:
                if not candidate or candidate.startswith("data:"):
                    continue
                url = absolute_url(candidate)
                if url not in urls:
                    urls.append(url)

        if urls:
            break

    if not urls:
        pattern = r'https?:\\/\\/[^"\\]+\.(?:jpg|jpeg|png|webp)'
        for match in re.findall(pattern, html, flags=re.I):
            url = match.replace("\\/", "/")
            if url not in urls:
                urls.append(url)

    return urls


def _image_extension(url: str) -> str:
    suffix = Path(urlparse(url).path).suffix.lower()
    if suffix in {".jpg", ".jpeg", ".png", ".webp"}:
        return suffix
    return ".jpg"


def download_image(url: str, filename: str, referer: str) -> bool:
    headers = HEADERS.copy()
    headers["Referer"] = referer
    parsed = urlparse(referer)
    headers["Origin"] = f"{parsed.scheme}://{parsed.netloc}"
    image_request = RequestHandler(headers=headers)

    if os.path.exists(filename):
        log.info(f"文件已存在，跳过下载: {filename}")
        return True

    try:
        response = image_request.get(url)
        if not response or not response.content:
            log.error(f"无法获取 Rawkuma 图片: {url}")
            return False

        os.makedirs(os.path.dirname(filename), exist_ok=True)
        with open(filename, "wb") as f:
            f.write(response.content)
        return True
    except Exception as e:
        log.error(f"Rawkuma 图片下载失败: {url}, {e}")
        return False


def get_chapter_images(chapter_url: str) -> List[str]:
    response = request.get(chapter_url)
    if not response:
        return []
    return extract_image_urls(response.text)


def download_chapter(task: Dict[str, Any], chapter_url: str, chapter_title: str, chapter_index: int):
    series_name = clean_filename(task["name"])
    current_name = clean_filename(chapter_title)
    save_path = os.path.join(config.DOWNLOAD_PATH, "rawkuma", series_name, current_name)
    os.makedirs(save_path, exist_ok=True)

    chapter_dir = Path(save_path)
    dl_url = task.get("drive_links", {}).get(chapter_url, "")
    if dl_url and "drive.google.com" in dl_url:
        log.info(f"尝试 Rawkuma Google Drive ZIP 下载: {series_name} - {current_name}")
        if _download_google_drive_zip(dl_url, chapter_dir):
            chapter_num = task.get("starting_index", 0) + chapter_index + 1
            chapter_filename = f"{chapter_num:04d} {current_name}"
            postprocess(series_name, current_name, chapter_filename, chapter_num, save_path, False)
            updater.update_chapter_record(task["site"], task["manga_url"], chapter_title)
            notifier.add_success("rawkuma", task["name"], chapter_title)
            return True
        log.info("Google Drive ZIP 下载不可用，切换到网页图片抓取")

    image_list = get_chapter_images(chapter_url)
    if not image_list:
        log.error(f"无法获取 Rawkuma 章节图片: {chapter_title} ({chapter_url})")
        notifier.add_error("rawkuma", f"{task['name']} - {chapter_title}", "获取章节图片失败")
        return False

    log.info(f"Rawkuma {series_name} - {current_name} 获取到 {len(image_list)} 张图片")

    download_failed = False
    for index, url in enumerate(image_list):
        image_path = os.path.join(save_path, f"{index:04d}{_image_extension(url)}")
        if download_image(url, image_path, chapter_url):
            log.debug(f"已下载 Rawkuma 图片: {index:04d}")
        else:
            download_failed = True

    if download_failed:
        notifier.add_error("rawkuma", f"{task['name']} - {chapter_title}", "部分图片下载失败")
        return False

    chapter_num = task.get("starting_index", 0) + chapter_index + 1
    chapter_filename = f"{chapter_num:04d} {current_name}"

    postprocess(series_name, current_name, chapter_filename, chapter_num, save_path, False)
    updater.update_chapter_record(task["site"], task["manga_url"], chapter_title)
    notifier.add_success("rawkuma", task["name"], chapter_title)
    log.info(f"Rawkuma {series_name} - {current_name} CBZ 打包完成")
    return True


def download_task(task: Dict[str, Any]):
    if not task.get("chapter_infos"):
        log.info(f"{task['name']} 没有待下载章节")
        return

    for index, (uuid, name) in enumerate(task["chapter_infos"]):
        try:
            success = download_chapter(task, uuid, name, index)
            if not success:
                log.error(f"Rawkuma 章节下载失败: {task['name']} {name}")
        except Exception as e:
            log.error(f"Rawkuma 章节处理异常: {e}")
            notifier.add_error("rawkuma", f"{task['name']} - {name}", str(e))
        time.sleep(3)


def download_batch(tasks: List[Dict[str, Any]]):
    if not tasks:
        log.info("Rawkuma 无需更新")
        return

    for task in tasks:
        info = (
            f"Rawkuma 任务: {task['name']}\n"
            f"漫画URL: {task['manga_url']}\n"
            f"当前章节: {task.get('latest_chapter') or '无'}\n"
            f"待更新数: {len(task.get('chapter_infos', []))}"
        )
        log.info(info)
        download_task(task)

    log.info("所有 Rawkuma 任务处理完成")
