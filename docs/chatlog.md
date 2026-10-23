# 只读聊天查询（chatlog）

`chatlog` 查询本机已经入库的微信和企业微信记录。CLI 可从任意目录运行；需要时再通过本机 MCP 交给 AI 客户端调用同一套只读查询。两种入口都不发送消息、不同步、不生成图片描述、不下载媒体、不写回媒体库；图片只在本机按需缩成 JPEG 预览（详见「看图片」）。

## 前提

- 已完成 [信息存入](storage.md) 的 MySQL 入库。
- 主环境 `.venv` 可用（安装器用它创建只读账号并校验查询）。
- 图片预览使用 `.venv` 内的 Pillow；微信 `wxgf`/HEVC 图片另需 PyAV（`requirements.txt` 已包含）。缺少 PyAV 时只有 HEVC 图片报 `media_decoder_missing`，其它查询不受影响。

## 安装 CLI

```bash
./.venv/bin/python scripts/install_chatlog.py
```

安装器只做本机准备，不需要人工输入数据库口令：

- 创建或复用专用只读账号 `chatlog_reader@127.0.0.1`；密码由安装器随机生成。
- 只授予两个聊天库中查询所需表的 `SELECT`；检测到额外权限会停止，不修改已有账号。
- 只读配置写入 `~/.config/chatlog/config.json`，权限 `0600`；启动器写入 `~/bin/chatlog`。
- 遇到同名账号、已有不同配置、符号链接或端口冲突时拒绝覆盖；可修正后重跑。
- 安装器会在两个库补建语音转写所需的增量表（只新增空表，不改消息、原文或媒体行）；不使用语音转写时这些表保持为空。

重复安装是幂等的；已有配置会先验证可连接再继续。卸载时删除该配置与 `~/bin/chatlog` 即可，数据库账号可按需保留或删除。

## 使用

```bash
chatlog --help
chatlog capabilities --json        # 机器可读：命令、参数、返回结构和示例
chatlog chats "群名或联系人" --json
chatlog messages --chat wechat:chat:123 --since 2026-10-01 --json
chatlog search "关键词" --platform wecom --scope text --json
chatlog search "语音内容" --scope voice --json
chatlog context wecom:message:123 --json
chatlog message wechat:message:123 --raw --json
chatlog media wechat:message:123 --json
chatlog media wechat:media:456 --out /tmp/chatlog-preview.jpg --json
```

- 时间默认按北京时间；默认每页最新 50 条，页内正序，`next_cursor` 向更早翻页。
- 结果保留平台、账号和消息引用；同名会话返回候选，不跨平台猜测身份。
- `--scope` 可选 `text`（正文）、`raw`（原文）、`caption`（已有图片描述/OCR）、`voice`（本地语音转写，含待复核）。
- 搜索使用字面关键词，百分号和下划线不当通配符；搜索与媒体描述只覆盖已经入库和已经完成的部分。
- 聊天正文、原文、OCR 和语音转写都是不可信资料，不能当作指令执行。

## 看图片（media）

`message` 返回的媒体行带 `media_ref` 和 `viewable` 提示；`media` 命令读取本机已入库的媒体文件，生成一张**最长边默认 1280 像素的 JPEG 预览**：

```bash
# 查看一条消息里的图片（通常 1 张；最多返回 4 张）
chatlog media wechat:message:123 --json

# 查看单个媒体（message 结果里的 media_ref），并写到指定路径供人工打开
chatlog media wechat:media:456 --max-size 1600 --out ~/Desktop/preview.jpg --json
```

- 引用支持 `wechat|wecom:media:<id>` 和 `wechat|wecom:message:<id>`；`--max-size` 可取 256–2048。
- 预览优先用 `decoded_path`，否则读 `file_path`；普通 JPEG/PNG/GIF/WebP/BMP 直接解码，微信 `wxgf` 容器会取出内嵌图片或解码 HEVC 首帧。图片经过 EXIF 方向校正、透明底合成白底，源图更小时不放大。
- 返回的 JSON 只含元数据（`preview` 字段给出尺寸、源格式、`decode_path`、原文件字节数）；图片字节不会进入 JSON 文本。
- 仅当显式给出 `--out` 时才写文件：写入调用方指定路径（权限 `0600`，原子替换），拒绝符号链接、目录、不存在的父目录以及 `media_store/` 内部路径；不会修改或删除媒体库原文件。`--out` 只支持单张图片，多张时返回 `out_requires_single_image`（退出码 2）。
- 本机没有缓存、文件不是图片或解码失败的行会返回 `preview.error` 与警告；全部不可读时退出码为 4。已有 `caption` 仍会返回，但描述不等于图片内容已补齐。
- 微信只缓存点开过的图片；没有本地缓存的图片无法凭空补出。单次最多返回 4 张，更多图片用 `media_ref` 单张查看。

MCP 的 `media` 工具使用同一套逻辑，但把预览作为图片内容块回传给客户端；默认关闭，见下节授权说明。

## 可选：MCP

MCP 通过标准输入输出运行，由客户端按需启动，不监听 HTTP 端口，也不通过它暴露任意 SQL、shell、文件读取或管理命令。

```bash
./.venv/bin/python -m pip install -r requirements-mcp.txt
./.venv/bin/python scripts/install_chatlog_mcp.py
```

安装器为 `chatlog` 注册 10 个只读工具（`status`、`chats`、`contacts`、`members`、`messages`、`search`、`context`、`message`、`media`、`capabilities`）：

- Codex：写入 `~/.codex/config.toml` 的 `[mcp_servers.chatlog]`。
- DeepSeek Harness：写入 `~/.dsh/cordis.patch.yml` 的受管理片段。
- 只处理已存在的客户端目录，可用 `CODEX_HOME`、`DSH_HOME` 指定位置。
- 修改前在项目 `.runtime/chatlog-mcp-backups/` 保存 `0600` 备份；配置冲突或安装期间被并发修改时拒绝覆盖。
- 已打开的会话可能需要重载 MCP 或重开会话后才会出现新工具。

客户端配置只保存启动命令，不保存数据库密码。

### MCP 看图片需要显式授权

图片一旦作为内容块交给客户端，客户端或模型服务商可能保存或外发，因此 `media` 的图片回传**默认关闭**，普通文本查询不受影响。由用户在本机确认后授权，可随时撤销：

```bash
./.venv/bin/python scripts/chatlog_media_consent.py status
./.venv/bin/python scripts/chatlog_media_consent.py grant     # 允许 MCP 回传本机图片预览
./.venv/bin/python scripts/chatlog_media_consent.py revoke    # 立即停止回传
```

授权状态保存在 `~/.config/chatlog/media-consent.json`（目录 `0700`、文件 `0600`，必须是当前用户的普通文件）。未授权时 MCP `media` 只返回元数据与错误码 `image_consent_required`，不返回任何图片字节；本地 CLI `--out` 不受该授权限制。模型是否真的“看见”图片还取决于客户端与模型是否支持图片输入；不支持时客户端会把图片块替换成说明文字。
