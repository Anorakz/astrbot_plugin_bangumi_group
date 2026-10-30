<div align="center">

# Bangumi 群友动态

[![AstrBot](https://img.shields.io/badge/AstrBot-%3E%3D4.26.2-blue)](https://github.com/AstrBotDevs/AstrBot)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)

**看看群里谁最近在追番**

</div>

让群友把自己的 Bangumi 账号绑定到机器人，然后一键查询「最近 24 小时谁在追番」。

和 [astrbot_plugin_bangumi](https://github.com/united-pooh/astrbot_plugin_bangumi)（按**番剧**订阅）不同，
本插件是**按人**的维度：谁最近更新了进度、谁把某部番标成了「在看」。

---

## 命令

| 命令 | 别名 | 说明 |
|:-----|:-----|:-----|
| `/bgm绑定 <用户名>` | `/bgm_bind` | 绑定你的 Bangumi 账号（用户名就是个人主页地址里的最后一段） |
| `/bgm解绑` | `/bgm_unbind` | 解除绑定（管理员可 `@某人` 解除他人） |
| `/bgm我` | `/bgm_me` | 看看我近 24 小时在追什么 |
| `/bgm查 @群友` | `/bgm_query` | 看某位群友近 24 小时的动态 |
| `/bgm查 <用户名>` | | 直接查任意账号，无需绑定 |
| `/bgm群报` | `/bgm_group` | 汇总全群近 24 小时谁在追番 |
| `/bgm帮助` | `/bgm_help` | 显示帮助 |

### 时间窗参数

查询类命令（`/bgm我`、`/bgm查`、`/bgm群报`）都支持在末尾加 `-N` 把时间窗放大：
默认是**近 24 小时**，每 +1 再加 24 小时，即 `-N` = 近 `24 × (N+1)` 小时。

| 写法 | 时间窗 |
|:-----|:-------|
| `/bgm我`（默认） | 近 24 小时 |
| `/bgm查 @群友 -1` | 近 48 小时 |
| `/bgm查 @群友 -3` | 近 96 小时（4 天） |
| `/bgm群报 -7` | 近 192 小时（8 天） |

`-N` 也可以紧跟名字写：`/bgm查 @群友-3` 与 `/bgm查 @群友 -3` 等价；
指令名后面漏打空格（如 `/bgm查@群友 -3`）也能识别。

### 输出示例

```
📊 Bangumi 群报 · 10-13 · 近 24 小时（10-12 21:00 → 10-13 21:00）
本群已绑定 3 人，每人一条

📖 Anorak · 近 24 小时有 2 条动态
21:04 [动画] 《葬送的芙莉莲》 标记为在看 · 进度到第 3 集
19:30 [动画] 《迷宫饭》 评分 8
📖 鲨鱼 · 近 24 小时有 1 条动态
12:11 [动画] 《胆大党》 标记为看过
```

---

## 判定口径

「有动态」= 该用户收藏条目的 `updated_at` 落在**查询的时间窗内**。
默认时间窗是「从此刻往前 24 小时」的滚动窗口，不按自然日切分——所以早上 9 点查和晚上 21 点查，覆盖的是各自往前 24 小时。

只要他在这段时间里在 Bangumi 上动过——推进了进度、改了评分、把某部番标成在看/看过——就会被算进来。

Bangumi 的收藏接口**默认按 `updated_at` 倒序**返回，所以只需拉前几页（默认 2 页 / 60 条）即可覆盖默认的 24 小时窗；
时间窗每宽一天多给一页预算（上限 10 页），因此即使用户有一两千条收藏，查近几天也不会把整份收藏翻完。

---

## 安装

### 方式一：AstrBot 插件市场

在 WebUI → 插件管理 → 安装插件，搜索 `astrbot_plugin_bangumi_group`。

### 方式二：手动 clone

```bash
docker exec astrbot sh -c \
  'cd /AstrBot/data/plugins && git clone --depth 1 \
   https://github.com/Anorakz/astrbot_plugin_bangumi_group.git'
docker compose restart astrbot
```

---

## 配置

在 AstrBot WebUI → 插件管理 → Bangumi 群友动态 → 配置：

| 配置项 | 默认值 | 说明 |
|:-------|:-------|:-----|
| `api_base` | `https://api.bgm.tv` | Bangumi API 地址（见下方说明） |
| `user_agent` | 插件默认值 | 请求头标识 |
| `timezone` | `Asia/Shanghai` | 时间显示与自然日窗口（`/番剧排名`）的时区 |
| `cache_ttl` | `300` | 同一用户的结果缓存秒数 |
| `max_pages` | `2` | 每人最多翻几页收藏 |
| `query_delay` | `0.5` | 群报时每人之间的请求间隔（秒） |
| `admin_uids` | `[]` | 管理员 QQ，可解除他人绑定 |

### 关于 api_base

插件默认请求官方地址 `https://api.bgm.tv`。

如果该地址在你的网络环境下无法访问（表现为请求超时或连接被重置），
把 `api_base` 改成你自己的 Bangumi API 反向代理地址即可，其余配置无需改动。

---

## 限制

- **只能读取公开收藏**。如果群友把收藏设为私密，接口拿不到数据；请在 Bangumi 的
  「设置 → 隐私」里把收藏改为公开。
- 本插件不使用 access token，仅调用公开接口。若走反向代理，请注意其自身的速率限制，
  人多时可适当调大 `cache_ttl` 和 `query_delay`。
- 本插件与 [united-pooh/astrbot_plugin_bangumi](https://github.com/united-pooh/astrbot_plugin_bangumi)
  **可以共存**，命令前缀不冲突（那边用 `/bgm`、`/追番`，这边用 `/bgm绑定`、`/bgm群报` 等）。

---

## 许可

MIT
