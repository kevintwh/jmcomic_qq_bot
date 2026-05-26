"""
下载管理器（小群简化版）
作者：浮浮酱 ฅ'ω'ฅ
"""

import asyncio
import jmcomic
import shutil
from pathlib import Path
from typing import Optional, List, Tuple
import logging
from .converter import ImageToPDFConverter

logger = logging.getLogger(__name__)


class JMDownloadManager:
    """JMComic下载管理器"""

    def __init__(self, config: dict):
        self.config = config
        self.download_dir = Path(config['download_dir'])
        self.download_dir.mkdir(parents=True, exist_ok=True)

        self.converter = ImageToPDFConverter(
            delete_images=config.get('delete_images_after_convert', True)
        )

    def _create_jm_option(self, base_dir: str) -> jmcomic.JmOption:
        """创建JMComic配置（动态设定下载目录）"""
        option = jmcomic.JmOption.default()
        option.dir_rule.base_dir = base_dir

        # 客户端配置
        option.client.impl = self.config.get('client_impl', 'api')

        # 代理配置
        if 'proxy' in self.config:
            option.client.proxies = {
                'http': self.config['proxy'],
                'https': self.config['proxy']
            }

        # 并发配置
        threading = self.config.get('threading', {})
        option.download.threading.album = threading.get('album', 1)
        option.download.threading.photo = threading.get('photo', 2)
        option.download.threading.image = threading.get('image', 5)

        return option

    async def download_and_convert(self, album_id: str) -> Tuple[Optional[List[Path]], Path]:
        """下载本子并转PDF，返回 (PDF路径列表, 临时目录路径)"""
        # 1. 使用全局统一的 temp 文件夹
        temp_dir = self.download_dir / "temp"
        
        try:
            print(f"[下载管理器] 开始处理: {album_id}")
            logger.info(f"[下载管理器] 开始处理: {album_id}")
            
            # （注：强行重置清理异常残留的功能已按顺序移至外层 jm_plugin.py 执行）
            temp_dir.mkdir(parents=True, exist_ok=True)

            option = self._create_jm_option(str(temp_dir))

            # 在线程池执行下载
            loop = asyncio.get_running_loop()
            print(f"[下载管理器] 调用 jmcomic.download_album, base_dir={temp_dir}")
            logger.info(f"[下载管理器] 调用 jmcomic.download_album")
            
            await loop.run_in_executor(
                None,
                jmcomic.download_album,
                album_id,
                option
            )

            print(f"[下载管理器] 下载完成: {album_id}")
            logger.info(f"[下载管理器] 下载完成: {album_id}")

            # 转PDF
            print(f"[下载管理器] 开始扫描并转换PDF: 目录={temp_dir}")
            logger.info(f"[下载管理器] 开始扫描并转换PDF: 目录={temp_dir}")
            
            pdf_files = self.converter.convert_album(temp_dir)
            
            print(f"[下载管理器] 转换结果: {len(pdf_files) if pdf_files else 0}个PDF")
            logger.info(f"[下载管理器] 转换结果: {len(pdf_files) if pdf_files else 0}个PDF")

            if not pdf_files:
                print(f"[下载管理器] PDF转换失败或没有找到图片: {album_id}")
                logger.error(f"[下载管理器] PDF转换失败或没有找到图片: {album_id}")
                return None, temp_dir

            return pdf_files, temp_dir

        except Exception as e:
            print(f"[下载管理器] 异常: {album_id}, {e}")
            logger.error(f"[下载管理器] 异常: {album_id}, {e}", exc_info=True)
            return None, temp_dir
