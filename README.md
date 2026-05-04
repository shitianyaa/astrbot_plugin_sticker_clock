# astrbot_plugin_sticker_clock

> 整点贴纸提醒 —— 每小时整点向订阅会话发送一张贴纸图片。

移植自 Telegram 项目 [imxieyi/sticker_time_bot](https://github.com/imxieyi/sticker_time_bot) 的 AstrBot Python 版。

## ✨ 功能

- 🕒 每小时整点（可改 `minute_offset`）自动发送对应小时的贴纸
- 🌍 每个会话独立时区
- 🌙 睡眠时段静音（如 22:00 ~ 7:00 不打扰）
- 📋 自定义小时白名单（只在指定整点发）
- 🗑️ 自动删除上一条贴纸（仅 QQ aiocqhttp）
- 🔌 多平台：aiocqhttp / Telegram / Discord / Lark / DingTalk

## 📦 安装

1. **下载插件**：
   ```bash
   cd AstrBot/data/plugins
   git clone https://github.com/shitianyaa/astrbot_plugin_sticker_clock
   ```
   或直接下载 zip 解压到 `data/plugins/astrbot_plugin_sticker_clock/`。

2. **贴纸图片**：仓库已自带 12 张贴纸，clone 下来即可使用。
   - 图片位于 `images/0.png` ~ `images/11.png`
   - 当前小时通过 `hour % 12` 选取（0/12 点用同一张，依此类推）
   - 想换成自己的贴纸：直接覆盖这 12 个文件即可（保持文件名）
   - 也支持 `.jpg` `.jpeg` `.gif` `.webp`，按 `image_ext_priority` 顺序匹配
   - 若开启 `use_24h_mode`，则需要 `0.png` ~ `23.png` 共 24 张

3. **安装依赖**（Windows 用户必装）：
   ```bash
   # 在 AstrBot 所在的 Python 环境里执行
   pip install tzdata
   ```
   不然非默认时区（如 `Asia/Tokyo`）会失败。Linux/macOS 通常系统自带可省略。

4. **重载插件**：在 AstrBot WebUI 的「插件管理」找到本插件 → 点 ⋯ → 重载。

## 🚀 使用

在群聊或私聊里发送：

```
/clock start              订阅当前会话
/clock stop               取消订阅
/clock status             查看当前会话状态
/clock timezone <tz>      设置时区，别名 /clock tz
/clock autodelete on      发新贴纸时删旧的（仅 QQ）
/clock sleeptime 22       设置睡眠开始（22 点）
/clock waketime 7         设置起床时间（7 点）
/clock nosleep            清除睡眠时段
/clock addhour 9          只在 9 点发送（白名单模式）
/clock delhour 9          移除白名单小时
/clock listhours          查看白名单
/clock clearhours         清空白名单
/clock test [hour]        立即测试发送
/clock targets            查看所有订阅会话（仅管理员）
/clock help               完整帮助
```

> 指令名仅支持英文（与原 sticker_time_bot 保持一致），不提供中文别名。

### 👀 谁能看什么

| 指令 | 谁可用 | 看到的内容 |
|------|--------|-----------|
| `/clock status` | 所有人 | 当前会话自己的订阅/时区/睡眠/白名单 |
| `/clock listhours` | 所有人 | 当前会话的白名单小时 |
| `/clock targets` | **管理员** | 所有订阅会话（用户订阅 + WebUI 预设，去重） |


## ⚙️ 配置

在 AstrBot WebUI 的插件配置面板里可以调整：

| 配置项 | 说明 | 默认 |
|--------|------|------|
| `enabled` | 全局总开关 | `true` |
| `default_timezone` | 默认时区 | `Asia/Shanghai` |
| `minute_offset` | 触发分钟（0-59） | `0` |
| `platform_id` | 默认平台适配器 ID（一般留空自动检测） | `""` |
| `push_targets` | **预设订阅列表**（管理员集中管理，无需 /clock start） | `[]` |
| `default_sleeptime` | **新订阅者默认睡眠开始时间**（-1 = 禁用） | `-1` |
| `default_waketime` | **新订阅者默认起床时间**（-1 = 禁用） | `-1` |
| `image_dir` | 自定义贴纸目录绝对路径（留空 = 插件 images/） | `""` |
| `image_ext_priority` | 贴纸文件扩展名优先级 | `[png, jpg, jpeg, gif, webp]` |
| `use_24h_mode` | 24 小时各不同图（需 0-23.png 共 24 张） | `false` |
| `send_target_interval` | 多目标间发送间隔（秒） | `1.5` |
| `auto_unsubscribe_on_block` | 被踢/拉黑时自动取消订阅 | `true` |

### 🎯 push_targets 格式示例

```yaml
push_targets:
  - "group:123456789"                          # 用 platform_id 平台 + 群 123456789
  - "private:10001"                            # 私聊
  - "aiocqhttp:group:987654321"                # 指定 aiocqhttp 平台
  - "aiocqhttp:GroupMessage:111222333"         # 完整 unified_msg_origin
```

### 🌙 默认睡眠时段的优先级

每个会话的"是否发送"判断顺序：

1. 会话有 **白名单小时**（`/clock addhour`）→ 只在白名单内发
2. 会话有自己的 **sleeptime+waketime**（`/clock sleeptime/waketime`）→ 用自己的
3. 会话执行过 `/clock nosleep` → 全天发，**不**继承默认
4. 会话什么都没设 + WebUI 配了 `default_sleeptime+default_waketime` → 用全局默认
5. 都没有 → 全天发

## 📁 目录结构

```
astrbot_plugin_sticker_clock/
├─ metadata.yaml
├─ _conf_schema.json
├─ requirements.txt
├─ README.md
├─ LICENSE
├─ main.py
└─ images/             # 已自带 12 张贴纸，可直接使用
   ├─ 0.png            # 0 点 / 12 点用
   ├─ 1.png            # 1 点 / 13 点用
   ├─ ...
   └─ 11.png           # 11 点 / 23 点用
```

## 🎨 自带贴纸版权说明

本仓库自带的 12 张贴纸来自 Telegram 表情包：

- **作品名**：現在幾點了?!!!!! !
- **作者**：@Actfs2013
- **预览 / 原始来源**：[https://fstik.app/stickerSet/what_what_time_is_it](https://fstik.app/stickerSet/what_what_time_is_it)

贴纸版权归原作者 [@Actfs2013](https://t.me/Actfs2013) 所有，本仓库仅作为整点报时插件的默认素材便利使用。

> **如果你是原作者或版权方，认为此处使用侵犯了你的权益，请通过 [Issue](https://github.com/shitianyaa/astrbot_plugin_sticker_clock/issues) 联系，我会立即下架相关图片。**

如不希望使用此套贴纸，将 `images/0.png` ~ `images/11.png` 替换为你自己的图片即可。

## 🔍 工作原理

- 调度器使用 30 秒轮询 + 每个会话独立判断当前小时
- 在每个会话本地时区的 `minute == minute_offset` 时触发
- 内存里维护 `(date, hour)` 去重，避免单小时内重复发送
- aiocqhttp 平台：直接调用 `send_group_msg` / `send_private_msg`，能拿到 `message_id` 用于自动删除
- 其他平台：走 AstrBot 标准消息链，自动删除功能不可用

## ⚠️ 注意事项

- **自动删除** 在 QQ 上要求消息发出后 **2 分钟内** 删，超时会失败（QQ 协议限制）
- **白名单模式** 与 **睡眠时段** 互斥：设置一个会清空另一个（与原 Telegram bot 行为一致）
- 调度器精度受 30 秒轮询影响，发送时间可能比 `HH:00:00` 晚 0~30 秒，属正常
- Windows 用户若没装 `tzdata`，非 `Asia/Shanghai` 的时区会失败

## 📝 License

MIT
