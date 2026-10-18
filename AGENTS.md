# 开发约束与已修过的坑

> 给 AI agent 的项目说明。动手前先读完，尤其是「已修过的坑」。
> 本机实测值（账号、密钥、群、服务路径）写在 `AGENTS.local.md`，该文件不进入 Git。
> 人类用户看 [`README.md`](README.md)、[`使用说明.md`](使用说明.md) 和 [`docs/`](docs/)。

## 1. 项目是什么

macOS 上读取**本人设备、本人账号**的微信 4.x / 企业微信 5.x 本机数据，提供两个核心：

1. **导出**：抓密钥与快照 → HTML/TXT/CSV + 图片/语音/视频/文件/表情 → 两个独立 MySQL 库。
2. **chatlog**：只读 CLI / MCP 查询已入库记录；不发送、不同步、不改数据。

公开仓库只含源码、文档和合成数据测试。任何真实聊天记录、密钥、导出、媒体、日志和个人报告都不得提交。

## 2. 数据模型里最容易搞错的地方

### 2.1 名字有五层，别混

| 字段 | 来源 | 含义 |
|---|---|---|
| `wxid` | `contact.username` | 微信内部 ID，形如 `<wxid_example>`，人人都有 |
| `alias` | `contact.alias` | 微信号（登录 ID），只有好友有 |
| `nick_name` | `contact.nick_name` | 微信昵称 |
| `remark` | `contact.remark` | 你给他设的备注，只有好友有 |
| 群昵称 | `chat_room.ext_buffer` / `chat_room_info_detail.ext_buffer_` | **每个群各不相同**，只有显式设置过的人有 |

**显示名优先级：备注 → 群昵称 → 微信昵称 → 微信号 → wxid。**

这个顺序是用微信自己生成的系统消息（`<sysmsg>`，「谁邀请谁入群」）对证出来的：备注会盖过群昵称。
想换顺序改 `lib/wxexport/names.py:NAME_PRIORITY`，或设 `WXEXPORT_NAME_ORDER=room_nick,remark,nick,alias`。

### 2.2 群昵称必须按群存

`names.Directory.room_names[群ID][wxid]`。**绝不能合并成一张全局 wxid→群昵称 表**：
同一个人在 A 群叫「A 群昵称」、在 B 群叫「B 群昵称」、在 C 群可能没设。合并后所有群会串名。

### 2.3 `message_0.db` / `message_1.db` 是时间分片，不是重复

微信把超大会话按时间切成两片。同一个 `Msg_<md5>` 表名会同时出现在两个库里，
**两段拼起来才是全量**，不能当重复删掉。唯一键必须带 `source_db` / `source_table`。

### 2.4 `Msg_<md5>` 表名反查会话

表名是 `"Msg_" + md5(会话ID)`。反查要把 `session.db` 的 `SessionTable` / `Name2Id`
和 `contact.db` 的 `contact` / `chat_room` / `stranger` 里所有用户名都做一次 md5 建反查表。
反查不到的表建**孤儿会话**，一条不丢。见 `export.py:enumerate_chats()`。

## 3. 已修过的坑（**别再改回去**）

### 微信导出

| # | 坑 | 正确做法 | 位置 |
|---|---|---|---|
| 1 | 群昵称合并成全局表，跨群串名 | 按群存 `room_names[room][wxid]`，`display()` 必须传 `room` | `names.py` |
| 2 | 显示名「群昵称优先」 | 改成 `备注 → 群昵称 → 微信昵称 → 微信号` | `names.py:NAME_PRIORITY` |
| 3 | 群成员数只数了「有群昵称的」 | 解析 ext_buffer 时**所有 `\x13wxid_` 都计入**，昵称单独收 | `names.py:_parse_ext_buffer` |
| 4 | 引用消息把原始 XML 摊进记录 | 先过 `clean_quote()`，剥出 `<title>` 或类型标签 | `parse.py:clean_quote` |
| 5 | 群系统消息前缀是会话 ID，被当成假成员 | sender 等于会话 ID 或以 `@chatroom` 结尾 → 归为「系统」 | `export.py:_collect_records` |
| 6 | `wxclone` 用具名变量导致第二次调用 `redefinition` 静默失败 | 表达式直接返回 `(int)clonefile(...)` | `lldb/wxcap.py` |
| 7 | 群成员数写成 0 | `chatroom_member` 可能为空，成员只能从 `chat_room.ext_buffer` 解 | `names.py` |

### 微信快照 / 密钥（W1–W6）

| # | 坑 | 正确做法 |
|---|---|---|
| W1 | `_eval` 用 `SetTopLevel(True)` / 从 Python 回调调 `expr` → lldb 卡死、微信假死 | 克隆只用**裸 `expr` 命令**、写在 lldb 脚本文件里 |
| W2 | 对整个 `db_storage` 做目录级 `clonefile` 会卡死 | 往下一层，逐个克隆 `DB_SUBDIRS` |
| W3 | `clonefile` 不建父目录，父目录不存在一律 -1 | 先 `os.makedirs(os.path.dirname(dst))` |
| W4 | staging 目录属 root，微信（普通用户）写不进 | staging 放发起 sudo 用户的 `~/Downloads`，并 `chown` |
| W5 | 微信自更新把副本签名换回官方签名 | 重签后 `chmod -R a-w` 锁只读；被换掉就重签 |
| W6 | 增量快照没有 `msg/`、`ilink/`，媒体密钥抛错 | `config.media_v2_key` 拿不到返回 `None`，导出跳过媒体 |

### MySQL 入库（M1–M18）

| # | 坑 | 正确做法 |
|---|---|---|
| M1 | 按行拼 `OR` 取消息 id，优化器卡死 | 同一 `(chat_pk, source_db, source_table)` 整表一次 `SELECT` 再建 map |
| M2 | 单条 600MB INSERT 断连 | 超过 8MB 的 BLOB 切块进 `blob_parts`，取回按 `part_no` 拼 |
| M3 | 每写一行 commit | 攒一批，`_close_media()` 收尾提交 |
| M4 | `import_sources` 残留把真会话判成孤儿重复入库 | 孤儿判定只信本轮写入；来源先删后写 |
| M5 | `<md5>_raw.mp4` 匹配不到 | 取前 32 位 hex，退化到前 8MB 内容 md5，再跨目录兜底 |
| M6 | 企微卡片随机 md5 让 `has_media` 虚高 | 只有真能在本机缓存配到的 md5 才算媒体引用 |
| M7 | Python 的 `%` 是格式化占位符 | 报告脚本里的 `LIKE 'R:%'` 写成 `LIKE 'R:%%'` |
| M8 | `Keyring.connect` 返回原生 tuple 行 | 一律按下标取 `r[0]` |
| M9 | 长任务期间改 `lib/`，内存里仍是旧代码 | 改动前后各跑一次全量；长任务用 `nohup` + 日志 |
| M10 | `sqlcipher3` 占位符是 `?`，写成 `%s` 抛错且被吞 | 用 `?`；异常第一次必须打日志 |
| M11 | 语音消息没有 md5，`refs` 为空 | `has_media` 按 `kind ∈ MEDIA_KINDS` 判，语音按 `(local_id, create_time)` 取 |
| M12 | 视频消息 md5 在 `newmd5`/`originsourcemd5` | `wx_refs.py` 在 `parse.describe` 之上补候选 md5 |
| M13 | 判文件是否漏了按路径比 | 覆盖率按 **md5** 比，不按路径 |
| M14 | 空壳媒体消息正文没有引用 | 仍落一行 `unavailable=1` + 原因 |
| M15 | `import_sources` 上建唯一键 | **不要建**：同一个 `Msg_<md5>` 会跨分片共用 chat_id |
| M16 | 统计工作日日均没剔除数据空洞日 | 判据双条件：本群 0 条 **且** 整机 <= 200 条 |
| M17 | 用绝对阈值找异常低的工作日 | 群有成长期，用月内同类日中位数相对比较 |
| M18 | 报告手写死统计数字 | 数字必须由脚本算出再填 |

### 企业微信（另一套，别和微信混着改）

| # | 事实 | 说明 |
|---|---|---|
| 1 | 容器读不了不是权限，是 TCC | `~/Library/Containers/com.tencent.WeWorkMac/Data` 对外部进程（含 root）EPERM |
| 2 | 唯一入口是 Node inspector | `SIGUSR1` 在 `127.0.0.1:9229` 起 inspector，在它自己的沙盒里跑 shell |
| 3 | 库是 wxSQLite3 AES-128 分页，不是 SQLCipher | 每页 key = `MD5(raw_key(16) + LE32(page) + b"sAlT")`，见 `lib/wecom/crypto.py` |
| 4 | key 不在磁盘，只能从副本内存挖 | 复制 App → 逐组件重签 → 启动副本 → `wxdump` 导出可写内存 → `native/wecom_keyscan` 扫 |
| 5 | `conv_snapshot` 是明文 | protobuf，不需要 key；解析在 `lib/wecom/conv.py` |
| 6 | 不要退回 `CC_MD5` 断点或 `leaks` 读受限原版 | 实测不可用；用可写内存扫描 |
| 7 | 群昵称表可能为空 | `room_nick` 允许为空，不编造 |
| 8 | 改 `lib/wecom/` 后跑 `wecom_selftest.py` | 合成加密库跑通「扫描→解密→WAL→解析→渲染」 |

### wechat-cli / 全文索引

| # | 事实 | 应对 |
|---|---|---|
| C1 | 上游 `search` 搜不到 zstd 压缩正文 | 用 `scripts/wccli_search.py --source full` |
| C2 | 会话名含特殊空白时匹配不上 | 粘会话 ID，别打名字 |
| C3 | 它会把整库明文缓存写到 `$TMPDIR` | `doctor.py` 体检 + `--clean-cache` |

## 4. 加密与格式速查

```
容器格式（V2）
  0   6B  magic  07 08 56 32 08 07
  6   4B  aes_size
  10  4B  xor_size
  14  1B  保留
  15  ..  payload
  plain = AES_ECB(key, payload[:align16(aes_size)])[:aes_size]
        + payload[align16(aes_size) : len(payload)-xor_size]
        + xor(payload[len(payload)-xor_size:], xorkey)

AES key = md5(uin + wxid)[:16]     V1 用固定 key cfcd208495d565ef
异或字节：JPEG 明文以 FF D9 结尾 → xorkey = 密文末字节 ^ 0xD9（自动探测，不要写死）
```

* `md5(解密后的明文) == 消息 XML 里的 md5`，图片配对靠全解一遍算 md5 建索引。
* 解出来开头是 `wxgf` 的是 HEVC 高效图，用 PyAV 转 JPEG。
* 密钥推导：`PBKDF2-SHA512(password, db_file[:16], 256000, 32)`，结果直接当 SQLCipher raw key。
* 别用 2 轮 PBKDF 的那条记录，那是单库派生密钥。

## 5. 覆盖率的真相

**别对外宣称「全部导出了」。** 微信只缓存你点开过的图片；视频/语音/文件/收藏/朋友圈的覆盖取决于本机缓存与是否单独采集。
缺失内容在记录里保留 `[图片]（本机无缓存 md5=…）` 或 `unavailable=1` + 原因，不能凭空恢复。
企微 `conv_snapshot` 只覆盖会话列表和最后一条消息；群昵称表可能为空。

## 6. 改完代码必须验证

改了 `lib/` 里任何东西，至少跑：

```bash
./.venv/bin/python -B -m unittest discover -s tests -v
./.venv/bin/python -B scripts/wecom_selftest.py
./.venv/bin/python -B scripts/isolated_validation.py --report .runtime/validation/report.md
./.venv/bin/python scripts/release.py check
```

导出不变量（写新代码时别破坏）：

1. 每个会话的 `聊天记录.csv` 都有 `发送者wxid` 列且不为空；
2. 群聊目录下都有 `成员名单.csv` / `成员名单.html`；
3. 群系统消息**不会**以会话 ID 的身份出现在发送者里；
4. `聊天记录.txt` 的 `[引用 …]` 里**不会**出现 `<msg>` / `<appmsg` 原始 XML；
5. 成员名单页数字对得上：`成员发言合计 + 系统/无法定位 = 消息总数`；
6. `doctor.py --links` 的资源引用缺失数为 **0**。

正式库保护：`--rebuild` 只能用于 `--database` 指定的非正式库；`wx_chat` / `wecom_chat` 及其大小写变体禁止重建。

## 7. 隐私与边界

* `keys/`、`data/`、`export/`、`media_store/`、`models/`、`.venv*`、`.runtime/` 是私密数据，不上传、不外发、不贴在线服务。
* 全程只读：数据库一律 `PRAGMA query_only = ON`；原版 App 一个字节都不动。
* 用途仅限导出**本人账号**在**本人设备**上的数据。
* `keys/capture.jsonl` 含账号口令，删了就要重新扫码登录才能再抓。
* 真实统计、验收报告、群画像、自动发消息实验只在本机，不进入公开仓库。

## 8. 发布约定

* 公开源码增加/删除时同步 `release-manifest.json`；`python scripts/release.py manifest` 生成，`check` 校验。
* 打包/发布只取清单文件；运行目录、压缩包和 Git 历史都要扫描，不能只依赖 `.gitignore`。
* 提交身份使用仓库级 `tcsn-xy`，不要带本机个人邮箱；推送凭据放 keychain，不写入 `.git/config`、plist 或文档。
* 真实覆盖或延迟按实际状态报告；新增样本不足 20 不宣称 P95 达标。
* 客户端版本、TCC 授权、重新登录和副本签名会影响可用性，不承诺所有版本通用。
