# 开发与发布

## 1. 目录与私密边界

公开仓库只包含源码、文档和合成数据测试。以下路径被 `.gitignore` 与 `release.py` 双重拦截，**永远不要** `git add -f`：

- 运行数据：`keys/`、`data/`、`export/`、`media_store/`、`models/`、`reference/`、`.runtime/`、`.venv*`、`dist/`
- 本机专用：`experiments/`、`docs/private/`、`AGENTS.local.md`、画像/拆分/证据脚本、`native/wecom_probe*`
- 通用产物：`*.db`、`*.jsonl`、`*.log`、`*.wav`、`*.silk`、`*.mp4`、`*.zip`、`*.pem`、`*.key`、`*.p12`

本机实测值放在 `AGENTS.local.md`（不公开）；公开的 `AGENTS.md` 只保留通用约束与坑位。

## 2. 发布清单

`release-manifest.json` 是**发布的唯一文件清单**，包含它自己。新增/删除公开文件后必须更新：

```bash
git add <files>
./.venv/bin/python scripts/release.py manifest   # 从 git ls-files 重新生成
./.venv/bin/python scripts/release.py check      # 清单、路径策略、大小、隐私扫描
```

`check` 会：

1. 校验清单与 `git ls-files` 完全一致；
2. 拒绝私有目录、私有后缀、符号链接、绝对路径和超过 2 MB 的单文件；
3. 扫描工作树和**全部 Git 历史**，命中 GitHub token、API key、私钥、用户主目录绝对路径、`wxid_*`、真实 `@chatroom`、企微 corp uid、QQ 邮箱、手机号等即失败；
4. 若存在本地 `keys/privacy-denylist.json`（0600、不提交），额外扫描真实账号/姓名/群号；只输出位置与类别，不输出值。

注意：`scripts/privacy_scan.py` 允许 `10000000001@chatroom` / `10000000002@chatroom` 这类合成占位符；真实群号一律用占位符。

## 3. 提交与推送

- 提交身份使用**仓库级** `tcsn-xy <tcsn-xy@users.noreply.github.com>`；不要用本机个人邮箱提交。
- 推送凭据存 macOS keychain（`git credential-osxkeychain`），remote 使用 `https://github.com/tcsn-xy/wechat-chatlog.git`，**不要**把 token 写进 URL、`.git/config` 或脚本。
- 提交前至少运行：

```bash
./.venv/bin/python -B -m unittest discover -s tests -v
./.venv/bin/python -B scripts/wecom_selftest.py
./.venv/bin/python -B scripts/isolated_validation.py --report .runtime/validation/report.md
./.venv/bin/python scripts/release.py check
```

- 改了 `lib/` 时，先确认没有长任务在跑（`launchctl list | grep wxexport`、`pgrep -f`）；必要时暂停 `local.wxexport.*`，改完再恢复。

## 4. CI

`.github/workflows/ci.yml` 在 `ubuntu-latest` + MySQL 8.0 service 上运行：

- `compileall`、全部合成回归、`wecom_selftest.py`、`isolated_validation.py`；
- `privacy_scan.py --git-history` 与 `release.py check`。

CI 的 MySQL 是 Docker service，端口映射后服务端看到的来源不是 `127.0.0.1`；`tests/test_chatlog.py` 因此用 `%` 建一次性只读账号（仅临时库 SELECT）。MySQL 8 的 `caching_sha2_password` 由 `PyMySQL[rsa]`（`cryptography`）支持。

## 5. 发布新版本

```bash
git tag -a v1.x.y -m "v1.x.y — 简要说明"
git push origin main
git push origin v1.x.y
```

在 GitHub 创建 Release，说明本次改动、已知限制和验证方式。不要宣称“全量导出”或未实测的覆盖率；新增样本不足 20 不宣称 P95 达标。

## 6. 服务与本机部署

本机部署目录就是仓库根目录时，`deploy.py`、launchd 标签和数据目录都按当前路径工作；不要把仓库移走后继续用旧 plist。
卸载/停用后台不会删除数据、密钥或恢复备份。真实数据操作仍以当轮用户授权为准。
