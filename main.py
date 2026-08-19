import asyncio
import json
import os
import random
from datetime import datetime, timedelta, timezone

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import At, Plain
from astrbot.api.star import Context, Star, register

try:
    from astrbot.api.all_in_one import get_astrbot_data_path
except ImportError:
    try:
        from astrbot.core.utils.io import get_astrbot_data_path
    except ImportError:
        get_astrbot_data_path = None


@register(
    "astrbot_plugin_group_cup",
    "bush",
    "群杯子 - 每天随机抽取一名群友作为今天的群杯子，并维护历史排行榜",
    "1.0.0",
    "",
)
class GroupCupPlugin(Star):
    """群杯子插件主类。

    功能：
      - ^群杯子      每天随机抽取一名群友作为「群杯子」，仅群主/管理员可用
      - ^群杯子排行   查看历史被抽中次数排行榜（前十名）
    """

    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.config = config or {}
        # 从配置读取时区偏移，默认东八区
        self.tz_offset = self.config.get("时区偏移", 8)
        # 每群一把异步锁，防止并发重复抽取
        self._locks: dict[str, asyncio.Lock] = {}

        # ── 初始化数据存储路径 ──
        if get_astrbot_data_path:
            self.data_dir = os.path.join(
                get_astrbot_data_path(), "plugin_data", "astrbot_plugin_group_cup"
            )
        else:
            # 回退方案：插件目录下 data/
            self.data_dir = os.path.join(os.path.dirname(__file__), "data")
        os.makedirs(self.data_dir, exist_ok=True)
        self.data_file = os.path.join(self.data_dir, "cup_data.json")
        self.data = self._load_data()

    # ═════════ 数据持久化 ═════════

    def _load_data(self) -> dict:
        """从 JSON 文件加载持久化数据，文件不存在或损坏时返回空结构。"""
        if os.path.exists(self.data_file):
            try:
                with open(self.data_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except (OSError, json.JSONDecodeError) as e:
                logger.warning(f"群杯子: 数据文件加载失败: {e}，将使用空数据。")
        return {"daily": {}, "stats": {}}

    def _save_data(self):
        """将数据持久化到 JSON 文件。"""
        try:
            with open(self.data_file, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
        except OSError as e:
            logger.error(f"群杯子: 数据文件保存失败: {e}")

    # ═════════ 工具方法 ═════════

    def _get_today(self) -> str:
        """获取今天的日期字符串（YYYY-MM-DD），使用配置的时区偏移。"""
        tz = timezone(timedelta(hours=self.tz_offset))
        return datetime.now(tz).strftime("%Y-%m-%d")

    @staticmethod
    def _get_group_id(event: AstrMessageEvent) -> str:
        """从事件中提取群号（统一转为 str）。"""
        return str(event.message_obj.group_id)

    @staticmethod
    def _is_group_message(event: AstrMessageEvent) -> bool:
        """判断是否为群消息。"""
        return event.message_obj.group_id is not None

    async def _get_bot(self, event: AstrMessageEvent):
        """获取 Bot 实例，用于调用 OneBot API。

        优先从 event.bot 获取，回退到平台实例的 bot 属性。
        """
        bot = getattr(event, "bot", None)
        if bot is not None:
            return bot
        platform = self.context.get_platform_inst(event.get_platform_id())
        bot = getattr(platform, "bot", None)
        if bot is not None:
            return bot
        raise RuntimeError(
            "无法获取 Bot 实例，请确保使用 OneBot v11 (aiocqhttp) 平台。"
        )

    async def _check_permission(self, event: AstrMessageEvent) -> bool:
        """检查发送者是否为群主或群管理员。

        优先从消息事件中读取 role 字段，不可用时回退到 OneBot API 查询。
        """
        sender = event.message_obj.sender
        if sender and getattr(sender, "role", "member") in ("owner", "admin"):
            return True
        # 回退：通过 OneBot API 查询成员信息
        try:
            bot = await self._get_bot(event)
            member_info = await bot.call_action(
                "get_group_member_info",
                group_id=int(event.message_obj.group_id),
                user_id=int(sender.user_id),
            )
            return member_info.get("role") in ("owner", "admin")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"群杯子: 权限检查失败: {e}")
            return False

    async def _get_group_members(self, event: AstrMessageEvent) -> list:
        """通过 OneBot API 获取群成员列表，排除机器人自身。"""
        try:
            bot = await self._get_bot(event)
            group_id = int(event.message_obj.group_id)
            members = await bot.call_action("get_group_member_list", group_id=group_id)
            # 排除机器人自己
            self_id = str(getattr(bot, "self_id", ""))
            if self_id:
                members = [m for m in members if str(m.get("user_id")) != self_id]
            return members
        except Exception as e:  # noqa: BLE001
            logger.error(f"群杯子: 获取群成员列表失败: {e}")
            return []

    @staticmethod
    def _get_display_name(member: dict) -> str:
        """获取成员显示名称（群名片 > 昵称 > QQ号）。"""
        return (
            member.get("card")
            or member.get("nickname")
            or str(member.get("user_id", "未知"))
        )

    def _get_lock(self, group_id: str) -> asyncio.Lock:
        """获取指定群的异步锁，防止并发抽取导致重复。"""
        if group_id not in self._locks:
            self._locks[group_id] = asyncio.Lock()
        return self._locks[group_id]

    def _get_nickname_by_uid(self, group_id: str, user_id: str) -> str:
        """从历史记录中查找用户昵称。"""
        for record in self.data["daily"].get(group_id, {}).values():
            if record.get("user_id") == user_id:
                return record.get("nickname", user_id)
        return user_id

    # ═════════ 指令处理 ═════════

    @filter.command("群杯子排行")
    async def group_cup_ranking(self, event: AstrMessageEvent):
        """^群杯子排行 — 显示群杯子历史排行榜（前十名）。"""
        # 私聊拦截
        if not self._is_group_message(event):
            yield event.plain_result("私聊无法使用此功能，请在群聊中使用。")
            return

        group_id = self._get_group_id(event)
        stats = self.data["stats"].get(group_id, {})

        if not stats:
            yield event.plain_result(
                "本群还没有群杯子记录，快使用「群杯子」开始第一次抽取吧！"
            )
            return

        # 按被抽中次数降序排列，取前十名
        sorted_stats = sorted(stats.items(), key=lambda x: x[1], reverse=True)[:10]

        lines = ["🏆 群杯子排行榜（前十名）\n"]
        medals = ["🥇", "🥈", "🥉"]
        for i, (user_id, count) in enumerate(sorted_stats):
            nickname = self._get_nickname_by_uid(group_id, user_id)
            if i < 3:
                lines.append(f"{medals[i]} {nickname} - {count}次")
            else:
                lines.append(f"{i + 1}. {nickname} - {count}次")

        yield event.plain_result("\n".join(lines))

    @filter.command("群杯子")
    async def group_cup(self, event: AstrMessageEvent):
        """^群杯子 — 每天随机抽取一名群友作为今天的群杯子。

        规则：
          - 仅群聊可用，私聊提示无法使用
          - 仅群主/管理员可触发抽取
          - 每天同一群只抽一次，已抽取则回复今日结果
        """
        # 私聊拦截
        if not self._is_group_message(event):
            yield event.plain_result("私聊无法使用此功能，请在群聊中使用。")
            return

        # 权限检查
        has_perm = await self._check_permission(event)
        if not has_perm:
            yield event.plain_result("权限不足，只有群主或群管理员可以使用此功能。")
            return

        group_id = self._get_group_id(event)
        today = self._get_today()

        # ── 锁内执行核心逻辑，收集结果文本/消息链 ──
        result_text: str | None = None
        result_chain: tuple[str, str] | None = None  # (user_id, nickname)

        async with self._get_lock(group_id):
            # 初始化群数据
            self.data["daily"].setdefault(group_id, {})
            self.data["stats"].setdefault(group_id, {})

            # 检查今天是否已经抽取过
            if today in self.data["daily"][group_id]:
                record = self.data["daily"][group_id][today]
                nickname = record.get("nickname", "未知")
                user_id = record.get("user_id", "")
                result_text = (
                    f"今天的群杯子已经诞生了！\n今天的群杯子是：{nickname}（{user_id}）"
                )
            else:
                # 获取群成员列表
                members = await self._get_group_members(event)
                if not members:
                    result_text = "获取群成员列表失败，请稍后重试。"
                else:
                    # 随机抽取一名群友
                    chosen = random.choice(members)
                    user_id = str(chosen.get("user_id", ""))
                    nickname = self._get_display_name(chosen)

                    # 记录今日结果
                    self.data["daily"][group_id][today] = {
                        "user_id": user_id,
                        "nickname": nickname,
                    }
                    # 更新统计计数
                    self.data["stats"][group_id][user_id] = (
                        self.data["stats"][group_id].get(user_id, 0) + 1
                    )
                    # 持久化
                    self._save_data()

                    # 准备消息链结果
                    result_chain = (user_id, nickname)

        # ── 锁外 yield 结果 ──
        if result_text:
            yield event.plain_result(result_text)
        elif result_chain:
            user_id, nickname = result_chain
            try:
                yield event.chain_result(
                    [
                        Plain("🎉 今天的群杯子已经诞生！\n恭喜 "),
                        At(qq=int(user_id)),
                        Plain(f"（{nickname}）成为今天的群杯子！"),
                    ]
                )
            except (ValueError, TypeError):
                # At 构造失败时回退为纯文本
                yield event.plain_result(
                    f"🎉 今天的群杯子已经诞生！\n"
                    f"恭喜 {nickname}（{user_id}）成为今天的群杯子！"
                )

    async def terminate(self):
        """插件卸载时保存数据。"""
        self._save_data()
