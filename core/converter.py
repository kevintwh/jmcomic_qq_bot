"""
PDF 转换器模块（集成 image2pdf）
作者：浮浮酱 ฅ'ω'ฅ
"""

import re
from pathlib import Path
from typing import List, Optional
from PIL import Image
import logging

logger = logging.getLogger(__name__)


class ImageToPDFConverter:
    """图片转PDF转换器"""

    def __init__(self, delete_images: bool = True):
        self.delete_images = delete_images
        self.supported_formats = {'.jpg', '.jpeg', '.png', '.webp', '.bmp', '.gif'}

    def convert_album(self, temp_dir: Path) -> List[Path]:
        """扫描临时目录下的所有章节，并转换成多个 PDF"""
        logger.info(f"[转换器] 开始扫描本子目录: {temp_dir}")
        pdf_files = []

        if not temp_dir.exists():
            logger.error(f"[转换器] 目录不存在: {temp_dir}")
            return pdf_files

        # 找出所有包含图片的子目录
        image_dirs = set()
        for f in temp_dir.rglob('*'):
            if f.is_file() and f.suffix.lower() in self.supported_formats:
                image_dirs.add(f.parent)

        def extract_dir_number(d: Path) -> int:
            nums = re.findall(r'\d+', d.name)
            return int(nums[-1]) if nums else 0
            
        sorted_dirs = sorted(list(image_dirs), key=extract_dir_number)
        logger.info(f"[转换器] 找到了 {len(sorted_dirs)} 个包含图片的文件夹")

        for chapter_dir in sorted_dirs:
            try:
                logger.info(f"[转换器] 转换章节: {chapter_dir.name}")
                pdf_path = self.convert_chapter(chapter_dir, temp_dir)
                if pdf_path:
                    pdf_files.append(pdf_path)
                    logger.info(f"[转换器] 章节转换成功: {pdf_path}")
            except Exception as e:
                logger.error(f"[转换器] 章节转换失败: {chapter_dir.name}, {e}", exc_info=True)

        logger.info(f"[转换器] 转换完成，共生成 {len(pdf_files)} 个PDF")
        return pdf_files

    def convert_chapter(self, chapter_dir: Path, output_dir: Path) -> Optional[Path]:
        """转换单个章节为PDF并输出到指定目录"""
        try:
            images = self._get_sorted_images(chapter_dir)

            if not images:
                logger.warning(f"章节目录为空: {chapter_dir.name}")
                return None

            # 2. 保持原本子文件夹名称作为 PDF 名称
            pdf_path = output_dir / f"{chapter_dir.name}.pdf"

            if pdf_path.exists():
                logger.info(f"PDF已存在: {pdf_path.name}")
                return pdf_path

            logger.info(f"转换PDF: {pdf_path.name} ({len(images)}张)")

            self._images_to_pdf(images, pdf_path)

            if self.delete_images:
                self._cleanup_images(chapter_dir)

            logger.info(f"PDF完成: {pdf_path.name}")
            return pdf_path

        except Exception as e:
            logger.error(f"PDF转换失败: {chapter_dir.name}, {e}")
            return None

    def _get_sorted_images(self, directory: Path) -> List[Path]:
        """获取排序后的图片"""
        images = [
            f for f in directory.iterdir()
            if f.is_file() and f.suffix.lower() in self.supported_formats
        ]

        def extract_number(path: Path) -> int:
            match = re.search(r'(\d+)', path.stem)
            return int(match.group(1)) if match else 0

        return sorted(images, key=extract_number)

    def _images_to_pdf(self, image_paths: List[Path], output_pdf: Path):
        """图片转PDF"""
        img_list = []

        for img_path in image_paths:
            try:
                with Image.open(img_path) as img:
                    img.load()  
                    # 转RGB
                    if img.mode in ('RGBA', 'LA', 'P'):
                        rgb_img = Image.new('RGB', img.size, (255, 255, 255))
                        if img.mode == 'P':
                            img = img.convert('RGBA')
                        if img.mode in ('RGBA', 'LA'):
                            rgb_img.paste(img, mask=img.split()[-1])
                        else:
                            rgb_img.paste(img)
                        img_list.append(rgb_img)
                    elif img.mode != 'RGB':
                        img_list.append(img.convert('RGB'))
                    else:
                        img_list.append(img.copy())

            except Exception as e:
                logger.warning(f"图片失败或损坏跳过: {img_path.name}, {e}")

        if not img_list:
            raise ValueError("没有有效图片可以转换")

        img_list[0].save(
            str(output_pdf),
            "PDF",
            save_all=True,
            append_images=img_list[1:],
            resolution=100.0
        )

    def _cleanup_images(self, chapter_dir: Path):
        """清理原图片(避免报错卡死)"""
        try:
            for img_path in chapter_dir.iterdir():
                if img_path.is_file() and img_path.suffix.lower() in self.supported_formats:
                    img_path.unlink()

            if not any(chapter_dir.iterdir()):
                chapter_dir.rmdir()

        except Exception as e:
            logger.error(f"清理临时图片失败: {e}")
