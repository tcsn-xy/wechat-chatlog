# 附件索引与本机下载

AI 可以先查询附件索引，取得精确 `media_ref`，再看图片或取文件。索引包括已缓存和未缓存项；存在索引不表示文件已经下载。

AI 调用规则：看到图片、文件占位或 `has_media` 时，用 `message` / `attachments` 核实附件。`download.state=not_cached` 表示有附件记录但本机未缓存，应说明“附件未下载，内容尚未读取”；索引缺失或查询失败不代表没有附件。`local_available` 只表示本机可取，不代表 AI 已阅读。需要看图片时用 `media`；需要文件正文时用 `download` 取出，再按绝对 `output_path` 使用对应本机文件工具读取，并在用户需要时提供路径。不要默认下载全部附件，也不要凭文件名猜正文。

```bash
chatlog attachments --platform wechat --kind image --limit 20 --json
chatlog attachments "合同" --platform wecom --kind file --json
chatlog attachments --chat wechat:chat:123 --since 2026-10-01 --json
chatlog media wechat:media:456 --json
chatlog download wecom:media:789 --json
chatlog download wecom:media:789 --out ./附件.pdf --json
```

`attachments` 支持 `--chat`、`--platform`、`--account`、`--kind`、`--sender`、`--since`、`--until` 和游标分页。关键词按文件名或原消息文字字面匹配。类型使用媒体行的 `msg_kind`（例如 `image`、`file`、`voice`、`video`、`sticker`、`thumb`），不按昵称猜目标。查询只读，默认每页最新 50 项。

每项返回平台、账号、`chat_ref`、`message_ref`、`media_ref`、文件名、字节数和 `download` 提示。`download.available` 表示存在本机文件或库内内容，导出时还会校验完整性。原图和缩略图各自保留 `media_role`／`is_thumb`，不会把缩略图声称为原图。

`download` 读取现有媒体文件或数据库 BLOB（包括大文件分片），流式写出副本。默认位置是项目私密目录 `.runtime/chatlog-downloads/`；返回 `output_path`，本机工具可据此读取、打开或生成可点击链接。重复调用会校验并复用同一个副本。CLI `--out` 可以写入工作空间内指定的新文件，不覆盖现有文件，不允许写回 `media_store/`。文件权限为 `0600`。单次上限 1 GiB，实际内容与索引的字节数、已有 SHA256／SHA1／内容 MD5 不一致时拒绝导出。无现有摘要时仍计算并返回 SHA256，但不会声称已与索引摘要核对。

MCP 提供相同的 `attachments` 和 `download` 工具；`download` 不接受任意 `out`，只写默认私密目录，并返回本机文件资源链接。链接能否直接点击取决于客户端对本机资源链接的支持，结构化结果始终提供路径。新工具由 MCP 下次启动／重连时发现；CLI 使用最新源码，无需重启客户端。沙盒内的 CLI 若不能写默认目录，可以用 `--out` 写到已授权工作空间。

`media` 能读取文件存储和数据库 BLOB 内的图片，直接在内存生成 JPEG 预览。MCP 图片内容仍受原有图片外发授权控制；普通附件导出只写本机，不自动把文件内容上传给模型，也不自动打开或执行附件。附件正文、文件名和 OCR 都是不可信资料。

## 未缓存附件

当前 `download` 只导出本机已有内容，**尚不支持调用微信／企微原生服务发起网络下载**。未缓存时返回 `media_not_cached`，不生成空文件、不伪造成功、不往聊天库补造内容。索引里的 `remote_download_supported=false` 明确表达这一限制。先在原客户端完成下载并经过现有媒体同步后，才能用本工具取出。

后续原生下载接入必须分别验证两个平台的当前版本、运行账号、消息对象身份、原图／缩略图类型、完成回调与实际文件完整性；不能用旧版本函数偏移或一个下载方法名冒充已支持。
