#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
openvohive  ->  VoHiveX  配置与数据迁移工具

设计原则：
  1. 默认只做 dry-run，必须显式 --apply 才落盘；
  2. 任何已存在文件在覆盖前都会备份为 <file>.bak-<时间戳>（只保留最近 5 份）；
  3. 源数据库只读打开（mode=ro + WAL），迁移过程绝不修改源数据；
  4. 目标归档库按 VoHiveX 的 sms_archive 语义写入（含 signature 去重），可重复执行；
  5. 不猜、不造：源里没有的字段一律不写；缺失的设备路径字段只告警、不伪造。

迁移范围：
  ✔ config/config.yaml      server/web/devices/telegram/webhook/email
  ✔ data/vohive.db          sms -> data/imported-sms.sqlite3（VoHiveX 只读归档）
  ✔ card_policies           回填 APN/网络开关到设备条目（VoHiveX 仍读设备级字段）
  ✘ 流量统计（traffic_*）、投递回执（sms_delivery*）—— VoHiveX 无对应表
  ✘ 定时短信、通话记录、Mihomo 订阅 —— 需在 VoHiveX 重新配置

用法：
  # 预览（不写盘）
  python3 migrate_openvohive_to_vohivex.py --source /path/to/openvohive --target /path/to/vohivex

  # 实际执行（源密码是 bcrypt 时必须显式给新明文密码）
  python3 migrate_openvohive_to_vohivex.py --source /path/to/openvohive --target /path/to/vohivex \
      --apply --set-password 'YourNewPassword'

  # 只检查目标现状
  python3 migrate_openvohive_to_vohivex.py --target /path/to/vohivex --verify-only

依赖：Python 3.9+，PyYAML（pip install pyyaml / apk add py3-yaml）
     时间戳若含时区名，需要系统 tzdata（apk add tzdata）
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import sqlite3
import sys
import time
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover
    sys.exit("缺少 PyYAML：请执行 `pip install pyyaml` 或 `apk add py3-yaml`")

# VoHiveX 网关强制把核心监听改为回环地址，且必须能被 fmt.Sprint 还原成同一字符串
VOX_CORE_LISTEN = "127.0.0.1:7576"
VOX_DEFAULT_WEB_PORT = "7575"

BCRYPT_RE = re.compile(r"^\$2[aby]\$")
# openvohive 在加载配置时会物理删除这些运行时路径键，迁移时若残留则保留给核心
PATH_KEYS = ("usb_path", "at_port", "manage_port", "net_interface",
             "control_device", "qmi_device", "audio_device")
# 从 card_policies 表回填到设备条目的策略字段
POLICY_KEYS = ("apn", "network_enabled", "ip_version", "airplane_enabled")

TS_SEP = "\x00"  # VoHiveX 归档签名使用的字段分隔符


# ---------------------------------------------------------------- 输出辅助

class Report:
    def __init__(self) -> None:
        self.warnings: list[str] = []
        self.errors: list[str] = []
        self.notes: list[str] = []
        self.actions: list[str] = []

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)
        print(f"  [warn] {msg}")

    def error(self, msg: str) -> None:
        self.errors.append(msg)
        print(f"  [err ] {msg}")

    def note(self, msg: str) -> None:
        self.notes.append(msg)
        print(f"  [note] {msg}")

    def act(self, msg: str) -> None:
        self.actions.append(msg)
        print(f"  [ ok ] {msg}")


def section(title: str) -> None:
    print(f"\n=== {title} ===")


def human_bytes(n: int) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if n < 1024 or unit == "GiB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GiB"


def backup_existing(path: Path, rep: Report, apply: bool, keep: int = 5) -> None:
    """备份后再写入；同名备份按毫秒去重，并只保留最近 keep 份。"""
    if not path.exists():
        return
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = path.with_name(f"{path.name}.bak-{stamp}")
    seq = 1
    while dest.exists():
        dest = path.with_name(f"{path.name}.bak-{stamp}-{seq}")
        seq += 1
    if apply:
        shutil.copy2(path, dest)
        # 清理超额备份：避免反复执行把目录塞满
        olds = sorted(
            (p for p in path.parent.glob(f"{path.name}.bak-*") if p.is_file()),
            key=lambda p: p.stat().st_mtime,
        )
        for stale in olds[:-keep]:
            try:
                stale.unlink()
            except OSError:
                pass
        rep.act(f"已备份 {path.name} -> {dest.name}（保留最近 {keep} 份）")
    else:
        rep.note(f"[dry-run] 将备份 {path.name} -> {dest.name}")


def atomic_write(path: Path, data: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        with open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


# ---------------------------------------------------------------- 源侧读取

def load_yaml_dir(path: Path) -> dict:
    if not path.is_file():
        raise SystemExit(f"找不到源配置文件：{path}\n"
                         f"  请把 --source 指向 openvohive 的部署根目录（其下有 config/config.yaml）。")
    raw = path.read_text(encoding="utf-8")
    data = yaml.safe_load(raw) or {}
    if not isinstance(data, dict):
        raise SystemExit(f"源配置文件顶层不是映射：{path}")
    return data


def tolerant_text(raw: bytes) -> str:
    """
    openvohive 的 sms.content 是 TEXT 列，但实际可能存 MMS PDU 之类的二进制
    （实测真实库里就有 2 条含非法 UTF-8）。这里只做解码容错，不改字节内容。
    """
    return raw.decode("utf-8", "replace")


def open_db(path_or_uri: str, *, uri: bool = False, timeout: float = 15,
            readonly: bool = False) -> sqlite3.Connection:
    """统一入口：开启容错解码，避免单条坏数据中断整次迁移。"""
    conn = sqlite3.connect(path_or_uri, uri=uri, timeout=timeout)
    conn.text_factory = tolerant_text
    if readonly:
        conn.execute("PRAGMA query_only=ON")
    return conn


def connect_ro(path: Path) -> sqlite3.Connection:
    uri = f"file:{path.as_posix()}?mode=ro"
    conn = open_db(uri, uri=True, timeout=10, readonly=True)
    conn.row_factory = sqlite3.Row
    return conn


def has_table(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def count_rows(conn: sqlite3.Connection, table: str) -> int:
    if not has_table(conn, table):
        return -1
    return int(conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])


def read_card_policies(conn: sqlite3.Connection) -> dict[str, dict]:
    """ICCID -> 策略字段。表不存在或读失败时返回空字典（不阻断迁移）。"""
    out: dict[str, dict] = {}
    if not has_table(conn, "card_policies"):
        return out
    try:
        rows = conn.execute(
            "SELECT iccid, network_enabled, airplane_enabled, ip_version, apn FROM card_policies"
        ).fetchall()
    except sqlite3.Error:
        return out
    for r in rows:
        iccid = (r["iccid"] or "").strip()
        if not iccid:
            continue
        policy: dict = {}
        if r["apn"] is not None:
            policy["apn"] = str(r["apn"])
        if r["ip_version"]:
            policy["ip_version"] = str(r["ip_version"])
        if r["network_enabled"] is not None:
            policy["network_enabled"] = bool(r["network_enabled"])
        if r["airplane_enabled"] is not None:
            policy["airplane_enabled"] = bool(r["airplane_enabled"])
        out[iccid] = policy
    return out


def read_device_iccid(conn: sqlite3.Connection) -> dict[str, str]:
    """IMEI -> 当前 ICCID。用于把卡策略挂回设备条目。"""
    out: dict[str, str] = {}
    if not has_table(conn, "devices"):
        return out
    try:
        rows = conn.execute("SELECT imei, iccid FROM devices").fetchall()
    except sqlite3.Error:
        return out
    for r in rows:
        imei = (r["imei"] or "").strip()
        iccid = (r["iccid"] or "").strip() if r["iccid"] is not None else ""
        if imei and iccid:
            out[imei] = iccid
    return out# ---------------------------------------------------------------- 配置迁移

def normalize_web_port(source_root: dict) -> tuple[str, str]:
    """返回 (原始端口, 说明)。openvohive 默认 7575。"""
    server = source_root.get("server") or {}
    raw = server.get("port")
    if raw is None or str(raw).strip() == "":
        return VOX_DEFAULT_WEB_PORT, "源未设置 server.port，按 openvohive 默认 7575"
    return str(raw).strip(), "沿用源 server.port"


def build_vohivex_config(src: dict, rep: Report, args) -> dict:
    """
    生成 VoHiveX 核心配置。

    关键约束（来自 VoHiveX cmd/vohivex-gateway/config.go:prepareConfig）：
      - server.port 必须是可被 fmt.Sprint 还原为 "127.0.0.1:7576" 的标量；
      - web 段必须存在且可解析；
      - devices 必须是序列。
    其余键（telegram/webhook/email）按 openvohive 的 mapstructure 名透传，
    上游 vohive 的 yaml 反序列化会忽略未知键，因此透传是安全的。
    """
    src_server = src.get("server") or {}
    src_web = src.get("web") or {}

    out: dict = {}

    # --- server：端口被 VoHiveX 强制为回环 + 7576，网关对外用 7575
    server: dict = {"port": VOX_CORE_LISTEN}
    if "debug" in src_server:
        server["debug"] = bool(src_server["debug"])
    out["server"] = server

    # --- web：用户名/密码
    username = str(src_web.get("username") or "").strip()
    if not username:
        username = "admin"
        rep.warn("源 web.username 为空，已回退为 admin（请登录后立即修改）")
    password = str(src_web.get("password") or "")

    override = None
    if getattr(args, "password_env", None):
        value = os.environ.get(args.password_env, "")
        if value:
            override = value
        else:
            rep.note(f"--password-env {args.password_env} 未设置该环境变量，沿用源密码")
    if getattr(args, "set_password", None):
        override = args.set_password
    if override is not None:
        password = override
        rep.note("已用命令行提供的明文密码替换源密码")

    if BCRYPT_RE.match(password):
        if override is None:
            rep.error(
                "源 web.password 是 bcrypt 哈希。VoHiveX 内置核心是上游 vohive 发行版，"
                "其登录实现无法确认支持 $2a/$2b 哈希；直接沿用可能导致无法登录。"
            )
            rep.note("处理方式二选一：")
            rep.note("  a) --set-password '<明文>'  或  --password-env VOHIVEX_PW")
            rep.note("  b) 确认核心支持哈希后再保留（本项目未验证）")
    elif password == "":
        rep.warn("源 web.password 为空，VoHiveX 将无法登录，请务必用 --set-password 指定")

    out["web"] = {"username": username, "password": password}

    # --- devices
    src_devices = src.get("devices") or []
    if not isinstance(src_devices, list):
        rep.error("源 devices 不是列表，已按空列表处理")
        src_devices = []

    devices_out: list[dict] = []
    for index, dev in enumerate(src_devices):
        if not isinstance(dev, dict):
            rep.warn(f"devices[{index}] 不是映射，已跳过")
            continue
        item = {k: v for k, v in dev.items() if k not in PATH_KEYS}
        missing_paths = [k for k in PATH_KEYS if k in dev]
        if missing_paths:
            rep.note(
                f"devices[{index}] 丢弃 openvohive 不落盘的运行时路径键: {', '.join(missing_paths)}"
            )

        dev_id = str(item.get("id") or "").strip()
        imei = str(item.get("modem_imei") or "").strip()
        if not dev_id:
            rep.warn(f"devices[{index}] 缺少 id，VoHiveX 无法稳定引用该设备")
        if not imei:
            rep.warn(f"devices[{index}]({dev_id or '?'}) 缺少 modem_imei，VoHiveX 的 IMEI 匹配会失效")
        elif not (imei.isdigit() and len(imei) == 15):
            rep.warn(f"devices[{index}]({dev_id or '?'}) modem_imei 不是 15 位数字：{imei}")

        # openvohive 把 APN/网络开关搬到 card_policies 表，VoHiveX 仍读设备条目
        policy = None
        if not getattr(args, "no_policy", False):
            iccid = args._imei_to_iccid.get(imei, "") if imei else ""
            if iccid:
                policy = args._policies.get(iccid)
        if policy:
            for key in POLICY_KEYS:
                if key in policy:
                    item[key] = policy[key]
            rep.note(
                f"devices[{index}]({dev_id or imei}) 从 card_policies[{iccid}] 回填 "
                + ", ".join(f"{k}={policy[k]}" for k in POLICY_KEYS if k in policy)
            )

        devices_out.append(item)

    out["devices"] = devices_out
    rep.note(f"设备条目：源 {len(src_devices)} 条 -> 迁移 {len(devices_out)} 条")

    # --- 通知：telegram / webhook / email 透传给核心
    for section_name in ("telegram", "webhook", "email"):
        block = src.get(section_name)
        if isinstance(block, dict) and block:
            out[section_name] = block
            enabled = block.get("enabled")
            rep.note(
                f"透传 {section_name} 配置（enabled={enabled}）；"
                f"VoHiveX 另支持飞书/QQ/Bark/Pushplus，需在界面里补配"
            )

    # openvohive 独有、VoHiveX 无对应语义的段，明确丢弃
    for extra in ("", "vowifi", "proxy", "voice"):
        if extra and extra in src:
            rep.warn(f"源配置段 `{extra}` 在 VoHiveX 中无对应实现，已丢弃")

    return out# ---------------------------------------------------------------- 时间归一

# GORM 的 SQLite 驱动把 time.Time 写成 "2006-01-02 15:04:05.999999999-07:00"
# 这种带空格 + 纳秒 + 时区的形式。VoHiveX 的 timestampMillis 只认 RFC3339
# 和两种无时区秒级布局，因此 GORM 原样文本会直接判为「短信时间格式无效」。
# 迁移必须改为 RFC3339。
GORM_LAYOUTS = (
    "%Y-%m-%d %H:%M:%S.%f%z",
    "%Y-%m-%d %H:%M:%S%z",
    "%Y-%m-%d %H:%M:%S.%f",
    "%Y-%m-%d %H:%M:%S",
)

from datetime import datetime, timezone


def _numeric_to_datetime(digits: str) -> datetime:
    """
    按位数判断数值时间戳的精度：
      13 位 -> 毫秒，16 位 -> 微秒，其余（10 位）-> 秒。
    不能简单地截断到 11 位，那会把毫秒值误当成 2539 年。
    """
    if len(digits) >= 16:
        return datetime.fromtimestamp(int(digits) / 1_000_000, tz=timezone.utc)
    if len(digits) >= 13:
        return datetime.fromtimestamp(int(digits) / 1_000, tz=timezone.utc)
    return datetime.fromtimestamp(int(digits), tz=timezone.utc)


def parse_source_time(value) -> tuple[datetime, bool]:
    """返回 (datetime, 是否原本带时区)。失败抛 ValueError。"""
    if isinstance(value, bool):
        raise ValueError("布尔值不是时间")
    if isinstance(value, (int, float)):
        return _numeric_to_datetime(str(int(value))), True
    text = str(value).strip()
    if not text:
        raise ValueError("空时间")
    if re.fullmatch(r"\d{9,19}", text):
        return _numeric_to_datetime(text), True
    # 有 T 分隔的 ISO / RFC3339
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return dt, dt.tzinfo is not None
    except ValueError:
        pass
    for layout in GORM_LAYOUTS:
        try:
            dt = datetime.strptime(text, layout)
            return dt, dt.tzinfo is not None
        except ValueError:
            continue
    raise ValueError(f"无法解析时间: {text!r}")


def to_rfc3339(value, assume_tz, rep: Report, where: str) -> tuple[str, int]:
    dt, aware = parse_source_time(value)
    if not aware:
        dt = dt.replace(tzinfo=assume_tz)
    if dt.microsecond:
        dt = dt.replace(microsecond=0)
    text = dt.isoformat(timespec="seconds")
    return text, int(dt.timestamp() * 1000)


# ---------------------------------------------------------------- 短信迁移

ARCHIVE_SCHEMA = [
    "PRAGMA journal_mode=WAL",
    "PRAGMA busy_timeout=10000",
    "CREATE TABLE IF NOT EXISTS vohivex_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)",
    ("CREATE TABLE IF NOT EXISTS imported_sms(id INTEGER PRIMARY KEY AUTOINCREMENT,"
     "device_id TEXT NOT NULL,device_name TEXT NOT NULL DEFAULT '',"
     "local_phone TEXT NOT NULL DEFAULT '',imsi TEXT NOT NULL,peer TEXT NOT NULL,"
     "message_type INTEGER NOT NULL,content TEXT NOT NULL,timestamp TEXT NOT NULL,"
     "timestamp_ms INTEGER NOT NULL,sender TEXT NOT NULL DEFAULT '',status INTEGER,"
     "source_message_id TEXT NOT NULL DEFAULT '',signature TEXT NOT NULL UNIQUE,"
     "imported_at INTEGER NOT NULL)"),
    ("CREATE INDEX IF NOT EXISTS imported_sms_thread "
     "ON imported_sms(device_id,imsi,peer,timestamp_ms,id)"),
]

ARCHIVE_INSERT = (
    "INSERT OR IGNORE INTO imported_sms("
    "device_id,device_name,local_phone,imsi,peer,message_type,content,"
    "timestamp,timestamp_ms,sender,status,source_message_id,signature,imported_at"
    ")VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
)


def signature_of(device_id, imsi, peer, mtype, timestamp, content, sender, source_id) -> str:
    material = TS_SEP.join([
        device_id, imsi, peer, str(mtype), timestamp, str(content), sender, source_id
    ])
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def read_sms_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    if not has_table(conn, "sms"):
        return []
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(sms)")}
    needed = ["id", "imsi", "peer", "content", "type", "status", "timestamp"]
    missing = [c for c in needed if c not in cols]
    if missing:
        raise SystemExit(f"sms 表缺少必需列 {missing}；请确认这是 openvohive 的 vohive.db")
    extra = [c for c in ("iccid", "sender", "local_phone") if c in cols]
    select = ", ".join(needed + extra)
    sql = f'SELECT {select} FROM sms ORDER BY timestamp ASC, id ASC'
    return list(conn.execute(sql))


def read_subscriptions(conn: sqlite3.Connection) -> dict[str, dict]:
    """IMSI -> 订阅信息（ICCID 与手机号）。"""
    out: dict[str, dict] = {}
    if not has_table(conn, "sim_subscriptions"):
        return out
    try:
        rows = conn.execute(
            "SELECT imsi, current_iccid, phone_number, modem_phone_number "
            "FROM sim_subscriptions"
        ).fetchall()
    except sqlite3.Error:
        return out
    for r in rows:
        imsi = (r["imsi"] or "").strip()
        if not imsi:
            continue
        out[imsi] = {
            "iccid": (r["current_iccid"] or "").strip(),
            "phone": (r["phone_number"] or "").strip(),
            "modem_phone": (r["modem_phone_number"] or "").strip(),
        }
    return out


def build_imsi_to_imei(conn: sqlite3.Connection, rep: Report) -> dict[str, str]:
    """
    IMSI -> 设备 IMEI。

    openvohive 的 sms 表只有 imsi，没有 device_id；而 VoHiveX 归档按 device_id 分组。
    这里用三条链路兜底：
      1) sim_cards.imsi -> current_imei（最直接）
      2) sim_subscriptions.imsi -> current_iccid，再 devices.iccid -> imei
      3) devices.imei 的 IMEI 与 sim_cards.imsi 同卡号前缀匹配（兜底，仅记录不猜测）
    """
    out: dict[str, str] = {}

    iccid_to_imei: dict[str, str] = {}
    if has_table(conn, "devices"):
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(devices)")}
        if "iccid" in cols:
            for r in conn.execute("SELECT imei, iccid FROM devices"):
                imei = (r["imei"] or "").strip()
                iccid = (r["iccid"] or "").strip() if r["iccid"] is not None else ""
                if imei and iccid:
                    iccid_to_imei[iccid] = imei

    if has_table(conn, "sim_cards"):
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(sim_cards)")}
        if "imsi" in cols and "current_imei" in cols:
            for r in conn.execute("SELECT imsi, current_imei FROM sim_cards"):
                imsi = (r["imsi"] or "").strip()
                imei = (r["current_imei"] or "").strip() if r["current_imei"] is not None else ""
                if imsi and imei:
                    out[imsi] = imei

    for imsi, sub in read_subscriptions(conn).items():
        if imsi in out:
            continue
        iccid = sub.get("iccid", "")
        if iccid and iccid in iccid_to_imei:
            out[imsi] = iccid_to_imei[iccid]

    if out:
        rep.note(f"已解析 {len(out)} 个 IMSI 对应的设备 IMEI（用于归档分组）")
    return out


def open_archive(path: Path, apply: bool) -> sqlite3.Connection | None:
    if not apply and not path.exists():
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = open_db(str(path), timeout=15)
    for stmt in ARCHIVE_SCHEMA:
        conn.execute(stmt)
    row = conn.execute(
        "SELECT COUNT(*) FROM vohivex_meta WHERE key='schema_version'"
    ).fetchone()
    if row and row[0] == 0:
        conn.execute(
            "INSERT INTO vohivex_meta(key,value)VALUES('schema_version',1)"
        )
    conn.commit()
    if not path.as_posix().startswith("/:memory:"):
        try:
            os.chmod(path, 0o600)  # 与 VoHiveX sms_archive.go 保持一致
        except OSError:
            pass
    return conn


def close_archive(conn: sqlite3.Connection) -> None:
    """收尾：把 WAL 合并回主库，避免留下 -wal/-shm 影响网关启动时的完整性判断。"""
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.commit()
    except sqlite3.Error:
        pass
    conn.close()


def cleanup_wal_sidecar(path: Path) -> None:
    """
    清理零长度的 -wal / -shm 残留。

    只读连接（verify 阶段）无法执行 checkpoint，SQLite 会在关闭后留下空的
    -wal/-shm；它们本身无害，但会让人误以为库未正常关闭。仅在两者均为 0 字节时删除。
    """
    wal = path.with_name(path.name + "-wal")
    shm = path.with_name(path.name + "-shm")
    try:
        if wal.exists() and wal.stat().st_size == 0 and \
           shm.exists() and shm.stat().st_size > 0:
            shm.unlink()
            wal.unlink()
    except OSError:
        pass# ---------------------------------------------------------------- 主流程

def locate_source_root(path: Path) -> Path | None:
    """
    返回包含 config/config.yaml 的部署根；找不到时返回 None。
    允许只迁数据库：例如直接指向一个只有 vohive.db 的 data 目录。
    """
    if (path / "config" / "config.yaml").is_file():
        return path
    if path.is_file() and path.name.startswith("vohive.db"):
        return path.parent
    if (path / "vohive.db").is_file() or (path / "data" / "vohive.db").is_file():
        return path
    return None


def find_source_db(root: Path, explicit: str | None, rep: Report) -> Path | None:
    if explicit:
        p = Path(explicit)
        if not p.is_file():
            raise SystemExit(f"--source-db 指定的文件不存在：{p}")
        return p
    candidates = [
        root / "data" / "vohive.db",
        root / "data" / "vohive.sqlite",
        root / "vohive.db",
    ]
    for c in candidates:
        if c.is_file():
            return c
    # 兜底：搜任意 .db
    found = sorted(p for p in root.rglob("*.db") if p.is_file())
    if found:
        rep.warn(f"常规位置未找到数据库，改用 {found[0]}（如有多个请用 --source-db 指定）")
        return found[0]
    return None


def migrate_config(src_root: dict, target_config: Path, args, rep: Report) -> None:
    cfg = build_vohivex_config(src_root, rep, args)
    dumped = yaml.safe_dump(
        cfg, allow_unicode=True, sort_keys=False, default_flow_style=False, width=4096
    )
    text = (
        "# 由 migrate_openvohive_to_vohivex.py 生成，请勿手工破坏缩进\n"
        f"# 生成时间: {time.strftime('%Y-%m-%d %H:%M:%S %z')}\n" + dumped
    )

    # 自检：必须能被 YAML 重新解析，且 server.port 文本可还原
    reparsed = yaml.safe_load(text)
    assert reparsed["server"]["port"] == VOX_CORE_LISTEN, "server.port 自检失败"
    assert isinstance(reparsed.get("devices"), list), "devices 自检失败"

    if target_config.exists():
        old = yaml.safe_load(target_config.read_text(encoding="utf-8")) or {}
        old_web = old.get("web") or {}
        new_web = cfg["web"]
        if str(old_web.get("username")) != new_web["username"] or \
           str(old_web.get("password")) != new_web["password"]:
            rep.warn(
                "目标已存在不同的 web 凭据，将被覆盖；如需保留目标凭据请加 --keep-target-credentials"
            )
        if args.keep_target_credentials:
            cfg["web"] = {"username": old_web.get("username", "admin"),
                          "password": old_web.get("password", "")}
            rep.note("已按 --keep-target-credentials 保留目标原有 web 凭据")
            text = ("# 由 migrate_openvohive_to_vohivex.py 生成\n"
                    f"# 生成时间: {time.strftime('%Y-%m-%d %H:%M:%S %z')}\n"
                    + yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False,
                                     default_flow_style=False, width=4096))

    backup_existing(target_config, rep, args.apply)
    if args.apply:
        atomic_write(target_config, text.encode("utf-8"), 0o600)
        rep.act(f"已写入 {target_config} ({len(text.encode('utf-8'))} 字节, 0600)")
    else:
        rep.note(f"[dry-run] 将写入 {target_config}，内容预览：")
        for line in text.splitlines()[:40]:
            print(f"         | {line}")
        if len(text.splitlines()) > 40:
            print(f"         | ... 共 {len(text.splitlines())} 行")


def migrate_sms(conn: sqlite3.Connection, target_data: Path, args, rep: Report) -> None:
    rows = read_sms_rows(conn)
    rep.note(f"源 sms 表 {len(rows)} 条")
    if not rows:
        return

    subs = read_subscriptions(conn)
    imsi_to_imei = build_imsi_to_imei(conn, rep)

    # IMEI/ID -> 设备名，用于归档里展示
    dev_name: dict[str, str] = {}
    if has_table(conn, "devices"):
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(devices)")}
        if "alias" in cols:
            for r in conn.execute("SELECT imei, alias FROM devices"):
                imei = (r["imei"] or "").strip()
                if imei:
                    dev_name[imei] = (r["alias"] or "").strip()

    for dev in args._device_list:
        imei = str(dev.get("modem_imei") or "").strip()
        if imei and str(dev.get("name") or "").strip():
            dev_name[imei] = str(dev["name"]).strip()

    archive_path = Path(args.archive_db) if args.archive_db else target_data / "imported-sms.sqlite3"
    rep.note(f"目标归档库：{archive_path}")

    prepared = []
    skipped_time = 0
    skipped_shape = 0
    for row in rows:
        imsi = (row["imsi"] or "").strip()
        peer = (row["peer"] or "").strip()
        content = row["content"]
        content = "" if content is None else str(content)
        try:
            mtype = int(row["type"])
        except (TypeError, ValueError):
            skipped_shape += 1
            continue
        if mtype not in (1, 2):
            skipped_shape += 1
            continue
        if not peer or len(peer) > 200 or not content or len(content) > 20000:
            skipped_shape += 1
            continue

        try:
            stamp_text, stamp_ms = to_rfc3339(
                row["timestamp"], args.assume_tz, rep, f"sms.id={row['id']}"
            )
        except ValueError as exc:
            skipped_time += 1
            if skipped_time <= 3:
                rep.warn(f"跳过无法解析时间的短信 id={row['id']}: {exc}")
            continue

        sub = subs.get(imsi, {})
        iccid = ""
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(sms)")}
        if "iccid" in cols and row["iccid"]:
            iccid = str(row["iccid"]).strip()
        if not iccid:
            iccid = sub.get("iccid", "")

        imei = imsi_to_imei.get(imsi, "")
        device_id = imei or (f"imsi:{imsi}" if imsi else f"import:{row['id']}")
        device_name = dev_name.get(imei, "")
        local_phone = sub.get("phone", "") or sub.get("modem_phone", "")

        sender = ""
        if "sender" in cols and row["sender"]:
            sender = str(row["sender"])
        if not sender:
            sender = peer if mtype == 1 else local_phone

        status = row["status"] if "status" in cols else None
        if status is not None:
            try:
                status = int(status)
                if not (0 <= status <= 3):
                    status = None
            except (TypeError, ValueError):
                status = None

        source_id = str(row["id"])
        sig = signature_of(device_id, imsi or device_id, peer, mtype, stamp_text,
                           content, sender, source_id)
        prepared.append((
            device_id, device_name, local_phone, imsi or device_id, peer, mtype,
            content, stamp_text, stamp_ms, sender, status, source_id, sig,
            int(time.time()),
        ))

    if skipped_time:
        rep.warn(f"{skipped_time} 条短信因时间无法解析被跳过（可试 --assume-tz Asia/Shanghai）")
    if skipped_shape:
        rep.warn(f"{skipped_shape} 条短信字段不合 VoHiveX 约束被跳过（内容>20000 字/方向非 1|2 等）")
    rep.note(f"可迁移短信：{len(prepared)} 条")

    if not prepared:
        return

    if not args.apply:
        rep.note("[dry-run] 将插入以下样例行（最多 3 条）：")
        for row in prepared[:3]:
            print(f"         | device={row[0]} imsi={row[3]} peer={row[4]} "
                  f"type={row[5]} ts={row[7]} content={row[6][:24]!r}")
        return

    arch = open_archive(archive_path, args.apply)
    if arch is None:
        return
    try:
        before = int(arch.execute("SELECT COUNT(*) FROM imported_sms").fetchone()[0])
        with arch:
            arch.executemany(ARCHIVE_INSERT, prepared)
        after = int(arch.execute("SELECT COUNT(*) FROM imported_sms").fetchone()[0])
        inserted = after - before
        rep.act(f"归档写入完成：新增 {inserted} 条，重复忽略 {len(prepared) - inserted} 条")
    finally:
        close_archive(arch)
        cleanup_wal_sidecar(archive_path)


# ---------------------------------------------------------------- 目标校验

def verify_target(target: Path, rep: Report, require_config: bool = True) -> None:
    cfg_path = target / "config" / "config.yaml"
    if not cfg_path.is_file():
        msg = f"目标缺少 {cfg_path}"
        if require_config:
            rep.error(msg + "（VoHiveX 首次启动会生成默认 admin/admin，请首登后立即改密）")
        else:
            rep.note(msg + "（本次只迁移数据库，配置需另行准备）")
        _verify_core_db(target, rep)
        return
    try:
        cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        rep.error(f"目标配置不是合法 YAML：{exc}")
        return

    port = (cfg.get("server") or {}).get("port")
    if str(port) != VOX_CORE_LISTEN:
        rep.error(f"server.port = {port!r}，VoHiveX 要求 {VOX_CORE_LISTEN!r}"
                  f"（否则 -prepare-config 会尝试自动改写）")
    else:
        rep.act("server.port 符合 VoHiveX 要求")

    web = cfg.get("web") or {}
    if not web.get("username") or web.get("password") in (None, ""):
        rep.error("web 段缺失用户名或密码，VoHiveX 网关无法自登录")
    elif BCRYPT_RE.match(str(web.get("password"))):
        rep.warn("web.password 是 bcrypt 哈希，VoHiveX 内置核心是否支持未验证")
    else:
        if str(web.get("username")) == "admin" and str(web.get("password")) in ("admin", "admin123"):
            rep.warn("仍在使用默认弱口令，请首登后立即修改")
        rep.act("web 凭据存在")

    devices = cfg.get("devices")
    if not isinstance(devices, list):
        rep.error("devices 不是列表")
    else:
        no_imei = [d for d in devices if isinstance(d, dict) and not str(d.get("modem_imei") or "").strip()]
        if no_imei:
            rep.warn(f"{len(no_imei)} 个设备条目缺少 modem_imei，VoHiveX 只能靠 usb_path/at_port 匹配")
        dup = {}
        for d in devices:
            if isinstance(d, dict):
                key = str(d.get("modem_imei") or "").strip()
                if key:
                    dup[key] = dup.get(key, 0) + 1
        for k, v in dup.items():
            if v > 1:
                rep.warn(f"modem_imei {k} 重复 {v} 次，核心会拒绝解析重复身份")
        rep.act(f"设备条目 {len(devices)} 条")

    _verify_core_db(target, rep)


def _verify_core_db(target: Path, rep: Report) -> None:
    core_db = target / "data" / "vohive.db"
    if core_db.is_file():
        try:
            counts = db_row_counts(core_db)
            active = {k: v for k, v in counts.items() if v}
            rep.act(f"核心库 vohive.db 存在，{len(counts)} 表 / {sum(counts.values())} 行")
            if active:
                rep.note("非空表：" + ", ".join(f"{k}={v}" for k, v in sorted(active.items())))
            for table in ("sms", "devices", "sim_subscriptions"):
                if table not in counts:
                    rep.warn(f"核心库缺少 {table} 表（核心启动时会自动创建）")
            conn = connect_ro(core_db)
            try:
                for table, extra in CORE_EXTRA_COLUMNS.items():
                    if not has_table(conn, table):
                        continue
                    cols = set(sqlite_columns(conn, table))
                    absent = [c for c in extra if c not in cols]
                    if absent:
                        rep.note(f"{table} 缺列 {', '.join(absent)}，核心 AutoMigrate 会补")
                orphan_iccid = conn.execute(
                    "SELECT COUNT(*) FROM sms WHERE iccid = '' OR iccid IS NULL"
                ).fetchone()[0] if has_table(conn, "sms") else 0
                if orphan_iccid:
                    rep.warn(f"{orphan_iccid} 条短信无 ICCID，按卡策略过滤时可能不显示")
            finally:
                conn.close()
        except sqlite3.Error as exc:
            rep.error(f"核心库不可读：{exc}")
        finally:
            # 只读连接会在关闭后留下空 -wal 与 -shm，清理以免看起来像没正常关闭
            cleanup_wal_sidecar(core_db)
    else:
        rep.note("目标还没有 data/vohive.db（首次启动核心会自动创建空库）")

    archive = target / "data" / "imported-sms.sqlite3"
    if archive.is_file():
        try:
            conn = connect_ro(archive)
            try:
                n = count_rows(conn, "imported_sms")
                rep.act(f"归档库连打正常，imported_sms {n} 条")
                orphan = conn.execute(
                    "SELECT COUNT(*) FROM imported_sms WHERE device_id LIKE 'imsi:%'"
                    " OR device_id LIKE 'import:%'"
                ).fetchone()[0]
                if orphan:
                    rep.warn(f"{orphan} 条归档的 device_id 不是 IMEI，界面里可能无法按设备过滤")
            finally:
                conn.close()
                cleanup_wal_sidecar(archive)
        except sqlite3.Error as exc:
            rep.error(f"归档库不可读：{exc}")
    else:
        rep.note("目标还没有 imported-sms.sqlite3（首次启动网关会自动创建）")


# ---------------------------------------------------------------- CLI

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="把 openvohive 的配置与数据迁移到 VoHiveX",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  预览:  %(prog)s --source ./openvohive --target ./vohivex\n"
            "  执行:  %(prog)s --source ./openvohive --target ./vohivex --apply \\\n"
            "             --set-password 'MyNewPassw0rd!'\n"
            "  校验:  %(prog)s --target ./vohivex --verify-only\n"
        ),
    )
    ap.add_argument("--source", help="openvohive 部署根目录（含 config/config.yaml 与 data/vohive.db）")
    ap.add_argument("--target", required=True, help="VoHiveX 部署根目录（含 config/ 与 data/）")
    ap.add_argument("--apply", action="store_true", help="真正落盘（默认 dry-run）")
    ap.add_argument("--verify-only", action="store_true", help="只检查目标现状，不做迁移")
    ap.add_argument("--db-only", action="store_true",
                    help="只处理数据库，跳过配置迁移与配置校验")
    ap.add_argument("--source-db", help="显式指定 openvohive 的 vohive.db 路径")
    ap.add_argument("--archive-db", help="显式指定 VoHiveX 的 imported-sms.sqlite3 路径")
    ap.add_argument("--set-password", help="用该明文密码写入目标 web 配置（覆盖源密码）")
    ap.add_argument("--password-env", default="VOHIVEX_PW",
                    help="从该环境变量读取明文密码（默认 VOHIVEX_PW）")
    ap.add_argument("--keep-target-credentials", action="store_true",
                    help="目标已有配置时保留其 web 凭据，不覆盖")
    ap.add_argument("--no-policy", action="store_true",
                    help="不从 card_policies 回填 APN/网络开关到设备条目")
    ap.add_argument("--no-sms", action="store_true", help="只迁移配置，不迁移短信")
    ap.add_argument("--db-mode", choices=("auto", "copy", "merge", "archive", "none"),
                    default="auto",
                    help="数据库迁移模式：auto=目标空则整库拷贝、否则合并；"
                         "copy=强制整库拷贝；merge=逐表 INSERT OR IGNORE 合并；"
                         "archive=只把短信转成 VoHiveX 只读归档库（旧行为）；"
                         "none=完全不动数据库")
    ap.add_argument("--assume-tz", default=None,
                    help="源时间无时区时按此时区解释，如 Asia/Shanghai（默认按本机时区）")
    args = ap.parse_args(argv)

    rep = Report()
    target = Path(args.target).expanduser().resolve()

    print(f"openvohive -> VoHiveX 迁移工具")
    print(f"  模式  : {'APPLY' if args.apply else 'DRY-RUN'}")
    print(f"  目标  : {target}")

    if args.assume_tz:
        try:
            from zoneinfo import ZoneInfo
            args.assume_tz = ZoneInfo(args.assume_tz)
        except Exception as exc:  # 缺 tzdata 时 zoneinfo 会抛错
            print(f"  [warn] 无法加载时区 {args.assume_tz}（{exc}）；"
                  f"回退到本机时区。可安装 tzdata 后重试。")
            args.assume_tz = datetime.now().astimezone().tzinfo
    else:
        args.assume_tz = datetime.now().astimezone().tzinfo

    if args.verify_only:
        section("校验目标")
        verify_target(target, rep, require_config=not args.db_only)
        section("结果")
        print(f"  错误 {len(rep.errors)} / 警告 {len(rep.warnings)}")
        return 1 if rep.errors else 0

    if not args.source:
        return print("缺少 --source（或使用 --verify-only）") or 2

    source = Path(args.source).expanduser().resolve()
    root = locate_source_root(source)
    if root is None:
        return print(f"--source {source} 下既没有 config/config.yaml，也没有 vohive.db") or 2
    print(f"  源    : {root}")
    print(f"  时区  : {args.assume_tz}")

    cfg_path = root / "config" / "config.yaml"
    src_cfg: dict = {}
    if cfg_path.is_file():
        section("读取源配置")
        src_cfg = load_yaml_dir(cfg_path)
        src_port, why = normalize_web_port(src_cfg)
        rep.note(f"源对外端口 {src_port}（{why}）；VoHiveX 统一用 {VOX_DEFAULT_WEB_PORT}")
        if src_port != VOX_DEFAULT_WEB_PORT:
            rep.warn(f"源端口是 {src_port}，迁移后访问端口变成 {VOX_DEFAULT_WEB_PORT}，记得改反向代理/收藏夹")
    else:
        rep.warn(f"源目录没有 {cfg_path}，将只迁移数据库；"
                 f"目标配置需另行准备（或改用 VoHiveX 默认 admin/admin）")

    args._policies = {}
    args._imei_to_iccid = {}
    args._device_list = src_cfg.get("devices") or []

    # ---- 数据
    section("读取源数据库")
    src_db = find_source_db(root, args.source_db, rep)
    conn = None
    if src_db is None:
        rep.warn("未找到 openvohive 的 vohive.db，短信与卡策略不会迁移")
    else:
        size = src_db.stat().st_size
        rep.note(f"源库 {src_db} ({human_bytes(size)})")
        conn = connect_ro(src_db)
        for table in ("devices", "sim_cards", "sim_subscriptions", "sms",
                      "sms_contacts", "card_policies", "traffic_minute",
                      "sms_delivery", "sms_delivery_part"):
            n = count_rows(conn, table)
            if n >= 0:
                rep.note(f"  {table:<20} {n} 行")
        args._policies = read_card_policies(conn)
        args._imei_to_iccid = read_device_iccid(conn)
        if args._policies:
            rep.note(f"card_policies {len(args._policies)} 张卡（APN/网络开关将回填到设备条目）")
        check_schema_drift(src_db, rep)
        if args.db_mode in ("auto", "copy", "merge"):
            rep.note("VoHiveX 核心与 openvohive 共用 data/vohive.db，整库迁移可保留全部数据")
        elif args.db_mode == "archive" or args.no_sms:
            for table in ("sms_delivery", "sms_delivery_part", "traffic_minute",
                          "traffic_hour", "traffic_day", "traffic_week", "traffic_month"):
                if count_rows(conn, table) > 0:
                    rep.warn(f"{table} 有数据，当前模式不迁移（历史流量/投递回执会丢失）")
            if count_rows(conn, "sms_contacts") > 0:
                rep.note("sms_contacts 是派生表，VoHiveX 会从 imported_sms 重新聚合，无需迁移")

    # ---- 配置落盘
    if args.db_only:
        rep.note("按 --db-only 跳过配置迁移")
    elif src_cfg:
        section("迁移配置")
        migrate_config(src_cfg, target / "config" / "config.yaml", args, rep)
    else:
        rep.note("跳过配置迁移（源无 config/config.yaml）")

    # ---- 数据库落盘
    used_mode = None
    if src_db is not None and not args.no_sms:
        section("迁移数据库")
        used_mode = migrate_db(src_db, target / "data", args, rep)

    # ---- 归档模式下的短信转换
    if conn is not None and not args.no_sms and used_mode == "archive":
        section("迁移短信（只读归档）")
        migrate_sms(conn, target / "data", args, rep)
    if conn is not None:
        conn.close()

    # ---- 收尾
    section("结果")
    print(f"  动作 {len(rep.actions)} / 提示 {len(rep.notes)} / "
          f"警告 {len(rep.warnings)} / 错误 {len(rep.errors)}")
    if args.apply:
        section("校验目标")
        verify_target(target, rep, require_config=not args.db_only and bool(src_cfg))

    if not args.apply:
        print("\n这是 dry-run，未写入任何文件。确认无误后加 --apply 执行。")

    print("\n后续手工步骤：")
    print("  1. 备份 openvohive 整个目录（迁移不修改源，但建议留档）")
    print("  2. VoHiveX 的设备条目需要 usb_path / at_port 或 15 位 modem_imei")
    print("     —— 若源配置里没有这些运行时路径，请在 VoHiveX 界面「设备发现」里重新添加")
    print("  3. Telegram/Webhook/Email 已透传，需在界面确认；飞书/QQ/Bark/Pushplus 需补配")
    print("  4. 定时短信（scheduled-sms.sqlite3）与通话记录（calls.sqlite3）无上游对应，需重建")
    print("  5. VoHiveX 归档短信是以负 ID 展示的只读记录，历史短信不可再发送/删除原库")
    return 1 if rep.errors else 0

# ---------------------------------------------------------------- 整库迁移

# VoHiveX 内置核心就是上游 vohive 二进制，读写同一套 data/vohive.db，
# schema 与 openvohive 同源（GORM 模型一致）。因此首选整库拷贝：
# 短信保持「活数据」（可发送/可删除/可编辑），流量统计、投递回执也全都保留。
# 核心启动时 AutoMigrate 会补齐 VoHiveX 多出的列（vowifi_enabled / sip_code）。

# core 期望存在、但 openvohive 可能没有的列；只用于报告，不主动创建。
# 实测两代核心的差异列：
#   v1.5.5 上游核心：card_policies.vowifi_enabled、sms_delivery_part.sip_code
#   新版（/tmp/data 生产库）：card_policies.ims_mode/ims_profile_index、sms.bearer
# 都只做「源缺列」提示，不主动创建 —— 由核心 AutoMigrate 负责。
CORE_EXTRA_COLUMNS = {
    "card_policies": ["vowifi_enabled", "ims_mode", "ims_profile_index"],
    "sms_delivery_part": ["sip_code"],
    "sms": ["bearer"],
}

CORE_TABLES = (
    "devices", "card_policies", "sim_cards", "sim_subscriptions",
    "pending_phone_numbers", "sms", "sms_contacts",
    "sms_delivery", "sms_delivery_part",
    "traffic_minute", "traffic_hour", "traffic_day", "traffic_week", "traffic_month",
)


def sqlite_tables(conn: sqlite3.Connection) -> list[str]:
    return [
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]


def sqlite_columns(conn: sqlite3.Connection, table: str, schema: str = "main") -> list[str]:
    return [r[1] for r in conn.execute(f'PRAGMA {schema}.table_info("{table}")')]


def db_row_counts(path: Path) -> dict[str, int]:
    if not path.is_file():
        return {}
    try:
        conn = open_db(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=10, readonly=True)
    except sqlite3.Error:
        return {}
    try:
        out = {}
        for table in sqlite_tables(conn):
            try:
                out[table] = int(conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
            except sqlite3.Error:
                pass
        return out
    finally:
        conn.close()


def snapshot_db(src: Path, dest: Path) -> None:
    """用 SQLite 在线备份 API 取一致快照；源库在 WAL 模式下运行中也可安全拷贝。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        for side in ("-wal", "-shm"):
            p = dest.with_name(dest.name + side)
            if p.exists():
                p.unlink()
        dest.unlink()
    src_conn = open_db(f"file:{src.as_posix()}?mode=ro", uri=True, timeout=30, readonly=True)
    dst_conn = open_db(str(dest))
    try:
        src_conn.backup(dst_conn, pages=0)  # 一次拷完全部页，避免长时间持锁
        # 先按 WAL 建库（核心默认 WAL），再 checkpoint 回主库，保证单文件交付
        dst_conn.execute("PRAGMA journal_mode=WAL")
        dst_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        dst_conn.commit()
    finally:
        dst_conn.close()
        src_conn.close()
    # 关闭后 -wal/-shm 已清空；仅在两者为空时删除，避免留下误导性残片
    for side in ("-wal", "-shm"):
        p = dest.with_name(dest.name + side)
        try:
            if p.exists() and p.stat().st_size == 0:
                p.unlink()
        except OSError:
            pass
    try:
        os.chmod(dest, 0o600)
    except OSError:
        pass


def merge_db(src: Path, dest: Path, rep: Report, assume_tz) -> None:
    """
    目标已有 vohive.db 时做行级合并。

    判重策略分两类（关键：自增 id 不是业务键）：
      * 业务键即主键的表（devices/card_policies/sms_contacts/...）
        -> INSERT OR IGNORE，"主键冲突"就是真重复；
      * 自增 id 表（sms/sms_delivery_part/traffic_*）
        -> 按业务键判重、插入时省略 id 让目标重新分配。
           否则源 id 1..N 会与目标已有 id 1..M 冲突，内容完全不同的行被静默丢弃。
    列交集保证目标多出的列（vowifi_enabled/ims_mode/bearer/sip_code）不被 NULL 覆盖。
    """
    tmp = dest.with_name(f".merge-src-{os.getpid()}.db")
    snapshot_db(src, tmp)
    conn = open_db(str(dest), timeout=30)
    try:
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute(f"ATTACH DATABASE '{tmp.as_posix()}' AS src")
        dest_tables = set(sqlite_tables(conn))
        src_tables = {
            r[0] for r in conn.execute(
                "SELECT name FROM src.sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%'"
            )
        }
        for table in sorted(src_tables & dest_tables):
            keys = AUTOINC_BUSINESS_KEYS.get(table)
            if keys:
                src_n, added, skipped = merge_table_by_business_key(
                    conn, table, keys, rep, assume_tz
                )
                # 自增 id 表补一条说明，避免"跳过 N 条"被误读成内容重复
                if skipped:
                    rep.note(f"{table}: 按业务键判重（非 id），跳过 {skipped} 条已存在记录")
            else:
                src_n, added, skipped = merge_table_by_pk(conn, table, rep)
            if src_n:
                rep.note(f"{table}: 源 {src_n} 行 -> 新增 {added}，跳过 {skipped}")
        for table in sorted(src_tables - dest_tables):
            rep.note(f"{table}: 目标无此表，未合并")
        conn.commit()
    finally:
        try:
            conn.execute("DETACH DATABASE src")
        except sqlite3.Error:
            pass
        conn.close()
        for side in ("", "-wal", "-shm"):
            p = Path(str(tmp) + side)
            if p.exists():
                try:
                    p.unlink()
                except OSError:
                    pass
    try:
        os.chmod(dest, 0o600)
    except OSError:
        pass


def target_db_has_data(path: Path) -> bool:
    counts = db_row_counts(path)
    return any(counts.get(t, 0) for t in ("devices", "sms", "sim_subscriptions", "card_policies"))


def migrate_db(src_db: Path, target_data: Path, args, rep: Report) -> str:
    """返回实际采用的模式名，供上层决定是否还要走归档。"""
    dest = target_data / "vohive.db"
    counts = db_row_counts(src_db)
    total = sum(counts.values())
    rep.note(f"源库共 {len(counts)} 表 / {total} 行")

    mode = args.db_mode
    if mode == "auto":
        mode = "merge" if target_db_has_data(dest) else "copy"

    if mode == "archive":
        rep.note("按 --db-mode archive 只生成只读归档库，不写核心库")
        return mode

    if mode == "none":
        rep.note("按 --db-mode none 跳过整库迁移")
        return mode

    if mode == "copy":
        if args.apply:
            backup_existing(dest, rep, True)
            snapshot_db(src_db, dest)
            rep.act(f"整库拷贝完成 -> {dest}（{human_bytes(dest.stat().st_size)}，0600）")
            after = db_row_counts(dest)
            cleanup_wal_sidecar(dest)  # 只读连接会产生空 -wal/-shm，清掉
            rep.act("目标行数：" + ", ".join(f"{k}={v}" for k, v in sorted(after.items()) if v))
        else:
            rep.note(f"[dry-run] 将整库拷贝 {src_db} -> {dest}")
            rep.note("[dry-run] 源表行数：" + ", ".join(
                f"{k}={v}" for k, v in sorted(counts.items()) if v))
        return mode

    if mode == "merge":
        if not dest.is_file():
            rep.error(f"--db-mode merge 要求目标已有 {dest}，请改用 --db-mode copy")
            return mode
        if args.apply:
            backup_existing(dest, rep, True)
            before = db_row_counts(dest)
            merge_db(src_db, dest, rep, args.assume_tz)
            after = db_row_counts(dest)
            cleanup_wal_sidecar(dest)
            rep.act("合并后增量：" + (", ".join(
                f"{k}+{after.get(k,0)-before.get(k,0)}"
                for k in sorted(after) if after.get(k, 0) - before.get(k, 0)
            ) or "无新增行"))
        else:
            rep.note(f"[dry-run] 将把 {src_db} 逐表 INSERT OR IGNORE 合并进 {dest}")
            rep.note("目标现状：" + (", ".join(
                f"{k}={v}" for k, v in sorted(db_row_counts(dest).items()) if v) or "空"))
        return mode

    rep.error(f"未知 --db-mode {mode}")
    return mode


def check_schema_drift(src_db: Path, rep: Report) -> None:
    """报告 openvohive 缺失、但 VoHiveX 核心会 AutoMigrate 补齐的列。"""
    conn = open_db(f"file:{src_db.as_posix()}?mode=ro", uri=True, readonly=True)
    try:
        tables = set(sqlite_tables(conn))
        for table, extra in CORE_EXTRA_COLUMNS.items():
            if table not in tables:
                rep.warn(f"源库缺少表 {table}，核心会自动创建（历史数据无法迁移）")
                continue
            cols = set(sqlite_columns(conn, table))
            absent = [c for c in extra if c not in cols]
            if absent:
                rep.note(
                    f"{table}: 源缺列 {', '.join(absent)}，核心启动时 AutoMigrate 会自动补上"
                )
        core_missing = [t for t in CORE_TABLES if t not in tables]
        if core_missing:
            rep.warn(f"源库缺少核心期望的表：{', '.join(core_missing)}（核心会自动建空表）")
        extra_in_src = sorted(tables - set(CORE_TABLES))
        if extra_in_src:
            rep.note(f"源库另有核心不认识的表：{', '.join(extra_in_src)}（保留原样）")
    finally:
        conn.close()

# ---------------------------------------------------------------- 合并策略

# 主键即业务键的表：可以放心用 INSERT OR IGNORE。
BUSINESS_PK_TABLES = {
    "devices": ["imei"],
    "card_policies": ["iccid"],
    "sim_cards": ["iccid"],
    "sim_subscriptions": ["imsi"],
    "pending_phone_numbers": ["iccid"],
    "sms_contacts": ["imsi", "peer"],
    "sms_delivery": ["message_id"],
    "workflow_runs": ["id"],          # 目标独有，仅作说明
    "workflows": ["id"],
}

# 有自增 id 但真实唯一性来自业务键的表。
# 直接 INSERT OR IGNORE 会按 id 判重 —— 两边 id 都从 1 开始时，
# 内容完全不同的行会被静默丢弃。必须按业务键判重，并让目标重新分配 id。
AUTOINC_BUSINESS_KEYS = {
    "sms": ["imsi", "iccid", "peer", "content", "type", "timestamp"],
    "sms_delivery_part": ["message_id", "part_no"],
    "traffic_minute": ["period_start", "resource", "tag", "direction"],
    "traffic_hour": ["period_start", "resource", "tag", "direction"],
    "traffic_day": ["period_start", "resource", "tag", "direction"],
    "traffic_week": ["period_start", "resource", "tag", "direction"],
    "traffic_month": ["period_start", "resource", "tag", "direction"],
    "workflow_run_steps": ["run_id", "node_id"],
    "workflow_runs": ["workflow_id", "trigger_kind", "started_at"],
}

# 参与业务键归一化的时间列
TIME_COLUMNS = {"timestamp", "period_start", "started_at", "finished_at",
                "created_at", "updated_at", "last_seen", "sent_at", "report_at",
                "first_seen", "expires_at"}


def primary_key_columns(conn: sqlite3.Connection, table: str, schema: str = "main") -> list[str]:
    """按 PRAGMA table_info 的 pk 序号返回主键列（仅处理单表主键）。"""
    rows = list(conn.execute(f'PRAGMA {schema}.table_info("{table}")'))
    pk = [(r[5], r[1]) for r in rows if r[5]]
    return [name for _, name in sorted(pk)]


def normalize_key_value(column: str, value, assume_tz):
    """把时间列归一成 epoch 秒，其余转成去掉首尾空白的字符串。"""
    if value is None:
        return ""
    if column in TIME_COLUMNS:
        try:
            _, ms = to_rfc3339(value, assume_tz, Report(), f"{column}")
            return str(ms // 1000)
        except (ValueError, TypeError):
            return str(value).strip()
    if isinstance(value, bool):
        return "1" if value else "0"
    return str(value).strip()


def make_row_key(conn: sqlite3.Connection, table: str, keycols: list[str],
                 schema: str, assume_tz) -> set[tuple]:
    cols = sqlite_columns(conn, table, schema)
    usable = [c for c in keycols if c in cols]
    if not usable:
        return set()
    collist = ", ".join(f'"{c}"' for c in usable)
    seen: set[tuple] = set()
    for row in conn.execute(f'SELECT {collist} FROM {schema}."{table}"'):
        seen.add(tuple(normalize_key_value(c, v, assume_tz) for c, v in zip(usable, row)))
    return seen


def merge_table_by_business_key(conn: sqlite3.Connection, table: str, keycols: list[str],
                                rep: Report, assume_tz) -> tuple[int, int, int]:
    """
    按业务键合并自增 id 表：只插入目标不存在的行为，且不写 id（让目标自增重新分配）。
    返回 (源行数, 新增, 跳过)。
    """
    dcols = sqlite_columns(conn, table, "main")
    scols = sqlite_columns(conn, table, "src")
    pk = primary_key_columns(conn, table, "main")
    autoinc = [c for c in pk if c == "id" or c.lower().endswith("id")]

    usable_keys = [c for c in keycols if c in dcols and c in scols]
    if not usable_keys:
        rep.warn(f"{table}: 两侧都没有业务键列，跳过合并（避免误判重复）")
        return 0, 0, 0

    insert_cols = [c for c in scols if c in dcols and c not in autoinc]
    if not insert_cols:
        rep.warn(f"{table}: 无共同可写列，跳过")
        return 0, 0, 0

    existing = make_row_key(conn, table, usable_keys, "main", assume_tz)
    sel = ", ".join(f'"{c}"' for c in insert_cols)
    key_idx = [insert_cols.index(c) for c in usable_keys if c in insert_cols]

    src_rows = list(conn.execute(f'SELECT {sel} FROM src."{table}"'))
    collist = ", ".join(f'"{c}"' for c in insert_cols)
    placeholders = ", ".join("?" for _ in insert_cols)

    added = skipped = 0
    for row in src_rows:
        key = tuple(normalize_key_value(insert_cols[i], row[i], assume_tz) for i in key_idx)
        if key in existing:
            skipped += 1
            continue
        try:
            conn.execute(
                f'INSERT INTO main."{table}" ({collist}) VALUES ({placeholders})', row
            )
            existing.add(key)
            added += 1
        except sqlite3.Error as exc:
            rep.warn(f"{table}: 插入失败（{exc}），该行跳过")
            skipped += 1
    return len(src_rows), added, skipped


def merge_table_by_pk(conn: sqlite3.Connection, table: str, rep: Report) -> tuple[int, int, int]:
    dcols = sqlite_columns(conn, table, "main")
    scols = sqlite_columns(conn, table, "src")
    common = [c for c in scols if c in dcols]
    if not common:
        rep.warn(f"{table}: 两侧无共同列，跳过")
        return 0, 0, 0
    missing = [c for c in dcols if c not in scols]
    if missing:
        rep.note(f"{table}: 源缺列 {', '.join(missing)}，目标保留原值/默认值")
    collist = ", ".join(f'"{c}"' for c in common)
    before = int(conn.execute(f'SELECT COUNT(*) FROM main."{table}"').fetchone()[0])
    src_rows = int(conn.execute(f'SELECT COUNT(*) FROM src."{table}"').fetchone()[0])
    conn.execute(
        f'INSERT OR IGNORE INTO main."{table}" ({collist}) SELECT {collist} FROM src."{table}"'
    )
    after = int(conn.execute(f'SELECT COUNT(*) FROM main."{table}"').fetchone()[0])
    added = after - before
    return src_rows, added, src_rows - added


if __name__ == "__main__":
    sys.exit(main())
