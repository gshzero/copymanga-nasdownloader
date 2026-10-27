import importlib
import logging
from typing import Dict, List, Any

from utils.notify import notifier
from utils.cbz_helper import compute_target_cbz_path, is_cbz_already_downloaded
from updater import updater

log = logging.getLogger(__name__)


class DownloadDispatcher:
    SITE_MODULES = {
        "copymanga": "plugins.copymanga.main",
        "terra_historicus": "plugins.terra_historicus.main",
        "antbyw": "plugins.antbyw.main",
        "ganganonline": "plugins.ganganonline.main",
        "bilibili": "plugins.bilibili.main",
        "rawkuma": "plugins.rawkuma.main",
    }

    @classmethod
    def download_site(cls, tasks: List[Dict[str, Any]]):
        """下载指定站点的任务"""
        site_tasks = {}

        for task in tasks:
            site = task['site']
            if site not in site_tasks:
                site_tasks[site] = []
            site_tasks[site].append(task)

        for site, tasks in site_tasks.items():
            if site in cls.SITE_MODULES:
                module_path = cls.SITE_MODULES[site]
                try:
                    module = importlib.import_module(module_path)
                    log.info(f"开始处理 {site} 的 {len(tasks)} 个下载任务")

                    # 过滤掉已经存在的章节（已生成的cbz文件），只下载缺失的章节
                    filtered_tasks = []
                    for task in tasks:
                        chapter_infos = task.get('chapter_infos', [])
                        if not chapter_infos:
                            filtered_tasks.append(task)
                            continue

                        chapter_meta = task.get('chapter_meta', {})
                        comic_id = (
                            task.get('path_word') or
                            task.get('comic_id') or
                            task.get('id') or
                            task.get('manga_url')
                        )

                        remaining = []
                        for index, (uuid, name) in enumerate(chapter_infos):
                            meta = chapter_meta.get(uuid)
                            chap_num = meta.get('chapter_num') if meta else (index + 1)

                            if is_cbz_already_downloaded(site, task, name, chapter_num=chap_num, chapter_meta=meta):
                                c_desc = meta.get('chapter_filename') if meta else name
                                log.info(f"已存在，跳过下载: {task['name']} - {c_desc}")
                                if comic_id:
                                    updater.update_chapter_record(site, comic_id, name, uuid)
                                continue

                            remaining.append((uuid, name))

                        if remaining:
                            new_task = task.copy()
                            new_task['chapter_infos'] = remaining
                            filtered_tasks.append(new_task)
                        else:
                            log.info(f"{task['name']} 没有需要下载的章节，全部已存在")

                    if filtered_tasks:
                        module.download_batch(filtered_tasks)
                    else:
                        log.info(f"{site} 的所有任务已跳过（无缺失章节）")

                    log.info(f"{site} 的所有任务处理完成")
                except ImportError as e:
                    err_msg = f"找不到 {site} 站点的下载器: {e}"
                    log.error(err_msg)
                    notifier.add_error(site, "调度器", err_msg)
                except Exception as e:
                    err_msg = f"处理 {site} 下载任务时出错: {e}"
                    log.error(err_msg)
                    notifier.add_error(site, "调度器", str(e))
            else:
                err_msg = f"未知站点: {site}, 无法处理下载任务"
                log.error(err_msg)
                notifier.add_error(site, "调度器", err_msg)
