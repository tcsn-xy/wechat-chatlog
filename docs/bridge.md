# 微信、企微接入 Codex / dsh

桥接把命中规则的新文字消息送进明确配置的 Codex 或 dsh 会话。按人指定后端、工作空间与会话；同一个人的不同平台、私聊与群聊共用绑定。问题排队和处理使用所选后端的原生机制，不自动回退或同时投递两个后端。

## CLI 管理（推荐）

日常配置不需要浏览器。CLI 和页面共用配置校验、路由锁及并发版本检查；命令默认输出 JSON，可显式添加 `--json`。本机管理命令不作为 MCP 工具暴露给聊天内容。

```bash
chatlog bridge people list --json
chatlog bridge people add --self --name 本人 --json
chatlog bridge people add --name 联系人 --wechat wechat:contact:123 --wecom wecom:contact:456 --json
chatlog bridge workspaces --backend dsh --json
chatlog bridge sessions --backend dsh --workspace "<工程绝对路径>" --json
chatlog bridge bind --person "<授权人ID>" --backend dsh --workspace "<工程绝对路径>" --session "<实际会话ID>" --json
chatlog bridge people enable "<授权人ID>" --confirm-identities --json
chatlog bridge backend use dsh --all --json
chatlog bridge pause --json
chatlog bridge resume --json
chatlog bridge perf --seconds 60 --json
```

`people disable/remove <ID>` 停用或移除授权人，不删除聊天记录。重复添加相同精确身份返回原 ID。姓名不唯一时返回候选，管理命令优先使用 ID。本人也要绑定并启用；未配置会话不能启用。`bind` 为指定后端保存目标，当前后端不同时先保留为备用；`backend use` 只在全部目标存在且后端可用时原子切换，保留另一套绑定供回切。切换建立新消息起点，旧问题不搬运、不双投，旧路由下的回复凭据失效，旧收据保留。

### dsh 首次接入

需要已有的 dsh `web` profile。运行本项目的 `scripts/install_dsh_bridge.py` 会备份 profile 的配置，仅追加一个受管理的本机 Cordis 扩展片段。模块按内容摘要复制到私密运行目录，profile 热加载后，在现有 dsh 进程中提供 Unix socket；不复制浏览器 Cookie、不打开公网接口、不修改 dsh 核心源码，也不另启 Web 或模型进程。

适配连接配置位于 `~/.config/chatlog/dsh.json`，专用 IPC 密钥和 socket 位于桥接私密目录，权限均为当前用户专用。扩展只提供工作空间/会话目录、明确创建、绑定核验、原生排队与有界的本机会话检查，不提供任意 shell 或配置管理。使用 dsh 自己的工作空间及会话控制器注册目录和创建会话，保存实际返回的 ID；不能拿 Codex ID 直接当成 dsh 会话 ID。

## 安装与管理

沿用已部署的 chatlog、同步服务和两个平台的发送后端。首次使用：

```bash
chatlog bridge install
chatlog bridge open
chatlog bridge status --json
```

页面监听 `127.0.0.1:27146`。`open` 使用本机生成的授权链接打开页面；链接片段中的令牌进入浏览器 sessionStorage 后移除。直接打开不带令牌的地址可以看到页面，但不能读取联系人或管理配置。不要分享授权链接。管理修改请求校验管理员 bearer、Host 和 Origin；不监听局域网。

在页面搜索联系人，逐项核对精确平台 ID，将同一人的微信与企微身份加到同一联系人卡片。选择已有工作空间和已有会话，勾选身份确认、启用联系人，保存后恢复总开关。新建绑定默认未启用；同名不自动关联。本人也可添加独立绑定，由你选择工作空间与会话。

配置和记录位于 `~/.local/share/chatlog/bridge/`，目录权限 `0700`、数据库与管理令牌 `0600`。后台为 `local.chatlog.codex-bridge` LaunchAgent。停止监听可在页面暂停，或运行：

```bash
launchctl bootout gui/$(id -u)/local.chatlog.codex-bridge
```

## 触发与回复

桥接沿用目标会话已有模型、上下文、工具和权限，提示词不额外降权。AI 应主动检索、调用工具和执行任务，持续做到可验证的结果；已授权的常规操作直接完成，仅在缺少关键资料或现有权限确实要求确认时询问。提示词保留每条请求独立的回复凭据、精确目标及未知发送结果不重发规则。修改提示词不会替代 dsh／Codex 会话自身的权限设置。

| 来源 | 条件 |
|---|---|
| 已授权联系人的私聊 | 文字包含 `豆包豆包` 口令和问题 |
| 已授权联系人的群聊 | 同一条文字真实 @本人，且包含 `豆包豆包` 口令和问题 |
| 本人 | 在本人绑定中，直接使用口令，无需 @自己 |

每次追问都需重新触发。授权者可以在你们共同所在的任何群调用，其他群成员不能调用。裸口令、表情、引用消息、手打 `@昵称` 和无法核实 @对象的群消息不触发。

微信读取与正文分开保存的 `source/msgsource/atuserlist`；企微 5.0.7 读取消息体顶层文本元素中的类型 5 @对象，不从引用内容、显示名或任意 protobuf 数字猜目标。未知结构不触发。旧快照没有微信 source 元数据时，群聊 @状态为未知；需要同步服务重新加载新版解析器并重新校验来源。

问题携带独立的来源标识和回复凭据进入指定 Codex 会话。Codex 完成回答后调用：

```bash
chatlog bridge reply <本次凭据> --text "最终答案" --json
```

本次成果也可在同一次 `reply` 中附带可重复的 `--image /绝对路径/图.png`、`--file /绝对路径/文件`，合计最多 20 项。支持类型以来源平台已验证的 `sending.types` 为准，未验证时整批拒发；每项发送前复核权限，批量部分成功返回逐项收据，重放不会续发。具体参数和状态见 [图片、文件与批量回复](sending-media.md)。

该命令从运行时读取当前 `CODEX_THREAD_ID` 或 `DSH_SESSION_ID`，将本次回复提交到本机常驻服务的专用 `/api/reply` 接口；服务验证一次性回复能力、联系人授权、固定会话及原始聊天 ID 后调用已有 `chatlog send`。该接口不能管理配置，不使用管理员密钥。CLI 自身不打开或写入收据数据库，正常回信不需要给 dsh 整机写权限。相同凭据使用同一 request ID；改变正文或附件集合会冲突。暂停、撤销、重新绑定或切换后端后拒绝旧凭据回复。每项发送状态仍遵循 `server_accepted`、`submitted_unconfirmed`、`unknown`、`not_sent`；附件批量额外报告部分成功的 `partial` 汇总状态。结果未知不换 ID 重发。

桥接只保存必要的消息去重标识、原生排队标识、回复正文摘要与收据，不保存问题正文，不建立自己的任务队列。不同来源消息即使正文相同，仍分别投递；同一条消息重复入库不会重复投递。程序自己的回复会被过滤，避免触发循环。初次启用和恢复监听建立新起点，不执行历史口令。等待导入中的原文落库后才判断群聊规则。

Codex 没有回答或没有调用回复命令时，就不回信。桥接不补答、不重启 Codex、不接管它的思考过程，也不把思考或执行日志发到聊天。

## 常驻运行与资源设置

同步默认用事件唤醒，合并窗口 250 毫秒、最大 1 秒；桥接保留 1 秒兜底轮询和持久游标。入库提交后只发送消息 ID 和检测时间的本机通知，不发送正文。只有授权候选消息才加载正文/@元数据；监听复用按线程隔离的只读连接，健康探测独立运行。

源库变化时优先读新增记录，不重新排入所有会话的完整尾部扫描。默认每 5 分钟启动一轮有界尾部检查（每源 100 行）；未完成的一轮不重复叠加。全量对账默认每天北京时间凌晨 3 点发起，未完成任务通过 MySQL 的独立源表扫描游标、行数及会话计数断点继续。后台处理默认两平台各 5% 的保守时间预算，新增记录优先且不受历史预算限制；手动 `scripts/chat_watch_sync.py --reconcile` 仍可要求完整对账。未修改源库，不依靠有限尾部检查宣称历史全覆盖。

可在 `~/.config/chatlog/performance.json` 保存当前用户所有、权限 `0600` 的调优值：`background_cpu_budget`（默认 0.10）、`full_scan_hour`（3）、`tail_interval_seconds`（300）、`tail_rows`（100）、`debounce_seconds`（0.25）、`max_debounce_seconds`（1.0）、`poll_seconds`（1.0）、`health_seconds`（15）。非法或不安全配置按默认值处理，修改后平滑重载对应后台。

企微可执行文件采用分块摘要，后台健康探测复用稳定文件身份；实际发送仍完整校验文件摘要、进程和账号。微信对活进程保持隔离检查，内核已确认的僵尸子进程不当作活进程。正常健康探测低频运行，异常时每 0.5 秒复查，避免短暂失败阻塞整个健康间隔。健康缓存不替代发送前检查，未知发送结果不自动重试。

`perf` 报告每个服务、dsh、MySQL 及客户端主进程的 CPU 与 RSS；100% CPU 表示一个核心，RSS 不是独占内存。模型推理、历史补齐和空闲监听分场景测量。只有收到提交后通知并能对应到真实新增消息的样本才计入“检测 → 后端入队”延迟；每平台不足 20 个有效样本，P95 保持为空，入库延迟与模型回答耗时另行报告。

## 后端接口与边界

调用固定的 `/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex queue --thread <ID> --message <内容>`。不使用 `codex exec`、另起 app-server 或复制会话。工作空间与会话通过 Codex 自己的本机目录只读列出，提交前验证精确 ID、目录和归档状态；不通过修改 Codex 数据库投递消息。客户端升级后应重新验收队列行为。

Codex 使用上述桌面接口；dsh 使用现有 profile 的原生 `session.prompt(mode=queue)`，不启动第二个 agent 或用 SDK 的最后一条回答猜测多个问题之间的归属。每个问题用自己的回复能力和目标发送。

沿用你选择的工作会话已有权限；本桥接不把该会话变成独立的权限沙箱。授权管理入口和聊天问题分开，授权配置不能通过聊天正文修改。已有模型审批仍在原会话中处理，不自动批准整机权限；回复 CLI 的专用服务接口只授予该条消息的回复能力。

聊天原库保持只读，不补造消息。发送收据、微信原库、快照和 MySQL 查询是不同层次；MySQL 暂时查不到发送消息不能直接证明微信未保存，也不能据此重发。

## 验收

合成测试覆盖账号与发送者校验、两平台真实 @、一人多聊天同一会话、历史起点、并发/重复导入、未知投递、撤销、回复目标和正文冲突、回环过滤，以及管理接口鉴权。实际验收必须确认桌面会话消费原生队列，并分别验证两个平台的完整问答。真实材料仅保存在本机私密运行目录。
