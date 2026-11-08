# 图片、文件与批量回复

接口已经具备附件和批量收据逻辑；实际能否发送取决于**本机已验证的原生后端**。先运行 `chatlog capabilities --json`，查看 `sending.<平台>.types.image/file.supported`，再用 `chatlog status --json` 检查就绪状态。公开包不包含私有原生发送后端；工具有参数不等于两个平台已经通过真实验收。

## CLI 与 MCP

```bash
chatlog send --chat wecom:chat:123 --text "这是结果" \
  --image /absolute/figure.png --file /absolute/report.pdf \
  --file /absolute/data.csv --request-id result-001 --json
chatlog send_status result-001 --json
```

会话引用必须先从查询取得。`--text` 可省略，但文字和附件不能同时为空；文字仍为有效非空 UTF-8、最多 16384 字节。图片限有效 PNG/JPEG，其它图片格式可显式作为文件发送，不自动转换。附件是本机可读取、非空普通文件的绝对路径，不接受 URL、目录、FIFO 或末级符号链接，不执行文件。每个附件有 100 MiB 本机暂存上限，实际发送还受 `types.<类型>.max_bytes` 中已验证的更低上限约束；未提供有效限制的类型拒发。

有文字时先发文字，`--image`、`--file` 可混合重复，按出现顺序发送，附件合计最多 20 项。整批先校验身份、全部文件和后端能力；任何一项未通过都不发送文字或附件。文件流式复制到当前用户私密暂存区，摘要及发送内容使用同一份副本。附件数据仅交给明确选定的平台原生服务。

MCP 使用同一个 `send` 工具，参数示例：

```json
{"chat":"wecom:chat:123","request_id":"result-001","text":"这是结果","attachments":[{"type":"image","path":"/absolute/figure.png"},{"type":"file","path":"/absolute/report.pdf"}]}
```

`send` 仍标注为有外部副作用；现有查询及 `send_status` 保持只读。读取附件的 `media`、`attachments`、`download` 与向聊天发送附件是不同能力。

## 批量收据

旧的纯文字调用保留返回格式。带附件时额外返回 `batch_status` 和按序 `items`；每项包含固定子请求 ID、类型、`attempted` 及原有四种发送状态，可单独用 `send_status` 查询子请求 ID。

| batch_status | 含义 |
|---|---|
| complete | 每项均获关联的服务端接受回执 |
| partial | 一部分已接受，随后明确未发送或未执行；顶层 status 为 partial |
| pending | 有 submitted_unconfirmed，后续停止，可能已有成功项 |
| unknown | 存在未知结果，后续停止，可能已有成功项 |
| not_sent | 没有已接受、未确认或未知项 |

每项的 `server_accepted` 只表示匹配发送任务的服务端接受，不表示已读、已下载或桌面显示。附件上传成功不等于消息发送成功。遇到首个非 `server_accepted` 结果，停止后续提交；后续项为 `attempted=false`，不能把整批当作全部失败。

请求 ID 同时绑定平台账号、精确目标、文字、附件顺序、类型、文件名和内容摘要；相同内容重放原收据，改变内容报冲突。重放不继续执行剩余项，未知项不得换 ID 重发。整批都成功退出码 0，存在已成功项但整批未成功为 5，其它未确认发送为 4。`send_status` 查到收据退出码 0 仅表示查询成功。

支持迟到回执的常驻原生服务可在核对账号、目标和任务后更新子收据及整批汇总；这只更新已存在的收据，不发送下一项。`send_status` 自身始终只读，服务未提供可验证的新回执时继续保留未知状态。

收据不保存正文、附件内容或鉴权 token。原生服务明确释放文件后才删除暂存副本；未知的原生生命周期会保留文件，不能用超时定时删除。暂存目录为 `~/.local/share/chatlog/sending/outgoing/`，属于本机私密数据，不纳入发布。原文件被删除后，使用 `send_status` 查看收据；再次 `send` 仍需读取输入文件以核对摘要。

## AI 桥接回复

```bash
chatlog bridge reply <本次凭据> --text "这是结果" \
  --image /absolute/figure.png --file /absolute/report.pdf --json
```

需要回传本次生成的成果时，AI 可在一次回复中附带多项，不需逐附件人工确认。仅使用本条桥接凭据，按参数数组调用，不拼接 shell；先检查该来源平台是否支持附件。正文和附件集合共同锁定，不能拿同一凭据另发一套内容。

每项提交前重新核验联系人权限、后端、工作空间、运行时会话 ID 和原聊天身份；撤权或重新绑定会停止剩余项，已提交项无法撤回。CLI 通过原本的本机回复能力接口代理，不能凭回复能力管理联系人或改变目标。调用方超时不会撤销已运行的批次，也不会自动重发；保留原凭据并查原收据。

## 原生验证门槛

每个平台及每种附件类型都必须分别完成当前客户端版本的对象、线程、回调、生命周期验证与本人助手验收后才能标注支持。完成标准包括关联服务端回执、相同请求去重、接收端图片可打开及文件可下载核对、混合批量回复和重连验证。静态函数名、旧版本地址、上传完成或合成测试通过均不能代替真实验收。发送状态、本机原始记录、MySQL 同步和桌面显示分别记录，不向聊天库补造消息。
