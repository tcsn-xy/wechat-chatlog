"""chatlog 图片预览核心的合成数据测试：不读真实聊天数据，不写项目目录。

所有图片现场用 PIL 生成（HEVC 用例用 PyAV 编码一帧），写入临时目录后自动清理；
用例里不出现真实 wxid、群 ID、媒体路径或个人目录。
"""
import io
from fractions import Fraction
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))

from PIL import Image  # noqa: E402
from chatlog_core import preview  # noqa: E402
from chatlog_core.common import QueryError  # noqa: E402


def image_bytes(size, fmt='JPEG', mode='RGB', color=(200, 40, 40), **save_args):
    """合成一张纯色图，返回编码后的字节。"""
    buffer = io.BytesIO()
    Image.new(mode, size, color).save(buffer, format=fmt, **save_args)
    return buffer.getvalue()


def hevc_bitstream(width=64, height=48):
    """用 PyAV 编码一帧，返回 Annex B 格式的 HEVC 码流；编码器不可用时跳过用例。"""
    try:
        import av
    except ImportError:
        raise unittest.SkipTest('环境缺少 PyAV，跳过 HEVC 用例') from None
    try:
        codec = av.CodecContext.create('libx265', 'w')
        codec.width, codec.height = width, height
        codec.pix_fmt = 'yuv420p'
        codec.framerate = Fraction(25, 1)
        try:
            codec.options = {'x265-params': 'log-level=none'}  # 避免 x265 把编码统计刷到测试输出
        except Exception:
            pass
        frame = av.VideoFrame.from_image(Image.new('RGB', (width, height), (40, 120, 200)))
        frame = frame.reformat(format='yuv420p')
        frame.pts = 0
        chunks = [bytes(packet) for packet in codec.encode(frame)]
        chunks += [bytes(packet) for packet in codec.encode(None)]
        data = b''.join(chunks)
    except (ValueError, OSError, av.error.FFmpegError) as exc:
        raise unittest.SkipTest(f'环境没有可用的 HEVC 编码器：{exc}') from None
    if b'\x00\x00\x00\x01' not in data:
        raise unittest.SkipTest('HEVC 编码器没有输出 Annex B 码流')
    return data


class PreviewTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def write(self, name, data):
        path = self.tmp / name
        path.write_bytes(data)
        return path

    def assert_error(self, path, code, **kwargs):
        with self.assertRaises(QueryError) as caught:
            preview.render_preview(path, **kwargs)
        self.assertEqual(caught.exception.detail['code'], code)
        return caught.exception

    def test_large_jpeg_scaled_to_max_edge(self):
        data = image_bytes((4000, 2000))
        path = self.write('large.jpg', data)
        result = preview.render_preview(path, max_edge=1280)
        self.assertEqual(result['mime'], 'image/jpeg')
        self.assertEqual(result['decode_path'], 'direct')
        self.assertEqual(result['source_format'], 'JPEG')
        self.assertEqual(result['source_bytes'], len(data))
        self.assertEqual((result['width'], result['height']), (4000, 2000))
        self.assertEqual((result['preview_width'], result['preview_height']), (1280, 640))
        self.assertLessEqual(max(result['preview_width'], result['preview_height']), 1280)
        self.assertIsInstance(result['warnings'], list)
        with Image.open(io.BytesIO(result['data'])) as decoded:
            self.assertEqual(decoded.format, 'JPEG')
            self.assertEqual(decoded.size, (1280, 640))

    def test_small_image_is_not_upscaled(self):
        path = self.write('small.jpg', image_bytes((100, 50)))
        result = preview.render_preview(path, max_edge=1280)
        self.assertEqual((result['preview_width'], result['preview_height']), (100, 50))
        with Image.open(io.BytesIO(result['data'])) as decoded:
            self.assertEqual(decoded.format, 'JPEG')
            self.assertEqual(decoded.size, (100, 50))

    def test_png_alpha_flattened_onto_white(self):
        path = self.write('alpha.png', image_bytes((120, 80), fmt='PNG', mode='RGBA', color=(0, 0, 0, 0)))
        result = preview.render_preview(path)
        self.assertEqual(result['decode_path'], 'direct')
        self.assertEqual(result['source_format'], 'PNG')
        with Image.open(io.BytesIO(result['data'])) as decoded:
            self.assertEqual(decoded.format, 'JPEG')
            self.assertEqual(decoded.mode, 'RGB')
            self.assertGreater(min(decoded.getpixel((60, 40))), 240)

    def test_multiframe_gif_uses_first_frame(self):
        frames = [Image.new('RGB', (80, 60), (220, 30, 30)), Image.new('RGB', (80, 60), (30, 30, 220))]
        buffer = io.BytesIO()
        frames[0].save(buffer, format='GIF', save_all=True, append_images=frames[1:], duration=50, loop=0)
        path = self.write('animated.gif', buffer.getvalue())
        result = preview.render_preview(path)
        self.assertEqual(result['source_format'], 'GIF')
        self.assertEqual((result['width'], result['height']), (80, 60))
        with Image.open(io.BytesIO(result['data'])) as decoded:
            self.assertEqual(decoded.format, 'JPEG')

    def test_exif_orientation_is_applied(self):
        image = Image.new('RGB', (200, 100), (10, 100, 200))
        exif = image.getexif()
        exif[274] = 6  # 顺时针旋转 90°，显示尺寸应为 100×200
        buffer = io.BytesIO()
        image.save(buffer, format='JPEG', exif=exif)
        path = self.write('rotated.jpg', buffer.getvalue())
        result = preview.render_preview(path)
        self.assertEqual((result['width'], result['height']), (100, 200))
        self.assertEqual((result['preview_width'], result['preview_height']), (100, 200))

    def test_wxgf_with_embedded_jpeg(self):
        jpeg = image_bytes((240, 120))
        path = self.write('embedded.wxgf', b'wxgf\x13\x00\x02\x04ICC_PROFILE' + jpeg)
        result = preview.render_preview(path)
        self.assertEqual(result['decode_path'], 'embedded')
        self.assertEqual(result['source_format'], 'JPEG')
        self.assertEqual((result['width'], result['height']), (240, 120))
        with Image.open(io.BytesIO(result['data'])) as decoded:
            self.assertEqual(decoded.format, 'JPEG')

    def test_wxgf_embedded_jpeg_after_header_4kb(self):
        jpeg = image_bytes((160, 90))
        path = self.write('embedded-late.wxgf', b'wxgf' + b'\x00' * 5000 + jpeg)
        result = preview.render_preview(path)
        self.assertEqual(result['decode_path'], 'embedded')
        self.assertTrue(result['warnings'])

    def test_wxgf_with_hevc_stream(self):
        bitstream = hevc_bitstream()
        # 伪造一个 NAL 类型恰为 VPS(32) 的起始码，模拟 ICC 元数据让首个候选失效的变体；
        # 实现必须在后续真实 VPS 候选处解码成功。
        fake_prefix = b'\x00\x00\x00\x01\x40\x01' + b'\x00' * 16
        data = b'wxgf\x13\x00\x02\x04ICC_PROFILE\x00' + fake_prefix + bitstream
        path = self.write('hevc.wxgf', data)
        result = preview.render_preview(path)
        self.assertEqual(result['decode_path'], 'wxgf-hevc')
        self.assertEqual(result['source_format'], 'HEVC')
        self.assertEqual((result['width'], result['height']), (64, 48))
        with Image.open(io.BytesIO(result['data'])) as decoded:
            self.assertEqual(decoded.format, 'JPEG')
            self.assertEqual(decoded.size, (result['preview_width'], result['preview_height']))

    def test_wxgf_without_image_payload(self):
        try:
            import av  # noqa: F401
        except ImportError:
            self.skipTest('缺少 PyAV，无法进入 HEVC 扫描分支')
        path = self.write('broken.wxgf', b'wxgf' + b'\x00' * 4096)
        self.assert_error(path, 'media_decode_failed')

    def test_missing_file_and_directory(self):
        self.assert_error(self.tmp / 'not-there.jpg', 'media_file_missing')
        self.assert_error(self.tmp, 'media_file_missing')

    def test_plain_text_is_not_image(self):
        path = self.write('note.txt', b'this is not an image; synthetic only\n')
        self.assert_error(path, 'media_not_image')

    def test_empty_file_is_not_image(self):
        path = self.write('empty.bin', b'')
        self.assert_error(path, 'media_not_image')

    def test_source_size_limit(self):
        path = self.write('small.png', image_bytes((10, 10), fmt='PNG'))
        with patch.object(preview, 'MAX_SOURCE_BYTES', 64):
            self.assert_error(path, 'media_too_large')

    def test_source_pixel_limit(self):
        path = self.write('wide.png', image_bytes((800, 600), fmt='PNG'))
        with patch.object(preview, 'MAX_SOURCE_PIXELS', 300_000):
            self.assert_error(path, 'media_too_large')

    def test_decoder_missing_is_reported_as_query_error(self):
        import builtins
        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == 'av' or name.startswith('av.'):
                raise ImportError('synthetic missing PyAV')
            return real_import(name, *args, **kwargs)

        jpeg_path = self.write('still-works.jpg', image_bytes((60, 40)))
        wxgf_path = self.write('needs-decoder.wxgf', b'wxgf' + b'\x00' * 4096)
        with patch('builtins.__import__', side_effect=fake_import):
            self.assertEqual(preview.render_preview(jpeg_path)['decode_path'], 'direct')
            self.assert_error(wxgf_path, 'media_decoder_missing')

    def test_returned_jpeg_matches_preview_dimensions(self):
        path = self.write('source.png', image_bytes((300, 500), fmt='PNG'))
        result = preview.render_preview(path, max_edge=200, quality=60)
        with Image.open(io.BytesIO(result['data'])) as decoded:
            self.assertEqual(decoded.format, 'JPEG')
            self.assertEqual(decoded.size, (result['preview_width'], result['preview_height']))
            self.assertEqual(decoded.size, (120, 200))

    def test_quality_is_clamped(self):
        path = self.write('clamp.png', image_bytes((40, 30), fmt='PNG'))
        for quality in (0, 101):
            result = preview.render_preview(path, max_edge=1280, quality=quality)
            with Image.open(io.BytesIO(result['data'])) as decoded:
                self.assertEqual(decoded.format, 'JPEG')


if __name__ == '__main__':
    unittest.main()
