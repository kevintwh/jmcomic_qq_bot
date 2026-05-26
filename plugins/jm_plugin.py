"""
JMComic QQ机器人插件（小群简化版）
作者：浮浮酱 ฅ'ω'ฅ
"""

from nonebot import on_command, get_driver, on_message
from nonebot.adapters.onebot.v11 import Bot, MessageEvent, GroupMessageEvent, Message, MessageSegment
from nonebot.params import CommandArg
from nonebot.rule import to_me
from pathlib import Path
import asyncio
import yaml
import logging
import sys
import shutil
import http.server
import socketserver
import threading
import urllib.parse
import zipfile
import json  # 新增：用于处理 info.json

logger = logging.getLogger(__name__)

# 添加父目录到路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from core.downloader import JMDownloadManager

# 加载配置
driver = get_driver()
config_path = Path(__file__).parent.parent / "config.yml"
with open(config_path, 'r', encoding='utf-8') as f:
    bot_config = yaml.safe_load(f)

# 初始化管理器
download_manager = JMDownloadManager(bot_config['jmcomic'])

# HTTP 文件服务器
FILE_SERVER_PORT = 18080
file_server = None
file_server_thread = None

def start_file_server():
    """启动HTTP文件服务器"""
    global file_server, file_server_thread

    class FileHandler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(Path(bot_config['jmcomic']['download_dir']).parent), **kwargs)

        def do_GET(self):
            from urllib.parse import unquote, quote
            decoded_path = unquote(self.path)

            if decoded_path.lower().endswith('.pdf'):
                file_path = Path(self.directory) / decoded_path.lstrip('/')
                
                try:
                    resolved_path = file_path.resolve()
                    base_dir = Path(self.directory).resolve()
                    if not resolved_path.is_relative_to(base_dir):
                        self.send_error(403, "Forbidden")
                        return
                except Exception:
                    self.send_error(400, "Bad Request")
                    return

                if resolved_path.exists() and resolved_path.is_file():
                    filename = resolved_path.name

                    self.send_response(200)
                    self.send_header('Content-Type', 'application/pdf')

                    encoded_filename = quote(filename)
                    self.send_header('Content-Disposition', f"attachment; filename=\"file.pdf\"; filename*=UTF-8''{encoded_filename}")
                    self.send_header('Content-Length', str(resolved_path.stat().st_size))
                    self.end_headers()

                    with open(resolved_path, 'rb') as f:
                        self.wfile.write(f.read())
                    return

            super().do_GET()

        def log_message(self, format, *args):
            pass

    try:
        file_server = socketserver.TCPServer(("0.0.0.0", FILE_SERVER_PORT), FileHandler)
        file_server_thread = threading.Thread(target=file_server.serve_forever, daemon=True)
        file_server_thread.start()
        logger.info(f"文件服务器启动成功: http://127.0.0.1:{FILE_SERVER_PORT}")
    except Exception as e:
        logger.error(f"文件服务器启动失败: {e}")

start_file_server()

task_queue = asyncio.Queue(maxsize=bot_config.get('max_queue_size', 5))
is_processing = False

jm_cmd = on_command("jm", aliases={"本子", "禁漫"}, priority=5, block=True)


@jm_cmd.handle()
async def handle_jm(bot: Bot, event: MessageEvent, args: Message = CommandArg()):
    """处理下载命令"""
    global is_processing
    logger.info(f"[handle_jm] 收到命令")

    user_id = event.user_id
    group_id = event.group_id if isinstance(event, GroupMessageEvent) else None

    admins = bot_config['auth'].get('admins', [])
    enable_whitelist = bot_config['auth'].get('enable_whitelist', False)

    if enable_whitelist and user_id not in admins:
        await jm_cmd.finish("❌ 你没有权限喵～")
        return

    album_id = args.extract_plain_text().strip()

    if not album_id:
        await jm_cmd.finish(
            "📖 使用方法喵～\n\n"
            "/jm <本子ID> - 下载本子\n"
            "/jm status - 查看状态\n\n"
            "示例: /jm 123456"
        )
        return

    if album_id.lower() == "status":
        status_msg = (
            f"📊 浮浮酱状态\n\n"
            f"队列: {task_queue.qsize()}/{task_queue.maxsize}\n"
            f"处理中: {'是' if is_processing else '否'}\n"
            f"下载目录: {download_manager.download_dir}"
        )
        await jm_cmd.finish(status_msg)
        return

    if not album_id.isdigit():
        await jm_cmd.finish("❌ ID格式错误喵～（应为纯数字）")
        return

    if task_queue.full():
        await jm_cmd.finish(f"❌ 队列已满喵～\n当前: {task_queue.qsize()}/{task_queue.maxsize}")
        return

    await task_queue.put((bot, event, album_id))

    try:
        await jm_cmd.send(f"✅ 已加入队列喵～\nID: {album_id}\n队列位置: {task_queue.qsize()}")
    except Exception as e:
        logger.error(f"发送消息失败: {e}")

    if not is_processing:
        asyncio.create_task(process_queue())


async def process_queue():
    """处理队列"""
    global is_processing
    is_processing = True

    try:
        while not task_queue.empty():
            bot, event, album_id = await task_queue.get()
            try:
                await process_task(bot, event, album_id)
            except Exception as e:
                logger.error(f"任务失败: {album_id}, {e}", exc_info=True)
                await bot.send(event, f"❌ 处理失败喵～\nID: {album_id}\n错误: {str(e)[:100]}")
            finally:
                task_queue.task_done()
                await asyncio.sleep(bot_config.get('task_interval', 3))
    finally:
        is_processing = False


async def process_task(bot: Bot, event: MessageEvent, album_id: str):
    """处理单个任务"""
    logger.info(f"开始处理任务: {album_id}")
    
    loop = asyncio.get_running_loop()

    base_dir = Path(download_manager.download_dir)
    id_folder = base_dir / album_id
    
    # 3. 寻找统一的单一缓存文件 和 信息文件
    existing_dat_file = id_folder / f"{album_id}.dat"
    info_file = id_folder / "info.json"
    
    # 断点续传涉及的目录变量
    global_temp_dir = base_dir / "temp"
    saved_temp_dir = id_folder / "temp"
    
    pdf_count = "未知"  # 默认显示未知
    task_success = False  # 控制是否成功并允许清除全局 temp 的标志位

    try:
        if existing_dat_file.exists() and existing_dat_file.is_file():
            logger.info(f"命中本地缓存，跳过下载: {album_id}")
            await bot.send(event, f"📦 检测到本地已有缓存喵～\nID: {album_id}\n直接开始发送...")
            final_path = existing_dat_file
            task_success = True  # 命中缓存视作成功，不需要存断点
            
            # 尝试读取 info.json 获取精确章节数
            if info_file.exists():
                try:
                    with open(info_file, 'r', encoding='utf-8') as f:
                        info_data = json.load(f)
                        pdf_count = info_data.get('chapter_count', "未知")
                except Exception as e:
                    logger.warning(f"读取 info.json 失败: {e}")
        else:
            # ======= 断点续传：强制清理与恢复 =======
            # 1. 每次下载前强行重置/清理 temp 目录（应对上次异常关闭程序的残留垃圾）
            if global_temp_dir.exists():
                logger.info("发现全局 temp 目录可能存在残留，正在清理重置...")
                await loop.run_in_executor(None, shutil.rmtree, global_temp_dir, True)
            
            # 2. 清理完成之后，再检测是否有该 ID 的断点备份，有则移动出来
            if saved_temp_dir.exists() and saved_temp_dir.is_dir():
                logger.info(f"发现该 ID 的断点续传临时文件夹，正在恢复并移动到全局 temp 目录: {saved_temp_dir}")
                await loop.run_in_executor(None, shutil.move, str(saved_temp_dir), str(global_temp_dir))
            else:
                # 若没有任何备份，创建一个全新干净的 temp 供使用
                global_temp_dir.mkdir(parents=True, exist_ok=True)
            # ========================================

            await bot.send(event, f"📥 开始下载喵～\nID: {album_id}\n请耐心等待...")
            
            result_files, _ = await download_manager.download_and_convert(album_id)
            
            if not result_files:
                await bot.send(event, f"❌ 下载超时或转换失败喵～\nID: {album_id}\n现有断点续传功能\n可稍后再试一次喵~")
                return

            pdf_count = len(result_files)
            await bot.send(event, f"✅ 完成喵～共 {pdf_count} 个章节，准备打包...")
            
            # 把所有 PDF 打包成一个 .dat 文件
            final_path = await loop.run_in_executor(
                None,
                disguise_and_zip_all,
                result_files, album_id, id_folder, global_temp_dir
            )

            # 下载、转换、打包全流程均已走通，标记成功
            task_success = True

            # 写入 info.json 保存章节数
            try:
                with open(info_file, 'w', encoding='utf-8') as f:
                    json.dump({'chapter_count': pdf_count}, f, ensure_ascii=False)
            except Exception as e:
                logger.error(f"写入 info.json 失败: {e}")

        # 发送这个唯一的文件
        try:
            file_size_mb = final_path.stat().st_size / (1024 * 1024)
            max_size = bot_config.get('max_file_size_mb', 200)

            # 严格限制拦截：超过大小直接终止发送
            if file_size_mb > max_size:
                await bot.send(
                    event,
                    f"❌ 打包后文件体积达到 {file_size_mb:.1f}MB。\n超出了设定的最大发送限制 ({max_size}MB)，已强制取消发送喵...\n建议手动前往服务器下载或降低分辨率。"
                )
                return  # 直接跳出，不再往下执行发送逻辑，去 finally 处理临时文件

            absolute_path = str(final_path.resolve())
            filename = final_path.name

            def escape_cq(text):
                return text.replace('&', '&amp;').replace('[', '&#91;').replace(']', '&#93;').replace(',', '&#44;')

            escaped_path = escape_cq(absolute_path)
            cq_code = f"[CQ:file,file={escaped_path}]"

            # 新增预先提示：如果文件较大，提前告诉群友
            if file_size_mb > 150:
                await bot.send(event, f"📦 文件较大 ({file_size_mb:.1f}MB)，框架正在努力上传发往群里。如果稍后没反应请耐心等待后台传输喵～")

            is_timeout = False
            try:
                if hasattr(event, 'group_id') and event.group_id:
                    await bot.call_api('send_group_msg', group_id=event.group_id, message=cq_code)
                else:
                    await bot.call_api('send_private_msg', user_id=event.user_id, message=cq_code)
            except Exception as api_e:
                # 捕获因超时产生的假报错
                if "timeout" in str(api_e).lower():
                    is_timeout = True
                    logger.warning(f"文件过大导致发送API超时，转为后台上传等待: {api_e}")
                else:
                    raise api_e

            # 根据是否超时发送不同的话术
            if is_timeout:
                await bot.send(
                    event, 
                    f"⏳ API返回超时啦喵～\n因为文件有 {file_size_mb:.1f}MB 比较大，机器人框架仍在后台默默上传中，过一会儿应该就会发出来了。\n\nID: {album_id} (共 {pdf_count} 章)\n使用方法：\n1️⃣ 把 .dat 改成 .zip\n2️⃣ 解压后即可得到原本名称的 PDF 章节"
                )
            else:
                await bot.send(
                    event, 
                    f"🎉 发送成功喵～\nID: {album_id} (共 {pdf_count} 章)\n大小: {file_size_mb:.1f}MB\n\n使用方法：\n1️⃣ 把 .dat 改成 .zip\n2️⃣ 解压后即可得到原本名称的 PDF 章节"
                )

            # 发完之后的清理逻辑，受 delete_after_send 控制
            if bot_config.get('delete_after_send', False):
                # 核心修复：如果超时了说明还在传，给足300秒时间再删文件，避免文件在上传中被删导致真正发送失败
                wait_seconds = 300 if is_timeout else 5
                await asyncio.sleep(wait_seconds)
                try:
                    await loop.run_in_executor(None, shutil.rmtree, id_folder, True)
                    logger.info(f"[清理] delete_after_send已开启，删除了缓存目录: {id_folder}")
                except Exception as e:
                    logger.error(f"[清理] 删除缓存目录失败: {id_folder}, {e}")

        except Exception as e:
            logger.error(f"文件发送失败: {final_path.name}, {e}", exc_info=True)
            await bot.send(event, f"❌ {final_path.name} 发送失败喵...\n错误: {str(e)[:100]}")

    finally:
        # ======= 断点续传：保存/清理 =======
        if global_temp_dir.exists():
            if not task_success:
                # 任务未走通（如下载失败/崩溃/没出转换结果），保存现场作为下一次的断点
                try:
                    if saved_temp_dir.exists():
                        await loop.run_in_executor(None, shutil.rmtree, saved_temp_dir, True)
                    id_folder.mkdir(parents=True, exist_ok=True)
                    await loop.run_in_executor(None, shutil.move, str(global_temp_dir), str(saved_temp_dir))
                    logger.info(f"检测到未完成情况，已将临时文件保存为断点续传: {saved_temp_dir}")
                except Exception as e:
                    logger.error(f"保存断点续传文件失败: {e}")
            else:
                # 任务已完美执行通过，正常清理临时目录
                try:
                    await loop.run_in_executor(None, shutil.rmtree, global_temp_dir, True)
                    logger.info(f"任务结束，清理全局临时目录完成: {global_temp_dir}")
                except Exception as e:
                    logger.error(f"清理全局临时目录失败: {e}")
        # ==================================

def disguise_and_zip_all(pdf_files: list, album_id: str, id_folder: Path, temp_dir: Path) -> Path:
    """将多个 PDF 打包进唯一一个 zip 并改名为 .dat，放入最终缓存目录"""
    temp_zip = temp_dir / f"{album_id}.zip"
    
    with zipfile.ZipFile(temp_zip, 'w', zipfile.ZIP_DEFLATED) as z:
        for pdf_path in pdf_files:
            # z.write(物理路径, 压缩包内显示的相对路径)
            # 这样写入压缩包后，PDF 依然保持原本的中文/章节名称
            z.write(pdf_path, pdf_path.name)
            
            # 写完一个删一个，节省 temp 文件夹的磁盘空间
            if pdf_path.exists():
                pdf_path.unlink()

    temp_dat = temp_zip.with_suffix(".dat")
    if temp_dat.exists():
        temp_dat.unlink()
    temp_zip.rename(temp_dat)

    id_folder.mkdir(parents=True, exist_ok=True)
    final_path = id_folder / temp_dat.name
    
    if final_path.exists():
        final_path.unlink()
        
    shutil.move(str(temp_dat), str(final_path))
    
    return final_path


jm_auto = on_message(rule=to_me(), priority=10, block=False)

@jm_auto.handle()
async def handle_auto(bot: Bot, event: MessageEvent):
    """智能识别本子ID"""
    global is_processing
    msg = str(event.get_message()).strip()

    if msg.isdigit() and 5 <= len(msg) <= 7:
        await task_queue.put((bot, event, msg))
        await bot.send(
            event,
            f"✅ 检测到本子ID: {msg}\n已自动加入队列喵～\n队列: {task_queue.qsize()}/{task_queue.maxsize}"
        )

        if not is_processing:
            asyncio.create_task(process_queue())
                    # 发送响应头
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/pdf')

                    # 对文件名进行 URL 编码以支持 Unicode 字符（RFC 2231 标准）
                    encoded_filename = quote(filename)
                    self.send_header('Content-Disposition', f"attachment; filename=\"file.pdf\"; filename*=UTF-8''{encoded_filename}")

                    self.send_header('Content-Length', str(file_path.stat().st_size))
                    self.end_headers()

                    # 发送文件内容
                    with open(file_path, 'rb') as f:
                        self.wfile.write(f.read())
                    return

            # 非 PDF 文件，使用默认处理
            super().do_GET()

        def log_message(self, format, *args):
            # 静默日志
            pass

    try:
        file_server = socketserver.TCPServer(("0.0.0.0", FILE_SERVER_PORT), FileHandler)
        file_server_thread = threading.Thread(target=file_server.serve_forever, daemon=True)
        file_server_thread.start()
        logger.info(f"文件服务器启动成功: http://127.0.0.1:{FILE_SERVER_PORT}")
    except Exception as e:
        logger.error(f"文件服务器启动失败: {e}")

# 启动文件服务器
start_file_server()

# 任务队列
task_queue = asyncio.Queue(maxsize=bot_config.get('max_queue_size', 5))
is_processing = False


# 命令: /jm <本子ID>
jm_cmd = on_command("jm", aliases={"本子", "禁漫"}, priority=5, block=True)


@jm_cmd.handle()
async def handle_jm(bot: Bot, event: MessageEvent, args: Message = CommandArg()):
    """处理下载命令"""
    global is_processing

    print("=" * 80)
    print("[handle_jm] 函数被调用！！！")
    print("=" * 80)
    logger.info(f"[handle_jm] 收到命令")

    user_id = event.user_id
    group_id = event.group_id if isinstance(event, GroupMessageEvent) else None
    logger.info(f"[handle_jm] user_id={user_id}, group_id={group_id}")

    # 权限检查（小群版简化）
    admins = bot_config['auth'].get('admins', [])
    enable_whitelist = bot_config['auth'].get('enable_whitelist', False)

    if enable_whitelist and user_id not in admins:
        logger.warning(f"[handle_jm] 用户无权限: {user_id}")
        await jm_cmd.finish("❌ 你没有权限喵～")
        return

    # 解析ID
    album_id = args.extract_plain_text().strip()
    logger.info(f"[handle_jm] 解析到ID: {album_id}")

    if not album_id:
        logger.info(f"[handle_jm] ID为空，返回使用说明")
        await jm_cmd.finish(
            "📖 使用方法喵～\n\n"
            "/jm <本子ID> - 下载本子\n"
            "/jm status - 查看状态\n\n"
            "示例: /jm 123456"
        )
        return

    # 状态查询
    if album_id.lower() == "status":
        logger.info(f"[handle_jm] 查询状态")
        status_msg = (
            f"📊 浮浮酱状态\n\n"
            f"队列: {task_queue.qsize()}/{task_queue.maxsize}\n"
            f"处理中: {'是' if is_processing else '否'}\n"
            f"下载目录: {download_manager.download_dir}"
        )
        await jm_cmd.finish(status_msg)
        return

    # 验证ID
    if not album_id.isdigit():
        logger.warning(f"[handle_jm] ID格式错误: {album_id}")
        await jm_cmd.finish("❌ ID格式错误喵～（应为纯数字）")
        return

    # 检查队列
    if task_queue.full():
        logger.warning(f"[handle_jm] 队列已满")
        await jm_cmd.finish(
            f"❌ 队列已满喵～\n"
            f"当前: {task_queue.qsize()}/{task_queue.maxsize}"
        )
        return

    # 加入队列
    logger.info(f"[handle_jm] 加入队列: {album_id}")
    await task_queue.put((bot, event, album_id))
    logger.info(f"[handle_jm] 已加入队列，当前队列大小: {task_queue.qsize()}")

    try:
        logger.info(f"[handle_jm] 发送加入队列消息")
        await jm_cmd.send(
            f"✅ 已加入队列喵～\n"
            f"本子ID: {album_id}\n"
            f"队列位置: {task_queue.qsize()}"
        )
        logger.info(f"[handle_jm] 消息发送成功")
    except Exception as e:
        logger.error(f"[handle_jm] 发送消息失败: {e}", exc_info=True)

    # 启动处理
    logger.info(f"[handle_jm] is_processing={is_processing}")
    if not is_processing:
        logger.info(f"[handle_jm] 启动 process_queue")
        asyncio.create_task(process_queue())
    else:
        logger.info(f"[handle_jm] process_queue 已在运行中")


async def process_queue():
    """处理队列"""
    global is_processing
    is_processing = True

    try:
        while not task_queue.empty():
            bot, event, album_id = await task_queue.get()

            try:
                await process_task(bot, event, album_id)
            except Exception as e:
                logger.error(f"任务失败: {album_id}, {e}", exc_info=True)
                await bot.send(event, f"❌ 处理失败喵～\nID: {album_id}\n错误: {str(e)}")
            finally:
                task_queue.task_done()
                await asyncio.sleep(bot_config.get('task_interval', 3))

    finally:
        is_processing = False


async def process_task(bot: Bot, event: MessageEvent, album_id: str):
    """处理单个任务"""

    print("=" * 80)
    print(f"[process_task] 开始处理任务: {album_id}")
    print("=" * 80)
    logger.info(f"开始处理任务: {album_id}, event={event}")

    try:
        await bot.send(event, f"📥 开始下载喵～\nID: {album_id}\n请耐心等待...")
        logger.info(f"已发送开始下载消息")
    except Exception as e:
        logger.error(f"发送开始消息失败: {e}", exc_info=True)

    # 下载转换
    print(f"[process_task] 调用 download_and_convert: {album_id}")
    logger.info(f"调用 download_and_convert: {album_id}")
    pdf_files = await download_manager.download_and_convert(album_id)
    print(f"[process_task] download_and_convert 返回: {pdf_files}")
    print(f"[process_task] pdf_files 类型: {type(pdf_files)}")
    print(f"[process_task] pdf_files 是否为None: {pdf_files is None}")
    print(f"[process_task] pdf_files 是否为空: {not pdf_files}")
    logger.info(f"download_and_convert 返回: {pdf_files}")

    if not pdf_files:
        print(f"[process_task] PDF文件列表为空，返回")
        logger.warning(f"PDF文件列表为空")
        await bot.send(event, f"❌ 下载失败喵～\nID: {album_id}")
        return

    print(f"[process_task] 准备发送完成消息，共 {len(pdf_files)} 个文件")
    await bot.send(event, f"✅ 完成喵～共{len(pdf_files)}个章节\n开始发送...")
    print(f"[process_task] 完成消息已发送")

    # 打印详细的pdf_files信息
    print("=" * 80)
    print(f"[process_task] pdf_files详细信息:")
    print(f"  类型: {type(pdf_files)}")
    print(f"  长度: {len(pdf_files)}")
    for idx, pf in enumerate(pdf_files):
        print(f"  [{idx}] {pf} (类型: {type(pf)}, 存在: {pf.exists() if hasattr(pf, 'exists') else 'N/A'})")
    print("=" * 80)

    # 发送文件
    print(f"[process_task] 准备进入文件发送循环")
    for i, pdf_path in enumerate(pdf_files, 1):
        print("=" * 80)
        print(f"[process_task] 进入循环，处理第 {i} 个文件")
        print(f"[process_task] pdf_path = {pdf_path}")
        print(f"[process_task] pdf_path 类型 = {type(pdf_path)}")
        print("=" * 80)
        try:
            print(f"[process_task] 获取文件大小...")
            file_size_mb = pdf_path.stat().st_size / (1024 * 1024)
            print(f"[process_task] 文件大小: {file_size_mb:.2f}MB")

            # 文件大小检查
            print(f"[process_task] 检查文件大小限制，max={bot_config.get('max_file_size_mb', 200)}MB")
            if file_size_mb > bot_config.get('max_file_size_mb', 200):
                print(f"[process_task] 文件过大，跳过")
                await bot.send(
                    event,
                    f"⚠️ 文件过大喵～\n"
                    f"[{i}/{len(pdf_files)}] {pdf_path.name}\n"
                    f"大小: {file_size_mb:.1f}MB"
                )
                continue

            print(f"[process_task] 文件大小检查通过")

            # 发送文件（使用本地文件路径）
            try:
                print(f"[process_task] 开始构建本地文件路径")

                # 获取文件的绝对路径（直接使用 Windows 路径，不转换成 file:// URL）
                absolute_path = str(pdf_path.resolve())
                print(f"[process_task] 绝对路径 = {absolute_path}")

                # 文件名
                filename = pdf_path.name
                print(f"[process_task] 文件名 = {filename}")

                logger.info(f"发送本地文件: {absolute_path}")

                print(f"[process_task] 检查消息类型...")

                # 转义 CQ 码特殊字符
                def escape_cq(text):
                    """转义 CQ 码中的特殊字符"""
                    return text.replace('&', '&amp;').replace('[', '&#91;').replace(']', '&#93;').replace(',', '&#44;')

                # 转义文件路径
                escaped_path = escape_cq(absolute_path)
                print(f"[process_task] 原始路径: {absolute_path}")
                print(f"[process_task] 转义后路径: {escaped_path}")

                # 构建 CQ 码（使用绝对路径而不是 file:// URL）
                cq_code = f"[CQ:file,file={escaped_path}]"
                print(f"[process_task] CQ码: {cq_code}")

                if hasattr(event, 'group_id') and event.group_id:
                    # 群消息
                    print(f"[process_task] 群消息，group_id={event.group_id}")
                    print(f"[process_task] 调用 send_group_msg API...")
                    await bot.call_api(
                        'send_group_msg',
                        group_id=event.group_id,
                        message=cq_code
                    )
                    print(f"[process_task] send_group_msg API 调用完成")
                else:
                    # 私聊消息
                    print(f"[process_task] 私聊消息，user_id={event.user_id}")
                    print(f"[process_task] 调用 send_private_msg API...")
                    await bot.call_api(
                        'send_private_msg',
                        user_id=event.user_id,
                        message=cq_code
                    )
                    print(f"[process_task] send_private_msg API 调用完成")

                print(f"[process_task] 发送成功确认消息...")
                await bot.send(event, f"✅ [{i}/{len(pdf_files)}] {filename} ({file_size_mb:.1f}MB) 发送成功喵～")
                print(f"[process_task] 成功确认消息已发送")
                logger.info(f"文件发送成功: {filename}")

            except Exception as e:
                print(f"[process_task] ❌ 内部异常: {type(e).__name__}: {e}")
                logger.error(f"文件发送失败: {pdf_path.name}, {e}", exc_info=True)
                import traceback
                traceback.print_exc()
                await bot.send(event,
                    f"❌ [{i}/{len(pdf_files)}] {pdf_path.name} 发送失败喵...\n"
                    f"错误: {str(e)[:100]}"
                )

            print(f"[process_task] 等待1秒...")
            await asyncio.sleep(1)
            print(f"[process_task] 第 {i} 个文件处理完成")

        except Exception as e:
            print(f"[process_task] ❌ 外部异常: {type(e).__name__}: {e}")
            logger.error(f"发送失败: {pdf_path.name}, {e}")
            import traceback
            traceback.print_exc()
            await bot.send(event, f"❌ 发送失败: {pdf_path.name}")

    print("=" * 80)
    print(f"[process_task] 文件发送循环结束")
    print("=" * 80)

    print(f"[process_task] 发送全部完成消息...")
    await bot.send(event, f"🎉 全部完成喵～\nID: {album_id}")
    print(f"[process_task] 全部完成消息已发送")

    # 清理文件
    if bot_config.get('delete_after_send', True):
        await cleanup_files(pdf_files)


async def cleanup_files(pdf_files: list):
    """清理文件"""
    try:
        for pdf_path in pdf_files:
            if pdf_path.exists():
                pdf_path.unlink()

                parent = pdf_path.parent
                if parent.exists() and not any(parent.iterdir()):
                    parent.rmdir()

        logger.info(f"清理完成: {len(pdf_files)}个")
    except Exception as e:
        logger.error(f"清理失败: {e}")


# 智能识别：直接发送数字ID
jm_auto = on_message(rule=to_me(), priority=10, block=False)


@jm_auto.handle()
async def handle_auto(bot: Bot, event: MessageEvent):
    """智能识别本子ID"""
    global is_processing
    msg = str(event.get_message()).strip()

    # 检查是否为纯数字（5-7位，本子ID通常这个范围）
    if msg.isdigit() and 5 <= len(msg) <= 7:
        # 模拟 /jm 命令
        await task_queue.put((bot, event, msg))

        await bot.send(
            event,
            f"✅ 检测到本子ID: {msg}\n"
            f"已自动加入队列喵～\n"
            f"队列: {task_queue.qsize()}/{task_queue.maxsize}"
        )

        global is_processing
        if not is_processing:
            asyncio.create_task(process_queue())
