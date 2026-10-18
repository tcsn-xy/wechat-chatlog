# 微信 + 企业微信 → MySQL 入库

## 2026-10 入库与迁移约定

默认幂等增量，`--keep-existing` 保留为兼容参数。显式 `--rebuild` 只能用于通过 `--database` 指定的非正式库；正式库名不区分大小写均受保护。

新增结构迁移可重复执行，保留来源表无唯一键。修复旧来源字段错位、清理相同来源重复登记；消息唯一身份包含账号、会话、数据集、源库、源表和本地 ID，微信时间分片及企微备份不会互相挤掉。

每条消息都有 `message_raw`，包括空正文及文字模式。`raw_blob` 保存原始正文二进制，`extra_blob` 保存企微额外报文，展示文本另存。媒体以 `identity_key` 幂等更新，`md5` 是消息声明值，`content_md5` 和 `sha1` 是实际原始字节校验值；转换结果另存 `decoded_data` / `decoded_sha1`。大原始文件与大转换结果分别切块保存。

`mysql_report.py` 支持 `--wx-database`、`--wecom-database`、`--snapshot-root`、`--capture`、`--wecom-plain`、`--wecom-raw` 和 `--out`。报告从指定源重新统计，区分 pass/fail/skip/info；失败或媒体跳过返回非零状态，不能显示全量验收通过。

历史错误数据不能仅凭新代码已经修复就认定通过。应先从快照导入独立候选库，对账后再决定正式数据处理；本次正式库未作迁移或重建。

---

把本机**个人微信**与**企业微信**的全部聊天记录导入本机 MySQL。纯本地、只读，
不改微信/企业微信任何文件。

* 面向使用者的命令见 [`../AGENTS.md`](../AGENTS.md) 与 [`deployment.md`](deployment.md)；
* 对账报告由 `scripts/mysql_report.py` 写到本机忽略目录，不进入仓库；
* 企业微信数据的来龙去脉见 [`企业微信.md`](企业微信.md)。

---

## 1. 两个库，绝不混表

| 库 | 内容 | `platform` |
|---|---|---|
| `wx_chat` | 个人微信（本人 wxid，示例 `<wxid_example>`） | `wechat` |
| `wecom_chat` | 企业微信（corp uid，示例 `<corp-id>`） | `wecom` |

两套的**账号体系、会话 ID 规则、加密、表结构全都不同**，所以分库 + 每张表带
`platform` 冗余列，防止将来被误合表。表结构对称（同一套 DDL 建两次），
这样一套 SQL 在两个库上都能用。

```
lib/mysql_store/
├── conn.py          # 连接、BatchLoader、大 BLOB 分片、重试
├── schema.py        # 两个库的 DDL + 视图
├── wx_import.py     # 个人微信 → wx_chat
├── wecom_import.py  # 企业微信 → wecom_chat
├── wx_media.py      # 微信媒体定位（attach/.dat、msg/file、msg/video）
├── silk.py          # 语音 SILK → WAV
scripts/
├── clone_data_local.py     # 本地直读快照（不需要 sudo / lldb）
├── mysql_import_wx.py      # 微信入口
├── mysql_import_wecom.py   # 企微入口
└── mysql_report.py         # 对账 + 验收报告
```

---

## 2. 表结构

### 2.1 公共（两个库各一份）

| 表 | 作用 | 关键点 |
|---|---|---|
| `accounts` | 账号 | `account_key` = 微信 wxid / 企微 corp uid |
| `datasets` | 数据集登记 | 企微的 3 份 Backup 在这里登记 `included=0`，不假装丢了 |
| `contacts` | 通讯录 | 微信：`alias`(微信号)/`nick_name`/`remark`；企微：`real_name`/`corp_name` |
| `chats` | 会话 | **`chat_id` 原样保留** |
| `chat_members` | 会话成员 | **`room_nick` 群昵称按群存** |
| `messages` | 消息主表 | 唯一键见 2.3 |
| `message_raw` | 原始正文 | 解压后的 XML / protobuf / 文本原样，含全文索引 |
| `message_links` | 链接 | 正文 URL ∪ appmsg 的 `url`/`href`/`cdnurl` |
| `media_items` | 媒体 | 图片/文件/视频/语音/表情/缩略图；BLOB 在这里 |
| `blob_parts` | 大 BLOB 分片 | 超过 8MB 的切块（见 §5） |
| `import_sources` | 逐源行数 | 每个 `Msg_*` 表 / 每个 message 表 |
| `import_reconciliation` | 对账结果 | 三层对账留痕 |
| `v_chat_summary` / `v_media_coverage` / `v_message_kinds` / `v_errors` / `v_chunked_media` | 视图 | 常用查询 |

### 2.2 会话 ID 的"讲究"（这是最容易做错的部分）

**微信**

* 群：`<数字>@chatroom`，例如 `10000000001@chatroom`。**绝不能数值化或截断**。
* 私聊：`wxid_xxx`；公众号/服务号也是 `gh_xxx` / `wxid_xxx` 形式。
* 群名：`contact.nick_name`（`names.rooms`）。
* 群成员：`chat_room.ext_buffer` 里的所有 `\x13wxid_`（`chatroom_member` 表在本机是空的）。
* 群昵称：`chat_room.ext_buffer` / `chat_room_info_detail.ext_buffer_`，
  **每个群一份**，存进 `chat_members.room_nick`，`(chat_id, wxid)` 唯一。

**企业微信**

* `R:<id>` 群、`S:<对方id>_<本人id>` 单聊、`M:` 微信联系人、`Y:`/`O:` 系统或应用。
  **前缀原样保留**，`kind` 由前缀判定。
* `S:` 取名要跳过本人那一段（`messages.resolve_conv_name`）。
* 群昵称表 `conversation_user_table` 在本机**是空的** → `room_nick` 允许为空，
  不编造数据。

### 2.3 消息的主键与去重

```sql
UNIQUE KEY uk_msg (platform, chat_pk, source_db, source_table, local_id)
```

* `message_0.db` / `message_1.db` 是**时间分片，不是重复**：同一个 `Msg_<md5>` 表名
  会在两个库里各出现一次，唯一键带上 `source_db`/`source_table` 才能两片都留下。
* 企微的 `local_id` = `message_table.message_id`。
* 跨源的同一条消息靠 `server_id` 识别（同名不同库用 `GROUP BY server_id` 去重）。
* 反查不到会话的 `Msg_*` 表 → 建 **孤儿会话**（`chats.is_orphan=1`，
  `chat_id = ORPHAN_<db>_<表名后 24 位 hex>`）把消息装进去，一条不丢。

---

## 3. 媒体

### 3.1 微信

| 类型 | 消息里的键 | 本机位置 | 配对方式 |
|---|---|---|---|
| 图片 | `md5` | `msg/attach/<md5(chat_id)>/**/*.dat` | 解密后 `md5(明文)` == 消息 md5；只有缩略图时标 `is_thumb=1` |
| 文件 | `md5` | `msg/file/<yyyy-MM>/<原名>` | `hardlink.db.file_hardlink_info_v4` 的 `md5 → file_name` |
| 视频 | `md5` | `msg/video/<yyyy-MM>/<md5>[_raw].mp4` | 同上 + 前 8MB 内容 md5 兜底 |
| 语音 | `local_id`+`create_time` | `db_storage/message/media_0.db` 的 `VoiceInfo` | 表内关联 |
| 表情 | `md5` | 本机 `emoticon` 缓存 | 没有就写"本机无该表情缓存" |

`MediaRoot._stem_keys()` 要处理 **`<md5>_raw.mp4`** 这种文件名（取前 32 位 hex）；
还找不到就 `find_any()` 跨 `file`/`video`/`attach` 再兜一层。

**配不到不丢**：写一行 `media_items`，`unavailable=1` + `reason`
（例如「本机无缓存（微信只缓存点开过的图片）」）。

### 3.2 企业微信

`Profiles/<id>/Caches/{Images,Files,Videos,Voices,Emotions,*_Thumbnail}/<年月>/<md5>/<文件>`
是**明文**，目录名就是 md5。

1. 每条消息从 `content` / `extra_content` / 解码后的正文里抠 32 位 hex；
2. **只有真能在缓存里配到的 md5 才算媒体引用**（卡片/提醒正文里的随机 16 字节
   长得像 md5，不筛就会让 `has_media` 虚高）；
3. 落库时同时登记 `source_path`；
4. **没有任何消息引用到的缓存文件也全部入库**（`message_pk NULL` + reason），
   保证「本机已有的一个不落」。

### 3.3 大 BLOB

服务端 `max_allowed_packet` 只有 64MB，单条 600MB 的 INSERT 会直接
`MySQL server has gone away`。所以：

* ≤ 8MB：直接进 `media_items.data`；
* \> 8MB：元数据进 `media_items`（`data NULL`、reason 注明），二进制按 8MB 切块进
  `blob_parts`；读的时候 `WHERE table_name='media_items' AND row_id=? ORDER BY part_no`
  拼回去；`v_chunked_media` 能看每行切了几片。

高压缩比时会自动把块降到 2MB 重试一次。

### 3.4 语音

`lib/mysql_store/silk.py`：

* 本机 ffmpeg 8.1.2 **没编译 silk 解码器**（`-f silk` 报 Unknown input format），
  所以用 `pysilk`（PyPI `silk-python`，cffi 绑定的官方 SILK SDK）。
* 实测：``pysilk.decode(BytesIO(整个 blob), out, 24000)`` 直接成功，
  **不要剥那 10 字节头**（剥了反而 INVALID）。抽样 60/60 成功、约 3.6ms/条。
* 成功 → `decoded_data`(WAV) + `decoded_mime=audio/wav` + `duration_ms`；
  失败 → 原始 SILK 字节照存，`needs_decode=1` + reason。**原始字节永远不丢。**

---

## 4. 链接

`message_links` 拆成两路取：

1. **结构化**：appmsg 的 `<url>`、小程序/视频号的 `<url>`、以及 XML 里所有
   `href` / `cdnurl` / `thumburl` / `src` / `lowurl` 节点；
2. **正文扫描**：解码后正文里所有 `https?://…`（正则）。

`(message_pk, sha1(url))` 唯一，重复不重复计数。企微那边还会扫原始
`content`/`extra_content` 字节流（有些链接只存在 protobuf 里）。

跨库统计口径：`messages.has_link=1` 的条数 == `COUNT(DISTINCT message_pk) FROM message_links`。

---

## 5. 运行

```bash
# 0) 依赖（已有 .venv 可跳过）
python3.11 scripts/deploy.py bootstrap

# 1) 微信
./.venv/bin/python scripts/clone_data_local.py      # 需要微信退出时可直接读取容器
./.venv/bin/python scripts/mysql_import_wx.py       # 默认幂等增量

# 2) 企业微信
./.venv/bin/python scripts/wecom_snapshot.py --with-media
./.venv/bin/python lib/wecom/extract.py
./.venv/bin/python scripts/mysql_import_wecom.py

# 3) 对账（报告写到本机忽略目录）
./.venv/bin/python scripts/mysql_report.py --out .runtime/mysql-report.md
```

两个入口默认幂等增量，重跑不会产生重复。`--rebuild` 只能用于 `--database` 指定的非正式库；正式库 `wx_chat` / `wecom_chat` 及其大小写变体禁止重建。

---

## 6. 性能参考

- 微信全量入库通常是小时级，瓶颈是图片 `.dat` 解密与 md5 配对；企业微信通常是分钟级。
- 具体消息数、会话数、媒体体积取决于本机账号与缓存。本仓库**不发布**任何真实统计或个人验收数字。

---

## 7. 已修的坑（对照 `AGENTS.md` §11 的 M1–M9）

1. **M1** 按行拼 OR 取消息 id → 优化器卡死。改成整表一次 `SELECT`。
2. **M2** 大 BLOB 单条插入断连。改成 `blob_parts` 分片。
3. **M3** 每行 commit → 上千次 fsync。改成批量收尾提交。
4. **M4** `import_sources` 残留导致真会话被当成孤儿重复入库。来源登记先删后写，孤儿判定只信本轮。
5. **M5** `<md5>_raw.mp4` 匹配不到。文件名取前 32 位 hex + 内容 md5 + 跨目录兜底。
6. **M6** 企微假 md5 → `has_media` 虚高。只认缓存里能配到的。
7. **M7** SQL 里的 `%` 要写成 `%%`。
8. **M8** `Keyring.connect` 返回原生 tuple 行，只能 `r[0]`。
9. **M9** 长任务期间改 `lib/` 只影响磁盘、不影响运行中的进程 → 改完要重跑一遍。

---

## 8. 边界

* **不做**：收藏/朋友圈/邮件/微盘/会议入库、企微 Backup 快照的合并（只在 `datasets` 里登记为排除）。
* **可选独立步骤**：表情 CDN 下载（`fetch_stickers.py`）与本地语音转写（`voice_transcribe.py`），都不属于入库主流程。
* **不写回**：微信/企业微信的原始文件一个字节都不动；MySQL 是唯一的写入目标。
* `data/`、`keys/`、用户库里都是私密数据，别外发。
