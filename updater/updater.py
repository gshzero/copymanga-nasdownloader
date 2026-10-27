import json
import logging
from pathlib import Path
from typing import Dict, List, Any

from utils import config
from utils.notify import notifier
from . import copymanga, terra_historicus, antbyw, ganganonline, bilibili, rawkuma

log = logging.getLogger(__name__)

# 站点映射
SITE_MAPPING = {
    "copymanga": copymanga.CopyMangaUpdater,
    "terra_historicus": terra_historicus.TerraHistoricusUpdater,
    "antbyw": antbyw.AntbywUpdater,
    "ganganonline": ganganonline.GanganOnlineUpdater,
    "bilibili": bilibili.BilibiliUpdater,
    "rawkuma": rawkuma.RawkumaUpdater,
}


def load_updater_json(file_path: str = "updater.json") -> Dict[str, List[Dict]]:
    path = Path(config.DATA_PATH, file_path)
    if not path.exists():
        return {}

    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)

            # 验证数据结构
            for site, records in data.items():
                site_class = SITE_MAPPING.get(site)
                if not site_class:
                    continue

                for record in records:
                    if not site_class.validate_record(record):
                        raise ValueError(f"站点 {site} 的记录结构错误")

            return data
    except Exception as e:
        log.error(f"加载updater.json出错: {e}")
        return {}


def save_updater_json(site_data: Dict[str, List[Dict]], file_path: str = "updater.json") -> bool:
    try:
        with open(Path(config.DATA_PATH, file_path), 'w', encoding='utf-8') as f:
            json.dump(site_data, f, indent=2, ensure_ascii=False)
        return True
    except Exception as e:
        log.error(f"保存updater.json失败: {e}")
        return False


def _downloaded_chapters_for_name(records: List[Dict], name: str) -> set[str]:
    """汇总 updater.json 中同站点、同名漫画已经记录的章节 UUID。"""
    downloaded = set()
    for record in records:
        if record.get('name') != name:
            continue
        downloaded.update(record.get('downloaded_chapters', []) or [])
    return downloaded


def process_updates() -> List[Dict[str, Any]]:
    tasks = []
    site_data = load_updater_json()
    records_modified = False

    for site_name, records in site_data.items():
        site_class = SITE_MAPPING.get(site_name)
        if not site_class:
            log.error(f"未知站点: {site_name}")
            continue

        site_instance = site_class()
        scheduled_chapters = set()

        for record in records:
            try:
                chapters = site_instance.get_chapters(record)
                old_downloaded = list(record.get('downloaded_chapters', []) or [])

                # 同名订阅共享已下载章节记录，避免同一漫画的多条订阅重复生成任务。
                downloaded_for_name = _downloaded_chapters_for_name(records, record.get('name', ''))
                record_for_check = record.copy()
                record_for_check['downloaded_chapters'] = list(downloaded_for_name)

                # 使用缺失章节检测：对比 get_chapters 完整列表与已下载章节（含磁盘CBZ检查）
                uuids = site_instance.find_missing_chapters(chapters, record_for_check)
                record_key = record.get('name', '')
                uuids = [
                    item for item in uuids
                    if (record_key, item[0]) not in scheduled_chapters
                ]
                scheduled_chapters.update((record_key, uuid) for uuid, _ in uuids)

                if list(record_for_check.get('downloaded_chapters', []) or []) != old_downloaded:
                    record['downloaded_chapters'] = record_for_check['downloaded_chapters']
                    records_modified = True

                if uuids:
                    notifier.add_update(site_name, record.get('name', '未知漫画'))
                    task = site_instance.create_download_task(record, uuids, chapters=chapters)
                    tasks.append(task)
                else:
                    log.info(f"{record.get('name', '未知漫画')} 没有需要下载的章节，全部已存在")

            except Exception as e:
                err_msg = f"处理站点 {site_name} 记录失败: {e}"
                log.error(err_msg)
                notifier.add_error(site_name, record.get('name', '更新检查'), str(e))

    if records_modified:
        log.info("检测到已下载历史记录回填变动，保存更新至 updater.json")
        save_updater_json(site_data)

    return tasks


def update_chapter_record(site_name: str, comic_id: str, new_chapter: str,
                          new_uuid: str = "", file_path: str = "updater.json") -> bool:
    site_data = load_updater_json(file_path)

    # 获取该站点的记录列表
    site_records = site_data.get(site_name, [])
    site_class = SITE_MAPPING.get(site_name)

    if not site_class:
        log.error(f"未知站点: {site_name}")
        return False

    site_instance = site_class()
    updated = False

    for i, record in enumerate(site_records):
        record_id = site_instance.get_unique_id(record)

        if record_id == comic_id:
            site_records[i] = site_instance.update_record(record, new_chapter, new_uuid)
            updated = True
            break

    if not updated:
        log.error(f"找不到站点 {site_name} 中ID为 {comic_id} 的记录")
        return False

    # 更新整个站点数据
    site_data[site_name] = site_records

    # 写回文件
    return save_updater_json(site_data, file_path)
