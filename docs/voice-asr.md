# 本地语音转写

可选模块：用 Qwen3-ASR-0.6B 的 MLX 8bit 版本，把已经入库的语音离线转成文字。模型固定仓库与 revision，推理环境独立在 `.venv-asr/`，不占用聊天同步环境。音频只在本机读取和识别，不上传任何服务。

## 前提

- 语音媒体已经通过 `chat_media_sync.py` / `chat_media_manage.py` 写入 `media_store/`，MySQL 中有对应 `media_items` 行。
- 本机为 Apple Silicon macOS，`ffmpeg` 可用（用于解码非 WAV 音频）。

## 安装

```bash
python3.11 -m venv .venv-asr
./.venv-asr/bin/pip install -r requirements-asr.txt
./.venv/bin/python scripts/install_voice_asr_model.py
```

- 模型下载到 `models/`，不进 Git；下载后校验文件长度、Git blob 和 LFS SHA256，任一项不符即停止。
- 固定 revision 在 `scripts/voice_asr_core.py`，下载端点与恢复式分段下载在 `scripts/install_voice_asr_model.py`；验证结果保存在 `models/`，运行前会再次核对。
- 运行阶段强制离线与遥测关闭；不会自动更新模型。

## 冻结批次

```bash
# 1) 冻结当前已入库语音为批次清单（只此一次；不自动接收未来语音）
./.venv/bin/python scripts/voice_transcribe.py create

# 2) 校验原文件并注册转写队列
./.venv/bin/python scripts/voice_transcribe.py init

# 3) 启动有限批次后台任务（处理完退出，没有周期触发）
./.venv/bin/python scripts/voice_transcribe.py start
./.venv/bin/python scripts/voice_transcribe.py status
./.venv/bin/python scripts/voice_transcribe.py disable

# 手动或中断续跑
./.venv-asr/bin/python scripts/voice_transcribe.py run --limit 10
./.venv-asr/bin/python scripts/voice_transcribe.py resume
./.venv/bin/python scripts/voice_transcribe.py retry
./.venv/bin/python scripts/voice_transcribe.py verify
```

- `create` 只冻结当时 `media_items` 中的语音；之后新入库的语音不会加入已冻结批次。确需另一批时用 `create --manifest <新路径>` 生成新清单，不覆盖当前清单。
- 清单、状态和证据都放在被 Git 忽略的 `.runtime/voice-asr/`；清单含 `0600` 权限。
- 批次按平台、账号、输入哈希和模型配置复用结果；重复 `init` 不扩充清单或重复入队。
- `disable` 只停止转写任务，不影响消息同步、媒体同步或 caption。

## 状态语义

| 状态 | 含义 |
|---|---|
| `complete` | 生成了非空转写文本 |
| `needs_review` | 空结果、异常重复或达到长度上限，需人工复核 |
| `silence` | 检测为静音，没有文本 |
| `pending_format` | 原始 PCM 的采样参数缺少证据，保持待处理 |
| `failed` | 处理失败，保留原因代码与原文件 |

`complete` 只表示有非空文本，不代表人工核验准确率；模型不提供可校准置信度，因此不报告虚构的置信分数；`segments` 是模型输出的 chunk 边界，不是逐字对齐。`verify` 会对整批原文件做哈希校验并检查转写与媒体行的关联。

## 数据边界

- 只新增 `voice_asr_batches`、`voice_asr_items`、`voice_transcripts` 三张表，不修改消息正文、原文、媒体行、原始音频或已有 WAV。
- 企微原始 PCM 只有在伴随 SILK 解码结果与已存 PCM 逐字节一致时才采用该采样率，并保存 SILK 与 WAV 证据；缺少证据时保持 `pending_format`，不按文件长度猜参数。
- 转写结果只在 MySQL 和 `.runtime/` 中；`chatlog search --scope voice` 可查询文字与状态。
