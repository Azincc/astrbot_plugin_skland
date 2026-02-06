# AstrBot Plugin - 森空岛签到

[![Build and Release](https://github.com/Azincc/astrbot_plugin_skland/actions/workflows/release.yml/badge.svg)](https://github.com/Azincc/astrbot_plugin_skland/actions/workflows/release.yml)

森空岛自动签到插件，支持明日方舟和终末地签到。

## 功能

- **skd** (群聊): 查看群内所有绑定用户的签到状态
- **skd** (私聊): 查看自己的签到状态
- **skdlogin** (私聊): 使用 token 登录并立即签到
- **skdlogout** (私聊): 登出并移除 token

## 安装

### 方式一：从 Release 下载

1. 前往 [Releases](https://github.com/Azincc/astrbot_plugin_skland/releases) 页面
2. 下载最新的 `astrbot_plugin_skland-vX.X.X.zip`
3. 解压到 AstrBot 的 `plugins` 目录

### 方式二：使用 Git

```bash
cd /path/to/astrbot/plugins
git clone https://github.com/Azincc/astrbot_plugin_skland.git
```

## 依赖

插件依赖已在 `requirements.txt` 中列出，AstrBot 会自动安装。

## 使用

### 获取 Token

1. 登录 [森空岛](https://www.skland.com/)
2. 访问 https://web-api.skland.com/account/info/hg
3. 找到返回的 JSON 中的 `{"content":"XXX"}`
4. 复制 `XXX` 部分（这就是你的 token）

### 登录与签到

1. 私聊发送 `/skdlogin <token>` 进行登录（将 `<token>` 替换为上面获取的 XXX）
2. 登录成功后会自动执行一次签到
3. 之后可以发送 `/skd` 查看签到状态

## 许可

MIT License
