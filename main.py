"""quota_guard · token 额度 + 限时 VIP

一个独立实现的群聊 token 流量控制插件：
- 按群、按人统计每日 token 用量（来自 `LLMResponse.usage`，是精确值）
- 超过限额时，在 LLM 请求前拦截
- 支持「限时 VIP」：指定群里指定成员在一段时间内不受单用户限额约束，到期自动恢复
- 不依赖、不修改任何其他插件

作者：大肥鱼 · MIT License
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Poke
from astrbot.api.star import Context, Star, register

PLUGIN_NAME = "astrbot_plugin_quota_guard"
DEFAULT_BLOCK_MESSAGE = "今天的额度用完了，明天再来吧。"


def _logical_day(reset_hour: int) -> str:
    """按「重置小时」计算逻辑日期，比如 4 点重置时，凌晨 2 点仍算前一天。"""
    try:
        offset = int(reset_hour) * 3600
    except (TypeError, ValueError):
        offset = 0
    return time.strftime("%Y-%m-%d", time.localtime(time.time() - offset))


def _fmt_ts(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def _human(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


class UsageStore:
    """按逻辑日期保存的用量计数。"""

    def __init__(self, path: Path):
        self.path = path
        self.day = ""
        self.groups: dict[str, int] = {}
        self.users: dict[str, int] = {}

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.day = str(data.get("day") or "")
            self.groups = {str(k): int(v) for k, v in (data.get("groups") or {}).items()}
            self.users = {
                str(k): int(v)
                for k, v in (data.get("users") or {}).items()
                if str(k).split(":")[-1].isdigit()
            }
        except FileNotFoundError:
            pass
        except Exception as exc:
            logger.warning(f"[quota_guard] 读取用量失败: {exc}")

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(
                    {"day": self.day, "groups": self.groups, "users": self.users},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        except Exception as exc:
            logger.warning(f"[quota_guard] 保存用量失败: {exc}")

    def ensure_day(self, reset_hour: int) -> None:
        today = _logical_day(reset_hour)
        if today != self.day:
            if self.day:
                logger.info(f"[quota_guard] 进入新的一天 {today}，用量清零")
            self.day = today
            self.groups = {}
            self.users = {}
            self._save()

    def add(self, group_id: str, user_id: str, tokens: int, reset_hour: int) -> None:
        if tokens <= 0:
            return
        self.ensure_day(reset_hour)
        if group_id:
            self.groups[group_id] = self.groups.get(group_id, 0) + tokens
        if user_id and user_id.isdigit():
            # 群内计数（按群限额用）
            if group_id:
                key = f"{group_id}:{user_id}"
                self.users[key] = self.users.get(key, 0) + tokens
            # 全局计数（按人限额用，跨群 + 私聊累加）
            gkey = f"*:{user_id}"
            self.users[gkey] = self.users.get(gkey, 0) + tokens
        self._save()

    def group_total(self, group_id: str) -> int:
        return self.groups.get(str(group_id), 0)

    def user_total(self, group_id: str, user_id: str) -> int:
        return self.users.get(f"{group_id}:{user_id}", 0)

    def user_global_total(self, user_id: str) -> int:
        """该 QQ 在当天所有群 + 私聊的 token 总和。"""
        return self.users.get(f"*:{user_id}", 0)


class VipStore:
    """限时 VIP 名单。"""

    def __init__(self, path: Path):
        self.path = path

    def _load(self) -> list[dict]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            items = data.get("vip", []) if isinstance(data, dict) else []
            return [x for x in items if isinstance(x, dict)]
        except FileNotFoundError:
            return []
        except Exception as exc:
            logger.warning(f"[quota_guard] 读取 VIP 名单失败: {exc}")
            return []

    def _save(self, items: list[dict]) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps({"vip": items}, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception as exc:
            logger.warning(f"[quota_guard] 保存 VIP 名单失败: {exc}")

    def remaining(self, group_id: str, user_id: str) -> float:
        """还有多少秒 VIP 时长；0 表示不是 VIP。"""
        now = time.time()
        best = 0.0
        for item in self._load():
            try:
                if str(item.get("group_id")) != str(group_id):
                    continue
                if str(item.get("user_id")) != str(user_id):
                    continue
                left = float(item.get("expires_at", 0)) - now
                best = max(best, left)
            except (TypeError, ValueError):
                continue
        return max(0.0, best)

    def add(self, group_id: str, user_id: str, days: float) -> float:
        items = self._load()
        items = [
            x
            for x in items
            if not (
                str(x.get("group_id")) == str(group_id)
                and str(x.get("user_id")) == str(user_id)
            )
        ]
        expires = time.time() + days * 86400
        items.append(
            {
                "group_id": str(group_id),
                "user_id": str(user_id),
                "expires_at": expires,
                "created_at": time.time(),
            }
        )
        self._save(items)
        return expires

    def remove(self, group_id: str | None, user_id: str) -> int:
        items = self._load()
        kept = [
            x
            for x in items
            if not (
                str(x.get("user_id")) == user_id
                and (group_id is None or str(x.get("group_id")) == str(group_id))
            )
        ]
        self._save(kept)
        return len(items) - len(kept)

    def listing(self) -> list[dict]:
        return self._load()


@register(
    "1.0.3",
    "大肥鱼",
    "群聊 token 额度控制 + 限时 VIP",
    "1.0.6",
)
class TokenGuard(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config or {}
        base = Path(__file__).resolve().parents[2] / "plugin_data" / PLUGIN_NAME
        self.usage = UsageStore(base / "usage.json")
        self.vip = VipStore(base / "vip.json")
        self.usage._load()
        self._poke_patched = False

    # ------------------------------------------------------------------ 配置
    def _cfg(self, key: str, default):
        return self.config.get(key, default)

    def _reset_hour(self) -> int:
        try:
            return int(self._cfg("reset_hour", 0))
        except (TypeError, ValueError):
            return 0

    def _group_limit(self) -> int:
        try:
            return int(self._cfg("group_daily_limit", 3_000_000))
        except (TypeError, ValueError):
            return 3_000_000

    def _user_limit(self) -> int:
        try:
            return int(self._cfg("user_daily_limit", 300_000))
        except (TypeError, ValueError):
            return 300_000

    def _enforced_groups(self) -> list[str]:
        raw = self._cfg("groups", []) or []
        if isinstance(raw, str):
            raw = [x.strip() for x in raw.replace("，", ",").split(",")]
        return [str(x).strip() for x in raw if str(x).strip()]

    def _is_enforced_group(self, group_id: str) -> bool:
        groups = self._enforced_groups()
        return True if not groups else str(group_id) in groups

    def _is_exempt_user(self, user_id: str) -> bool:
        raw = self._cfg("exempt_users", []) or []
        if isinstance(raw, str):
            raw = [x.strip() for x in raw.replace("，", ",").split(",")]
        return str(user_id) in {str(x).strip() for x in raw if str(x).strip()}

    # ------------------------------------------------------------------ 超额判断
    def is_group_over_limit(self, group_id: str) -> bool:
        """本群今日用量是否已达上限。"""
        if not self._cfg("enable", True):
            return False
        gid = str(group_id or "")
        if not gid or not self._is_enforced_group(gid):
            return False
        self.usage.ensure_day(self._reset_hour())
        return self.usage.group_total(gid) >= self._group_limit()

    @staticmethod
    def _has_poke(event: AstrMessageEvent) -> bool:
        try:
            for comp in event.get_messages() or []:
                if isinstance(comp, Poke):
                    return True
        except Exception:
            pass
        return False

    def should_mute_poke(self, event: AstrMessageEvent) -> bool:
        """群额度用完后，是否该吞掉这次戳一戳（避免继续做表情回应）。"""
        if not self._cfg("mute_poke_over_limit", True):
            return False
        gid = str(event.get_group_id() or "")
        uid = str(event.get_sender_id() or "")
        if not gid:
            return False
        if uid and self._is_exempt_user(uid):
            return False
        if not self.is_group_over_limit(gid):
            return False
        return self._has_poke(event)

    def try_patch_pokepro(self) -> bool:
        """把 pokepro 的消息处理包一层：群超限时吞掉戳一戳。"""
        if self._poke_patched:
            return True
        module = None
        for name, mod in list(sys.modules.items()):
            if name.endswith("astrbot_plugin_pokepro.main"):
                module = mod
                break
        if module is None:
            return False
        cls = getattr(module, "PokeproPlugin", None)
        original = getattr(cls, "on_message", None)
        if cls is None or original is None:
            return False

        guard = self

        async def _patched(inner_self, event):
            try:
                if guard.should_mute_poke(event):
                    logger.info("[quota_guard] 群额度已满，跳过戳一戳反应")
                    return None
            except Exception as exc:
                logger.debug(f"[quota_guard] 戳一戳判断异常: {exc}")
            return await original(inner_self, event)

        cls.on_message = _patched
        self._poke_patched = True
        logger.info("[quota_guard] 已接管 pokepro 的戳一戳处理")
        return True

    @filter.on_astrbot_loaded()
    async def on_loaded(self):
        if not self.try_patch_pokepro():
            logger.warning("[quota_guard] 未找到 pokepro，戳一戳静音功能不可用（不影响限额）")

    @filter.on_llm_request()
    async def ensure_patched(self, event: AstrMessageEvent, req):
        if not self._poke_patched:
            self.try_patch_pokepro()

    # ------------------------------------------------------------------ 拦截
    @filter.on_waiting_llm_request()
    async def gate(self, event: AstrMessageEvent) -> None:
        if not self._cfg("enable", True):
            return
        group_id = str(event.get_group_id() or "")
        user_id = str(event.get_sender_id() or "")
        if not group_id:
            if not self._cfg("apply_private", False):
                return
        elif not self._is_enforced_group(group_id):
            return
        if user_id and self._is_exempt_user(user_id):
            return
        if group_id and user_id and self.vip.remaining(group_id, user_id) > 0:
            return

        self.usage.ensure_day(self._reset_hour())
        scope = str(self._cfg("user_limit_scope", "group")).lower()
        if group_id and scope != "global":
            user_used = self.usage.user_total(group_id, user_id)
        else:
            user_used = self.usage.user_global_total(user_id)
        reason = ""
        if user_id and user_used >= self._user_limit():
            reason = "user"
        elif group_id and self.usage.group_total(group_id) >= self._group_limit():
            reason = "group"
        if not reason:
            return

        event.stop_event()
        logger.info(
            f"[quota_guard] 拦截 reason={reason} group={group_id} user={user_id} "
            f"群用量={self.usage.group_total(group_id)} 个人用量={user_used} scope={scope}"
        )
        if self._cfg("notify_blocked", True):
            try:
                text = str(self._cfg("block_message", DEFAULT_BLOCK_MESSAGE))
                await event.send(event.plain_result(text).chain)
            except Exception as exc:
                logger.debug(f"[quota_guard] 发送提示失败: {exc}")

    # ------------------------------------------------------------------ 记账
    @filter.on_llm_response()
    async def account(self, event: AstrMessageEvent, response) -> None:
        if not self._cfg("enable", True):
            return
        usage = getattr(response, "usage", None)
        if usage is None:
            return
        try:
            tokens = int(getattr(usage, "total", 0))
            if not tokens:
                tokens = int(getattr(usage, "input_other", 0)) + int(
                    getattr(usage, "input_cached", 0)
                ) + int(getattr(usage, "output", 0))
        except (TypeError, ValueError):
            return
        group_id = str(event.get_group_id() or "")
        user_id = str(event.get_sender_id() or "")
        # 私聊默认既不拦截也不计数，避免私聊消耗影响群额度
        if not group_id and not self._cfg("apply_private", False):
            return
        self.usage.add(group_id, user_id, tokens, self._reset_hour())

    # ------------------------------------------------------------------ 指令
    @filter.command("token")
    async def token_status(self, event: AstrMessageEvent):
        """查看今日用量。"""
        self.usage.ensure_day(self._reset_hour())
        group_id = str(event.get_group_id() or "")
        user_id = str(event.get_sender_id() or "")
        gl, ul = self._group_limit(), self._user_limit()
        lines = [f"今日（{self.usage.day}）"]
        if group_id:
            lines.append(
                f"本群：{_human(self.usage.group_total(group_id))} / {_human(gl)}"
            )
        # 显示口径必须和拦截口径一致，否则会出现「A 群满了、B 群显示 0」的误导
        scope = str(self._cfg("user_limit_scope", "group")).lower()
        if scope == "global" or not group_id:
            user_used = self.usage.user_global_total(user_id)
            user_label = "你（全局）"
        else:
            user_used = self.usage.user_total(group_id, user_id)
            user_label = "你（本群）"
        lines.append(f"{user_label}：{_human(user_used)} / {_human(ul)}")
        if group_id and user_id:
            left = self.vip.remaining(group_id, user_id)
            if left > 0:
                lines.append(f"VIP：剩余 {left / 3600:.1f} 小时（{_fmt_ts(time.time() + left)} 到期）")
        yield event.plain_result("\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("vip")
    async def vip_cmd(self, event: AstrMessageEvent):
        """VIP 名单管理。

        用法：
          /vip add <QQ> <天数> [群号]
          /vip del <QQ> [群号]
          /vip list
        """
        parts = (event.message_str or "").strip().split()
        if len(parts) < 2:
            yield event.plain_result(
                "用法：/vip add <QQ> <天数> [群号] | /vip del <QQ> [群号] | /vip list"
            )
            return
        action = parts[1].lower()

        if action == "list":
            items = self.vip.listing()
            if not items:
                yield event.plain_result("当前没有 VIP。")
                return
            lines = [
                f"{x.get('group_id')} / {x.get('user_id')} → {_fmt_ts(float(x.get('expires_at', 0)))}"
                for x in items
            ]
            yield event.plain_result("VIP 名单：\n" + "\n".join(lines))
            return

        if action == "del":
            if len(parts) < 3:
                yield event.plain_result("用法：/vip del <QQ> [群号]")
                return
            target_user = parts[2]
            target_group = parts[3] if len(parts) >= 4 else None
            removed = self.vip.remove(target_group, target_user)
            yield event.plain_result(f"已移除 {target_user} 的 VIP（{removed} 条）。")
            return

        if action == "add":
            if len(parts) < 4:
                yield event.plain_result("用法：/vip add <QQ> <天数> [群号]")
                return
            target_user = parts[2]
            try:
                days = float(parts[3])
            except ValueError:
                yield event.plain_result("天数要填数字，比如 4.5。")
                return
            target_group = parts[4] if len(parts) >= 5 else str(event.get_group_id() or "")
            if not target_group:
                yield event.plain_result("请指定群号：/vip add <QQ> <天数> <群号>")
                return
            expires = self.vip.add(target_group, target_user, days)
            yield event.plain_result(
                f"已给 {target_user} 开 {days} 天 VIP（群 {target_group}），到期 {_fmt_ts(expires)}。"
            )
            return

        yield event.plain_result(
            "未知操作。用法：/vip add <QQ> <天数> [群号] | /vip del <QQ> [群号] | /vip list"
        )
