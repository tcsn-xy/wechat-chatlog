"""本机媒体文件 → JPEG 预览（单文件纯函数）。

供 CLI ``chatlog media`` 与 MCP ``media`` 工具使用：调用方已完成 media_store 目录越界校验，
本模块只把一个具体普通文件解码成可内联返回的小尺寸 JPEG。不写文件、不缓存、不联网、
不读配置、不打印、不启动外部进程；同一输入的结果确定（JPEG 字节可能随 Pillow 版本略有差异，
测试只校验可解码与尺寸，不比对字节）。

``decode_path`` 取值：
    direct     源文件本身就是 PIL 可直接打开的图片（JPEG/PNG/GIF/WebP/BMP）。
    embedded   ``wxgf`` 容器里内嵌了 JPEG/PNG，取出内嵌字节后解码。
    wxgf-hevc  ``wxgf`` 容器里是 HEVC 码流，用 PyAV 解码首帧。

``source_format`` 是实际解出的格式名：直读/内嵌为 PIL 的 ``format``；HEVC 记为 ``HEVC``。

失败统一抛 :class:`chatlog_core.common.QueryError`，错误码固定：
    media_file_missing     文件不存在、不是普通文件或无法读取
    media_too_large        源文件超过字节上限，或像素数超过上限
    media_not_image        内容不是可识别的图片
    media_decode_failed    识别为图片或 wxgf 容器但解码失败
    media_decoder_missing  需要 PyAV 解码 HEVC 但环境缺少 av
    media_preview_failed   缩放或 JPEG 编码失败
"""

from __future__ import annotations

import io
from pathlib import Path
import stat

from PIL import Image, ImageOps

from .common import QueryError

DEFAULT_MAX_EDGE = 1280
DEFAULT_QUALITY = 82
MAX_SOURCE_BYTES = 40 * 1024 * 1024
MAX_SOURCE_PIXELS = 64_000_000

_JPEG_MAGIC = b'\xff\xd8\xff'
_PNG_MAGIC = b'\x89PNG\r\n\x1a\n'
_WXGF_MAGIC = b'wxgf'
_HEVC_START = b'\x00\x00\x00\x01'
_HEVC_SHORT_START = b'\x00\x00\x01'
# wxgf 的 HEVC 起始码扫描上限与候选次数：单个候选解一帧就够，全部失败才算损坏。
_HEVC_SCAN_BYTES = 1024 * 1024
_HEVC_SCAN_CANDIDATES = 512
_HEVC_ATTEMPTS = 32
# wxgf 内嵌图的扫描：先看头部 4KB，再兜底扫到 1MB；只认能完整通过 PIL 解码的候选，
# 因为真实 HEVC 码流里可能偶然出现 FF D8 FF 字节。
_EMBED_SCAN_BYTES = 4096
_EMBED_FALLBACK_BYTES = 1024 * 1024
_EMBED_ATTEMPTS = 32


def render_preview(path, *, max_edge: int = DEFAULT_MAX_EDGE, quality: int = DEFAULT_QUALITY) -> dict:
    """把单个图片文件渲染成最长边不超过 max_edge 的 JPEG 预览。

    ``path`` 是调用方已做目录越界校验的具体文件（pathlib.Path 或 str），本函数只按普通文件读取。
    源图比 max_edge 小时保持原尺寸、不放大，但仍重编码为 JPEG。

    成功返回固定键的 dict：``data``（JPEG bytes）、``mime``、``width``/``height``（EXIF 方向
    校正后的源图显示尺寸）、``preview_width``/``preview_height``（输出尺寸）、``source_bytes``、
    ``source_format``、``decode_path``、``warnings``；失败抛 ``QueryError``，错误码见模块文档。
    """
    edge, level = _params(max_edge, quality)
    data, source_bytes = _read_source(path)
    image, source_format, decode_path, warnings = _decode(data)
    image, normalize_warnings = _normalize(image)
    warnings.extend(normalize_warnings)
    width, height = image.size
    image, payload = _encode(image, edge, level)
    return dict(data=payload, mime='image/jpeg', width=width, height=height,
                preview_width=image.width, preview_height=image.height,
                source_bytes=source_bytes, source_format=source_format,
                decode_path=decode_path, warnings=warnings)


def _params(max_edge, quality):
    """收敛渲染参数：quality 夹在 1–100，max_edge 至少为 1。"""
    try:
        edge = max(1, int(max_edge))
        level = max(1, min(int(quality), 100))
    except (TypeError, ValueError):
        raise QueryError('media_preview_failed', '预览参数无效：max_edge 与 quality 必须是整数') from None
    return edge, level


def _read_source(path):
    """stat 校验 + 字节上限检查后一次性读出文件内容，返回 (data, 源文件字节数)。"""
    try:
        target = Path(path)
        info = target.stat()
    except (OSError, TypeError, ValueError):
        raise QueryError('media_file_missing', '媒体文件不存在或不是普通文件') from None
    if not stat.S_ISREG(info.st_mode):
        raise QueryError('media_file_missing', '媒体文件不存在或不是普通文件')
    if info.st_size > MAX_SOURCE_BYTES:
        raise QueryError('media_too_large',
                         f'媒体文件 {info.st_size} 字节超过上限 {MAX_SOURCE_BYTES} 字节；请改用缩略图',
                         source_bytes=info.st_size)
    if info.st_size == 0:
        raise QueryError('media_not_image', '媒体文件是空文件，无法生成预览', source_bytes=0)
    try:
        return target.read_bytes(), info.st_size
    except OSError:
        raise QueryError('media_file_missing', '媒体文件不存在或无法读取') from None


def _check_pixels(width, height):
    """在解码像素前拦截超大图，避免内存被撑爆。"""
    if width <= 0 or height <= 0:
        raise QueryError('media_decode_failed', '图片尺寸无效，无法生成预览')
    if width * height > MAX_SOURCE_PIXELS:
        raise QueryError('media_too_large',
                         f'图片像素数 {width}×{height} 超过上限 {MAX_SOURCE_PIXELS}；请改用缩略图或提高上限',
                         width=width, height=height)


def _sniff(data):
    """只按文件头判断格式，不信任扩展名；认不出返回 None。"""
    if data.startswith(_JPEG_MAGIC):
        return 'JPEG'
    if data.startswith(_PNG_MAGIC):
        return 'PNG'
    if data.startswith((b'GIF87a', b'GIF89a')):
        return 'GIF'
    if len(data) >= 12 and data.startswith(b'RIFF') and data[8:12] == b'WEBP':
        return 'WEBP'
    if data.startswith(b'BM'):
        return 'BMP'
    return None


def _open_image(data):
    """用 PIL 打开并解码首帧（多帧 GIF/APNG 只取第一帧）；失败异常由调用方分类。"""
    image = Image.open(io.BytesIO(data))
    _check_pixels(*image.size)
    image.load()
    return image


def _decode(data):
    """按文件头决定解码路径，返回 (首帧图像, source_format, decode_path, warnings)。"""
    if data.startswith(_WXGF_MAGIC):
        return _decode_wxgf(data)
    sniffed = _sniff(data)
    if sniffed is None:
        raise QueryError('media_not_image',
                         '文件内容不是可识别的图片；支持 JPEG/PNG/GIF/WebP/BMP 与微信 wxgf 容器')
    try:
        image = _open_image(data)
    except QueryError:
        raise
    except Exception:
        raise QueryError('media_decode_failed', '图片数据无法解码；文件可能损坏或格式不受支持') from None
    return image, image.format or sniffed, 'direct', []


def _decode_wxgf(data):
    """解 wxgf 容器：先找内嵌 JPEG/PNG（4KB，兜底 1MB），否则用 PyAV 解 HEVC 首帧。"""
    warnings = []
    tried = set()
    image = _embedded_image(data, _EMBED_SCAN_BYTES, tried)
    if image is not None:
        return image, image.format or 'JPEG', 'embedded', warnings
    image = _embedded_image(data, _EMBED_FALLBACK_BYTES, tried)
    if image is not None:
        warnings.append('内嵌图片不在 wxgf 头部 4KB 内；已用兜底扫描解出')
        return image, image.format or 'JPEG', 'embedded', warnings
    av = _load_av()
    decoded = _hevc_image(data, av)
    if decoded is None:
        raise QueryError('media_decode_failed',
                         'wxgf 容器内未能还原出图片；内嵌 JPEG/PNG 与 HEVC 解码均失败，文件可能损坏或编码不受支持')
    image, hevc_warnings = decoded
    return image, 'HEVC', 'wxgf-hevc', warnings + hevc_warnings


def _embedded_image(data, limit, tried):
    """在 wxgf 前 limit 字节内找内嵌 JPEG/PNG，返回解出的首帧图像或 None。

    候选必须完整通过 PIL 解码才算数：真实 HEVC 码流里可能出现假的 FF D8 FF 字节。
    ``tried`` 记录已尝试偏移，避免头部扫描与兜底扫描重复解码。
    """
    end = min(len(data), max(limit, _EMBED_SCAN_BYTES))
    positions = []
    for magic in (_JPEG_MAGIC, _PNG_MAGIC):
        start = 0
        while True:
            index = data.find(magic, start, end)
            if index < 0:
                break
            positions.append(index)
            start = index + len(magic)
    attempts = 0
    for index in sorted(positions):
        if index in tried:
            continue
        tried.add(index)
        attempts += 1
        if attempts > _EMBED_ATTEMPTS:
            break
        try:
            return _open_image(data[index:])
        except QueryError as exc:
            if exc.detail.get('code') == 'media_too_large':
                raise
        except Exception:
            continue
    return None


def _hevc_priority(nal_type):
    """HEVC NAL 候选优先级：VPS(32) → SPS(33)/PPS(34) → 其它。"""
    if nal_type == 32:
        return 0
    if nal_type in (33, 34):
        return 1
    return 2


def _hevc_candidates(data):
    """按 NAL 类型优先级列出 HEVC 起始码候选偏移。

    某些 wxgf 变体在 HEVC 前插入 ICC_PROFILE 等元数据，使文件里第一个 00 00 00 01 落在无效区域，
    从该处喂给 PyAV 会失败；因此从 VPS 候选开始逐个尝试（扫描上限 1MB，最多重试 32 个候选）。
    """
    end = min(len(data), _HEVC_SCAN_BYTES)
    found = []
    index = 0
    while len(found) < _HEVC_SCAN_CANDIDATES:
        index = data.find(_HEVC_START, index, end)
        if index < 0:
            break
        nal_type = (data[index + 4] >> 1) & 0x3F if index + 4 < len(data) else -1
        found.append((_hevc_priority(nal_type), index))
        index += 4
    if not found:
        # 少数码流只用 3 字节起始码；仅在没有 4 字节候选时兜底。
        index = 0
        while len(found) < _HEVC_SCAN_CANDIDATES:
            index = data.find(_HEVC_SHORT_START, index, end)
            if index < 0:
                break
            if index == 0 or data[index - 1] != 0:
                nal_type = (data[index + 3] >> 1) & 0x3F if index + 3 < len(data) else -1
                found.append((_hevc_priority(nal_type), index))
            index += 3
    found.sort()
    return [index for _, index in found[:_HEVC_ATTEMPTS]]


def _load_av():
    """惰性导入 PyAV；只在确实需要解 HEVC 时调用，缺失时给可操作的 QueryError。"""
    try:
        import av
    except Exception:
        raise QueryError('media_decoder_missing',
                         'wxgf HEVC 图片需要 PyAV 解码，当前环境缺少 av；请在项目 .venv 中安装 av') from None
    return av


def _hevc_image(data, av):
    """逐个候选起始码让 PyAV 解一帧；返回 (图像, warnings) 或 None。"""
    warnings = []
    for attempt, offset in enumerate(_hevc_candidates(data)):
        try:
            with av.open(io.BytesIO(data[offset:]), format='hevc') as container:
                stream = next((s for s in container.streams if s.type == 'video'), None)
                if stream is None:
                    continue
                frame = next(iter(container.decode(stream)), None)
                if frame is None:
                    continue
                _check_pixels(frame.width, frame.height)
                image = frame.to_image()
        except QueryError:
            raise
        except Exception:
            continue
        if attempt:
            warnings.append(f'wxgf 前部元数据使前 {attempt} 个 HEVC 起始码候选无效；已在第 {attempt + 1} 个候选处解码成功')
        return image, warnings
    return None


def _normalize(image):
    """按 EXIF 方向校正，把透明背景合成到白底，并统一成 RGB；返回 (图像, warnings)。"""
    warnings = []
    try:
        image = ImageOps.exif_transpose(image)
    except Exception:
        warnings.append('EXIF 方向信息无法解析；已按原始方向输出')
    try:
        if image.mode in ('RGBA', 'LA', 'PA', 'La', 'RGBa') or 'transparency' in image.info:
            rgba = image.convert('RGBA')
            background = Image.new('RGBA', rgba.size, (255, 255, 255, 255))
            background.alpha_composite(rgba)
            image = background.convert('RGB')
        else:
            image = image.convert('RGB')
    except Exception:
        raise QueryError('media_preview_failed', '图片色彩处理失败；文件可能包含不支持的像素格式') from None
    return image, warnings


def _encode(image, max_edge, quality):
    """等比缩放到 max_edge 以内（不放大）并编码为 JPEG；返回 (缩放后图像, JPEG bytes)。"""
    try:
        image.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
        buffer = io.BytesIO()
        image.save(buffer, format='JPEG', quality=quality, optimize=True)
    except Exception:
        raise QueryError('media_preview_failed', '预览缩放或 JPEG 编码失败；请稍后重试') from None
    payload = buffer.getvalue()
    if not payload:
        raise QueryError('media_preview_failed', '预览 JPEG 输出为空；请稍后重试')
    return image, payload
