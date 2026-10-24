# tg-media-suite

A single-container pipeline that moves media out of Telegram and into a 115 cloud drive:
watch a channel, reply **转发** to a post, and the batch is re-uploaded to a target group,
then archived to 115 with a generated poster.

> 中文说明见下方 [中文文档](#中文文档)。

## Why

Telegram's Bot API caps uploads at 50 MB and strips some media metadata. Pulling the file
through a user session (MTProto) gets the original bytes. This suite does that
automatically: it watches where you reply, collects the whole batch, sends it through the
official bot, then hands the batch to a 115 uploader.

## Architecture

```
 Telegram channel / group / file bot
              │  you reply "转发" to a post
              ▼
        ┌─────────────┐   Telethon user session
        │  forwarder  │   resolves the batch, downloads oversized media
        └─────────────┘
              │  Bot API (small) or MTProto (large)
              ▼
        target group  ──── batch manifest (pending → complete)
              │
              ▼
        ┌─────────────┐   watches whitelisted senders only
        │  watch115   │   builds poster.jpg, uploads to 115
        └─────────────┘
              │
              ▼
          115 cloud drive
```

Both processes run under supervisord in one container. State lives in `/data`.

## Requirements

- A Telegram **user account** session (`TG_API_ID` / `TG_API_HASH`)
- One or more **file bots** that can serve original files (`/start getFile_<token>`)
- The official **115bot** running as a separate container — this suite reads its
  `application.properties` and SQLite DB read-only to obtain the bot token and 115 cookie.
  It never writes to them and never calls `getUpdates`, so it does not steal the bot's polling.

## Quick start

```bash
git clone https://github.com/<you>/tg-media-suite.git
cd tg-media-suite

cp .env.example .env
$EDITOR .env          # TG_API_ID, TG_API_HASH, FORWARDER_BOT_USERNAMES

mkdir -p data 115bot
# point these at your existing 115bot deployment:
#   115bot/application.properties
#   115bot/data/115bot.db

docker compose up -d --build
docker compose logs -f
```

First run creates the Telethon session inside `./data/forwarder`. If the account has 2FA,
watch the logs and enter the code/password once interactively:

```bash
docker compose exec tg-media-suite python /app/forwarder/manual_forwarder.py --login
```

## Commands

Send these to the official 115bot in a private chat; the suite observes your own messages
through the user session and replies via the Bot API.

| Command | Purpose |
| --- | --- |
| `/watch_menu` | Unified management keyboard |
| `/watch_target` / `/watch_target_set` | Show or set the shared target group |
| `/watch_users` | List senders seen in the target group |
| `/watch_user_add` / `/watch_user_remove` | Whitelist who may be archived |
| `/watch_state` | 115 login state, target folder, current batch |
| `/watch_pause` / `/watch_resume` / `/watch_retry` | Control the watcher |
| `/watch_cid` | Set the 115 folder id |
| `/forwarder_menu` | Add / list / remove sources |

## Behaviour worth knowing

- A batch is only archived once its manifest is `complete`, so one album never splits into
  several 115 folders.
- Media over the Bot API limit (49 MB here) is sent through MTProto instead.
- Staging is persistent: retries and container restarts never re-download finished files.
- Only whitelisted senders in the target group are picked up; the watcher starts from the
  newest message on first init so history is not imported by accident.
- Posters are generated with a three-step fallback: batch image → Telegram thumbnail →
  FFmpeg frame.

## Layout

```
source/forwarder/   reply-triggered batch forwarder (Telethon)
source/watch115/    target-group watcher + 115 uploader
source/archive/     optional NAS archiver (kept, not started by default)
container/          supervisord config + healthcheck
tests/              unittest suite, runs during docker build
```

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `TG_API_ID` / `TG_API_HASH` | — | Telegram user account credentials |
| `FORWARDER_BOT_USERNAMES` | — | Comma separated file bots, no `@` |
| `FORWARDER_FOLDER_BOT` | — | Which bot is the folder-mode bot |
| `FORWARDER_DATA_DIR` | `/data/forwarder` | Writable state |
| `SUITE_CONFIG_PATH` | `/data/config/config.json` | Shared config |
| `WATCH115_BOT_PROPERTIES` / `WATCH115_BOT_DB` | `/run/115bot/...` | Read-only 115bot files |
| `FORWARDER_DOWNLOAD_CONCURRENCY` | `3` | Parallel downloads per batch |
| `FORWARDER_DOWNLOAD_RETRIES` | `3` | Retries per file |

## Notes

- The image contains no credentials; everything sensitive is mounted at runtime.
- `source/archive/` is the older NAS path and is not started by the container. Drop it if you
  do not need it.
- 115 credentials are read from the official bot's database and never logged.

---

## 中文文档

### 这是什么

一条把 Telegram 媒体搬进 115 网盘的自动化管道：在频道里回复一条消息并发送 **转发**，
整批媒体会被重新发送到目标群，再由 `watch115` 收集、生成封面、上传到 115 指定目录。

### 为什么需要它

Bot API 单文件上限 50 MB，且部分媒体会被压缩。用用户账号走 MTProto 能拿到原始文件，
但手动搬运太麻烦。这个项目把「解析批次 → 下载 → 重发 → 归档 115」全串起来。

### 部署前提

1. 一个 Telegram **用户账号**（`TG_API_ID` / `TG_API_HASH`）
2. 一个或多个能取原始文件的**文件机器人**（发 `/start getFile_<token>` 取件）
3. 官方 **115bot** 以独立容器运行 —— 本项目只读它的 `application.properties` 和
   SQLite 数据库来拿 bot token 和 115 Cookie，不写入、不抢 `getUpdates`

### 三步启动

```bash
cp .env.example .env && $EDITOR .env   # 填 TG_API_ID / TG_API_HASH / 机器人用户名
mkdir -p data 115bot                   # 放入 115bot 的配置与数据库
docker compose up -d --build
```

首次启动会在 `./data/forwarder` 生成 Telethon session；账号开了两步验证的话，看日志
按提示输一次验证码即可。

### 日常使用

私聊官方 115bot 发送 `/watch_menu` 打开管理键盘，用 `/watch_target_set` 设目标群，
`/watch_user_add` 加白名单发送者，`/watch_cid` 设 115 目录。

之后在来源频道里回复任意媒体并发送「转发」，剩下的自动完成。

### 设计要点

- 批次清单 `complete` 之后才归档，一个相册不会拆成多个 115 目录
- 超过 49 MB 的文件改走 MTProto 直传，绕过 Bot API 限制
- staging 持久化，重试和重启都不会重复下载
- 只处理目标群白名单发送者的内容；首次初始化从最新消息开始，不会误导入历史
- 封面三级回退：批次图片 → Telegram 缩略图 → FFmpeg 截帧

## License

MIT
