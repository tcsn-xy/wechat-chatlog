# 安全说明

## 适用范围

本项目只在 **本人设备、本人账号** 上读取本机微信 / 企业微信数据，默认只读。
它不是腾讯官方 SDK，也不提供绕过他人设备、远程读取或批量收集他人聊天记录的能力。

## 不要提交或外发

- 真实聊天记录、导出 HTML/TXT/CSV、SQLite/MySQL 数据、媒体文件。
- `keys/`、`data/`、`.runtime/`、`media_store/`、`models/`、`.venv*` 以及任何转储。
- 数据库口令、`keys/local.json`、`keys/minimax.json`、`keys/wecom_key.json`、`~/.config/chatlog/config.json`、MCP 客户端配置。
- GitHub token、API key、私钥、`*.pem`、`*.p12`、`*.mobileprovision`。

仓库用 `.gitignore`、`release-manifest.json` 和 `scripts/privacy_scan.py` 做三重检查；仍然需要提交前人工核对 `git status` 与 `git diff --cached`。

## 报告问题

发现凭据泄露、路径穿越、任意 SQL/命令执行或查询接口越权时，请**不要**在公开 issue 里粘贴真实数据或密钥。
用仓库的私密渠道联系维护者，附最小复现（合成数据、错误类别、版本）。收到后先撤销/轮换受影响凭据，再修复和补测试。

## 凭据与后台任务

- 数据库没有内置口令；本机配置权限应为 `0600`，后台任务读取同一份私密文件，plist 只保存配置路径。
- `chatlog` 使用专用只读账号，只授予查询所需表的 `SELECT`；MCP 不暴露任意 SQL、shell、路径读取或管理命令。
- 推送凭据放在系统 keychain，不写入 `.git/config`、脚本、plist 或文档。
- 可选 caption 会把图片 JPEG 识别版本和固定提示词发送到用户显式授权的服务；未配置或未开启 `image_external_consent` 时拒绝请求。
- 语音转写离线运行，音频不上传；只处理用户明确冻结的批次。

## 第三方

WeChat、企业微信、MiniMax、Qwen、wechat-cli 等名称与商标归各自所有者。
依赖和外部工具的许可见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。本项目按 MIT 许可分发，不附带任何客户端二进制、模型权重或真实数据。
