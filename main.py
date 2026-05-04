"""AstrBot 整点贴纸提醒插件

移植自 imxieyi/sticker_time_bot（Telegram Node.js 版）。
核心：每小时整点向已订阅会话发送 images/{hour % 12}.png 贴纸。
"""

import asyncio
import datetime
from pathlib import Path
from typing import Any

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from astrbot.api.event import MessageChain, filter, AstrMessageEvent
from astrbot.api.message_components import Image
from astrbot.api.star import Context, Star, register
from astrbot.api import logger, AstrBotConfig


# ========== 常量 ==========
KV_SUBSCRIBERS = "subscribers"      # list[str] —— 订阅会话的 unified_msg_origin
KV_CHATS = "chats"                  # dict[umo, dict] —— 每个会话的配置
SCHEDULER_POLL_SECONDS = 30         # 调度器轮询间隔
DEFAULT_TIMEZONE = "Asia/Shanghai"
LOG_PREFIX = "[StickerClock]"


def _safe_zoneinfo(name: str) -> datetime.tzinfo:
    """获取 IANA 时区，失败时回退到 UTC+8（避免 Windows 上没装 tzdata 时插件挂掉）"""
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        if name == DEFAULT_TIMEZONE or name == "Asia/Shanghai":
            return datetime.timezone(datetime.timedelta(hours=8), name=name)
        raise


# 启动时缓存默认时区，避免 Windows 上每次都报警
try:
    _DEFAULT_TZ = ZoneInfo(DEFAULT_TIMEZONE)
except ZoneInfoNotFoundError:
    logger.warning(
        f"{LOG_PREFIX} 未找到 IANA 时区数据库，回退到固定 UTC+8。"
        "建议执行: pip install tzdata"
    )
    _DEFAULT_TZ = datetime.timezone(datetime.timedelta(hours=8), name=DEFAULT_TIMEZONE)


@register(
    "astrbot_plugin_sticker_clock",
    "shitianyaa",
    "整点贴纸提醒",
    "1.0.0",
)
class StickerClockPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._scheduler_task: asyncio.Task | None = None
        # 每个会话最近一次成功发送的 (date, hour)，用于在同一小时内去重
        self._last_sent: dict[str, tuple[str, int]] = {}
        # 内存缓存：会话 -> 最近一次发送的 message_id（aiocqhttp）
        # 持久化在 KV 中（chats[umo].last_msg_id），这里只是为了热路径快一点
        self._ensure_scheduler_started(reason="__init__")

    async def initialize(self):
        """插件加载/重载时触发"""
        self._ensure_scheduler_started(reason="initialize")
        logger.info(f"{LOG_PREFIX} 已加载，当前订阅数: "
                    f"{len(await self._get_subscribers())}")

    @filter.on_astrbot_loaded()
    async def on_loaded(self):
        """AstrBot 主程序加载完毕的兜底"""
        self._ensure_scheduler_started(reason="on_loaded")

    async def terminate(self):
        """卸载/停用时取消调度器"""
        if self._scheduler_task and not self._scheduler_task.done():
            self._scheduler_task.cancel()
            try:
                await self._scheduler_task
            except asyncio.CancelledError:
                pass
        logger.info(f"{LOG_PREFIX} 已停止")

    # =====================================================================
    # 调度器
    # =====================================================================

    def _ensure_scheduler_started(self, reason: str = "") -> None:
        if self._scheduler_task is not None and not self._scheduler_task.done():
            return
        try:
            self._scheduler_task = asyncio.create_task(self._scheduler_loop())
            logger.info(f"{LOG_PREFIX} 调度器已启动（{reason}）")
        except RuntimeError:
            # 可能此时还没有运行的 event loop，等下一次回调兜底
            logger.debug(f"{LOG_PREFIX} {reason} 阶段无运行中事件循环，等待下一层兜底")

    async def _scheduler_loop(self):
        """每 30 秒轮询一次，对每个订阅会话独立判断是否到达触发时间"""
        # 启动时延迟一下，避免和 initialize 抢资源
        await asyncio.sleep(2)
        while True:
            try:
                if self.config.get("enabled", True):
                    await self._tick()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"{LOG_PREFIX} 调度器异常: {e}", exc_info=True)
                await asyncio.sleep(60)
                continue
            await asyncio.sleep(SCHEDULER_POLL_SECONDS)

    async def _tick(self):
        """单次调度心跳：遍历订阅者，到达触发时间的发贴纸"""
        targets = await self._get_all_targets()
        if not targets:
            return

        chats = await self._get_chats()
        target_minute = self._get_minute_offset()

        # 一次心跳里向不同目标发送的间隔
        target_interval = self._get_float("send_target_interval", 1.5, 0.0, 60.0)

        sent_count = 0
        for umo in targets:
            cd = chats.get(umo, {})
            tz_name = cd.get("tz") or self.config.get("default_timezone", DEFAULT_TIMEZONE)
            tz = _safe_zoneinfo(tz_name) if tz_name else _DEFAULT_TZ

            now = datetime.datetime.now(tz)
            hour, minute = now.hour, now.minute

            # 容忍 1 分钟的窗口期，避免轮询恰好错过 minute=target_minute
            if not (target_minute <= minute <= target_minute + 1):
                continue

            date_key = now.strftime("%Y-%m-%d")
            sent_key = (date_key, hour)
            if self._last_sent.get(umo) == sent_key:
                continue  # 这小时已经发过

            # 是否在该小时被静音（睡眠时段 / 白名单）
            if not self._should_send_at_hour(cd, hour):
                self._last_sent[umo] = sent_key  # 记账，避免反复检查
                continue

            try:
                ok = await self._send_sticker(umo, hour, cd)
                if ok:
                    self._last_sent[umo] = sent_key
                    sent_count += 1
                    chats[umo] = cd  # 更新 last_msg_id
                # 失败的会话已经在 _send_sticker 里处理（必要时取消订阅）
            except Exception as e:
                logger.warning(f"{LOG_PREFIX} 发送 {umo} 失败: {e}")

            if target_interval > 0:
                await asyncio.sleep(target_interval)

        # 持久化 chats（last_msg_id 等被更新过）
        if sent_count > 0:
            await self._put_chats(chats)
            logger.info(f"{LOG_PREFIX} 本次心跳发送 {sent_count} 条")

    def _should_send_at_hour(self, cd: dict, hour: int) -> bool:
        """根据会话配置判断当前小时是否应该发送

        优先级（与原 Telegram bot 一致 + 全局默认兜底）：
        1. timelist（白名单）非空：只有 hour 在列表里才发
        2. 会话自己设置了 sleeptime+waketime：按睡眠窗口计算
        3. 会话设了 no_default_sleep：忽略全局默认，全天发
        4. 否则使用全局 default_sleeptime+default_waketime（若都有效）
        5. 都没有：全天发
        """
        timelist = cd.get("timelist") or []
        if timelist:
            return hour in timelist

        sleep = cd.get("sleeptime")
        wake = cd.get("waketime")
        if sleep is not None and wake is not None:
            return self._in_awake_window(hour, sleep, wake)

        # 会话主动 opt-out 全局默认
        if cd.get("no_default_sleep"):
            return True

        # 全局默认兜底
        ds = self._parse_default_hour("default_sleeptime")
        dw = self._parse_default_hour("default_waketime")
        if ds is not None and dw is not None:
            return self._in_awake_window(hour, ds, dw)

        return True

    @staticmethod
    def _in_awake_window(hour: int, sleep: int, wake: int) -> bool:
        """判断 hour 是否在"清醒窗口"内（即应该发送）

        与原 JS 行为完全一致：strict inequality，sleep/wake 端点本身仍发送
          sleep < wake : skip when sleep < hour < wake
          sleep > wake : skip when hour > sleep or hour < wake
          sleep == wake: 不过滤
        """
        if sleep < wake:
            return not (sleep < hour < wake)
        if sleep > wake:
            return not (hour > sleep or hour < wake)
        return True

    def _parse_default_hour(self, key: str) -> int | None:
        try:
            v = int(self.config.get(key, -1))
        except (TypeError, ValueError):
            return None
        return v if 0 <= v <= 23 else None

    # =====================================================================
    # 发送
    # =====================================================================

    async def _send_sticker(self, umo: str, hour: int, cd: dict) -> bool:
        """发送贴纸到指定会话。返回是否成功。会顺便处理自动删除上一条"""
        image_path = self._resolve_image_path(hour)
        if image_path is None:
            logger.warning(
                f"{LOG_PREFIX} 找不到 hour={hour} 对应的贴纸图片。"
                f"请把图片放到 {self._get_image_dir()}"
            )
            return False

        autodelete = bool(cd.get("autodelete"))
        last_msg_id = cd.get("last_msg_id")

        # 优先尝试 aiocqhttp 直发以拿到 message_id
        handled, new_msg_id, send_err = await self._aiocqhttp_send_image(umo, image_path)

        if handled:
            # 平台是 aiocqhttp。无论成败，都不再走通用回退（避免双发）
            if send_err is not None:
                await self._handle_send_error(umo, send_err)
                return False
        else:
            # 不是 aiocqhttp 会话，走通用消息链
            try:
                chain = MessageChain([Image.fromFileSystem(str(image_path))])
                await self.context.send_message(umo, chain)
            except Exception as e:
                await self._handle_send_error(umo, e)
                return False

        # 自动删除上一条（仅 aiocqhttp 支持）
        if autodelete and last_msg_id and handled and new_msg_id is not None:
            await self._aiocqhttp_delete(last_msg_id)

        # 更新 last_msg_id
        if handled and new_msg_id is not None:
            cd["last_msg_id"] = new_msg_id
        else:
            cd.pop("last_msg_id", None)

        return True

    async def _aiocqhttp_send_image(
        self, umo: str, image_path: Path
    ) -> tuple[bool, int | None, Exception | None]:
        """通过 aiocqhttp 直发图片。

        返回 (handled, message_id, send_error)：
        - handled=False: 不是 aiocqhttp 会话，调用方应走通用回退
        - handled=True, send_error=None: 发送成功，message_id 可能为 None（协议端没返回）
        - handled=True, send_error 非空: 发送失败，调用方不要再走回退（避免双发）
        """
        parts = umo.split(":", 2)
        if len(parts) != 3:
            return False, None, None
        plat_id, kind, ident = parts
        if kind not in ("GroupMessage", "FriendMessage"):
            return False, None, None

        try:
            platform = self.context.get_platform(filter.PlatformAdapterType.AIOCQHTTP)
        except Exception:
            return False, None, None
        if platform is None:
            return False, None, None

        # 检查 UMO 的 platform_id 是不是这个 aiocqhttp 实例（多实例场景）
        try:
            meta = getattr(platform, "metadata", None)
            inst_id = getattr(meta, "id", None) if meta else None
        except Exception:
            inst_id = None
        if inst_id and inst_id != plat_id:
            return False, None, None

        # OneBot 协议要求 file:// 绝对路径
        abs_path = str(image_path.resolve())
        # Windows: D:\foo\bar.png -> file:///D:/foo/bar.png
        file_uri = "file:///" + abs_path.replace("\\", "/").lstrip("/")
        message = [{"type": "image", "data": {"file": file_uri}}]

        try:
            client = platform.get_client()
            if kind == "GroupMessage":
                resp = await client.api.call_action(
                    "send_group_msg", group_id=int(ident), message=message
                )
            else:
                resp = await client.api.call_action(
                    "send_private_msg", user_id=int(ident), message=message
                )
        except Exception as e:
            logger.debug(f"{LOG_PREFIX} aiocqhttp 直发失败 ({umo}): {e}")
            return True, None, e

        msg_id = resp.get("message_id") if isinstance(resp, dict) else None
        return True, msg_id, None

    async def _aiocqhttp_delete(self, message_id: Any) -> bool:
        try:
            platform = self.context.get_platform(filter.PlatformAdapterType.AIOCQHTTP)
            if platform is None:
                return False
            client = platform.get_client()
            await client.api.call_action("delete_msg", message_id=message_id)
            return True
        except Exception as e:
            logger.debug(f"{LOG_PREFIX} 删除消息 {message_id} 失败: {e}")
            return False

    async def _handle_send_error(self, umo: str, err: Exception):
        """发送失败时的处理：被拉黑/踢出 -> 自动取消订阅"""
        msg = str(err).lower()
        block_signals = (
            "blocked", "kicked", "not a member", "chat not found",
            "deactivated", "have no rights", "not enough rights",
            "peer_id_invalid", "deleted", "no permission",
            "user_not_in_group", "group_not_found",
        )
        if not any(s in msg for s in block_signals):
            logger.warning(f"{LOG_PREFIX} 发送 {umo} 失败: {err}")
            return

        if not self.config.get("auto_unsubscribe_on_block", True):
            logger.warning(f"{LOG_PREFIX} {umo} 似乎已不可达，但 auto_unsubscribe_on_block=false")
            return

        logger.info(f"{LOG_PREFIX} {umo} 似乎已不可达，自动取消订阅")
        subs = await self._get_subscribers()
        if umo in subs:
            subs.remove(umo)
            await self._put_subscribers(subs)
        chats = await self._get_chats()
        if umo in chats:
            chats.pop(umo)
            await self._put_chats(chats)
        self._last_sent.pop(umo, None)

    # =====================================================================
    # 配置 / 路径解析
    # =====================================================================

    def _get_minute_offset(self) -> int:
        try:
            v = int(self.config.get("minute_offset", 0))
        except (TypeError, ValueError):
            return 0
        return v if 0 <= v <= 59 else 0

    def _get_float(self, key: str, default: float, lo: float, hi: float) -> float:
        try:
            v = float(self.config.get(key, default))
        except (TypeError, ValueError):
            return default
        return v if lo <= v <= hi else default

    def _get_image_dir(self) -> Path:
        custom = (self.config.get("image_dir", "") or "").strip()
        if custom:
            return Path(custom)
        # 默认: 当前文件所在目录的 images/ 子目录
        return Path(__file__).parent / "images"

    def _get_default_platform(self) -> str:
        """获取推送使用的默认平台 ID。优先使用配置，其次自动检测"""
        configured = (self.config.get("platform_id", "") or "").strip()
        if configured:
            return configured
        try:
            all_plats = self.context.get_all_platforms()
            if not all_plats:
                return "aiocqhttp"
            if isinstance(all_plats, dict):
                return next(iter(all_plats.keys()))
            first = all_plats[0]
            for attr in ("platform_name", "name"):
                val = getattr(first, attr, None)
                if isinstance(val, str) and val:
                    return val
        except Exception as e:
            logger.debug(f"{LOG_PREFIX} 平台自动检测失败: {e}")
        return "aiocqhttp"

    @staticmethod
    def _parse_target_to_umo(t: str, default_plat: str) -> str | None:
        """把单条 push_targets 解析成 unified_msg_origin

        支持格式：
          group:ID / private:ID                       使用 default_plat
          平台ID:group:ID / 平台ID:private:ID         指定平台
          平台ID:GroupMessage:ID / 平台ID:FriendMessage:ID   完整 UMO 原样使用
          纯数字                                       兼容只填 ID 的情况，按群处理
        """
        if ":GroupMessage:" in t or ":FriendMessage:" in t:
            return t
        parts = t.split(":")
        if len(parts) == 2:
            kind, ident = parts[0].strip().lower(), parts[1].strip()
            if not ident:
                return None
            if kind == "group":
                return f"{default_plat}:GroupMessage:{ident}"
            if kind == "private":
                return f"{default_plat}:FriendMessage:{ident}"
            return None
        if len(parts) == 3:
            plat, kind, ident = parts[0].strip(), parts[1].strip().lower(), parts[2].strip()
            if not plat or not ident:
                return None
            if kind == "group":
                return f"{plat}:GroupMessage:{ident}"
            if kind == "private":
                return f"{plat}:FriendMessage:{ident}"
            return None
        if len(parts) == 1 and t.isdigit():
            return f"{default_plat}:GroupMessage:{t}"
        return None

    @staticmethod
    def _format_umo_human(umo: str) -> str:
        """把 UMO 格式化为人类可读形式"""
        parts = umo.split(":", 2)
        if len(parts) != 3:
            return umo
        plat, kind, ident = parts
        kind_zh = {"GroupMessage": "群", "FriendMessage": "私聊"}.get(kind, kind)
        return f"{kind_zh} {ident} ({plat})"

    async def _get_all_targets(self) -> list[str]:
        """合并 KV 订阅者 + WebUI 配置的 push_targets，去重保序"""
        seen: dict[str, None] = {}

        subscribers = await self._get_subscribers()
        for umo in subscribers:
            if isinstance(umo, str) and umo and umo not in seen:
                seen[umo] = None

        config_targets = self.config.get("push_targets", []) or []
        if config_targets:
            default_plat = self._get_default_platform()
            for raw in config_targets:
                if not isinstance(raw, str):
                    continue
                t = raw.strip().replace("：", ":")
                if not t:
                    continue
                umo = self._parse_target_to_umo(t, default_plat)
                if umo is None:
                    logger.warning(
                        f"{LOG_PREFIX} 跳过格式错误的 push_targets 条目: {raw!r}"
                    )
                    continue
                if umo not in seen:
                    seen[umo] = None

        return list(seen.keys())

    def _resolve_image_path(self, hour: int) -> Path | None:
        """根据小时找到对应的贴纸文件"""
        use_24h = bool(self.config.get("use_24h_mode", False))
        idx = hour if use_24h else (hour % 12)

        ext_priority = self.config.get("image_ext_priority", []) or [
            "png", "jpg", "jpeg", "gif", "webp"
        ]
        if not isinstance(ext_priority, list):
            ext_priority = ["png"]

        image_dir = self._get_image_dir()
        for ext in ext_priority:
            ext = str(ext).lstrip(".").lower()
            p = image_dir / f"{idx}.{ext}"
            if p.exists() and p.is_file():
                return p
        return None

    # =====================================================================
    # KV 存取小工具
    # =====================================================================

    async def _get_subscribers(self) -> list[str]:
        v = await self.get_kv_data(KV_SUBSCRIBERS, [])
        return v if isinstance(v, list) else []

    async def _put_subscribers(self, subs: list[str]):
        await self.put_kv_data(KV_SUBSCRIBERS, subs)

    async def _get_chats(self) -> dict[str, dict]:
        v = await self.get_kv_data(KV_CHATS, {})
        return v if isinstance(v, dict) else {}

    async def _put_chats(self, chats: dict[str, dict]):
        await self.put_kv_data(KV_CHATS, chats)

    async def _get_chat_data(self, umo: str) -> dict:
        chats = await self._get_chats()
        return chats.get(umo, {})

    async def _update_chat_data(self, umo: str, **kwargs):
        chats = await self._get_chats()
        cd = chats.get(umo, {})
        cd.update(kwargs)
        # None 值代表删除该字段
        for k in list(cd.keys()):
            if cd[k] is None:
                cd.pop(k)
        chats[umo] = cd
        await self._put_chats(chats)

    # =====================================================================
    # 指令组 /clock
    # =====================================================================

    @filter.command_group("clock", alias={"贴纸时钟", "整点报时"})
    def clock(self):
        """整点贴纸提醒指令组"""
        pass

    @clock.command("start", alias={"订阅", "开始"})
    async def cmd_start(self, event: AstrMessageEvent):
        """订阅当前会话的整点贴纸推送"""
        umo = event.unified_msg_origin
        subs = await self._get_subscribers()
        if umo in subs:
            yield event.plain_result(f"已订阅过了，会话 ID: {umo}")
            return
        subs.append(umo)
        await self._put_subscribers(subs)
        # 初始化空配置
        await self._update_chat_data(umo)
        logger.info(f"{LOG_PREFIX} {umo} 订阅")
        tz = self.config.get("default_timezone", DEFAULT_TIMEZONE)
        yield event.plain_result(
            f"✅ 订阅成功！将在每小时整点（{tz} 时区）发送贴纸。\n"
            f"会话 ID: {umo}\n"
            f"使用 /clock help 查看更多指令"
        )

    @clock.command("stop", alias={"退订", "停止"})
    async def cmd_stop(self, event: AstrMessageEvent):
        """取消当前会话的整点贴纸推送"""
        umo = event.unified_msg_origin
        subs = await self._get_subscribers()
        if umo not in subs:
            yield event.plain_result(f"未订阅，会话 ID: {umo}")
            return
        subs.remove(umo)
        await self._put_subscribers(subs)
        chats = await self._get_chats()
        chats.pop(umo, None)
        await self._put_chats(chats)
        self._last_sent.pop(umo, None)
        logger.info(f"{LOG_PREFIX} {umo} 退订")
        yield event.plain_result(f"已取消订阅，会话 ID: {umo}")

    @clock.command("status", alias={"状态"})
    async def cmd_status(self, event: AstrMessageEvent):
        """查看当前会话的订阅状态和配置"""
        umo = event.unified_msg_origin
        subs = await self._get_subscribers()
        cd = await self._get_chat_data(umo)
        all_targets = await self._get_all_targets()
        is_subscribed_kv = umo in subs
        is_in_targets = umo in all_targets

        if is_subscribed_kv:
            sub_status = "✅ 已订阅"
        elif is_in_targets:
            sub_status = "✅ 已订阅（管理员预设）"
        else:
            sub_status = "❌ 未订阅"

        tz_name = cd.get("tz") or self.config.get("default_timezone", DEFAULT_TIMEZONE)
        tz = _safe_zoneinfo(tz_name)
        now = datetime.datetime.now(tz)

        lines = [
            "⏰ 整点贴纸状态",
            f"订阅: {sub_status}",
            f"全局开关: {'✅ 启用' if self.config.get('enabled', True) else '❌ 禁用'}",
            f"会话 ID: {umo}",
            f"时区: {tz_name}（当前时间: {now.strftime('%Y-%m-%d %H:%M:%S')}）",
            f"自动删除上一条: {'✅' if cd.get('autodelete') else '❌'}",
        ]

        # 过滤规则：显示生效的设置（chat 自己的 / 默认兜底 / 无）
        timelist = cd.get("timelist") or []
        sleep, wake = cd.get("sleeptime"), cd.get("waketime")
        if timelist:
            tl = ", ".join(f"{h}:00" for h in sorted(timelist))
            lines.append(f"过滤规则: 仅白名单小时 [{tl}]")
        elif sleep is not None and wake is not None:
            lines.append(f"过滤规则: 睡眠时段 {sleep}:00 ~ {wake}:00（不发送）")
        elif cd.get("no_default_sleep"):
            lines.append("过滤规则: 全天发送（已禁用全局默认）")
        else:
            ds = self._parse_default_hour("default_sleeptime")
            dw = self._parse_default_hour("default_waketime")
            if ds is not None and dw is not None:
                lines.append(f"过滤规则: 全局默认睡眠 {ds}:00 ~ {dw}:00（可用 /clock nosleep 禁用）")
            else:
                lines.append("过滤规则: 全天发送")
        yield event.plain_result("\n".join(lines))

    @clock.command("tz", alias={"timezone", "时区"})
    async def cmd_tz(self, event: AstrMessageEvent, zone: str = ""):
        """设置或查看时区。用法: /clock tz Asia/Shanghai"""
        umo = event.unified_msg_origin
        if not zone:
            cd = await self._get_chat_data(umo)
            cur = cd.get("tz") or self.config.get("default_timezone", DEFAULT_TIMEZONE)
            yield event.plain_result(f"当前时区: {cur}")
            return
        try:
            ZoneInfo(zone)
        except ZoneInfoNotFoundError:
            yield event.plain_result(
                f"❌ 无效时区: {zone}\n"
                "请使用 IANA 时区名，如 Asia/Shanghai、Asia/Tokyo、Europe/London。\n"
                "完整列表: https://en.wikipedia.org/wiki/List_of_tz_database_time_zones"
            )
            return
        await self._update_chat_data(umo, tz=zone)
        yield event.plain_result(f"✅ 时区已设为 {zone}")

    @clock.command("autodelete", alias={"自动删除"})
    async def cmd_autodelete(self, event: AstrMessageEvent, value: str = ""):
        """开启或关闭自动删除上一条贴纸（仅 QQ 平台支持）。用法: /clock autodelete on|off"""
        umo = event.unified_msg_origin
        if not value:
            cd = await self._get_chat_data(umo)
            yield event.plain_result(
                f"自动删除: {'on' if cd.get('autodelete') else 'off'}"
            )
            return
        v = value.lower()
        if v in ("on", "true", "1", "开"):
            await self._update_chat_data(umo, autodelete=True)
            yield event.plain_result("✅ 已开启自动删除（仅 QQ aiocqhttp 平台支持，且只能删 2 分钟内的消息）")
        elif v in ("off", "false", "0", "关"):
            await self._update_chat_data(umo, autodelete=False)
            yield event.plain_result("已关闭自动删除")
        else:
            yield event.plain_result("用法: /clock autodelete on|off")

    @clock.command("sleeptime", alias={"睡眠"})
    async def cmd_sleeptime(self, event: AstrMessageEvent, hour: str = ""):
        """设置睡眠开始时间。用法: /clock sleeptime 22  （22 点开始静音）"""
        umo = event.unified_msg_origin
        if not hour:
            cd = await self._get_chat_data(umo)
            v = cd.get("sleeptime")
            yield event.plain_result(f"睡眠开始: {v}:00" if v is not None else "未设置睡眠时间")
            return
        h = self._parse_hour(hour)
        if h is None:
            yield event.plain_result(f"❌ {hour} 不是有效小时（应为 0-23）")
            return
        # 设置 sleep/wake 后清空白名单 + 清除 opt-out 标志
        cd = await self._get_chat_data(umo)
        cleared = bool(cd.get("timelist"))
        await self._update_chat_data(
            umo,
            sleeptime=h,
            timelist=None if cleared else cd.get("timelist"),
            no_default_sleep=None,
        )
        msg = f"✅ 睡眠开始时间设为 {h}:00"
        if cleared:
            msg += "，已清空白名单小时"
        yield event.plain_result(msg)

    @clock.command("waketime", alias={"起床"})
    async def cmd_waketime(self, event: AstrMessageEvent, hour: str = ""):
        """设置起床时间。用法: /clock waketime 7  （7 点恢复发送）"""
        umo = event.unified_msg_origin
        if not hour:
            cd = await self._get_chat_data(umo)
            v = cd.get("waketime")
            yield event.plain_result(f"起床: {v}:00" if v is not None else "未设置起床时间")
            return
        h = self._parse_hour(hour)
        if h is None:
            yield event.plain_result(f"❌ {hour} 不是有效小时（应为 0-23）")
            return
        cd = await self._get_chat_data(umo)
        cleared = bool(cd.get("timelist"))
        await self._update_chat_data(
            umo,
            waketime=h,
            timelist=None if cleared else cd.get("timelist"),
            no_default_sleep=None,
        )
        msg = f"✅ 起床时间设为 {h}:00"
        if cleared:
            msg += "，已清空白名单小时"
        yield event.plain_result(msg)

    @clock.command("nosleep", alias={"取消睡眠"})
    async def cmd_nosleep(self, event: AstrMessageEvent):
        """删除睡眠/起床时间设置，并禁用全局默认睡眠时段"""
        umo = event.unified_msg_origin
        cd = await self._get_chat_data(umo)
        already_cleared = (
            cd.get("sleeptime") is None
            and cd.get("waketime") is None
            and cd.get("no_default_sleep")
        )
        if already_cleared:
            yield event.plain_result("已经禁用睡眠时段了，全天发送中")
            return
        await self._update_chat_data(
            umo,
            sleeptime=None,
            waketime=None,
            no_default_sleep=True,
        )
        yield event.plain_result(
            "✅ 已禁用睡眠时段，全天发送（不再继承 WebUI 全局默认）"
        )

    @clock.command("addhour", alias={"添加小时"})
    async def cmd_addhour(self, event: AstrMessageEvent, hour: str = ""):
        """把小时加入白名单。用法: /clock addhour 9  （加入后只在白名单内发送）"""
        umo = event.unified_msg_origin
        if not hour:
            yield event.plain_result("用法: /clock addhour <0-23>")
            return
        h = self._parse_hour(hour)
        if h is None:
            yield event.plain_result(f"❌ {hour} 不是有效小时（应为 0-23）")
            return
        cd = await self._get_chat_data(umo)
        timelist = list(cd.get("timelist") or [])
        if h in timelist:
            yield event.plain_result(f"{h}:00 已经在白名单里了")
            return
        timelist.append(h)
        timelist.sort()
        had_sleep = cd.get("sleeptime") is not None or cd.get("waketime") is not None
        await self._update_chat_data(
            umo,
            timelist=timelist,
            sleeptime=None if had_sleep else cd.get("sleeptime"),
            waketime=None if had_sleep else cd.get("waketime"),
            # 白名单优先级最高，opt-out 标志失效，留给将来 /clock nosleep 再设
            no_default_sleep=None,
        )
        msg = f"✅ 已添加 {h}:00"
        if had_sleep:
            msg += "，已清除睡眠/起床时间"
        yield event.plain_result(msg)

    @clock.command("delhour", alias={"删除小时"})
    async def cmd_delhour(self, event: AstrMessageEvent, hour: str = ""):
        """从白名单移除小时。用法: /clock delhour 9"""
        umo = event.unified_msg_origin
        if not hour:
            yield event.plain_result("用法: /clock delhour <0-23>")
            return
        h = self._parse_hour(hour)
        if h is None:
            yield event.plain_result(f"❌ {hour} 不是有效小时（应为 0-23）")
            return
        cd = await self._get_chat_data(umo)
        timelist = list(cd.get("timelist") or [])
        if h not in timelist:
            yield event.plain_result(f"{h}:00 不在白名单里")
            return
        timelist.remove(h)
        await self._update_chat_data(
            umo, timelist=timelist if timelist else None
        )
        msg = f"✅ 已移除 {h}:00"
        if not timelist:
            msg += "，白名单已清空（恢复全天/睡眠时段规则）"
        yield event.plain_result(msg)

    @clock.command("listhours", alias={"小时列表"})
    async def cmd_listhours(self, event: AstrMessageEvent):
        """查看白名单小时"""
        cd = await self._get_chat_data(event.unified_msg_origin)
        timelist = sorted(cd.get("timelist") or [])
        if not timelist:
            yield event.plain_result("白名单为空（按睡眠时段规则发送，或全天发送）")
            return
        lines = [f"白名单小时（共 {len(timelist)} 个）:"]
        lines.extend(f"  {h:02d}:00" for h in timelist)
        yield event.plain_result("\n".join(lines))

    @clock.command("clearhours", alias={"清空小时"})
    async def cmd_clearhours(self, event: AstrMessageEvent):
        """清空白名单小时"""
        umo = event.unified_msg_origin
        cd = await self._get_chat_data(umo)
        if not (cd.get("timelist") or []):
            yield event.plain_result("白名单本来就是空的")
            return
        await self._update_chat_data(umo, timelist=None)
        yield event.plain_result("✅ 白名单已清空")

    @clock.command("test", alias={"测试"})
    async def cmd_test(self, event: AstrMessageEvent, hour_str: str = ""):
        """立即发送当前小时（或指定小时）的贴纸用于测试。用法: /clock test [0-23]"""
        umo = event.unified_msg_origin
        cd = await self._get_chat_data(umo)
        tz = _safe_zoneinfo(
            cd.get("tz") or self.config.get("default_timezone", DEFAULT_TIMEZONE)
        )
        if hour_str:
            h = self._parse_hour(hour_str)
            if h is None:
                yield event.plain_result(f"❌ {hour_str} 不是有效小时（应为 0-23）")
                return
        else:
            h = datetime.datetime.now(tz).hour

        path = self._resolve_image_path(h)
        if path is None:
            yield event.plain_result(
                f"❌ 找不到 hour={h} 的贴纸。\n"
                f"请把图片放到: {self._get_image_dir()}\n"
                f"命名: {h % 12 if not self.config.get('use_24h_mode') else h}.png"
            )
            return
        yield event.image_result(str(path))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @clock.command("targets", alias={"目标"})
    async def cmd_targets(self, event: AstrMessageEvent):
        """[管理员] 查看所有推送目标（用户订阅 + WebUI 预设）"""
        subs = await self._get_subscribers()
        config_targets = self.config.get("push_targets", []) or []
        all_targets = await self._get_all_targets()
        default_plat = self._get_default_platform()

        parts = ["📋 推送目标列表"]

        if subs:
            parts.append(f"\n[用户订阅 - {len(subs)} 个]")
            for umo in subs:
                if isinstance(umo, str):
                    parts.append(f"  {self._format_umo_human(umo)}")

        if config_targets:
            parts.append(f"\n[WebUI 预设 - {len(config_targets)} 个]")
            for raw in config_targets:
                if not isinstance(raw, str) or not raw.strip():
                    continue
                t = raw.strip().replace("：", ":")
                umo = self._parse_target_to_umo(t, default_plat)
                if umo is None:
                    parts.append(f"  ⚠️ 格式错误: {raw}")
                else:
                    parts.append(f"  {self._format_umo_human(umo)}")

        if not subs and not config_targets:
            parts.append("\n暂无推送目标")

        parts.append(f"\n共 {len(all_targets)} 个目标（已去重）")
        yield event.plain_result("\n".join(parts))

    @clock.command("help", alias={"帮助"})
    async def cmd_help(self, event: AstrMessageEvent):
        """查看所有指令"""
        yield event.plain_result(
            "🕒 整点贴纸时钟 指令一览\n\n"
            "[订阅]\n"
            "/clock start          订阅当前会话\n"
            "/clock stop           取消订阅\n"
            "/clock status         查看状态\n"
            "\n[配置]\n"
            "/clock tz <时区>      设置时区，如 Asia/Shanghai\n"
            "/clock autodelete on  发新贴纸时删旧的（仅 QQ）\n"
            "/clock sleeptime 22   设置睡眠开始（22 点）\n"
            "/clock waketime 7     设置起床时间（7 点）\n"
            "/clock nosleep        清除睡眠时段（也禁用全局默认）\n"
            "\n[白名单小时]\n"
            "/clock addhour 9      只在 9 点发送\n"
            "/clock delhour 9      移除\n"
            "/clock listhours      查看\n"
            "/clock clearhours     清空\n"
            "\n[其他]\n"
            "/clock test [hour]    立即测试发送贴纸\n"
            "/clock targets        查看所有推送目标（管理员）\n"
            "\n💡 全局设置（默认时区、默认睡眠时段、预设订阅列表等）"
            "在 AstrBot WebUI 的插件配置面板里调整。"
        )

    # =====================================================================
    # 工具
    # =====================================================================

    @staticmethod
    def _parse_hour(s: str) -> int | None:
        try:
            n = int(str(s).strip())
        except (TypeError, ValueError):
            return None
        return n if 0 <= n <= 23 else None
