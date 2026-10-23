# 微信 / 企业微信聊天记录导出 + chatlog

macOS 上读取**本人设备、本人账号**的微信 4.x / 企业微信 5.x 本机数据，提供两个核心能力：

1. **聊天记录导出**：抓取本机快照与密钥，导出 HTML / TXT / CSV，带图片、语音、视频、文件和表情；可导入本机 MySQL，支持每小时全来源核验与准实时文字同步。
2. **chatlog 只读查询**：把已入库记录交给 `chatlog` CLI 或本机 stdio MCP，供人和 AI 检索正文、原文、链接、语音转写、已有图片描述，并可按需查看本机图片的 JPEG 预览；不发送消息、不同步、不导出数据库 BLOB。

> 本项目不是腾讯官方 SDK。只应用于本人账号在本人设备上的数据；请遵守当地法律与客户端服务条款，不要用它导出或传播他人的聊天记录。
>
> 仓库只包含源码、文档和合成数据测试，**不含**任何真实聊天记录、密钥、导出、媒体或账号配置。克隆后第一次运行需要你在电脑前完成客户端登录/重签等人工步骤。

## 能做什么

- 微信 SQLCipher 密钥准备、有效快照、全部消息分片枚举；企业微信 wxSQLite3 解密、WAL 合并与 Node inspector 快照。
- 导出 HTML / TXT / CSV、群成员名单、本机图片、语音、视频、附件与表情；缺失媒体如实保留缺失标记。
- 两个独立 MySQL 库（`wx_chat` / `wecom_chat`），保留正文、原文、链接和来源身份，默认幂等增量。
- 每小时全来源核验 + 准实时文字同步 + 独立媒体采集；后台任务由 `launchd` 管理。
- 可选图片描述/OCR（MiniMax Coding Plan，默认关闭，必须显式授权图片外发）。
- 可选本地语音转写（Qwen3-ASR，独立 `.venv-asr`，离线运行，只处理冻结批次）。
- 可选只读 `chatlog` CLI 与 MCP（10 个只读工具，不暴露任意 SQL、shell 或路径读取；MCP 回传图片需用户显式授权）。

没有缓存的媒体不能凭空恢复；客户端版本、TCC 权限、重新登录和副本签名都会影响可用性。**不保证所有版本通用，也不对外宣称“全部导出了”**。

## 快速开始

推荐 Apple Silicon macOS、Python 3.11、MySQL 8.x、Homebrew 和 Xcode Command Line Tools。

```bash
git clone https://github.com/tcsn-xy/wechat-chatlog.git
cd wechat-chatlog
xcode-select --install
brew install python@3.11 sqlcipher openssl@3 mysql pkg-config
brew services start mysql

python3.11 scripts/deploy.py bootstrap
./.venv/bin/python scripts/deploy.py check
./.venv/bin/python scripts/deploy.py configure-mysql   # 交互输入本机 MySQL 账号；写入 keys/local.json (0600)
./.venv/bin/python scripts/deploy.py initialize-databases
```

`bootstrap` 只创建 `.venv` 并安装依赖，不会抓密钥、不会启动后台。数据库账号创建、权限与私密配置见 [完整部署](docs/deployment.md)。

## 第一次读取

先按 [读取方法](docs/reading.md) 拿到有效密钥与快照，再导出。首次抓取需要人工操作，原版 App 不会被修改。

```bash
# 微信：抓密钥（需 sudo，会退出当前微信并启动重签副本，按提示登录），然后拉快照
sudo ./.venv/bin/python scripts/capture_keys.py --keep-copy
./.venv/bin/python scripts/clone_data_local.py

# 企业微信：先取加密快照；首次需要自行退出企业微信后重签副本抓 key
./.venv/bin/python scripts/wecom_snapshot.py --with-media
./.venv/bin/python scripts/wecom_capture_key.py --yes
./.venv/bin/python lib/wecom/extract.py
```

## 导出

```bash
./.venv/bin/python scripts/export_all.py            # 微信：全量增量导出
./.venv/bin/python scripts/wecom_export.py          # 企业微信
./.venv/bin/python scripts/fetch_stickers.py        # 可选：从微信 CDN 下载表情
./.venv/bin/python scripts/doctor.py --links --invariants
./.venv/bin/python scripts/recent.py --list         # 最近 N 天视图
```

输出在 `export/`（企业微信在 `export/wecom/`）。图片、语音、视频、附件在能取到的情况下写入本机；微信只缓存你点开过的图片，缺的会显示缺失 md5。

## 入库与后台同步

```bash
./.venv/bin/python scripts/mysql_import_wx.py
./.venv/bin/python scripts/mysql_import_wecom.py
./.venv/bin/python scripts/chat_media_sync.py
./.venv/bin/python scripts/deploy.py service hourly install      # 每小时
./.venv/bin/python scripts/deploy.py service realtime install    # 准实时文字
./.venv/bin/python scripts/deploy.py service caption install --allow-image-upload   # 可选
```

细节：[信息存入与后台同步](docs/storage.md)、[媒体与 caption](docs/media-caption.md)、[故障排查与验证](docs/troubleshooting.md)。

## chatlog：只读查询 CLI / MCP

完成入库后，用专用只读账号安装：

```bash
./.venv/bin/python scripts/install_chatlog.py
~/bin/chatlog --help            # 确认 ~/bin 在 PATH
chatlog capabilities --json
chatlog chats "群名或联系人" --json
chatlog search "关键词" --platform wecom --scope text --json
chatlog messages --chat wechat:chat:123 --since 2026-10-01 --json
chatlog media wechat:message:123 --json    # 看该消息的本机图片预览
```

只读 MCP（可选，需要 `requirements-mcp.txt`）：

```bash
./.venv/bin/python -m pip install -r requirements-mcp.txt
./.venv/bin/python scripts/install_chatlog_mcp.py
```

安装器只授予查询所需表的 `SELECT`；配置写入 `~/.config/chatlog/config.json`（0600），不保存到客户端配置。MCP 的 `media` 工具可把本机图片的 JPEG 预览交给 AI 客户端，默认关闭，需用户显式运行 `scripts/chatlog_media_consent.py grant` 授权（可随时 revoke）。完整参数与 MCP 说明见 [只读聊天查询](docs/chatlog.md)。

## 可选：本地语音转写

```bash
./.venv/bin/python scripts/install_voice_asr_model.py
./.venv/bin/python scripts/voice_transcribe.py init --batch <name>
./.venv/bin/python scripts/voice_transcribe.py run
```

独立 `.venv-asr`、固定模型版本、离线识别、音频不上传；只新增转写表，不改消息、原文、媒体行或原始音频。见 [本地语音转写](docs/voice-asr.md)。

## 验证

```bash
./.venv/bin/python -B -m unittest discover -s tests -v
./.venv/bin/python -B scripts/wecom_selftest.py
./.venv/bin/python -B scripts/isolated_validation.py --report .runtime/validation/report.md
./.venv/bin/python scripts/release.py check
```

测试全部使用合成数据和临时 `repairtest_*` 数据库。合成验收不证明真实历史完整，也不覆盖客户端登录、TCC 和签名行为。

## 隐私与网络

- `keys/`、`data/`、`export/`、`media_store/`、`models/`、`.venv*`、`.runtime/`、个人分析和备份**不进入 Git**。
- 数据库没有内置口令；本机配置在 `keys/local.json`（0600），后台读取同一份文件。
- 仓库发布使用显式 `release-manifest.json` 与 `scripts/privacy_scan.py` 扫描工作树和 Git 历史，不仅依赖 `.gitignore`。
- 常规解析与入库都在本机完成。可选表情下载访问微信 CDN；可选 caption 只上传图片 JPEG 识别版本和固定提示词，不附带聊天上下文，未配置或未授权时拒绝请求；语音转写离线运行。

## 目录结构

```text
bin/                双击入口（抓密钥、拉快照、导出、体检、企微流程）
lib/wxexport/       微信导出核心（密钥、图片解密、解析、名字、渲染）
lib/wecom/          企业微信（容器访问、分页加密、快照、导出）
lib/mysql_store/    MySQL 结构、导入、媒体、实时同步
lldb/               lldb 命令模块（密钥捕获、快照克隆）
native/             企业微信内存扫描器源码
scripts/            CLI 入口、部署、发布扫描、chatlog、语音转写
docs/               部署、读取、入库、chatlog、排错、架构
tests/              合成数据回归（不读取本机真实数据）
```

MIT 许可，版权归 `tcsn-xy`。依赖与外部工具说明见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)，安全问题见 [SECURITY.md](SECURITY.md)。
