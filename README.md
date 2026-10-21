# 消息元数据与构造

从引用消息或合并转发中导出原始元数据，编辑 JSON 后重新发送；也可以直接编写 JSON 构造消息、带摘要的图片、图片链接卡片和聊天记录。

截至 **2026-10-21**，已核对 AstrBot 最新稳定版 **v4.29.1**（2026-10-17 发布）的消息组件、事件、插件加载和 aiocqhttp 发送接口。插件使用当前的 `Star` 自动发现机制，不使用已废弃的 `@register`。

## 适用范围

| 环境 | 能力 |
| --- | --- |
| AstrBot v4.29.1，Python 3.12+ | 插件接口开发基线 |
| QQ OneBot v11（AstrBot 中的 aiocqhttp，推荐 NapCat） | 原始元数据、按 ID 查询、合并转发展开、消息构造 |
| AstrBot 其他适配器 | 导出当前消息原始对象或引用组件快照；不支持本插件的 OneBot 消息构造 |

元数据、URL、QQ 号和资源 ID 都来自协议端实际返回，不补造协议端未提供的字段。`raw` 保留原始数据，`build` 则是便于重建的消息结构。不能取回或不支持展开的资源会保留引用并写入 `warnings`。

## 安装

1. 把本目录放入 AstrBot 的 `data/plugins/astrbot_plugin_analyse_message/`，确保 `main.py`、`metadata.yaml` 位于该目录第一层。
2. 在 AstrBot 管理面板重载插件，或重启 AstrBot。
3. 使用 QQ OneBot v11 适配器连接协议端，发送 `/消息帮助`。
4. 构造功能默认只允许 **AstrBot 管理员**使用；在 AstrBot 全局配置中设置管理员 QQ 号。需要所有用户使用时，再关闭插件配置 `build_admin_only`。

运行依赖只有 Python 标准库和 AstrBot 已提供的 aiohttp。无需额外图片服务或 API Key。该目录尚未绑定远程仓库，所以 `metadata.yaml` 没有虚构 `repo` 地址；发布到自己的仓库后可补上以启用在线更新。

以下指令以 `/` 为唤醒前缀，实际以前台 AstrBot 配置为准。别名可使用相同前缀。

## 指令

| 指令 | 别名 | 说明 |
| --- | --- | --- |
| `/消息帮助` | `/msghelp` | 帮助 |
| `/元数据` | `/msgmeta`、`/消息元数据` | 引用消息时解析被引用内容，否则解析当前消息 |
| `/元数据 -123456` | 同上 | 通过消息 ID 查询 |
| `/元数据 forward:资源ID` | 同上 | 直接查询合并转发资源 |
| `/构造消息 JSON` | `/msgbuild` | 构造普通消息或聊天记录；类型由 JSON 决定 |
| `/构造记录 JSON` | `/forwardbuild` | 构造聊天记录；普通消息自动包装为一个节点 |
| `/消息模板 message` | `/msgtemplate message` | 获取模板；另有 `image`、`image_card`、`forward`、`nested` |

JSON 可以：

- 直接接在构造指令后，保留字符串中的连续空格与转义换行。
- 放在指令后的 JSON Markdown 代码块中。
- 作为同一消息中的 `.json` 附件发送。
- 先发送 JSON 文本或 `.json` 文件，再**回复该消息**发送 `/构造消息` 或 `/构造记录`。

文件需要 UTF-8 编码。JSON 错误会提示行号、列号；重复字段、NaN、Infinity、过大的文件、过深的嵌套会被拒绝。不会执行 Python、CQ 码字符串或用户指定的 OneBot action。

## 最常见的流程

1. 回复一张图片、一条消息或一张合并转发卡片，发送 `/元数据`。
2. 短元数据直接回复为 JSON；长元数据作为 `message-metadata.json` 文件发送。
3. 编辑输出中的 `build`。可以只提交 `build` 对象，也可以提交完整的导出对象。
4. 将 JSON 接在 `/构造消息` 后发送，或附加 JSON 文件，或回复含 JSON 的消息发送指令。

OneBot 长 JSON 使用 Base64 文件段传给协议端，无需 AstrBot 与 NapCat 共享文件目录。其他适配器使用 AstrBot 文件组件输出，实际投递能力取决于该适配器。

导出结构：

```json
{
  "schema": "astrbot.message-metadata/v1",
  "platform": "aiocqhttp",
  "source": {"type": "quoted", "id": "123456"},
  "raw": {},
  "trigger_components": [],
  "forwards": {},
  "build": {
    "kind": "message",
    "message": [{"type": "text", "data": {"text": "可编辑内容"}}]
  },
  "warnings": []
}
```

`raw` 是目标消息的原始结果；`trigger_components` 是触发本次指令的 AstrBot 组件快照，其中可能包含引用快照；`forwards` 以资源 ID 索引原始合并转发结果。重建时只读取 `build`，忽略导出对象中的原发送目标。

## 构造示例

### 文本与图片

```text
/构造消息 {"message":[{"type":"text","data":{"text":"这段文本和图片位于同一消息"}},{"type":"image","data":{"file":"https://example.com/image.jpg","summary":"[图片] 自定义摘要","sub_type":0}}]}
```

直接提交 OneBot 消息段数组、单个消息段、含 `message` 的 `get_msg` 返回对象都可以。保留 `data` 中未知的协议扩展字段。接收图片常带缓存文件名和下载 URL，重建时优先使用 URL；图片 URL 过期后需自行更换。

示例中的 `example.com` 地址都是占位符，使用前替换为真实可访问资源。`file` 支持协议端认可的 HTTP(S) URL、`base64://...`、协议端本地文件路径或缓存标识。本地路径指 **NapCat/协议端** 的文件系统，不是操作指令者的电脑；容器部署应使用 URL 或 Base64。

### 带文字摘要的图片

```text
/构造消息 {"kind":"image","file":"https://example.com/image.jpg","summary":"[这是一段图片文本]","sub_type":0}
```

`summary` 改变协议消息携带的图片摘要，常用于消息列表、通知或引用中的文字。它**不会把文字绘制到图片像素上**，QQ 客户端不一定展示该摘要。需要图文同条消息时用前一个示例的 `text + image`；需要图片像素内的文字时，先提供已合成的图片。

### 预览图与点击目标不同的卡片

```text
/构造消息 {"kind":"image_card","preview":"https://example.com/preview.jpg","url":"https://example.com/original.jpg","title":"点击查看大图","description":"预览与点击后的图片可以不同"}
```

这会构造 QQ `com.tencent.structmsg` 链接卡片：聊天窗口的预览使用 `preview`，点击跳转到 `url`，由 QQ 内置浏览器或系统浏览器打开。两张图片可以完全不同。

**这不是原生图片查看器中“缩略图 A，点击图片后原图 B”的保证实现。**已核对的 NapCat 发送图片实现只使用上传图片、`summary` 和 `sub_type`，没有把独立 `thumb` 作为图片发送参数使用。插件不会宣称设置 `image.thumb` 就能得到该效果。其他协议端特殊字段仍可通过原始消息段透传，但是否生效由协议端与 QQ 决定。原生双图通常涉及协议层上传/图片编码方案，不是标准 OneBot v11 接口能力。

QQ 对卡片可能校验、重写或限制预览及跳转域名；未完成真实账号联调前，不能保证任一 QQ 客户端都显示指定卡片。

### 自定义聊天记录

```text
/构造记录 {"kind":"forward","source":"聊天记录","summary":"查看 2 条消息","prompt":"[聊天记录] 演示","news":[{"text":"张三：你好"}],"messages":[{"user_id":"10001","nickname":"张三","content":"你好"},{"user_id":"10002","nickname":"李四","content":[{"type":"image","data":{"file":"https://example.com/image.jpg","summary":"[图片]"}}]}]}
```

每个节点可以设置：

- `user_id` / `uin`：节点展示的 QQ 号，通常决定头像；不是实际发消息的登录账号。
- `nickname` / `name`：节点展示昵称。
- `content` / `message`：纯文本或 OneBot 消息段数组。
- `time`：可选时间戳，透传给协议端；协议端可能忽略或重写。

省略发送者时，使用调用者 QQ 号和昵称。`source`、`summary`、`prompt`、`news` 为可选的合并转发卡片扩展，具体展示依赖协议端；NapCat 的高级合并转发可能需要其自身的包体/packet 功能可用。

也可以使用标准 OneBot 节点，引用既有消息：

```text
/构造记录 {"messages":[{"type":"node","data":{"id":"123456"}},{"type":"node","data":{"user_id":"10001","nickname":"张三","content":[{"type":"text","data":{"text":"自定义节点"}}]}}]}
```

嵌套记录：让某个节点的 `content` 包含一组 `type: node` 的消息段，参见 [examples/nested.json](examples/nested.json)。同一层的 `node` 与普通消息段不能混合；把普通内容另放入一个节点即可。

所有构造消息都发往**当前会话**。构造节点改变的是转发记录中的展示信息，真实消息仍由机器人账号发送。

## 配置

| 配置 | 默认值 | 说明 |
| --- | --- | --- |
| `build_admin_only` | `true` | 构造指令仅 AstrBot 管理员使用 |
| `max_json_bytes` | `1048576` | 输入/输出 JSON 字节上限，可设 1024–16777216 |
| `inline_chars` | `3000` | 超过后发送 JSON 文件，可设 256–10000 |
| `max_nodes` | `100` | 单次解析/构造节点总数，含所有嵌套，可设 1–1000 |
| `max_depth` | `5` | 合并转发最大嵌套深度，可设 1–10 |
| `max_forward_fetches` | `20` | 单次获取的转发资源数，可设 1–100 |
| `api_timeout` | `20` | 单次协议 API / 文件下载超时秒数，可设 1–120 |

元数据保留协议端原始信息，其中可能包含下载签名链接、用户标识等；导出前应确认当前会话是适合接收这些内容的位置。插件不主动拉取历史消息、不调用 LLM，也不把元数据提交到额外第三方服务。

## 开发与验证

```sh
uv sync --python 3.12
uv run ruff check .
uv run ruff format --check .
uv run pytest -q
```

自动化覆盖 JSON 原文空格、编码/大小限制、图片摘要、链接卡片、群聊/私聊路由、多机器人 self_id、原始字段保留、元数据重建、合并转发递归/循环/数量限制、构造权限、文件输入和长 JSON 文件输出。

当前 **70 项自动化测试通过**。另外，已直接加载 AstrBot v4.29.1 源码中的真实消息组件与指令过滤器进行兼容性检查，验证 Python 3.12 下的 pydantic.v1 组件序列化、引用快照、文件组件、消息链与 JSON 原文传递。

自动化测试的发送端使用模拟的 AstrBot 事件与 OneBot transport，**不等于真实 QQ 账号端到端联调**。部署后建议依次验收：

1. 回复一条文字发送 `/元数据`，将导出 JSON 用 `/构造消息` 发回。
2. 回复普通图片导出元数据，修改 `build.message[0].data.summary` 并重建。
3. 使用真实图片 URL 测试 `/消息模板 image_card` 中的卡片。
4. 测试普通、嵌套聊天记录，以及引用记录后重新构造。
5. 用普通成员测试权限拒绝，并测试较大的 JSON 文件往返。

## 接口核对来源

- [AstrBot v4.29.1 发布页](https://github.com/AstrBotDevs/AstrBot/releases/tag/v4.29.1)
- [AstrBot 消息组件](https://github.com/AstrBotDevs/AstrBot/blob/v4.29.1/astrbot/core/message/components.py)
- [AstrBot OneBot 事件与发送](https://github.com/AstrBotDevs/AstrBot/blob/v4.29.1/astrbot/core/platform/sources/aiocqhttp/aiocqhttp_message_event.py)
- [AstrBot 指令过滤实现](https://github.com/AstrBotDevs/AstrBot/blob/v4.29.1/astrbot/core/star/filter/command.py)
- [NapCat OneBot 消息段定义](https://github.com/NapNeko/NapCatQQ/blob/main/packages/napcat-onebot/types/message.ts)
- [NapCat 图片发送参数处理](https://github.com/NapNeko/NapCatQQ/blob/main/packages/napcat-onebot/api/msg.ts)
- [NapCat 合并转发读取](https://github.com/NapNeko/NapCatQQ/blob/main/packages/napcat-onebot/action/go-cqhttp/GetForwardMsg.ts)

NapCat `main` 是滚动分支，核对时间为 2026-10-21；不同安装版本和 QQ 客户端行为需要实测。
