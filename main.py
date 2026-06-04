"""
AstrBot Plugin - 森空岛签到 (Skland Sign-In)

Commands:
- skypw <手机号> (private): 输入手机号后，下一条私聊消息输入密码完成登录
- skyph <手机号> (private): 获取验证码后，下一条私聊消息输入验证码完成登录
- sky (private): 立即签到全部已绑定账号
- sky <序号> (private): 只签到指定账号
- skylist (private): 查看当前已绑定账号
- skylogout (private): 解除绑定
- skyhelp: 查看帮助
"""

from datetime import datetime
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from astrbot.api import logger, AstrBotConfig
from astrbot.api.event import AstrMessageEvent, filter, MessageChain
from astrbot.api.star import Context, Star, register
from astrbot.core.star.config import put_config
import asyncio
import copy
import random
import re

from .skland_api import SklandAPI

PLUGIN_NAME = "astrbot_plugin_skland"
PENDING_EXPIRE_SECONDS = 600
PHONE_RE = re.compile(r"^1\d{10}$")
COMMAND_TEXT_RE = re.compile(
    r"^/?(sky|skypw|skyph|skylist|skylogout|skyhelp)(\s|$)",
    re.IGNORECASE,
)


@register(PLUGIN_NAME, "AstrBot", "森空岛自动签到插件", "2.0.0")
class SklandPlugin(Star):
    """森空岛签到插件"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.api = SklandAPI(max_retries=3)
        self.scheduler = AsyncIOScheduler()
        self._init_config()

    def _init_config(self):
        put_config(
            namespace=PLUGIN_NAME,
            name="自动签到开关",
            key="auto_sign_enabled",
            value=False,
            description="开启后，将在指定时间自动为已绑定用户签到，并私发结果",
        )
        put_config(
            namespace=PLUGIN_NAME,
            name="自动签到时间（小时）",
            key="auto_sign_hour",
            value=9,
            description="自动签到执行的小时（0-23）",
        )
        put_config(
            namespace=PLUGIN_NAME,
            name="自动签到时间（分钟）",
            key="auto_sign_minute",
            value=0,
            description="自动签到执行的分钟（0-59）",
        )
        put_config(
            namespace=PLUGIN_NAME,
            name="自动签到随机延迟",
            key="auto_sign_delay",
            value=10,
            description="每个账号签到前随机延迟秒数上限（0为不延迟）",
        )
        put_config(
            namespace=PLUGIN_NAME,
            name="最大绑定用户数",
            key="max_users",
            value=20,
            description="0 为不限制，超过限制则不允许新用户绑定",
        )

    def _get_config(self) -> dict:
        return {
            "auto_sign_enabled": self.config.get("auto_sign_enabled", False),
            "auto_sign_hour": self.config.get("auto_sign_hour", 9),
            "auto_sign_minute": self.config.get("auto_sign_minute", 0),
            "auto_sign_delay": self.config.get("auto_sign_delay", 10),
            "max_users": self.config.get("max_users", 20),
        }

    async def initialize(self):
        logger.info("森空岛签到插件已加载")
        config = self._get_config()
        if config.get("auto_sign_enabled", False):
            self._start_auto_sign_job(
                config.get("auto_sign_hour", 9),
                config.get("auto_sign_minute", 0),
            )
        if not self.scheduler.running:
            self.scheduler.start()

    async def terminate(self):
        if self.scheduler.running:
            self.scheduler.shutdown()
        await self.api.close()
        logger.info("森空岛签到插件已卸载")

    def _start_auto_sign_job(self, hour: int, minute: int):
        hour = max(0, min(23, int(hour)))
        minute = max(0, min(59, int(minute)))
        trigger = CronTrigger(hour=hour, minute=minute)
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
        logger.info(f"森空岛自动签到任务已启动，每天 {hour:02d}:{minute:02d} 执行")

    async def _send_private_message(self, user_id: str, user_data: dict, message: str):
        try:
            umo = user_data.get("umo")
            if not umo:
                logger.warning(f"用户 {user_id} 没有统一会话ID，无法发送私聊消息")
                return
            await self.context.send_message(umo, MessageChain().message(message))
        except Exception as e:
            logger.error(f"发送私聊消息失败: {e}")

    def _is_private(self, event: AstrMessageEvent) -> bool:
        return not bool(getattr(event.message_obj, "group_id", None))

    def _valid_phone(self, phone: str) -> bool:
        return bool(PHONE_RE.fullmatch((phone or "").strip()))

    def _build_user_keys(self, event: AstrMessageEvent) -> list[str]:
        keys: list[str] = []
        platform_name = str(event.get_platform_name() or "").strip().lower()
        sender_id = str(event.get_sender_id() or "").strip()
        if sender_id:
            if platform_name:
                keys.append(f"{platform_name}:{sender_id}")
            keys.append(sender_id)
        umo = str(getattr(event, "unified_msg_origin", "") or "").strip()
        if umo:
            keys.append(f"umo:{umo}")

        deduped: list[str] = []
        for key in keys:
            if key and key not in deduped:
                deduped.append(key)
        return deduped

    @staticmethod
    def _pick_existing_key(store: dict, keys: list[str]) -> str | None:
        for key in keys:
            if key in store:
                return key
        return None

    def _normalize_accounts(self, user_data: dict) -> list[dict]:
        accounts = user_data.get("accounts")
        normalized: list[dict] = []
        if isinstance(accounts, list):
            for item in accounts:
                if not isinstance(item, dict):
                    continue
                token = str(item.get("token") or "").strip()
                if not token:
                    continue
                normalized.append(
                    {
                        "token": token,
                        "phone": str(item.get("phone", "")).strip(),
                        "nickname": str(item.get("nickname", "")).strip(),
                        "bound_at": item.get("bound_at") or datetime.now().isoformat(),
                        "last_sign_at": item.get("last_sign_at"),
                        "last_results": copy.deepcopy(item.get("last_results") or []),
                        "award_cache": copy.deepcopy(item.get("award_cache") or {}),
                    }
                )
        if normalized:
            return normalized

        legacy_token = str(user_data.get("token") or "").strip()
        if legacy_token:
            return [
                {
                    "token": legacy_token,
                    "phone": str(user_data.get("phone", "")).strip(),
                    "nickname": str(user_data.get("nickname", "")).strip(),
                    "bound_at": user_data.get("bound_at") or datetime.now().isoformat(),
                    "last_sign_at": user_data.get("last_sign_at"),
                    "last_results": [],
                    "award_cache": {},
                }
            ]
        return []

    def _store_accounts(self, user_data: dict, accounts: list[dict]):
        user_data["accounts"] = accounts
        if accounts:
            latest = accounts[-1]
            user_data["token"] = latest.get("token", "")
            user_data["phone"] = latest.get("phone", "")
            user_data["nickname"] = latest.get("nickname", "")
            user_data["bound_at"] = latest.get("bound_at")
            user_data["last_sign_at"] = latest.get("last_sign_at")
        else:
            user_data.pop("token", None)
            user_data.pop("phone", None)
            user_data.pop("nickname", None)
            user_data.pop("bound_at", None)
            user_data.pop("last_sign_at", None)

    def _upsert_account(self, accounts: list[dict], new_entry: dict) -> tuple[str, int]:
        new_phone = str(new_entry.get("phone") or "").strip()
        new_token = str(new_entry.get("token") or "").strip()
        for idx, item in enumerate(accounts):
            phone = str(item.get("phone") or "").strip()
            token = str(item.get("token") or "").strip()
            if new_phone and phone == new_phone:
                accounts[idx] = new_entry
                return "updated", idx
            if new_token and token == new_token:
                accounts[idx] = new_entry
                return "updated", idx
        accounts.append(new_entry)
        return "added", len(accounts) - 1

    @staticmethod
    def _format_time(value: str | None) -> str:
        if not value:
            return "未签到"
        try:
            return datetime.fromisoformat(value).strftime("%Y-%m-%d %H:%M")
        except Exception:
            return str(value)

    @staticmethod
    def _today() -> str:
        return datetime.now().strftime("%Y-%m-%d")

    @staticmethod
    def _mask_phone(phone: str) -> str:
        phone = str(phone or "").strip()
        if len(phone) >= 4:
            return phone[-4:]
        return phone or "未知"

    def _format_account_brief(self, entry: dict, index: int) -> str:
        nickname = str(entry.get("nickname") or "").strip() or "未知角色"
        phone_tail = self._mask_phone(entry.get("phone", ""))
        results = entry.get("last_results") or []
        games = []
        for item in results:
            game = str(item.get("game") or "").strip()
            if game and game not in games:
                games.append(game)
        game_text = "/".join(games) if games else "未签到"
        last_sign = self._format_time(entry.get("last_sign_at"))
        return f"{index}. {nickname} | 手机尾号={phone_tail} | 游戏={game_text} | 最后签到={last_sign}"

    def _is_signed_today(self, result) -> bool:
        if result.success:
            return True
        error = result.error.lower() if result.error else ""
        return any(k in error for k in ["已签到", "请勿重复", "重复签到", "already", "签到过", "今日已"])

    @staticmethod
    def _result_snapshot(results: list) -> list[dict]:
        snapshot = []
        for item in results:
            snapshot.append(
                {
                    "success": bool(getattr(item, "success", False)),
                    "game": getattr(item, "game", ""),
                    "nickname": getattr(item, "nickname", ""),
                    "channel": getattr(item, "channel", ""),
                    "awards": list(getattr(item, "awards", []) or []),
                    "error": getattr(item, "error", ""),
                    "skipped": bool(getattr(item, "skipped", False)),
                }
            )
        return snapshot

    @staticmethod
    def _result_key(result) -> str:
        return "|".join(
            [
                str(getattr(result, "game", "") or ""),
                str(getattr(result, "nickname", "") or ""),
                str(getattr(result, "channel", "") or ""),
            ]
        )

    def _build_award_cache(self, entry: dict, previous_results: list[dict]) -> dict:
        cache = copy.deepcopy(entry.get("award_cache") or {})
        today = self._today()
        today_cache = cache.get(today)
        if not isinstance(today_cache, dict):
            today_cache = {}
            cache[today] = today_cache

        for item in previous_results:
            if not isinstance(item, dict):
                continue
            awards = item.get("awards")
            if not isinstance(awards, list) or not awards:
                continue
            key = "|".join(
                [
                    str(item.get("game") or ""),
                    str(item.get("nickname") or ""),
                    str(item.get("channel") or ""),
                ]
            )
            if key.strip("|"):
                today_cache[key] = list(awards)

        for day in list(cache.keys()):
            if day != today:
                del cache[day]
        return cache

    def _apply_cached_awards(self, entry: dict, results: list):
        cache = entry.get("award_cache") or {}
        today_cache = cache.get(self._today()) or {}
        if not isinstance(today_cache, dict):
            return

        for item in results:
            if list(getattr(item, "awards", []) or []):
                continue
            if not self._is_signed_today(item):
                continue
            cached_awards = today_cache.get(self._result_key(item))
            if cached_awards:
                item.awards = list(cached_awards)

    def _format_sign_status(self, results: list, nickname: str = "") -> str:
        if not results:
            return "未找到可签到的游戏角色"

        lines = []
        if nickname:
            lines.append(f"账号：{nickname}")

        for result in results:
            if getattr(result, "skipped", False):
                continue

            role_name = getattr(result, "nickname", "") or nickname or "未知角色"
            channel = getattr(result, "channel", "")
            channel_text = f"({channel})" if channel else ""
            game = getattr(result, "game", "未知游戏")
            awards = list(getattr(result, "awards", []) or [])

            if result.success:
                award_text = ", ".join(awards) if awards else "本次未返回奖励"
                lines.append(f"{game} {role_name}{channel_text}：签到成功，奖励：{award_text}")
            elif self._is_signed_today(result):
                award_text = (
                    ", ".join(awards)
                    if awards
                    else "今日已领取；重复签到接口不返回奖励，需等下次首次签到后缓存显示"
                )
                lines.append(f"{game} {role_name}{channel_text}：今日已签到，奖励：{award_text}")
            else:
                error = getattr(result, "error", "") or "未知错误"
                lines.append(f"{game} {role_name}{channel_text}：签到失败，原因：{error}")
        return "\n".join(lines) if len(lines) > (1 if nickname else 0) else "未找到可签到的游戏角色"

    async def _do_sign_for_account(self, entry: dict) -> tuple[bool, str]:
        token = str(entry.get("token") or "").strip()
        if not token:
            raise Exception("账号数据缺失，请重新登录")

        previous_results = copy.deepcopy(entry.get("last_results") or [])
        entry["award_cache"] = self._build_award_cache(entry, previous_results)
        results, nickname = await self.api.do_full_sign_in(token)
        entry["award_cache"] = self._build_award_cache(entry, self._result_snapshot(results))
        self._apply_cached_awards(entry, results)
        entry["nickname"] = nickname or entry.get("nickname", "")
        entry["last_sign_at"] = datetime.now().isoformat()
        entry["last_results"] = self._result_snapshot(results)
        effective_results = [item for item in results if not getattr(item, "skipped", False)]
        ok = bool(effective_results) and all(self._is_signed_today(item) for item in effective_results)
        return ok, self._format_sign_status(results, entry.get("nickname", ""))

    async def _auto_sign_all_users(self):
        config = self._get_config()
        if not config.get("auto_sign_enabled", False):
            return

        users = await self.get_kv_data("users", {})
        if not users:
            return

        max_delay = max(0, int(config.get("auto_sign_delay", 10)))
        for user_id, user_data in users.items():
            accounts = self._normalize_accounts(user_data)
            if not accounts:
                continue

            summaries: list[str] = []
            all_ok = True
            for index, entry in enumerate(accounts, start=1):
                if max_delay > 0:
                    await asyncio.sleep(random.uniform(0, max_delay))
                try:
                    ok, detail = await self._do_sign_for_account(entry)
                    all_ok = all_ok and ok
                    summaries.append(f"【账号 {index}】\n{detail}")
                except Exception as e:
                    all_ok = False
                    logger.error(f"用户 {user_id} 的第 {index} 个森空岛账号自动签到失败: {e}")
                    summaries.append(f"【账号 {index}】\n签到失败：{str(e)}")

            self._store_accounts(user_data, accounts)
            users[user_id] = user_data
            header = "森空岛自动签到完成\n结果：成功" if all_ok else "森空岛自动签到完成\n结果：部分失败，请查看详情"
            message = header
            if summaries:
                message = f"{message}\n\n" + "\n\n".join(summaries[:12])
            await self._send_private_message(user_id, user_data, message)
        await self.put_kv_data("users", users)

    async def _set_pending(self, user_id: str, data: dict):
        pending = await self.get_kv_data("pending_login", {})
        pending[user_id] = {
            **data,
            "created_at": int(datetime.now().timestamp()),
        }
        await self.put_kv_data("pending_login", pending)

    async def _clear_pending(self, user_id: str):
        pending = await self.get_kv_data("pending_login", {})
        if user_id in pending:
            del pending[user_id]
            await self.put_kv_data("pending_login", pending)

    @filter.command("skyhelp")
    async def skyhelp(self, event: AstrMessageEvent):
        yield event.plain_result(
            "森空岛签到插件帮助\n"
            "1. /skypw <手机号> -> 下一条私聊消息发送密码完成登录\n"
            "2. /skyph <手机号> -> 获取验证码后，下一条私聊消息发送验证码完成登录\n"
            "3. /sky 立即签到全部已绑定账号\n"
            "4. /sky <序号> 只签到指定账号\n"
            "5. /skylist 查看当前绑定账号\n"
            "6. /skylogout 解除全部绑定\n"
            "7. /skylogout <序号> 删除指定账号绑定"
        )

    @filter.command("skylist")
    async def skylist(self, event: AstrMessageEvent):
        if not self._is_private(event):
            yield event.plain_result("请在私聊中使用 /skylist")
            return

        user_keys = self._build_user_keys(event)
        if not user_keys:
            yield event.plain_result("无法识别当前用户，请稍后重试")
            return
        user_id = user_keys[0]
        users = await self.get_kv_data("users", {})
        existing_user_key = self._pick_existing_key(users, user_keys)
        user_data = users.get(existing_user_key) if existing_user_key else None
        if not user_data:
            yield event.plain_result("你还未绑定账号，请先使用 /skypw 或 /skyph 登录")
            return

        if existing_user_key and existing_user_key != user_id:
            users[user_id] = users.pop(existing_user_key)
            user_data = users[user_id]
            await self.put_kv_data("users", users)

        accounts = self._normalize_accounts(user_data)
        if not accounts:
            yield event.plain_result("你还未绑定账号，请先使用 /skypw 或 /skyph 登录")
            return

        summaries = "\n".join(self._format_account_brief(item, i) for i, item in enumerate(accounts, start=1))
        yield event.plain_result(f"当前共绑定 {len(accounts)} 个账号：\n{summaries}")

    @filter.command("skypw")
    async def skypw(self, event: AstrMessageEvent, phone: str = ""):
        if not self._is_private(event):
            yield event.plain_result("请在私聊中使用 /skypw 登录，避免泄露隐私")
            return
        phone = phone.strip()
        if not self._valid_phone(phone):
            yield event.plain_result("手机号格式错误，请使用：/skypw 13800138000")
            return

        user_keys = self._build_user_keys(event)
        if not user_keys:
            yield event.plain_result("无法识别当前用户，请稍后重试")
            return
        user_id = user_keys[0]
        users = await self.get_kv_data("users", {})
        config = self._get_config()
        max_users = int(config.get("max_users", 20))
        existing_key = self._pick_existing_key(users, user_keys)
        if existing_key is None and max_users > 0 and len(users) >= max_users:
            yield event.plain_result(f"绑定失败：已达到最大用户数限制（{max_users}）")
            return

        await self._set_pending(user_id, {"mode": "password", "phone": phone})
        yield event.plain_result("已记录手机号，请直接回复密码（10分钟内有效）")

    @filter.command("skyph")
    async def skyph(self, event: AstrMessageEvent, phone: str = ""):
        if not self._is_private(event):
            yield event.plain_result("请在私聊中使用 /skyph 获取验证码，避免泄露隐私")
            return
        phone = phone.strip()
        if not self._valid_phone(phone):
            yield event.plain_result("手机号格式错误，请使用：/skyph 13800138000")
            return

        user_keys = self._build_user_keys(event)
        if not user_keys:
            yield event.plain_result("无法识别当前用户，请稍后重试")
            return
        user_id = user_keys[0]
        users = await self.get_kv_data("users", {})
        config = self._get_config()
        max_users = int(config.get("max_users", 20))
        existing_key = self._pick_existing_key(users, user_keys)
        if existing_key is None and max_users > 0 and len(users) >= max_users:
            yield event.plain_result(f"绑定失败：已达到最大用户数限制（{max_users}）")
            return

        try:
            await self.api.send_login_code(phone)
        except Exception as e:
            yield event.plain_result(f"发送验证码失败：{str(e)}")
            return

        await self._set_pending(user_id, {"mode": "sms", "phone": phone})
        yield event.plain_result("验证码已发送，请直接回复验证码（10分钟内有效）")

    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    @filter.regex(r"^[^/].+")
    async def handle_pending_login_input(self, event: AstrMessageEvent):
        user_keys = self._build_user_keys(event)
        if not user_keys:
            return
        user_id = user_keys[0]
        pending = await self.get_kv_data("pending_login", {})
        pending_key = self._pick_existing_key(pending, user_keys)
        session = pending.get(pending_key) if pending_key else None
        if not session:
            return

        now_ts = int(datetime.now().timestamp())
        created_at = int(session.get("created_at", 0))
        if created_at <= 0 or now_ts - created_at > PENDING_EXPIRE_SECONDS:
            await self._clear_pending(pending_key or user_id)
            yield event.plain_result("登录流程已过期，请重新发送 /skypw 或 /skyph")
            return

        content = event.get_message_str().strip()
        if not content:
            return
        if COMMAND_TEXT_RE.match(content):
            return

        mode = session.get("mode")
        phone = str(session.get("phone", "")).strip()

        try:
            if mode == "password":
                token = await self.api.login_by_password(phone, content)
            elif mode == "sms":
                token = await self.api.login_by_sms(phone, content)
            else:
                await self._clear_pending(pending_key or user_id)
                yield event.plain_result("登录状态异常，请重新发送 /skypw 或 /skyph")
                return
        except Exception as e:
            retry_hint = "可直接重试发送密码" if mode == "password" else "可直接重试发送验证码"
            yield event.plain_result(f"登录失败：{str(e)}\n{retry_hint}")
            return

        users = await self.get_kv_data("users", {})
        existing_user_key = self._pick_existing_key(users, user_keys)
        if existing_user_key and existing_user_key != user_id:
            users[user_id] = users.pop(existing_user_key)
        user_data = users.get(user_id, {})
        accounts = self._normalize_accounts(user_data)
        new_entry = {
            "token": token,
            "phone": phone,
            "nickname": "",
            "bound_at": datetime.now().isoformat(),
            "last_sign_at": None,
            "last_results": [],
            "award_cache": {},
        }

        try:
            ok, detail = await self._do_sign_for_account(new_entry)
        except Exception as e:
            yield event.plain_result(f"登录成功，但首次签到失败：{str(e)}\n账号未保存，请稍后重新登录。")
            await self._clear_pending(pending_key or user_id)
            return

        action, idx = self._upsert_account(accounts, new_entry)
        user_data.update(
            {
                "last_username": event.get_sender_name(),
                "platform_name": event.get_platform_name(),
                "umo": event.unified_msg_origin,
            }
        )
        self._store_accounts(user_data, accounts)
        users[user_id] = user_data
        await self.put_kv_data("users", users)
        await self._clear_pending(pending_key or user_id)

        summaries = "\n".join(self._format_account_brief(item, i) for i, item in enumerate(accounts, start=1))
        action_text = "已更新已有账号" if action == "updated" else "已新增绑定账号"
        sign_header = "首次签到成功" if ok else "首次签到完成，但存在失败项"
        yield event.plain_result(
            f"登录成功，{action_text}。\n当前共绑定 {len(accounts)} 个账号。\n"
            f"本次账号序号：{idx + 1}\n\n{summaries}\n\n"
            f"【{sign_header}】\n{detail}\n\n发送 /sky 即可签到全部账号。"
        )

    @filter.command("skylogout")
    async def skylogout(self, event: AstrMessageEvent, index: str = ""):
        if not self._is_private(event):
            yield event.plain_result("请在私聊中使用 /skylogout")
            return
        user_keys = self._build_user_keys(event)
        if not user_keys:
            yield event.plain_result("无法识别当前用户，请稍后重试")
            return

        users = await self.get_kv_data("users", {})
        user_id = user_keys[0]
        existing_user_key = self._pick_existing_key(users, user_keys)
        if existing_user_key and existing_user_key != user_id:
            users[user_id] = users.pop(existing_user_key)
            existing_user_key = user_id

        changed = False
        message = "你当前没有绑定账号"
        if existing_user_key and existing_user_key in users:
            user_data = users[existing_user_key]
            accounts = self._normalize_accounts(user_data)
            raw_index = index.strip()
            if raw_index:
                if not raw_index.isdigit():
                    yield event.plain_result("序号格式错误，请使用 /skylogout 1")
                    return
                target = int(raw_index)
                if target <= 0 or target > len(accounts):
                    yield event.plain_result(f"序号超出范围，当前共有 {len(accounts)} 个账号")
                    return
                removed = accounts.pop(target - 1)
                changed = True
                if accounts:
                    self._store_accounts(user_data, accounts)
                    users[existing_user_key] = user_data
                    message = (
                        f"已删除第 {target} 个账号绑定：{self._format_account_brief(removed, target)}\n"
                        f"剩余 {len(accounts)} 个账号。"
                    )
                else:
                    del users[existing_user_key]
                    message = "已删除最后一个账号绑定，并清空当前用户的登录信息"
            else:
                del users[existing_user_key]
                changed = True
                message = "已清除全部登录信息"

        if changed:
            await self.put_kv_data("users", users)

        pending = await self.get_kv_data("pending_login", {})
        pending_changed = False
        for key in user_keys:
            if key in pending:
                del pending[key]
                pending_changed = True
        if pending_changed:
            await self.put_kv_data("pending_login", pending)
            changed = True

        yield event.plain_result(message if changed else "你当前没有绑定账号")

    @filter.command("sky")
    async def sky_sign(self, event: AstrMessageEvent, index: str = ""):
        if not self._is_private(event):
            yield event.plain_result("请在私聊中使用 /sky 签到")
            return

        user_keys = self._build_user_keys(event)
        if not user_keys:
            yield event.plain_result("无法识别当前用户，请稍后重试")
            return
        user_id = user_keys[0]
        users = await self.get_kv_data("users", {})
        existing_user_key = self._pick_existing_key(users, user_keys)
        user_data = users.get(existing_user_key) if existing_user_key else None
        if not user_data:
            yield event.plain_result("你还未绑定账号，请先使用 /skypw 或 /skyph 登录")
            return

        if existing_user_key and existing_user_key != user_id:
            users[user_id] = users.pop(existing_user_key)
            user_data = users[user_id]

        accounts = self._normalize_accounts(user_data)
        if not accounts:
            yield event.plain_result("你还未绑定账号，请先使用 /skypw 或 /skyph 登录")
            return

        target_indexes = list(range(len(accounts)))
        index = str(index or "").strip()
        if index:
            if not index.isdigit():
                yield event.plain_result("序号格式错误，请使用 /sky 1")
                return
            target = int(index)
            if target < 1 or target > len(accounts):
                yield event.plain_result(f"序号超出范围，当前共有 {len(accounts)} 个账号。发送 /skylist 查看列表")
                return
            target_indexes = [target - 1]

        if len(target_indexes) == 1 and index:
            yield event.plain_result(
                f"正在签到第 {target_indexes[0] + 1} 个账号，请稍候...\n"
                f"{self._format_account_brief(accounts[target_indexes[0]], target_indexes[0] + 1)}"
            )
        else:
            yield event.plain_result(f"正在签到，请稍候...（共 {len(accounts)} 个账号）")

        all_ok = True
        summaries: list[str] = []
        for target_index in target_indexes:
            entry = accounts[target_index]
            account_number = target_index + 1
            try:
                ok, detail = await self._do_sign_for_account(entry)
                all_ok = all_ok and ok
                summaries.append(f"【账号 {account_number}】\n{detail}")
            except Exception as e:
                all_ok = False
                summaries.append(f"【账号 {account_number}】\n签到失败：{str(e)}")

        self._store_accounts(user_data, accounts)
        users[user_id] = user_data
        await self.put_kv_data("users", users)
        detail_text = "\n\n".join(summaries[:12]) if summaries else "无详细信息"
        if all_ok:
            yield event.plain_result(f"签到完成\n\n{detail_text}")
        else:
            yield event.plain_result(f"签到完成，但存在失败项\n\n{detail_text}")
