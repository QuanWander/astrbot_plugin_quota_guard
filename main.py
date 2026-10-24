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
from astrbot.api.star import Context, Star, register

PLUGIN_NAME = "astrbot_plugin_quota_guard"
DEFAULT_BLOCK_MESSAGE = "今天的额度用完了，明天再来吧。"
DEFAULT_WARN_MESSAGE = "本群今天的额度已用 {percent}%（{used} / {limit}），到顶后就不再回复了。"


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
    """按逻辑日期保存的用量计数，附带输入/缓存/输出明细（用于成本估算）。"""

    def __init__(self, path: Path):
        self.path = path
        self.day = ""
        self.groups: dict[str, int] = {}
        self.users: dict[str, int] = {}
        self.detail: dict[str, dict[str, int]] = {}

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
            for k, v in (data.get("detail") or {}).items():
                if isinstance(v, dict):
                    self.detail[str(k)] = {
                        "i": int(v.get("i", 0) or 0),
                        "c": int(v.get("c", 0) or 0),
                        "o": int(v.get("o", 0) or 0),
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
                    {
                        "day": self.day,
                        "groups": self.groups,
                        "users": self.users,
                        "detail": self.detail,
                    },
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
            self.detail = {}
            self._save()

    def add(
        self,
        group_id: str,
        user_id: str,
        tokens: int,
        reset_hour: int,
        in_other: int = 0,
        in_cached: int = 0,
        out: int = 0,
    ) -> None:
        if tokens <= 0:
            return
        self.ensure_day(reset_hour)
        if group_id:
            self.groups[group_id] = self.groups.get(group_id, 0) + tokens
        if user_id and user_id.isdigit():
            if group_id:
                key = f"{group_id}:{user_id}"
                self.users[key] = self.users.get(key, 0) + tokens
            gkey = f"*:{user_id}"
            self.users[gkey] = self.users.get(gkey, 0) + tokens

        def bump(key: str) -> None:
            row = self.detail.setdefault(key, {"i": 0, "c": 0, "o": 0})
            row["i"] += max(0, int(in_other))
            row["c"] += max(0, int(in_cached))
            row["o"] += max(0, int(out))

        if group_id:
            bump(f"g:{group_id}")
        if user_id and user_id.isdigit():
            if group_id:
                bump(f"u:{group_id}:{user_id}")
            bump(f"u:*:{user_id}")
        self._save()

    def group_total(self, group_id: str) -> int:
        return self.groups.get(str(group_id), 0)

    def user_total(self, group_id: str, user_id: str) -> int:
        return self.users.get(f"{group_id}:{user_id}", 0)

    def user_global_total(self, user_id: str) -> int:
        """该 QQ 在当天所有群 + 私聊的 token 总和。"""
        return self.users.get(f"*:{user_id}", 0)

    def group_detail(self, group_id: str) -> dict[str, int]:
        return self.detail.get(f"g:{group_id}", {"i": 0, "c": 0, "o": 0})

    def user_detail(self, group_id: str, user_id: str) -> dict[str, int]:
        return self.detail.get(f"u:{group_id}:{user_id}", {"i": 0, "c": 0, "o": 0})

    def user_global_detail(self, user_id: str) -> dict[str, int]:
        return self.detail.get(f"u:*:{user_id}", {"i": 0, "c": 0, "o": 0})


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
    "1.1.1",
)
class TokenGuard(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config or {}
        base = Path(__file__).resolve().parents[2] / "plugin_data" / PLUGIN_NAME
        self.usage = UsageStore(base / "usage.json")
        self.vip = VipStore(base / "vip.json")
        self.usage._load()
        self._notice_path = base / "notices.json"
        self._notices = self._load_notices()

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

    # ------------------------------------------------------------------ 提示状态
    def _load_notices(self) -> dict:
        today = _logical_day(self._reset_hour())
        try:
            data = json.loads(self._notice_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or data.get("day") != today:
                raise ValueError("stale")
            data.setdefault("block", [])
            data.setdefault("warn", [])
            return data
        except Exception:
            return {"day": today, "block": [], "warn": []}

    def _save_notices(self) -> None:
        try:
            self._notice_path.parent.mkdir(parents=True, exist_ok=True)
            self._notice_path.write_text(
                json.dumps(self._notices, ensure_ascii=False), encoding="utf-8"
            )
        except Exception as exc:
            logger.debug(f"[quota_guard] 保存提示状态失败: {exc}")

    def _mark_notice(self, kind: str, key: str) -> bool:
        """当天第一次返回 True，之后返回 False。"""
        today = _logical_day(self._reset_hour())
        if self._notices.get("day") != today:
            self._notices = {"day": today, "block": [], "warn": []}
        bucket = self._notices.setdefault(kind, [])
        if key in bucket:
            return False
        bucket.append(key)
        self._save_notices()
        return True

    # ------------------------------------------------------------------ 成本估算
    def _price(self, key: str, default: float) -> float:
        try:
            return float(self._cfg(key, default))
        except (TypeError, ValueError):
            return default

    def _cost(self, detail: dict) -> float:
        i = int(detail.get("i", 0) or 0)
        c = int(detail.get("c", 0) or 0)
        o = int(detail.get("o", 0) or 0)
        return (
            (i / 1_000_000) * self._price("price_in", 1.0)
            + (c / 1_000_000) * self._price("price_cached", 0.02)
            + (o / 1_000_000) * self._price("price_out", 8.0)
        )

    async def _maybe_warn(self, event: AstrMessageEvent, group_id: str) -> None:
        if not group_id:
            return
        try:
            ratio = float(self._cfg("warn_ratio", 0.8))
        except (TypeError, ValueError):
            ratio = 0.8
        if ratio <= 0:
            return
        limit = self._group_limit()
        total = self.usage.group_total(group_id)
        if limit <= 0 or total < limit * ratio:
            return
        if not self._mark_notice("warn", group_id):
            return
        try:
            text = str(self._cfg("warn_message", DEFAULT_WARN_MESSAGE)).format(
                percent=int(total * 100 / limit), used=_human(total), limit=_human(limit)
            )
            await event.send(event.plain_result(text).chain)
            logger.info(f"[quota_guard] 群 {group_id} 用量 {total}/{limit}，已发预警")
        except Exception as exc:
            logger.debug(f"[quota_guard] 发送预警失败: {exc}")

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
            await self._maybe_warn(event, group_id)
            return

        event.stop_event()
        logger.info(
            f"[quota_guard] 拦截 reason={reason} group={group_id} user={user_id} "
            f"群用量={self.usage.group_total(group_id)} 个人用量={user_used} scope={scope}"
        )

        action = str(self._cfg("over_limit_action", "notify_once")).lower()
        if action not in ("silent", "notify", "notify_once"):
            action = "notify" if self._cfg("notify_blocked", True) else "silent"
        if action == "silent":
            return
        if action == "notify_once" and not self._mark_notice("block", group_id or user_id):
            return
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
            in_other = int(getattr(usage, "input_other", 0) or 0)
            in_cached = int(getattr(usage, "input_cached", 0) or 0)
            out = int(getattr(usage, "output", 0) or 0)
            try:
                weight = float(self._cfg("cached_token_weight", 1.0))
            except (TypeError, ValueError):
                weight = 1.0
            # 缓存 token 的实际单价远低于未命中，按权重折算后再计入额度
            tokens = in_other + int(in_cached * max(0.0, weight)) + out
            if not tokens:
                tokens = int(getattr(usage, "total", 0) or 0)
        except (TypeError, ValueError):
            return
        group_id = str(event.get_group_id() or "")
        user_id = str(event.get_sender_id() or "")
        # 私聊默认既不拦截也不计数，避免私聊消耗影响群额度
        if not group_id and not self._cfg("apply_private", False):
            return
        self.usage.add(
            group_id,
            user_id,
            tokens,
            self._reset_hour(),
            in_other=in_other,
            in_cached=in_cached,
            out=out,
        )

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

        # 成本估算：按配置单价把输入/缓存/输出分别折算
        cost_parts = []
        if group_id:
            cost_parts.append(f"本群 ¥{self._cost(self.usage.group_detail(group_id)):.2f}")
        if user_id:
            if scope == "global" or not group_id:
                ud = self.usage.user_global_detail(user_id)
            else:
                ud = self.usage.user_detail(group_id, user_id)
            cost_parts.append(f"你 ¥{self._cost(ud):.2f}")
        if cost_parts:
            lines.append("折合（估算）：" + " / ".join(cost_parts))
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
