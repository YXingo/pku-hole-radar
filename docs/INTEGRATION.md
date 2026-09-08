# 外部集成核验记录

最后核验：2026-09-08（Asia/Shanghai）

本文只记录已经看到的证据，不把客户端代码中的推断写成平台保证。树洞部分已经通过
Chrome 中的合法登录会话观察到真实列表请求和成功响应，并由 PKUHoleRadar 自己的 CLI
使用同一会话完成了一页只读 probe。PushPlus 首次实名前测试曾被拒绝；实名认证后按用户
授权重新执行的合成测试已取得服务端受理流水号，用户随后确认在微信中收到通知，并提供了
设备通知卡片和详情页截图。该证据区分为“实际收件”，不把 PushPlus 的 `accepted` 冒称为
永久送达保证或个人好友聊天。

## 1. 树洞

### 1.1 来源与证据等级

| 来源 | 日期 / 版本 | 证据等级 | 结论 |
| --- | --- | --- | --- |
| [北大树洞官网](https://treehole.pku.edu.cn/) 首页 HTML 与当前前端资源 `index-da7e78a1.js` | 2026-09-05 | 公开代码确认 | 页面标题为“北大树洞”；前端 API 基址为当前站点的 `/chapi/`，请求使用 `withCredentials`。当前前端路由表同时列出 `/api/v3/hole/list` 与 `/api/v3/hole/list_comments`。 |
| [PKUHoleTUI](https://github.com/dfshfghj/PKUHoleTUI/tree/8ac64808c97ab87bbcc5ed70e28932cdf3989120) | commit `8ac64808c97ab87bbcc5ed70e28932cdf3989120`，2026-08-23 | 公开代码确认；线索而非官方承诺 | MIT 许可客户端将最新列表实现为 `GET /chapi/api/v3/hole/list_comments`，请求 `page`、`limit`、`comment_limit`、`comment_stream`，并解析 `data.list` / `data.total`。 |
| 无会话最小请求 | 2026-09-05 | 账号实测（未登录负例） | 对下述列表地址请求 `page=1&limit=1&comment_limit=0&comment_stream=1` 返回 HTTP 401、`WWW-Authenticate: jwt-auth` 和 HTML `Unauthorized` 页面；未取得成功列表，不能据此确认登录后的字段值。 |
| Chrome 登录会话的官网 Network 面板 | 2026-09-05/06 | 账号实测（浏览器） | 首页实际发出 `GET https://treehole.pku.edu.cn/chapi/api/v3/hole/list_comments?page=1&limit=10&comment_limit=10&comment_stream=1`，随后观察到 page 2、page 3；三次均为 HTTP 200，响应可在 Preview 中解析。 |

### 1.2 账号实测的列表响应

本次只记录结构、参数和不含凭据的样本，不保存原始响应正文。浏览器首页的真实请求使用
`limit=10`，因此已把本地 `config.toml` 的初始 `page_size` 调整为 10；适配器仍按首版
“不主动读取评论”的要求发送 `comment_limit=0`。CLI probe 已验证这一参数组合在当前会话
下可用，但不把网页的 `comment_limit=10` 与适配器的 `0` 静默宣称为完全等价。

已观察到：

- page 1、2、3 均使用从 1 开始的十进制页码；实际 URL 分别包含
  `page=1`、`page=2`、`page=3`。
- Preview 顶层为 `code=20000`、`message="success"`、`success=true`，帖子数组位于
  `data.list`，并有整数 `data.total`。
- 帖子样本包含 `pid`、`text`、`timestamp`、`is_top`、`media_ids` 等字段；`pid` 为
  十进制整数，时间戳样本 `1788623541` 对应本地时间 `2026-09-05 23:52:21 +0800`，
  与页面显示的 `09-05 23:52` 一致，故当前样本支持 Unix 秒。
- page 2 的列表从 `8514244` 递减到 `8514235`；随后抓到的 page 3 从 `8514236`
  递减，和 page 2 的末尾重叠 `8514236、8514235`。期间 `data.total` 也从 21 变为
  31，说明页码分页面对新增帖会发生页漂移；跨页去重是必要的，不能把这两页当成
  一致快照。
- 2026-09-06 的 PKUHoleRadar CLI probe 使用 `comment_limit=0` 成功请求 page 1，
  解析 10 个帖子，响应字段映射和 `timestamp(seconds)` 均通过；该 probe 未写入基线或
  outbox。
- 每个已观察页内 PID 都按降序排列，但页漂移使“跨页严格连续”和“PID 永久单调”
  仍不能视为平台保证。没有在样本页中看到可单独证明置顶边界的案例；`is_top` 字段
  确实存在，程序仍必须排除置顶对水位的影响。

浏览器列表请求的 Request Headers 只记录字段名和存在性：`Authorization: Bearer ...`、
`uuid`、`useragent: pku_web`、`X-XSRF-TOKEN`，以及同站 Cookie 中的 `pku_token`、
`XSRF-TOKEN`。本记录不保存任何值；该请求中没有把完整 Cookie 或 token 写入文档。
`_session` 是否是列表请求的必需字段仍未确认。

### 1.3 当前已确认的列表契约与适配差异

合法浏览器会话已经证明当前首页实际使用 `/chapi/api/v3/hole/list_comments`，因此本地配置已填写
这个 endpoint；CLI 也已在本机使用同一合法会话运行 `probe-source`，验证本项目
`comment_limit=0` 的 page 1 请求被接受并成功解析。用户又确认候选原帖链接可以打开；这些结果
覆盖当前会话和样本，不等于平台长期稳定或跨页永不漂移。

请求：

```text
GET https://treehole.pku.edu.cn/chapi/api/v3/hole/list_comments
?page=1&limit=<page_size>&comment_limit=0&comment_stream=1
```

公开代码和浏览器响应共同确认：

- `page` 的起点是 1；网页本次实际使用 `limit=10`，PKUHoleTUI 的默认值是 20。
- `comment_limit=0` 可以让本项目不主动获取评论；2026-09-06 CLI probe 已验证该参数组合
  在当前会话下可用；`comment_stream=1` 是该客户端的默认列表参数。
- 可选查询字段在客户端中还包括 `pid`、`keyword`、`label`、`kind`、`is_follow`；PKUHoleRadar 首版不使用这些服务端筛选字段，关键词在本地完成。
- 成功 JSON 至少应有顶层 `code=20000`，`data.list` 数组和 `data.total` 数字。列表元素公开代码读取的业务字段包括：`pid`、`text`、`timestamp`、`is_top`、`media_ids`，以及若干状态字段。
- `pid` 是十进制整数形态，`timestamp` 的本次账号实测样本符合 Unix 秒（`int32`）；仍不把时区显示或服务端长期稳定性写成平台保证。
- `is_top=1` 被客户端显示为“置顶”。本项目会把置顶帖从水位边界判断中排除。

证据链接：

- [PKUHoleTUI `treehole.go`（commit 固定链接）](https://github.com/dfshfghj/PKUHoleTUI/blob/8ac64808c97ab87bbcc5ed70e28932cdf3989120/internal/client/treehole.go#L24-L40)：host、旧 API 常量和候选列表地址。
- [PKUHoleTUI `treehole.go`（commit 固定链接）](https://github.com/dfshfghj/PKUHoleTUI/blob/8ac64808c97ab87bbcc5ed70e28932cdf3989120/internal/client/treehole.go#L516-L530)：列表方法、页码和参数。
- [PKUHoleTUI `treehole_v3.go`（commit 固定链接）](https://github.com/dfshfghj/PKUHoleTUI/blob/8ac64808c97ab87bbcc5ed70e28932cdf3989120/internal/client/treehole_v3.go#L248-L300)：V3 列表 envelope 和 `list` / `total` 映射。
- [PKUHoleTUI `treehole_v3.go`（commit 固定链接）](https://github.com/dfshfghj/PKUHoleTUI/blob/8ac64808c97ab87bbcc5ed70e28932cdf3989120/internal/client/treehole_v3.go#L813-L855)：帖子字段映射。

### 1.4 会话字段

公开代码观察到的名称和格式如下，不能替代用户会话验证：

| 位置 | 名称 / 格式 | 证据等级 | PKUHoleRadar 处理 |
| --- | --- | --- | --- |
| HTTP header | `Authorization: Bearer <token>` | 公开代码 + 浏览器实测 + CLI probe | 从显式 secrets 文件读取原始 token，运行时加 `Bearer `；不放 URL。当前四字段组合已成功请求。 |
| HTTP header | `uuid: <device-id>` | 公开代码 + 浏览器实测 + CLI probe | 从本地会话填写稳定值；当前请求携带该字段并成功解析。 |
| Cookie | `pku_token=<token>`，域 `treehole.pku.edu.cn` | 公开代码 + 浏览器实测 + CLI probe | 从本地 secrets 中提供；当前请求携带该 Cookie 并成功解析。 |
| Cookie / header | `XSRF-TOKEN` → `x-xsrf-token`（官网前端大小写写法为 `X-XSRF-TOKEN`） | 公开代码 + 浏览器实测 + CLI probe | 当前请求同时出现对应 header/cookie，适配器用同一 secrets 值发送到同一 host。 |
| Cookie | `_session` | 公开代码/页面线索；本次列表请求未见 | 当前未填写；是否需要由会话失效样本确认。 |

当前官网前端把 token 放在浏览器 localStorage 的 `token`，同时维护 `pku_token` cookie；PKUHoleTUI 则从 cookie 读取 token 并构造 Authorization header。这是两种客户端保存方式，不代表服务端要求两份都存在。

本项目不实现账号密码、IAAA、短信或 OTP 自动登录。用户如要进行实测，应在本地 `.env` 中填写适配器要求的会话字段；不要把值粘贴到对话、日志或提交记录。真实字段在 T4 代码与 `.env.example` 中固定；本次只确认浏览器请求中出现的字段名，不记录值。

### 1.5 分页、排序和数据边界

| 必须核实项 | 当前结论 | 状态 |
| --- | --- | --- |
| 页码起点 | 公开客户端从 1 开始 | 公开代码确认 |
| page size | 官网本次真实请求为 10；CLI probe 以 10 成功 | 账号实测 + CLI probe；跨页仍待完整验证 |
| 下一页 / 末页信号 | 没有 opaque cursor；响应有 `data.total`，但 page 2/3 样本受新增帖影响而重叠 | 账号实测显示页漂移；末页/快照一致性未确认 |
| 排序 | 已观察页内按 PID 降序 | 账号实测样本；不等于平台稳定保证 |
| 置顶 | `is_top` 字段存在 | 公开代码 + 账号实测字段；没有拿到置顶跨页案例 |
| PID 单调性 | 样本页内降序，但跨页发生重叠 | 账号实测部分确认；不能直接作为无漏检水位证明 |
| 删除/屏蔽 | `hidden`、`status` 等字段存在；具体值语义未确认 | 未确认 |
| 原帖 URL | 当前前端源码登记移动端 `/pages/postDetail`，候选为 `https://treehole.pku.edu.cn/ch/web/pages/postDetail?pid={id}`；用户已确认该链接能打开原帖 | 公开代码 + 用户实测确认 |

因此当前配置可以在已确认的账号上试运行，但不能发布“平台保证完整增量”的声明。fixture
采集器覆盖分页不完整、置顶、重复和重启去重；真实适配器已经完成一页字段/时间戳 probe
和链接人工确认，跨页页漂移、末页信号和长期稳定性仍需运行中持续观察。

### 1.6 异常识别

- HTTP 401/403：认证失效或访问拒绝，暂停自动采集；不重试登录、不换账号或代理。
- HTTP 429：记录 Retry-After 并进入至少 1 小时冷却；失败请求计入本轮请求预算。
- HTTP 200 但 `Content-Type` 为 HTML、正文以 HTML 登录页开头、JSON 解析失败：按登录页/响应格式异常处理，不能建立基线或推进水位。
- JSON 顶层 `code` 不是 20000、缺少 `data.list`、`list` 不是数组或帖子缺少有效 `pid`：按业务/契约错误处理，保留旧水位。
- 5xx、连接失败和超时：最多一次重试，仍受请求间隔和整轮运行看门狗约束；普通临时失败不会设置来源冷却。

## 2. PushPlus 微信渠道

### 2.1 公开契约

核验日期：2026-09-05。依据 [PushPlus 消息接口文档](https://www.pushplus.plus/doc/guide/api.html) 和 [开放接口文档](https://www.pushplus.plus/doc/guide/openApi.html)。

- 发送地址为 `https://www.pushplus.plus/send`（官方正文仍展示 http 形式，但 FAQ 说明支持 HTTPS；实现固定使用 HTTPS）。
- 使用 `POST` JSON body，必填 `token`、`content`；实现使用 `template=txt`，`channel` 可配置为 `wechat` 或 `app`，不填 `topic`、`to` 或其他接收人字段，目标是 token 对应的本人。
- `channel=wechat` 并不等于个人微信好友聊天。PushPlus 普通微信渠道使用微信服务号模板消息，默认会显示为服务号通知卡片，点击后查看详情；用户本次提供的截图“设备通知 / 查看详情”与该形态一致。若要让内容直接出现在服务号会话中，需要用户先向“pushplus 推送加”服务号发送“激活消息”，由 PushPlus 在有限时间/条数内改用客服消息；项目无法通过 API 强制永久保持这种形态。
- 同步返回顶层 `code=200` 只表示服务端收到并接受了请求处理，不表示微信已发送或用户已看到；`data` 是 `shortCode` 流水号。
- 官方开放接口可用 `GET https://www.pushplus.plus/api/open/message/sendMessageResult?shortCode=<shortCode>` 查询，响应 `data.status`：0 未投递、1 发送中、2 已发送、3 发送失败，失败原因在 `errorMessage`。查询需要另外配置 AccessKey；AccessKey 需要 secretKey 和安全 IP，且有效期目前约 7200 秒，因此首版默认不自动开启查询，未配置查询凭据时只记录 `accepted`。
- 可选回调会在消息完成时返回 `messageInfo.sendStatus`，0/1/2/3 对应未发送/发送中/成功/失败；本地单进程首版不提供 Web 回调接收服务，因此不依赖回调确认。

### 2.2 额度和交互限制（平台说明，不是账号保证）

截至官方额度页当前内容，微信渠道日请求额度为未实名 0、实名 200、会员 2,000；接口频率实名用户为 1 分钟 5 次；错误请求也可能计入次数。实名用户单条内容上限为 20,000 字符，会员上限为 100,000 字符，详见 [PushPlus 官方限制说明](https://pushplus.plus/doc/help/limit.html)。PushPlus 另说明，用户向公众号发送“激活消息”后 48 小时内可使用客服消息，连续 5 条后或超过 48 小时会降级为模板消息，一次交互场景最多 5 条客服消息。

这意味着项目默认每 30 分钟最多一条合并通知只是调度选择，不能保证账号额度或微信展示内容。当前用户配置由 LaunchAgent 按北京时间分时触发（01:00–07:30 每 30 分钟，08:00 至次日 00:50 每 10 分钟），应用层 `interval_seconds` 设为 0；不设置本地请求次数或帖子数量上限，但仍遵守请求间隔、整轮运行看门狗和远端 429 冷却。实现必须把“accepted”“delivered”和“微信实际收到”分开。

### 2.3 实测状态

- PushPlus token：已写入本机 `.env`，文件权限保持为 600；本文不记录 token 值。
- 发送请求：实名前的一次合成 `notify-test` 返回 `failed`；实名认证后按用户授权再次执行，返回 `accepted` 并生成服务端受理流水号。
- 微信实际接收：用户确认在手机微信收到实名后的合成测试和真实帖子批次，并提供两张截图：一张是“设备通知”卡片，另一张是点击后的消息详情页。该证据证明实际收件和详情渲染，不证明它是个人好友聊天消息。
- 用户随后在服务号发送了“激活消息”；后续请求仍返回 `accepted`。当前截图仍是设备通知/详情形态，没有足够证据证明已切换成服务号会话内的客服消息；PushPlus 的客服消息还受时间和条数限制。

## 3. 最小实测步骤（不在对话中提交凭据）

1. 在自己的浏览器中合法登录北大树洞，导出或复制当前会话所需的 token / cookie / uuid 到项目根目录 `.env`，字段名以项目 `.env.example` 为准；文件权限设为 600。本次浏览器观察已确认列表请求字段名，但 Agent 不保存或转发这些值。
2. 使用 `pku-hole-radar --config /绝对路径/config.toml doctor` 检查“存在性”和权限；输出不包含值。
3. 使用 `probe-source` 只请求最新一页，不推进水位；保留数量、字段集合、PID 顺序、时间戳单位和 HTTP/业务结果，不保存正文或原始响应。
4. 只有 probe 成功并人工确认结果后，才允许执行 `preview --live`；首轮正式运行仍只建立基线。
5. 微信联调必须使用已完成实名认证且绑定接收微信的 PushPlus 账号。当前这台本机的合成 `notify-test` 已返回 `accepted`，且用户已实际打开微信确认收件，因此记录为“微信实际收件验证通过”；这不代表其他账号或未来批次自动通过。测试正文必须是合成文本。

## 4. 未确认项

- 合法树洞账号下 `/hole/list` 与 `/hole/list_comments` 的完整差异；本次浏览器首页和 CLI probe 均使用 `/hole/list_comments`，但另一接口未比较。
- `Authorization`、`pku_token`、`uuid`、XSRF 的最小组合及会话失效时具体 HTTP/业务响应；当前四字段组合已验证可用，但尚未做最小化实验。
- `timestamp` 的长期单位/时区保证、PID 是否跨页严格按新帖降序、页漂移下的末页信号和删除/屏蔽字段语义；当前样本证明 Unix 秒形态，用户已确认候选原帖链接可打开。
- PushPlus 账号的长期剩余额度、消息服务号绑定状态，以及后续每一批消息的实际收件；当前已有用户确认和截图，但 `accepted` 仍不是平台投递回执。
