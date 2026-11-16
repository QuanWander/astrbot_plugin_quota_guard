# 可移植性说明

## 核心（可迁移的规则）

- 群/用户日限额
- 峰谷价、缓存 token 权重
- 限时 VIP 豁免
- 超限提示与静默策略

## AstrBot 壳（不直接搬）

- `filter.on_waiting_llm_request`
- `filter.on_llm_response`
- `AstrMessageEvent` / `LLMResponse.usage`

## DSH 迁移

- 不把插件本身搬成 DSH 插件
- 规则迁到模型网关或 DSH 预算服务
- 现有 AstrBot 插件在网关完成前继续保留

## 不迁移

- AstrBot 命令 `/token`、页面注入
