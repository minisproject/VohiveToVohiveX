# openvohive → VoHiveX 迁移

脚本：`migrate_openvohive_to_vohivex.py`（Python 3.9+，需 PyYAML）

## 关键前提（实测确认）

VoHiveX 的内置核心是**上游 vohive 的预编译二进制**（`release/vohivex-*`，UPX 压缩，
由 `app/patch-release.py` 打补丁），与 openvohive **共用同一套 `data/vohive.db`**（同一组 GORM 模型）。

实测三代 schema：

| 版本 | 特有内容 |
|---|---|
| 上游 v1.5.5 核心（本机实跑） | 21 表；`card_policies.vowifi_enabled`、`sms_delivery_part.sip_code` |
| 生产库 `/tmp/data`（新版核心） | 27 表；`card_policies.ims_mode` / `ims_profile_index`、`sms.bearer`、整套 `sip_*` / `aka_identity_records` / `home_identities` |
| openvohive（本仓库） | 14 表；上述列与表都没有 |

结论：**整库拷贝才是正确做法**，逐表转换会丢数据。核心启动时 `AutoMigrate`
自动补齐它多出来的列与表，源缺列由目标保留默认值。

实测验证（用真实生产库 + 真实核心二进制）：

```
INFO  vohive/main.go:92   数据库已初始化 {"path": "data/vohive.db", ...}
INFO  vohive/main.go:220  所有服务已启动
```

迁移后原库 118 条短信**零丢失、零改动**，新增 7 条，总数 125 条，
`PRAGMA integrity_check` 返回 `ok`，API 能正确读出新增短信。

## 快速开始

```sh
# 1) 预览
python3 migrate_openvohive_to_vohivex.py --source /path/to/openvohive --target /path/to/vohivex

# 2) 执行
python3 migrate_openvohive_to_vohivex.py --source /path/to/openvohive --target /path/to/vohivex \
    --apply --set-password 'YourNewPassword'

# 3) 复核
python3 migrate_openvohive_to_vohivex.py --target /path/to/vohivex --verify-only

# 只迁数据库（源里没有 config，或不想动目标配置）
python3 migrate_openvohive_to_vohivex.py --source /path/to/data --target /path/to/vohivex \
    --db-only --apply
```

## 数据库迁移模式（`--db-mode`）

| 模式 | 行为 | 短信可用性 | 适用场景 |
|---|---|---|---|
| `auto`（默认） | 目标无有效数据 → `copy`；有数据 → `merge` | 活数据 | 多数情况 |
| `copy` | SQLite 在线备份整库覆盖 | 活数据 | 目标全新部署 |
| `merge` | 逐表合并，自动按业务键判重 | 活数据 | 目标已跑过一段时间 |
| `archive` | 只把短信转成 `imported-sms.sqlite3` 只读归档 | 只读（负 ID） | 保守方案 |
| `none` | 完全不动数据库 | — | 只迁配置 |

### merge 的判重策略（重要）

按「主键是否即业务键」分两类处理：

| 类型 | 表 | 策略 |
|---|---|---|
| 主键即业务键 | `devices`(imei)、`card_policies`(iccid)、`sim_cards`(iccid)、`sim_subscriptions`(imsi)、`pending_phone_numbers`(iccid)、`sms_contacts`(imsi+peer)、`sms_delivery`(message_id) | `INSERT OR IGNORE`，主键冲突即真重复 |
| 自增 id，业务键另有其列 | `sms`(imsi+iccid+peer+content+type+timestamp)、`sms_delivery_part`(message_id+part_no)、`traffic_*`(period_start+resource+tag+direction) | **按业务键判重，插入时省略 id 让目标重新分配** |

**为什么必须这样**：`sms.id` 是自增主键，源库 id 从 1 开始，目标库 id 也是 1..118。
若直接 `INSERT OR IGNORE`，源库那 7 条**内容完全不同**的短信会因为 id 冲突被
全部静默丢弃 —— 这是用真实生产库测出来的真实事故，不是理论风险。

修复后实测：

```
sms: 源 7 行 -> 新增 7，跳过 0
目标 id 范围 (1, 126)，原 118 条内容零改动
重复执行：sms: 源 7 行 -> 新增 0，跳过 7
```

## 真实数据中发现的兼容问题

### 1. 非 UTF-8 的短信内容

生产库里有 2 条短信内容是 MMS PDU 二进制（`application/vnd.wap.mms-message`），
含非法 UTF-8 字节。Python 的 sqlite3 默认会抛
`Could not decode to UTF-8 column 'content'`，导致**整次迁移中断**。

处理：所有连接统一设 `text_factory` 容错解码（`errors="replace"`），
只影响读取，不改动写入目标的字节。实测这 2 条记录迁移后长度与内容不变。

### 2. GORM 的时间格式

openvohive 用 GORM SQLite 驱动，写 `2026-11-27 09:00:00.123456789+08:00`
（空格 + 9 位纳秒 + 时区）。整库迁移时**原样保留**即可（同源模型，核心能解析）。
但 `--db-mode archive` 转归档时必须改成 RFC3339，因为 VoHiveX 的
`timestampMillis` 只接受 RFC3339，否则整批判为「短信时间格式无效」。

### 3. 源库可能没有 config

真实生产库常常只剩 `data/vohive.db`（配置在别处或已删）。脚本允许
`--source` 直接指向 data 目录或 `.db` 文件，此时自动降级为只迁数据库，
并在纯库迁移时不把「目标缺配置」判为错误（用 `--db-only` 显式声明更清晰）。

## 配置迁移

| 源 | 目标 | 处理 |
|---|---|---|
| `server.port` | `server.port` | **强制改为 `127.0.0.1:7576`** |
| `server.debug` | 同名 | 透传 |
| `web.username` / `web.password` | 同名 | 透传；bcrypt 报错要求明文 |
| `devices[]` | 同名 | 透传，丢弃 7 个运行时路径键 |
| `card_policies`(库) | `devices[].apn/network_enabled/ip_version/airplane_enabled` | 按 IMEI→ICCID 回填 |
| `telegram` / `webhook` / `email` | 同名 | 整段透传 |
| `proxy` / `vowifi` / `voice` | — | 丢弃并告警 |

**`server.port` 必须精确为 `127.0.0.1:7576`**：VoHiveX 的
`cmd/vohivex-gateway/config.go:prepareConfig` 用
`fmt.Sprint(cfg.Server["port"]) == coreListen` 判断，不等就做文本替换，
非标量形态会直接 `refusing automatic migration` 退出。脚本生成后经
`yaml.safe_load` 自检确认 `str(port)` 精确匹配。

**为什么回填 `card_policies`**：openvohive 已把 APN/网络开关迁到卡级表，
VoHiveX 核心仍读设备级字段。

## 不迁移的内容

| 项 | 原因 |
|---|---|
| 定时短信 `scheduled-sms.sqlite3` | VoHiveX 自有结构，需在界面重建 |
| 通话记录 `calls.sqlite3` | 同上 |
| Mihomo 订阅与节点 | VoHiveX 用 Mihomo，无对应结构 |

注意：`sms_delivery*` 和 `traffic_*` 在 `copy` / `merge` 模式下**会完整迁移**，
只有 `archive` 模式才丢失。

## 安全约束

- 源库以 `file:...?mode=ro` 只读打开 + SQLite 在线备份 API，**不修改源**；
- 目标配置原子写入（tmp + fsync + rename），`0600`；
- 已有文件备份为 `*.bak-<时间戳>`，保留最近 5 份；
- 不打印密码；`--password-env` 从环境变量取值，避免进 shell 历史；
- 迁移后清理空 `-wal` / `-shm`。

## 校验（`--verify-only`）

1. `server.port` 是否为 `127.0.0.1:7576`
2. `web` 凭据是否存在、是否默认弱口令、是否 bcrypt
3. `devices[]` 合法性、缺 `modem_imei` 数量、`modem_imei` 重复
4. `data/vohive.db` 表数/行数、缺列提示、无 ICCID 短信数
5. 归档库可读性、孤儿 `device_id`

`--db-only` 时跳过 1–3（纯库迁移不要求目标有配置）。

退出码：`0` 无错误 / `1` 有错误 / `2` 参数错误。

## 测试

`tests/make_fixture.py` 以**真实核心生成的 schema 为基线**，删掉核心独有的
表和列后灌入 openvohive 形状的数据（含 GORM 纳秒时间、RFC3339、无时区、
非法方向、坏时间五种短信形态），与真实 openvohive 库结构等价。

已验证用例：

| 用例 | 结果 |
|---|---|
| `copy` / `merge` / `archive` / `none` 全模式 | 退出码 0 ✓ |
| dry-run 不落盘 | 目标目录 0 项 ✓ |
| **merge 到真实生产库副本** | 原 118 条零丢失，新增 7 条 ✓ |
| merge 幂等 | 源 7 行 → 新增 0、跳过 7 ✓ |
| merge 增量 | 源 +1 行 → 新增 1、跳过 6 ✓ |
| 真实核心启动迁移后库 | `数据库已初始化` + `所有服务已启动` ✓ |
| 核心 AutoMigrate 补列/表 | `ims_mode`、`bearer`、`sip_*` 正常 ✓ |
| 核心 API 读新增短信 | 正确返回 ✓ |
| 非 UTF-8 短信内容 | 容错解码，字节不变 ✓ |
| 无 config 的纯库迁移 | `--db-only` 正常 ✓ |
| bcrypt 密码 | 报错要求明文 ✓ |
| `--set-password` / `--password-env` | 正确覆盖 ✓ |
| `--keep-target-credentials` | 保留目标凭据 ✓ |
| 备份轮转 | 8 次 apply 后仅留 5 份 ✓ |
| 时间解析 | 秒/毫秒/微秒/ISO/无时区/非法 6 类正确 ✓ |

## 限制

- 未在**真实 openvohive 生产实例**上端到端跑过（用真实生产库副本 + 真实核心验证过）；
- 核心是 UPX 压缩二进制，`web.password` 对 bcrypt 的支持、以及
  `telegram`/`webhook`/`email` 段的确切字段名无法从二进制确认，故透传 +
  遇 bcrypt 报错；
- 设备能否真正启动取决于宿主是否插着模组，迁移只保证配置与数据完整。

## 迁移后手工步骤

1. 停 openvohive，启动 VoHiveX，用迁移后的账号登录 `7575`
2. 检查「设备」页：设备未运行时用「设备发现」重新添加（需 `usb_path`/`at_port`）
3. 检查通知：Telegram/Webhook/Email 已透传；飞书/QQ/Bark/Pushplus 需补配
4. 定时短信、通话记录、Mihomo 订阅需重配
5. 立即修改默认弱口令