# quota_guard · Token 额度与限时 VIP

> 按群、按人限制每天能烧多少 token；超额在请求前拦下。
> 支持「限时 VIP」：某个群的某个成员，在一段时间内不受单用户限额约束，到期自动恢复。

独立实现，**不依赖、不修改任何其他插件**。

## 功能

- 每群每日 token 上限
- 每人每日 token 上限
- 用量来自模型返回的**真实 token 数**（`LLMResponse.usage`），不是估算
- 超额时在 LLM 请求前拦截，可配置提示语
- 永久豁免名单（不区分群）
- 限时 VIP：按群 + 人 + 到期时间，到点自动失效
- `/token` 查当日用量，`/vip` 管理名单（管理员）

## 安装

插件市场搜「Token 额度管家」，或：

```bash
cd AstrBot/data/plugins
git clone https://github.com/QuanWander/astrbot_plugin_quota_guard
```

重启 AstrBot 即可，无需其他依赖。

## 配置

| 项 | 说明 | 默认 |
|---|---|---|
| `enable` | 总开关 | `true` |
| `groups` | 生效的群号，留空=所有群 | `[]` |
| `group_daily_limit` | 每群每日上限 | `3000000` |
| `user_daily_limit` | 每人每日上限 | `250000` |
| `reset_hour` | 每天几点清零 | `0` |
| `exempt_users` | 永久豁免的 QQ 列表 | `[]` |
| `notify_blocked` | 拦截时是否提示 | `true` |
| `block_message` | 提示语 | `今天的额度用完了，明天再来吧。` |
| `apply_private` | 私聊是否也限额 | `false` |
| `user_limit_scope` | 个人额度按群算还是全局算 | `global` |

## 指令

```
/token                       查看今日用量（本群 / 自己 / VIP 剩余）
/vip add <QQ> <天数> [群号]   开限时 VIP，支持小数：4.5 = 108 小时
/vip del <QQ> [群号]         移除
/vip list                    列出全部
```

`/vip` 需要管理员权限（AstrBot 配置里的 `admins_id`）。

## 数据文件

`AstrBot/data/plugin_data/astrbot_plugin_quota_guard/`

- `usage.json` —— 当日用量，跨逻辑日期自动清零
- `vip.json` —— VIP 名单，`expires_at` 是 Unix 时间戳，过期自动失效

两个文件都可以手改，**改完不用重启**（每次判断都会重新读取）。

## 和「Token 流量控制」的关系

本插件是独立实现，功能和它不冲突，但也**不能同时启用**——两个插件都拦截请求，
谁先拦到谁生效，容易排查困难。

如果你原来用的是 `astrbot_plugin_token_controller`，迁移步骤：

1. 记下它的群号列表和两个限额；
2. 填进本插件的 `groups` / `group_daily_limit` / `user_daily_limit`；
3. 停用原插件，启用本插件。

本插件没有它的可视化统计面板、模型阶梯回退等高级功能，只做限额和 VIP。

## 统计口径

**群用量**：该群所有请求的 token 之和（含所有成员）。

**个人用量**：由 `user_limit_scope` 决定——

| 模式 | 含义 |
|---|---|
| `global`（默认） | 同一个 QQ 在**所有群**的用量合并计算，一份 25 万额度 |
| `group` | 每个群各有一份额度，在 9 个群就是 9 × 25 万 |

**私聊**：默认既不计数也不限额，避免私聊消耗影响群额度。
想要私聊也管，把 `apply_private` 打开——此时私聊计入全局个人用量，并同样受限额约束。

**非 QQ 号的事件**（如系统通知 `qq_notice_response:<hash>`）只计入群总量，不计入个人。

**重置**：按 `reset_hour` 切逻辑日期，比如设 4，凌晨 2 点仍算前一天。

## License

MIT

## 1.1 新增配置

| 字段 | 类型 | 说明 |
|---|---|---|
| `over_limit_action` | string | 超额后：`silent` 静默 / `notify_once` 每天提示一次（默认）/ `notify` 每次都提示 |
| `warn_ratio` | float | 用量预警阈值，默认 0.8，填 0 关闭 |
| `warn_message` | text | 预警文案，可用 `{percent}` `{used}` `{limit}` |
| `price_in` | float | 未命中输入单价（元/百万），默认 1.0 |
| `price_cached` | float | 缓存输入单价（元/百万），默认 0.02 |
| `price_out` | float | 输出单价（元/百万），默认 8.0 |

`/token` 末尾会显示「折合（估算）」，按上面的单价把输入、缓存、输出分别折算。
