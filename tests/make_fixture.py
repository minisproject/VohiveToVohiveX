#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
构造 openvohive 的模拟部署。

关键点：openvohive 与 VoHiveX 内置核心（上游 vohive 二进制）共用同一套 GORM 模型，
因此真实 openvohive 库的 schema 与核心 AutoMigrate 出来的 schema 高度一致。
这里直接用核心生成的库当基线，再删掉核心独有的表和列 —— 这样得到的
「源库」与真实 openvohive 库结构等价，能真实暴露 AutoMigrate 兼容性问题。

用法：python3 make_fixture.py <输出目录> [核心生成的 vohive.db]
"""

import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

root = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/ovh-src").resolve()
baseline = Path(sys.argv[2] if len(sys.argv) > 2 else "/tmp/coretest/data/vohive.db")

(root / "config").mkdir(parents=True, exist_ok=True)
(root / "data").mkdir(parents=True, exist_ok=True)

(root / "config" / "config.yaml").write_text("""\
server:
    port: 7575
    debug: true
web:
    username: admin
    password: admin123
devices:
    - id: ec25-a
      name: 客厅模组
      modem_imei: "860000000000001"
      device_backend: qmi
      qmi_use_proxy: true
      mbim_transport: auto
      esim_transport: at
      baud_rate: 115200
      data_bits: 8
      stop_bits: 1
      parity: N
      usb_path: "1-1.2"
      at_port: /dev/ttyUSB2
      manage_port: /dev/ttyUSB3
      operator_selection_mode: auto
    - id: ec20-b
      name: 阳台模组
      modem_imei: "860000000000002"
      device_backend: at
    - id: bad-device
      name: 缺 IMEI
telegram:
    enabled: true
    bot_token: "123:ABC"
    chat_id: 100
    admin_id: 100
webhook:
    enabled: true
    urls: ["https://example.com/hook"]
    secret: "shh"
    timeout_ms: 5000
    retry_max: 3
email:
    enabled: false
    smtp_host: smtp.example.com
    smtp_port: 465
    username: me@example.com
    password: pw
    from_address: me@example.com
    to_addresses: ["a@example.com"]
proxy:
    enabled: true
""", encoding="utf-8")

db = root / "data" / "vohive.db"
for side in ("", "-wal", "-shm"):
    p = Path(str(db) + side)
    if p.exists():
        p.unlink()

if not baseline.is_file():
    sys.exit(f"缺少基线库 {baseline}\n  请先用真实核心启动一次以生成 schema：\n"
             f"    /tmp/vohive-core -c <某个config>")

# --- 1) 以核心 schema 为基线
src = sqlite3.connect(f"file:{baseline.as_posix()}?mode=ro", uri=True)
dst = sqlite3.connect(db)
src.backup(dst)
src.close()

# --- 2) 删掉核心独有、openvohive 没有的表
CORE_ONLY_TABLES = (
    "proxy_instances", "upstream_proxies", "upstream_proxy_country_rules",
    "workflows", "workflow_runs", "workflow_run_steps",
)
for table in CORE_ONLY_TABLES:
    dst.execute(f'DROP TABLE IF EXISTS "{table}"')

# --- 3) 删掉核心独有、openvohive 没有的列（SQLite 3.35+ 支持 DROP COLUMN）
CORE_ONLY_COLUMNS = {
    "card_policies": ["vowifi_enabled"],
    "sms_delivery_part": ["sip_code"],
}
for table, cols in CORE_ONLY_COLUMNS.items():
    for col in cols:
        try:
            dst.execute(f'ALTER TABLE "{table}" DROP COLUMN "{col}"')
        except sqlite3.Error as exc:
            print(f"  [warn] 无法删除 {table}.{col}: {exc}")

# --- 4) 灌入 openvohive 形状的数据
tz8 = timezone(timedelta(hours=8))
def ts(delta_hours=0, nanos=0):
    """GORM 的 SQLite 驱动格式：空格分隔 + 9 位纳秒 + 时区偏移。"""
    base = datetime(2026, 11, 27, 10, 0, 0, tzinfo=tz8) + timedelta(hours=delta_hours)
    text = base.strftime("%Y-%m-%d %H:%M:%S")
    if nanos:
        text += f".{nanos:09d}"
    return text + "+08:00"

dst.executemany(
    "INSERT INTO devices (imei,alias,model,firmware,port,public_ip,private_ip,"
    "public_ipv6,private_ipv6,iccid,sim_inserted,signal_dbm,signal_rsrq,signal_rsrp,"
    "last_seen,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
    [
        ("860000000000001", "客厅模组", "EC25", "EC25EFAR06A11M4G", "/dev/ttyUSB2",
         "1.2.3.4", "10.0.0.2", "", "", "89860012345678901234", 1, -70, -12, -100,
         ts(), ts(-240), ts()),
        ("860000000000002", "阳台模组", "EC20", "EC20CEFILG", "/dev/ttyUSB4",
         "", "", "", "", "", 0, 0, 0, 0, ts(), ts(-240), ts()),
    ],
)
dst.execute(
    "INSERT INTO sim_subscriptions (imsi,current_iccid,phone_number,modem_phone_number,"
    "vowifi_phone_number,operator,last_seen,created_at,updated_at)"
    " VALUES (?,?,?,?,?,?,?,?,?)",
    ("460001234567890", "89860012345678901234", "13800138000", "13800138000", "",
     "CMCC", ts(), ts(-240), ts()),
)
dst.execute(
    "INSERT INTO sim_cards (iccid,imsi,operator,current_imei,reg_status,reg_status_text,"
    "lac,cell_id,apn,ims_status,last_seen,created_at,updated_at)"
    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
    ("89860012345678901234", "460001234567890", "CMCC", "860000000000001", 1, "已注册",
     "1234", "56789", "cmnet", 1, ts(), ts(-240), ts()),
)
dst.execute(
    "INSERT INTO card_policies (iccid,network_enabled,airplane_enabled,ip_version,apn,"
    "source,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
    ("89860012345678901234", 1, 0, "v4v6", "cmnet", "user", ts(-240), ts()),
)
dst.execute(
    "INSERT INTO pending_phone_numbers (iccid,phone_number,modem_phone_number,"
    "vowifi_phone_number,created_at,updated_at) VALUES (?,?,?,?,?,?)",
    ("89860012345678901234", "", "13800138000", "", ts(), ts()),
)

# 短信：覆盖 GORM 纳秒时间、RFC3339、无时区、非法方向、坏时间五种形态
dst.executemany(
    "INSERT INTO sms (id,imsi,iccid,peer,local_phone,sender,recipient,content,type,"
    "status,timestamp,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
    [
        (1, "460001234567890", "89860012345678901234", "10690001", "13800138000",
         "10690001", "13800138000", "【验证码】123456", 1, 1, ts(-1, 123456789), ts(-1)),
        (2, "460001234567890", "89860012345678901234", "10690001", "13800138000",
         "", "", "收到谢谢", 2, 2, ts(0), ts(0)),
        (3, "460001234567890", "", "10086", "", "10086", "", "余额提醒", 1, 0,
         ts(-160), ts(-160)),
        (4, "460001234567890", "", "10010", "", "", "", "", 1, 0, ts(-161), ts(-161)),
        (5, "460001234567890", "", "10011", "", "", "", "方向非法", 3, 0, ts(-162), ts(-162)),
        (6, "460001234567890", "", "10012", "", "", "", "坏时间", 1, 0,
         "not-a-time", ts(-163)),
    ],
)
dst.execute(
    "INSERT INTO sms_contacts (imsi,iccid,peer,last_sms_id,last_timestamp,last_content,"
    "last_type,unread_count,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
    ("460001234567890", "89860012345678901234", "10690001", 2, ts(0), "收到谢谢", 2, 0,
     ts(-240), ts(0)),
)
dst.execute(
    "INSERT INTO sms_delivery (message_id,imsi,iccid,device_id,peer,content,parts_total,"
    "acks,state,last_error,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
    ("msg-1", "460001234567890", "89860012345678901234", "860000000000001",
     "10690001", "收到谢谢", 1, 1, "acked", "", ts(0), ts(0)),
)
dst.execute(
    "INSERT INTO sms_delivery_part (message_id,part_no,call_id,in_reply_to,rp_mr,state,"
    "rp_cause,error_text,sent_at,report_at,created_at,updated_at)"
    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
    ("msg-1", 1, "call-1", "", 1, "acked", 0, "", ts(0), ts(0, 500000000), ts(0), ts(0)),
)
dst.execute(
    "INSERT INTO traffic_minute (period_start,resource,tag,direction,traffic_bytes,"
    "created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
    (ts(-1), "ec25-a", "", 0, 1024, ts(-1), ts(-1)),
)

dst.commit()
tables = [r[0] for r in dst.execute(
    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
print(f"fixture ready: {root}")
print(f"  tables ({len(tables)}): {', '.join(tables)}")
for t in tables:
    n = dst.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
    if n:
        print(f"  {t:22} {n:>3} 行")
dst.close()