"""
AstrBot Plugin - 森空岛签到 (Skland Sign-In)

Commands:
- skd (group): Show sign-in status for all bound users in the group
- skd (private): Show user's own sign-in status
- skdlogin (private): Login with token and immediately sign in
- skdlogout (private): Logout and remove token

Config (AstrBot plugin config):
- auto_sign_enabled: 自动签到开关
- auto_sign_hour: 自动签到时间（小时，0-23）
"""

from datetime import datetime
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from astrbot.core.star.filter.permission import PermissionType
import astrbot.api.message_components as Comp
from astrbot.api import logger, AstrBotConfig
from astrbot.api.event import AstrMessageEvent, filter, MessageChain
from astrbot.api.star import Context, Star, register
from astrbot.core.star.config import put_config

from .skland_api import SklandAPI

PLUGIN_NAME = "astrbot_plugin_skland"


@register(PLUGIN_NAME, "AstrBot", "森空岛自动签到插件", "1.1.0")
class SklandPlugin(Star):
    """森空岛签到插件"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.api = SklandAPI(max_retries=3)
        self.scheduler = AsyncIOScheduler()
        self._init_config()

    def _init_config(self):
        """注册后台配置项"""
        put_config(
            namespace=PLUGIN_NAME,
            name="自动签到开关",
            key="auto_sign_enabled",
            value=True,
            description="开启后，将在指定时间自动为所有已注册用户签到，并私发结果"
        )
        put_config(
            namespace=PLUGIN_NAME,
            name="自动签到时间（小时）",
            key="auto_sign_hour",
            value=1,
            description="自动签到执行的小时（0-23），默认凌晨1点"
        )

    def _get_config(self) -> dict:
        """获取当前配置"""
        return {
            "auto_sign_enabled": self.config.get("auto_sign_enabled", True),
            "auto_sign_hour": self.config.get("auto_sign_hour", 1),
        }

    async def initialize(self):
        """插件初始化"""
        logger.info("森空岛签到插件已加载")
        config = self._get_config()
        if config.get("auto_sign_enabled", False):
            hour = config.get("auto_sign_hour", 1)
            self._start_auto_sign_job(hour)
        if not self.scheduler.running:
            self.scheduler.start()

    async def terminate(self):
        """插件卸载"""
        if self.scheduler.running:
            self.scheduler.shutdown()
        await self.api.close()
        logger.info("森空岛签到插件已卸载")

    # ==================== Auto Sign-In ====================

    def _start_auto_sign_job(self, hour: int = 1):
        """启动自动签到定时任务"""
        hour = max(0, min(23, hour))
        trigger = CronTrigger(hour=hour, minute=0)
        try:
            self.scheduler.remove_job("skland_auto_sign")
        except Exception:
            pass

        self.scheduler.add_job(
            self._auto_sign_all_users,
            trigger=trigger,
            id="skland_auto_sign",
            misfire_grace_time=3600,
        )
        logger.info(f"森空岛自动签到任务已启动，每天 {hour:02d}:0 执行")

    async def _auto_sign_all_users(self):
        """为所有已注册用户执行自动签到"""
        config = self._get_config()
        if not config.get("auto_sign_enabled", False):
            logger.info("自动签到已关闭，跳过执行")
            return

        logger.info("开始执行自动签到...")
        users = await self.get_kv_data("users", {})
        if not users:
            logger.info("没有已注册的用户，跳过自动签到")
            return

        for user_id, user_data in users.items():
            if "token" not in user_data:
                continue

            try:
                token = user_data["token"]
                results, nickname = await self.api.do_full_sign_in(token)

                # 更新签到状态
                for r in results:
                    if r.game == "明日方舟" and self._is_signed_today(r):
                        user_data.setdefault("last_sign", {})["arknights"] = datetime.now().strftime("%Y-%m-%d")
                    elif r.game == "终末地" and self._is_signed_today(r):
                        user_data.setdefault("last_sign", {})["endfield"] = datetime.now().strftime("%Y-%m-%d")

                # 构建消息
                message = f"🎮 森空岛自动签到结果\n\n{self._format_sign_status(results, nickname)}"
                await self._send_private_message(user_id, user_data, message)
                users[user_id] = user_data
                logger.info(f"用户 {user_id} ({nickname}) 自动签到完成")
            except Exception as e:
                logger.error(f"用户 {user_id} 自动签到失败: {e}")
                message = f"⚠️ 自动签到失败\n错误: {str(e)}\n请使用 /skdlogin 重新登录"
                await self._send_private_message(user_id, user_data, message)

        await self.put_kv_data("users", users)
        logger.info("自动签到执行完毕")

    async def _send_private_message(self, user_id: str, user_data: dict, message: str):
        """使用统一会话ID发送私聊消息"""
        try:
            umo = user_data.get("umo")
            if not umo:
                logger.warning(f"用户 {user_id} 没有统一会话ID，无法发送私聊消息")
                return

            message_chain = MessageChain().message(message)
            await self.context.send_message(umo, message_chain)
            logger.info(f"已发送私聊消息给用户 {user_id}")
        except Exception as e:
            logger.error(f"发送私聊消息失败: {e}")

    # ==================== Helpers ====================

    def _is_signed_today(self, result) -> bool:
        if result.success:
            return True
        error = result.error.lower() if result.error else ""
        return any(k in error for k in ["已签到", "请勿重复", "重复签到", "already", "签到过", "今日已"])

    def _format_sign_status(self, results: list, nickname: str = "") -> str:
        if not results:
            return "没有绑定游戏"
        lines = []
        if nickname:
            lines.append(f"【{nickname}】")
        for r in results:
            if r.success or self._is_signed_today(r):
                award = ", ".join(r.awards) if getattr(r, "awards", None) else "无奖励"
                lines.append(f"{r.game} 已签到 ({award})")
            else:
                lines.append(f"{r.game} 签到失败: {r.error}")
        return "\n".join(lines)

    # ==================== Commands ====================

    @filter.command("skdhelp")
    async def skdhelp(self, event: AstrMessageEvent):
        """森空岛签到插件帮助"""
        yield event.plain_result("森空岛签到插件帮助\n"
                                 "1. 私聊机器人发送/skdlogin <token> 登录并签到\n"
                                 "2. 私聊机器人发送/skdlogout 登出\n"
                                 "3. /skd 查看签到状态"
                          )
        
    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    @filter.command("skdlogin")
    async def skdlogin(self, event: AstrMessageEvent, token: str = ""):
        user_id = event.get_sender_id()
        token = token.strip()
        if not token:
            yield event.plain_result(
                "请先获取token，方法如下:\n"
                "1. 登录 鹰角网络通行证 后，打开 (https://web-api.hypergryph.com/account/info/hg) 记下 content 字段的值（推荐）。"
                "   或者登录 森空岛网页版 (https://www.skland.com/) 后，打开 (https://web-api.skland.com/account/info/hg) 记下 content 字段的值。\n"
                "2. 使用方法:\n"
                "   /skdlogin <content>")
            return
        yield event.plain_result("正在登录并签到，请稍候...")
        try:
            results, nickname = await self.api.do_full_sign_in(token)
            user_data = {
                "token": token,
                "nickname": nickname,
                "last_sign": {},
                "bound_at": datetime.now().isoformat(),
                "platform_name": event.get_platform_name(),
                "umo": event.unified_msg_origin,  # 保存统一会话ID
            }
            for r in results:
                if r.game == "明日方舟" and self._is_signed_today(r):
                    user_data["last_sign"]["arknights"] = datetime.now().strftime("%Y-%m-%d")
                elif r.game == "终末地" and self._is_signed_today(r):
                    user_data["last_sign"]["endfield"] = datetime.now().strftime("%Y-%m-%d")
            await self.put_kv_data("users", {**(await self.get_kv_data("users", {})), user_id: user_data})
            yield event.plain_result(f"登录成功！\n{self._format_sign_status(results, nickname)}")
        except Exception as e:
            logger.error(f"skdlogin失败: {e}")
            yield event.plain_result(f"登录失败: {str(e)}")

    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    @filter.command("skdlogout")
    async def skdlogout(self, event: AstrMessageEvent):
        user_id = event.get_sender_id()
        users = await self.get_kv_data("users", {})
        if user_id in users:
            del users[user_id]
            await self.put_kv_data("users", users)
            yield event.plain_result("已退出登录并清除绑定信息")
        else:
            yield event.plain_result("您尚未绑定森空岛账号")

    @filter.command("skd")
    async def skd(self, event: AstrMessageEvent):
        """群聊显示群成员签到状态，私聊显示自己"""
        user_id = event.get_sender_id()
        group_id = getattr(event.message_obj, "group_id", None)
        is_group = bool(group_id)
        users_data = await self.get_kv_data("users", {})

        if is_group:
            # 群聊模式
            message_lines = ["📊 森空岛签到统计", "═══════════════", "方舟 | 终末 | 昵称", "-----------------"]
            group_users = (await self.get_kv_data("groups", {})).get(group_id, [])
            for uid in group_users:
                user_data = users_data.get(uid)
                if not user_data:
                    continue
                try:
                    results, nickname = await self.api.do_full_sign_in(user_data["token"])
                    user_data["nickname"] = nickname
                    for r in results:
                        if r.game == "明日方舟" and self._is_signed_today(r):
                            user_data.setdefault("last_sign", {})["arknights"] = datetime.now().strftime("%Y-%m-%d")
                        elif r.game == "终末地" and self._is_signed_today(r):
                            user_data.setdefault("last_sign", {})["endfield"] = datetime.now().strftime("%Y-%m-%d")
                    users_data[uid] = user_data
                    ak_icon = "✅" if user_data.get("last_sign", {}).get("arknights") else "❌"
                    ef_icon = "✅" if user_data.get("last_sign", {}).get("endfield") else "❌"
                    message_lines.append(f" {ak_icon} | {ef_icon} | {nickname}")
                except:
                    message_lines.append(" ⚠️ | ⚠️ | (Error)")
            await self.put_kv_data("users", users_data)
            yield event.plain_result("\n".join(message_lines))
        else:
            # 私聊模式
            user_data = users_data.get(user_id)
            if not user_data:
                yield event.plain_result("你还未绑定账号，请使用 /skdlogin <token>")
                return
            try:
                results, nickname = await self.api.do_full_sign_in(user_data["token"])
                response = self._format_sign_status(results, nickname)
                yield event.plain_result(response)
            except Exception as e:
                yield event.plain_result(f"查询失败: {str(e)}")
