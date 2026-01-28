"""
AstrBot Plugin - 森空岛签到 (Skland Sign-In)

Commands:
- skd (group): Show sign-in status for all bound users in the group
- skd (private): Show user's own sign-in status
- skdlogin (private): Login with token and immediately sign in
- skdlogout (private): Logout and remove token

Config (AstrBot 后台):
- auto_sign_enabled: 自动签到开关
- auto_sign_hour: 自动签到时间（小时，0-23）
"""

from datetime import datetime
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.star.config import load_config, put_config

from .skland_api import SklandAPI

PLUGIN_NAME = "astrbot_plugin_skland"


@register(PLUGIN_NAME, "AstrBot", "森空岛自动签到插件", "1.1.0")
class SklandPlugin(Star):
    """森空岛签到插件"""

    def __init__(self, context: Context):
        super().__init__(context)
        self.api = SklandAPI(max_retries=3)
        self.scheduler = AsyncIOScheduler()
        self._init_config()

    def _init_config(self):
        """初始化后台配置项"""
        # 注册配置项到 AstrBot 后台
        put_config(
            namespace=PLUGIN_NAME,
            name="自动签到开关",
            key="auto_sign_enabled",
            value=False,
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
        config = load_config(PLUGIN_NAME)
        if not config:
            return {"auto_sign_enabled": False, "auto_sign_hour": 1}
        return config

    async def initialize(self):
        """插件初始化"""
        logger.info("森空岛签到插件已加载")
        
        # 根据后台配置决定是否启动自动签到
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
        # 确保 hour 在有效范围
        hour = max(0, min(23, hour))
        
        # 每天指定小时执行
        trigger = CronTrigger(hour=hour, minute=0)
        
        # 移除已存在的任务（如果有）
        try:
            self.scheduler.remove_job("skland_auto_sign")
        except Exception:
            pass
        
        self.scheduler.add_job(
            self._auto_sign_all_users,
            trigger=trigger,
            id="skland_auto_sign",
            misfire_grace_time=3600,  # 1小时容错
        )
        logger.info(f"森空岛自动签到任务已启动，将在每天 {hour:02d}:00 执行")

    def _stop_auto_sign_job(self):
        """停止自动签到定时任务"""
        try:
            self.scheduler.remove_job("skland_auto_sign")
            logger.info("森空岛自动签到任务已停止")
        except Exception:
            pass

    async def _auto_sign_all_users(self):
        """为所有已注册用户执行自动签到"""
        # 再次检查配置，确保功能仍然开启
        config = self._get_config()
        if not config.get("auto_sign_enabled", False):
            logger.info("自动签到已在后台关闭，跳过执行")
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
                
                users[user_id] = user_data
                
                # 构建签到结果消息
                result_message = f"🎮 森空岛自动签到结果\n\n{self._format_sign_status(results, nickname)}"
                
                # 私发给用户
                await self._send_private_message(user_id, user_data, result_message)
                
                logger.info(f"用户 {user_id} ({nickname}) 自动签到完成")
                
            except Exception as e:
                logger.error(f"用户 {user_id} 自动签到失败: {e}")
                
                # 通知用户签到失败
                error_message = f"⚠️ 森空岛自动签到失败\n\n错误: {str(e)}\n\n如果 Token 已过期，请使用 /skdlogin 重新登录"
                await self._send_private_message(user_id, user_data, error_message)
        
        # 保存更新后的用户数据
        await self.put_kv_data("users", users)
        logger.info("自动签到执行完毕")

    async def _send_private_message(self, user_id: str, user_data: dict, message: str):
        """发送私聊消息给用户"""
        try:
            # 获取用户的平台信息
            platform_name = user_data.get("platform_name")
            
            if not platform_name:
                logger.warning(f"用户 {user_id} 没有保存平台信息，无法发送私聊消息")
                return
            
            # 通过 context 获取平台适配器并发送消息
            platform = self.context.platform_manager.get_platform_by_name(platform_name)
            if platform:
                await platform.send_message(user_id, message)
                logger.debug(f"已向用户 {user_id} 发送私聊消息")
            else:
                logger.warning(f"找不到平台适配器: {platform_name}")
                
        except Exception as e:
            logger.error(f"发送私聊消息给用户 {user_id} 失败: {e}")

    # ==================== Storage Helpers ====================

    async def _get_user_data(self, user_id: str) -> dict[str, Any] | None:
        """Get user data from storage"""
        users = await self.get_kv_data("users", {})
        return users.get(user_id)

    async def _save_user_data(self, user_id: str, data: dict[str, Any]):
        """Save user data to storage"""
        users = await self.get_kv_data("users", {})
        users[user_id] = data
        await self.put_kv_data("users", users)

    async def _get_group_users(self, group_id: str) -> list[str]:
        """Get list of user IDs bound in a group"""
        groups = await self.get_kv_data("groups", {})
        return groups.get(group_id, [])

    async def _add_user_to_group(self, group_id: str, user_id: str):
        """Add user to group binding list"""
        groups = await self.get_kv_data("groups", {})
        if group_id not in groups:
            groups[group_id] = []
        if user_id not in groups[group_id]:
            groups[group_id].append(user_id)
        await self.put_kv_data("groups", groups)

    async def _update_sign_status(self, user_id: str, game: str, signed: bool):
        """Update sign-in status for a user"""
        user_data = await self._get_user_data(user_id)
        if user_data:
            if "last_sign" not in user_data:
                user_data["last_sign"] = {}
            today = datetime.now().strftime("%Y-%m-%d")
            if signed:
                user_data["last_sign"][game] = today
            await self._save_user_data(user_id, user_data)

    def _is_signed_today(self, result) -> bool:
        """Check if the result indicates already signed today"""
        if result.success:
            return True
        error = result.error.lower() if result.error else ""
        # Match various "already signed" messages
        return any(keyword in error for keyword in [
            "已签到", "请勿重复", "重复签到", "already", "签到过", "今日已"
        ])

    def _format_sign_status(self, results: list, nickname: str = "") -> str:
        """Format sign-in results into a readable message"""
        if not results:
            return "没有找到任何游戏绑定"

        lines = []
        if nickname:
            lines.append(f"【{nickname}】")

        arknights_status = None
        endfield_status = None

        for r in results:
            if r.game == "明日方舟":
                if r.success:
                    awards = ", ".join(r.awards) if r.awards else "无奖励"
                    arknights_status = f"明日方舟已签到 ({awards})"
                elif self._is_signed_today(r):
                    arknights_status = "明日方舟已签到"
                else:
                    arknights_status = f"明日方舟签到失败: {r.error}"
            elif r.game == "终末地":
                if r.success:
                    awards = ", ".join(r.awards) if r.awards else "无奖励"
                    endfield_status = f"终末地已签到 ({awards})"
                elif self._is_signed_today(r):
                    endfield_status = "终末地已签到"
                else:
                    endfield_status = f"终末地签到失败: {r.error}"

        if endfield_status:
            lines.append(endfield_status)
        if arknights_status:
            lines.append(arknights_status)

        return "\n".join(lines)

    # ==================== Commands ====================

    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    @filter.command("skdlogin")
    async def skdlogin(self, event: AstrMessageEvent, token: str = ""):
        """
        私聊登录森空岛并签到

        使用方法: /skdlogin <token>
        token获取: 登陆 [森空岛](https://www.skland.com/) 后获取token: https://web-api.skland.com/account/info/hg
        将token中的{"content":"XXX"}中的XXX作为参数输入skdlogin，格式skdlogin XXX
        """
        user_id = event.get_sender_id()

        if not token or not token.strip():
            yield event.plain_result(
                "请提供token参数\n"
                "使用方法: /skdlogin <token>\n"
                "token获取: 登陆 [森空岛](https://www.skland.com/) 后获取token: https://web-api.skland.com/account/info/hg\n"
                "将token中的{\"content\":\"XXX\"}中的XXX作为参数输入skdlogin，格式skdlogin XXX"
            )
            return

        token = token.strip()

        yield event.plain_result("正在登录并签到，请稍候...")

        try:
            # Perform sign-in
            results, nickname = await self.api.do_full_sign_in(token)

            if not results:
                yield event.plain_result("登录成功，但没有找到任何游戏绑定")
                return

            # Save user data with platform info for private messaging
            user_data = {
                "token": token,
                "nickname": nickname,
                "last_sign": {},
                "bound_at": datetime.now().isoformat(),
                "platform_name": event.get_platform_name(),  # 保存平台信息
            }

            # Update sign status
            for r in results:
                if r.game == "明日方舟" and self._is_signed_today(r):
                    user_data["last_sign"]["arknights"] = datetime.now().strftime("%Y-%m-%d")
                elif r.game == "终末地" and self._is_signed_today(r):
                    user_data["last_sign"]["endfield"] = datetime.now().strftime("%Y-%m-%d")

            await self._save_user_data(user_id, user_data)

            # Format response
            response = f"登录成功！\n{self._format_sign_status(results, nickname)}"
            yield event.plain_result(response)

        except Exception as e:
            logger.error(f"skdlogin failed for user {user_id}: {e}")
            yield event.plain_result(f"登录失败: {str(e)}")

    def _get_status_icon(self, status: str) -> str:
        """Get icon for status"""
        if status == "已签到":
            return "✅"
        elif status == "未签到":
            return "❌"
        elif status == "未绑定":
            return "➖"
        elif status == "失败":
            return "⚠️"
        else:
            return "❓"

    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    @filter.command("skdlogout")
    async def skdlogout(self, event: AstrMessageEvent):
        """
        退出登录，删除绑定的Token
        """
        user_id = event.get_sender_id()
        
        users = await self.get_kv_data("users", {})
        if user_id in users:
            del users[user_id]
            await self.put_kv_data("users", users)
            yield event.plain_result("已退出登录并清除绑定信息。")
        else:
            yield event.plain_result("您尚未绑定森空岛账号。")

    @filter.command("skd")
    async def skd(self, event: AstrMessageEvent):
        """
        查看/执行签到

        私聊: 显示自己的签到状态
        群聊: 显示群内所有绑定用户的签到状态
        """
        user_id = event.get_sender_id()
        group_id = event.message_obj.group_id

        # Check if this is a group message or private message
        is_group = bool(group_id)

        if is_group:
            # ==================== Group Message ====================
            # Check if sender is bound
            sender_data = await self._get_user_data(user_id)
            sender_bound = sender_data is not None and "token" in sender_data

            # If sender is bound, add to group
            if sender_bound:
                await self._add_user_to_group(group_id, user_id)

            # Get all bound users in this group
            group_users = await self._get_group_users(group_id)

            if not group_users:
                # No bound users in this group
                chain = [
                    Comp.Plain("当前群组还没有绑定森空岛的用户\n"),
                    Comp.Plain("请私聊使用 /skdlogin <token> 进行登录\n"),
                    Comp.Plain("token获取: 登陆 [森空岛](https://www.skland.com/) 后获取token: https://web-api.skland.com/account/info/hg\n"),
                    Comp.Plain("将token中的{\"content\":\"XXX\"}中的XXX作为参数输入skdlogin，格式skdlogin XXX"),
                ]
                yield event.chain_result(chain)

                if not sender_bound:
                    yield event.plain_result(
                        "您还未绑定，请私聊使用 /skdlogin 进行登录\n"
                        "token获取: 登陆 [森空岛](https://www.skland.com/) 后获取token: https://web-api.skland.com/account/info/hg\n"
                        "将token中的{\"content\":\"XXX\"}中的XXX作为参数输入skdlogin，格式skdlogin XXX"
                    )
                return

            yield event.plain_result("正在查询群成员签到状态...")

            # Query each user's status
            # Build the message string first to ensure proper formatting
            message_lines = []
            message_lines.append("📊 森空岛签到统计")
            message_lines.append("════════════════")
            # Header
            message_lines.append("方舟 | 终末 | 昵称 ")
            message_lines.append("-------------------------------")

            users_data = await self.get_kv_data("users", {})

            for uid in group_users:
                user_data = users_data.get(uid)
                if not user_data or "token" not in user_data:
                    continue

                try:
                    token = user_data["token"]
                    results, nickname = await self.api.do_full_sign_in(token)

                    # Update stored data
                    user_data["nickname"] = nickname
                    for r in results:
                        if r.game == "明日方舟" and self._is_signed_today(r):
                            user_data.setdefault("last_sign", {})["arknights"] = datetime.now().strftime(
                                "%Y-%m-%d"
                            )
                        elif r.game == "终末地" and self._is_signed_today(r):
                            user_data.setdefault("last_sign", {})["endfield"] = datetime.now().strftime(
                                "%Y-%m-%d"
                            )
                    users_data[uid] = user_data

                    # Format status
                    arknights_status = "未绑定"
                    endfield_status = "未绑定"

                    for r in results:
                        if r.game == "明日方舟":
                            if self._is_signed_today(r):
                                arknights_status = "已签到"
                            else:
                                arknights_status = "未签到"
                        elif r.game == "终末地":
                            if self._is_signed_today(r):
                                endfield_status = "已签到"
                            else:
                                endfield_status = "未签到"
                    
                    ak_icon = self._get_status_icon(arknights_status)
                    ef_icon = self._get_status_icon(endfield_status)
                    
                    # Row: Icon Icon | Nickname
                    message_lines.append(f" {ak_icon}  |  {ef_icon}  | {nickname}")

                except Exception as e:
                    logger.error(f"Failed to check status for user {uid}: {e}")
                    message_lines.append(f" ⚠️  |  ⚠️  | (Error)")

            # Save updated user data
            await self.put_kv_data("users", users_data)

            if len(message_lines) > 4: # If there are users (header is 4 lines)
                yield event.plain_result("\n".join(message_lines))

            # If sender is not bound, send additional message
            if not sender_bound:
                yield event.plain_result(
                    "您还未绑定森空岛账号，请私聊使用 /skdlogin 进行登录\n"
                    "token获取: 登陆 [森空岛](https://www.skland.com/) 后获取token: https://web-api.skland.com/account/info/hg\n"
                    "将token中的{\"content\":\"XXX\"}中的XXX作为参数输入skdlogin，格式skdlogin XXX"
                )

        else:
            # ==================== Private Message ====================
            user_data = await self._get_user_data(user_id)

            if not user_data or "token" not in user_data:
                yield event.plain_result(
                    "您还未绑定森空岛账号\n"
                    "请使用 /skdlogin <token> 进行登录\n"
                    "token获取: 登陆 [森空岛](https://www.skland.com/) 后获取token: https://web-api.skland.com/account/info/hg\n"
                    "将token中的{\"content\":\"XXX\"}中的XXX作为参数输入skdlogin，格式skdlogin XXX"
                )
                return

            yield event.plain_result("正在查询签到状态...")

            try:
                token = user_data["token"]
                results, nickname = await self.api.do_full_sign_in(token)

                if not results:
                    yield event.plain_result("没有找到任何游戏绑定")
                    return

                # Update stored data
                user_data["nickname"] = nickname
                for r in results:
                    if r.game == "明日方舟" and self._is_signed_today(r):
                        user_data.setdefault("last_sign", {})["arknights"] = datetime.now().strftime(
                            "%Y-%m-%d"
                        )
                    elif r.game == "终末地" and self._is_signed_today(r):
                        user_data.setdefault("last_sign", {})["endfield"] = datetime.now().strftime(
                            "%Y-%m-%d"
                        )
                await self._save_user_data(user_id, user_data)

                response = self._format_sign_status(results, nickname)
                yield event.plain_result(response)

            except Exception as e:
                logger.error(f"skd failed for user {user_id}: {e}")
                if "过期" in str(e) or "登录" in str(e):
                    yield event.plain_result(
                        "Token已过期，请重新登录\n" "使用 /skdlogin <token> 进行登录"
                    )
                else:
                    yield event.plain_result(f"查询失败: {str(e)}")
