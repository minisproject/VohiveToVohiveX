# VoHive → VoHiveX 迁移工具

把 [openvohive](https://github.com/openvohive/openvohive) 的配置与数据迁移到
[VoHiveX](https://github.com/NXN-MAX/VoHiveX)。

## 为什么能做整库迁移

VoHiveX 的内置核心是**上游 vohive 的预编译二进制**（`release/vohivex-*`，UPX 压缩），
与 openvohive **共用同一套 `data/vohive.db`**（同一组 GORM 模型）。

实测三代 schema：

| 版本 | 表数 | 特有内容 |
|---|---|---|
| 上游 v1.5.5 核心 | 21 | `card_policies.vowifi_enabled`、`sms_delivery_part.sip_code` |
| 新版生产核心 | 27 | `card_policies.ims_mode`/`ims_profile_index`、`sms.bearer`、整套 `sip_*`、`aka_identity_records`、`home_identities` |
| openvohive | 14 | 以上都没有 |

所以正确做法是**整库拷贝**，核心启动时 `AutoMigrate` 自动补齐它多出来的列与表。
逐表转换会丢数据，且会让历史短信变成只读归档（负 ID）。

## 快速开始

```sh
pip install pyyaml   # 或 apk add py3-yaml

# 1) 预览，不写盘
python3 migrate_openvohive_to_vohivex.py \
    --source /path/to/openvohive --target /path/to/vohivex

# 2) 执行
python3 migrate_openvohive_to_vohivex.py \
    --source /path/to/openvohive --target /path/to/vohivex \
    --apply --set-password 'YourNewPassword'

# 3) 复核
python3 migrate_openvohive_to_vohivex.py --target /path/to/vohivex --verify-only
```

只迁数据库（源里没有 config，生产库常常如此）：

```sh
python3 migrate_openvohive_to_vohivex.py \
    --source /path/to/data --target /path/to/vohivex --db-only --apply
```

## 数据库迁移模式

| `--db-mode` | 行为 | 短信 |
|---|---|---|
| `auto`（默认） | 目标空 → `copy`，否则 `merge` | 活数据 |
| `copy` | SQLite 在线备份整库覆盖 | 活数据 |
| `merge` | 逐表合并，按业务键判重 | 活数据 |
| `archive` | 只转成只读归档 `imported-sms.sqlite3` | 只读（负 ID） |
| `none` | 不动数据库 | — |

## 两个必须知道的坑

### 1. `sms.id` 是自增主键，不能按 id 判重

源库 id 从 1 开始，目标库 id 也可能是 1..N。直接 `INSERT OR IGNORE` 会让
**内容完全不同的短信因 id 冲突被静默丢弃**（实测：7 条全丢，脚本还报成功）。

工具按业务键判重，并省略 id 让目标重新分配：

| 类型 | 表 | 判重键 |
|---|---|---|
| 主键即业务键 | `devices`、`card_policies`、`sim_cards`、`sim_subscriptions`、`pending_phone_numbers`、`sms_contacts`、`sms_delivery` | 主键 |
| 自增 id | `sms` | `imsi+iccid+peer+content+type+timestamp` |
| 自增 id | `sms_delivery_part` | `message_id+part_no` |
| 自增 id | `traffic_*` | `period_start+resource+tag+direction` |

### 2. 短信内容可能是非 UTF-8 二进制

真实生产库里存在 MMS PDU（`application/vnd.wap.mms-message`）短信，含非法 UTF-8 字节。
Python 的 sqlite3 默认会抛 `Could not decode to UTF-8 column 'content'`，**中断整次迁移**。

工具对所有连接启用容错解码（`errors="replace"`），只影响读取，写入目标的字节不变。

## 其它约束

- **`server.port` 必须精确为 `127.0.0.1:7576`**。VoHiveX 网关用
  `fmt.Sprint(cfg.Server["port"]) == coreListen` 判断，不等就做文本替换，
  非标量形态会 `refusing automatic migration` 退出。
- **bcrypt 密码会被拒绝**。VoHiveX 核心是否支持 `$2a$`/`$2b$` 无法从二进制确认，
  需用 `--set-password` 或 `--password-env` 提供明文。
- **`card_policies` 会回填到设备条目**。openvohive 已把 APN/网络开关迁到卡级表，
  VoHiveX 核心仍读设备级字段。
- **运行时路径键被丢弃**。`usb_path`/`at_port`/`manage_port` 等 openvohive 已不再
  从配置文件加载；迁移后设备靠 15 位 `modem_imei` 匹配，缺失时需在 VoHiveX
  界面「设备发现」重新添加。

## 安全特性

- 源库以 `file:...?mode=ro` 只读打开 + SQLite 在线备份 API，**不修改源**
- 默认 dry-run，`--apply` 才落盘
- 目标配置原子写入（tmp + fsync + rename），`0600`
- 已有文件备份为 `*.bak-<时间戳>`，保留最近 5 份
- 不打印密码，`--password-env` 从环境变量取值

## 测试

`tests/make_fixture.py` 以真实核心生成的 schema 为基线，删掉核心独有的表和列后
灌入 openvohive 形状的数据，与真实 openvohive 库结构等价。

```sh
# 需要一个真实核心生成的 schema 作为基线
python3 tests/make_fixture.py /tmp/ovh-src /path/to/core-generated/vohive.db
python3 migrate_openvohive_to_vohivex.py --source /tmp/ovh-src --target /tmp/target --apply
```

已验证用例见 [MIGRATION.md](MIGRATION.md#测试)。

## 详细文档

完整的字段映射、模式语义、实测记录与限制说明见 [MIGRATION.md](MIGRATION.md)。

## 许可证

MIT（仅涵盖本仓库的工具代码；openvohive 与 VoHiveX 各自遵循其原有许可，
VoHiveX 为 PolyForm Noncommercial / 自定义非商业许可，商用需授权）。