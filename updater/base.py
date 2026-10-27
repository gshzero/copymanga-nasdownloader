import logging
from datetime import datetime
from typing import Dict, List, Any, Tuple

from utils import config
from utils.cbz_helper import compute_target_cbz_path, is_cbz_already_downloaded

log = logging.getLogger(__name__)


class BaseUpdater:
    SITE_NAME = "base"
    ID_FIELD = ""
    REQUIRED_FIELDS = ['last_download_date', 'latest_chapter']

    @classmethod
    def get_field_meta(cls) -> Dict[str, Dict[str, str]]:
        meta = {}
        # 默认字段配置
        defaults = {
            'name': {'label': '漫画名称', 'type': 'text', 'placeholder': '请输入文件夹名称', 'cols': 6},
            'comic_id': {'label': '漫画ID', 'type': 'text', 'placeholder': 'Site ID', 'cols': 6},
            'path_word': {'label': '路径词 (path_word)', 'type': 'text', 'cols': 6},
            'group_word': {'label': '汉化组/分类', 'type': 'text', 'default': 'default', 'cols': 6},
            'latest_chapter': {'label': '最后章节', 'type': 'text', 'placeholder': '留空下载所有', 'advanced': True,
                               'cols': 12},
            'downloaded_chapters': {'label': '已下载章节 (UUID)', 'type': 'readonly', 'advanced': True, 'cols': 12},
            'last_download_date': {'label': '更新时间', 'type': 'readonly', 'advanced': True, 'cols': 12},
            'ep_pattern': {'label': '话数正则', 'type': 'text', 'advanced': True, 'cols': 6},
            'vol_pattern': {'label': '卷数正则', 'type': 'text', 'advanced': True, 'cols': 6},
        }

        if cls.ID_FIELD:
            if cls.ID_FIELD in defaults:
                meta[cls.ID_FIELD] = defaults[cls.ID_FIELD].copy()
            else:
                meta[cls.ID_FIELD] = {'label': cls.ID_FIELD, 'type': 'text', 'cols': 6}
            meta[cls.ID_FIELD]['required'] = True
            meta[cls.ID_FIELD]['primary'] = True

        for field in cls.REQUIRED_FIELDS:
            if field == cls.ID_FIELD: continue  # 跳过已处理的

            if field in defaults:
                meta[field] = defaults[field]
            else:
                meta[field] = {'label': field, 'type': 'text', 'cols': 12}

        return meta

    @classmethod
    def validate_record(cls, record: Dict) -> bool:
        """验证记录是否符合站点要求"""
        if not cls.ID_FIELD:
            raise ValueError(f"{cls.SITE_NAME}站点未定义ID_FIELD")
        return all(field in record for field in [cls.ID_FIELD] + cls.REQUIRED_FIELDS)

    def get_unique_id(self, record: Dict) -> str:
        """获取记录的唯一标识符"""
        if not self.ID_FIELD:
            raise ValueError(f"{self.SITE_NAME}站点未定义ID_FIELD")
        return record.get(self.ID_FIELD, "")

    def get_chapters(self, record: Dict) -> List[Dict]:
        """获取漫画章节列表"""
        raise NotImplementedError

    def find_subsequent_uuids(self, chapters: List[Dict], target_chapter: str) -> List[Tuple[str, str]]:
        """查找后续章节UUID和名称 (uuid, name)"""
        raise NotImplementedError

    def _chapter_name(self, chapter: Dict) -> str:
        """提取章节名称（各站点可覆盖）"""
        return chapter.get('name') or chapter.get('title') or ''

    def _chapter_uuid(self, chapter: Dict) -> str:
        """提取章节唯一标识（各站点可覆盖）"""
        return chapter.get('uuid') or chapter.get('cid') or ''

    def build_chapter_meta(self, record: Dict, chapters: List[Dict]) -> Dict[str, Dict[str, Any]]:
        """为所有章节构建固化的元数据（包含精确 CBZ 路径与绝对章节序号），避免切片过滤后序号错位"""
        meta = {}
        for idx, chap in enumerate(chapters):
            uuid = self._chapter_uuid(chap)
            name = self._chapter_name(chap)
            num = chap.get('index')
            if num is None:
                num = idx + 1
            else:
                # 若已有 index (0-based)，转为 1-based 标准章节序号
                num = num + 1 if isinstance(num, int) and num >= 0 else idx + 1

            cbz_path, chapter_filename, final_num, is_special = compute_target_cbz_path(
                self.SITE_NAME, record, name, chapter_num=num
            )
            meta[uuid] = {
                "abs_index": idx + 1,
                "chapter_num": final_num,
                "chapter_filename": chapter_filename,
                "is_special": is_special,
                "cbz_path": str(cbz_path),
            }
        return meta

    def find_missing_chapters(self, chapters: List[Dict], record: Dict) -> List[Tuple[str, str]]:
        """
        对比 get_chapters 返回的完整章节列表与已下载章节（downloaded_chapters 及磁盘 CBZ），
        返回缺失章节的 (uuid, name) 列表。
        自动回填已存在的历史章节和磁盘章节 UUID，防止后续重复下载。
        """
        if not chapters:
            return []

        downloaded = set(record.get('downloaded_chapters', []) or [])

        # 保守策略：若已有 latest_chapter，将 latest_chapter 及之前所有章节视为已下载
        if record.get('latest_chapter') and config.MISSING_CHAPTER_CONSERVATIVE:
            target_uuid = None
            for chap in chapters:
                if self._chapter_name(chap) == record['latest_chapter'] or \
                   self._chapter_uuid(chap) == record['latest_chapter']:
                    target_uuid = self._chapter_uuid(chap)
                    break

            if target_uuid:
                for chap in chapters:
                    downloaded.add(self._chapter_uuid(chap))
                    if self._chapter_uuid(chap) == target_uuid:
                        break
            else:
                log.warning(
                    f"未在章节列表中找到 latest_chapter='{record['latest_chapter']}'"
                )

        # 磁盘物理文件检查：只要磁盘已存在 CBZ 文件，绝对视为已下载
        for idx, chap in enumerate(chapters):
            uuid = self._chapter_uuid(chap)
            if not uuid or uuid in downloaded:
                continue
            name = self._chapter_name(chap)
            num = chap.get('index')
            num = num + 1 if isinstance(num, int) and num >= 0 else idx + 1
            if is_cbz_already_downloaded(self.SITE_NAME, record, name, chapter_num=num):
                log.info(f"检测到磁盘已存在 CBZ 文件，标记为已下载: {record.get('name')} - {name}")
                downloaded.add(uuid)

        # 回填已识别的下载章节（包含历史章节和磁盘现有章节），防止下次运行误判
        record['downloaded_chapters'] = list(downloaded)

        missing = []
        for chap in chapters:
            uuid = self._chapter_uuid(chap)
            if not uuid:
                continue
            if uuid not in downloaded:
                missing.append((uuid, self._chapter_name(chap)))

        return missing

    def create_download_task(self, record: Dict, chapter_infos: List[Tuple[str, str]], chapters: List[Dict] = None) -> Dict[str, Any]:
        """创建下载任务"""
        raise NotImplementedError

    def update_record(self, record: Dict, new_chapter: str, new_uuid: str = "") -> Dict:
        """更新记录"""
        updated = record.copy()
        updated['latest_chapter'] = new_chapter
        updated['last_download_date'] = datetime.now().isoformat()

        # 维护已下载章节集合（以 uuid 为唯一标识）
        downloaded = list(updated.get('downloaded_chapters', []) or [])
        if new_uuid and new_uuid not in downloaded:
            downloaded.append(new_uuid)
        updated['downloaded_chapters'] = downloaded

        return updated
