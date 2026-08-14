# -*- coding: utf-8 -*-
"""
在线时长助手（京麦咚咚客服用）

一个 Windows 小工具：
  - 一键记录“在线 / 离线”，自动累计当天在线时长
  - 对比目标在线时长（默认 7 小时 30 分），实时显示还差多少
  - 预计达标时间、下班时预计在线时长、需要加班补多少时长
  - 三个班次预设：早班 08:30-17:00 / 中班 11:00-20:00 / 晚班 14:00-22:00
  - 托盘图标、全局热键、锁屏自动记离线、悬浮窗、历史记录

运行：
  python main.py         开发模式
  main.py --selftest     核心逻辑自测（无界面）
"""
from __future__ import annotations

import ctypes
import datetime
import json
import os
import queue
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
from tkinter import messagebox, ttk

try:
    import winsound
except Exception:  # 非 Windows
    winsound = None

APP_NAME = "在线时长助手"
APP_VERSION = "1.0.0"

PRESETS = {
    "早班": ("08:30", "17:00"),
    "中班": ("11:00", "20:00"),
    "晚班": ("14:00", "22:00"),
}

DEFAULT_CFG = {
    "preset": "早班",
    "start": "08:30",
    "end": "17:00",
    "required_min": 450,          # 目标在线时长，默认 7小时30分
    "hotkey": "ctrl+alt+o",       # 全局热键：切换在线/离线
    "sound": True,                # 提示音
    "mini": True,                 # 启动时显示悬浮窗
    "lock_detect": True,          # 锁屏自动记离线
    "unlock_auto": True,          # 解锁自动回到在线（仅当离线是由锁屏自动记的）
    "confirm_offline": True,      # 未达标时点“离线”需确认
    "pre_alert": True,            # 下班前 10 分钟提醒
    "topmost": False,             # 主窗口置顶
}


# --------------------------------------------------------------------------
# 路径与文件读写
# --------------------------------------------------------------------------
def app_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def data_dir() -> str:
    env = os.environ.get("KEEPER_DATA_DIR")
    d = env if env else os.path.join(app_dir(), "data")
    try:
        os.makedirs(d, exist_ok=True)
        probe = os.path.join(d, ".probe")
        with open(probe, "w", encoding="utf-8") as f:
            f.write("ok")
        os.remove(probe)
        return d
    except Exception:
        fallback = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "OnlineTimeKeeper")
        os.makedirs(fallback, exist_ok=True)
        return fallback


def config_path() -> str:
    return os.path.join(data_dir(), "config.json")


def daily_path() -> str:
    return os.path.join(data_dir(), "daily.json")


def log_path() -> str:
    return os.path.join(data_dir(), "app.log")


def log(msg: str) -> None:
    try:
        with open(log_path(), "a", encoding="utf-8") as f:
            f.write("[%s] %s\n" % (datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg))
    except Exception:
        pass


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, obj) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


# --------------------------------------------------------------------------
# 格式化
# --------------------------------------------------------------------------
def fmt_hms(seconds) -> str:
    """秒 -> HH:MM:SS"""
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return "%02d:%02d:%02d" % (h, m, s)


def fmt_hm(minutes) -> str:
    """分钟 -> H:MM"""
    minutes = max(0, int(round(minutes)))
    return "%d:%02d" % (minutes // 60, minutes % 60)


def fmt_dur(seconds) -> str:
    """秒 -> 中文时长，如 1小时23分45秒"""
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    parts = []
    if h:
        parts.append("%d小时" % h)
    if m or h:
        parts.append("%d分" % m)
    parts.append("%d秒" % s)
    return "".join(parts)


def parse_hm(text) -> tuple:
    """'HH:MM' -> (小时, 分钟)，非法抛 ValueError"""
    parts = text.strip().split(":")
    if len(parts) != 2:
        raise ValueError("时间格式应为 小时:分钟，如 08:30")
    h, m = int(parts[0]), int(parts[1])
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError("时间超出范围")
    return h, m


# --------------------------------------------------------------------------
# 核心数据逻辑
# --------------------------------------------------------------------------
class Keeper:
    """负责配置、当日在线时段、统计。线程安全（内部锁）。"""

    def __init__(self):
        self.cfg = load_json(config_path(), {})
        for k, v in DEFAULT_CFG.items():
            self.cfg.setdefault(k, v)
        # 配置容错：损坏的时间/时长自动恢复默认
        try:
            parse_hm(self.cfg["start"])
            parse_hm(self.cfg["end"])
        except Exception:
            self.cfg["start"], self.cfg["end"] = "08:30", "17:00"
        try:
            self.cfg["required_min"] = max(1, min(1440, int(self.cfg["required_min"])))
        except Exception:
            self.cfg["required_min"] = 450
        self.daily = load_json(daily_path(), {})
        self._lock = threading.RLock()
        self.events = queue.Queue()      # 后台线程 -> 主界面 的事件队列
        self._last_save = 0.0

    # ---------------- 日期与记录 ----------------
    def today_key(self) -> str:
        return datetime.date.today().isoformat()

    def day_record(self, create=True):
        k = self.today_key()
        if k not in self.daily:
            if not create:
                return None
            self.daily[k] = {
                "shift": {
                    "preset": self.cfg["preset"],
                    "start": self.cfg["start"],
                    "end": self.cfg["end"],
                    "required_min": int(self.cfg["required_min"]),
                },
                "sessions": [],      # [[开始时间戳, 结束时间戳或None], ...] None 表示当前在线中
                "auto_offline": False,  # 最近一次离线是否为锁屏自动触发
            }
        return self.daily[k]

    # ---------------- 状态 ----------------
    def status(self, now=None) -> str:
        with self._lock:
            rec = self.day_record(create=False)
            if rec and rec["sessions"] and rec["sessions"][-1][1] is None:
                return "online"
            return "offline"

    def online_total(self, now=None) -> float:
        now = time.time() if now is None else now
        with self._lock:
            rec = self.day_record(create=False)
            if not rec:
                return 0.0
            total = 0.0
            for s, e in rec["sessions"]:
                if e is None:
                    total += max(0.0, now - s)
                else:
                    total += max(0.0, e - s)
            return total

    def offline_since(self, now=None):
        """离线中：返回本次离线已持续的秒数；在线中：None"""
        now = time.time() if now is None else now
        with self._lock:
            rec = self.day_record(create=False)
            if rec and rec["sessions"] and rec["sessions"][-1][1] is not None:
                return max(0.0, now - rec["sessions"][-1][1])
            return None

    def session_start_ts(self):
        """在线中：本次在线开始时间戳；离线中：None"""
        with self._lock:
            rec = self.day_record(create=False)
            if rec and rec["sessions"] and rec["sessions"][-1][1] is None:
                return rec["sessions"][-1][0]
            return None

    # ---------------- 切换 ----------------
    def toggle(self, source="manual") -> str:
        with self._lock:
            now = time.time()
            rec = self.day_record()
            if rec["sessions"] and rec["sessions"][-1][1] is None:
                rec["sessions"][-1][1] = now
                rec["auto_offline"] = False
                log("[%s] 离线 @ %s" % (source, datetime.datetime.fromtimestamp(now).strftime("%H:%M:%S")))
            else:
                rec["sessions"].append([now, None])
                rec["auto_offline"] = False
                log("[%s] 在线 @ %s" % (source, datetime.datetime.fromtimestamp(now).strftime("%H:%M:%S")))
            self.save_data()
            return self.status(now)

    def mark_offline(self, auto=False) -> bool:
        """强制切到离线。auto=True 表示锁屏自动触发。返回是否发生了切换。"""
        with self._lock:
            if self.status() == "online":
                rec = self.day_record()
                rec["sessions"][-1][1] = time.time()
                rec["auto_offline"] = bool(auto)
                self.save_data()
                log("锁屏自动离线" if auto else "离线")
                return True
            return False

    def mark_online(self, auto=False) -> bool:
        """强制切到在线。auto=True 且上次离线非自动触发时不做任何事。"""
        with self._lock:
            rec = self.day_record(create=False)
            if self.status() == "offline":
                if auto and not (rec and rec.get("auto_offline")):
                    return False
                day = self.day_record()
                day["sessions"].append([time.time(), None])
                day["auto_offline"] = False
                self.save_data()
                log("解锁自动回到在线" if auto else "回到在线")
                return True
            return False

    # ---------------- 统计 ----------------
    def stats(self, now=None) -> dict:
        now = time.time() if now is None else now
        dtnow = datetime.datetime.fromtimestamp(now)
        sh, sm = parse_hm(self.cfg["start"])
        eh, em = parse_hm(self.cfg["end"])
        start_dt = dtnow.replace(hour=sh, minute=sm, second=0, microsecond=0)
        end_dt = dtnow.replace(hour=eh, minute=em, second=0, microsecond=0)
        shift_len_min = (end_dt - start_dt).total_seconds() / 60.0
        if shift_len_min <= 0:      # 跨零点班次（如 22:00-次日06:00）
            shift_len_min += 1440.0
        req_min = max(1, int(self.cfg["required_min"]))
        online_sec = self.online_total(now)
        online_min = online_sec / 60.0
        remaining_min = max(0.0, req_min - online_min)
        qualified = remaining_min <= 0
        budget_min = shift_len_min - req_min            # 班次内可离线的总额度
        used_offline = max(0.0, shift_len_min - min(online_min, shift_len_min))
        free_left = max(0.0, budget_min - used_offline)
        over_used = max(0.0, used_offline - budget_min)
        past_end = now >= end_dt.timestamp()
        est_qualify_dt = dtnow + datetime.timedelta(minutes=remaining_min) if not qualified else None
        if past_end:
            at_end_online = online_min
        else:
            at_end_online = online_min + (0.0 if self.status(now) == "offline" else (end_dt.timestamp() - now) / 60.0)
        shortfall_at_end = max(0.0, req_min - at_end_online)
        need_overtime = past_end and not qualified
        ot_finish_dt = dtnow + datetime.timedelta(minutes=remaining_min) if need_overtime else None
        return {
            "now": now,
            "dtnow": dtnow,
            "start_dt": start_dt,
            "end_dt": end_dt,
            "shift_len_min": shift_len_min,
            "required_min": req_min,
            "online_sec": online_sec,
            "online_min": online_min,
            "remaining_min": remaining_min,
            "qualified": qualified,
            "budget_min": budget_min,
            "used_offline_min": used_offline,
            "free_left_min": free_left,
            "over_used_min": over_used,
            "past_end": past_end,
            "est_qualify_dt": est_qualify_dt,
            "at_end_online_min": at_end_online,
            "shortfall_at_end_min": shortfall_at_end,
            "need_overtime": need_overtime,
            "ot_finish_dt": ot_finish_dt,
            "status": self.status(now),
            "offline_since_sec": self.offline_since(now),
            "session_start_ts": self.session_start_ts(),
            "date": dtnow.date().isoformat(),
        }

    # ---------------- 手动修正 ----------------
    def add_session(self, start_ts, end_ts) -> None:
        with self._lock:
            rec = self.day_record()
            rec["sessions"].append([start_ts, end_ts])
            self.save_data()

    def delete_session(self, idx) -> None:
        with self._lock:
            rec = self.day_record(create=False)
            if rec and 0 <= idx < len(rec["sessions"]):
                rec["sessions"].pop(idx)
                self.save_data()

    def set_last_end(self, end_ts) -> None:
        with self._lock:
            rec = self.day_record(create=False)
            if rec and rec["sessions"]:
                rec["sessions"][-1][1] = end_ts
                rec["auto_offline"] = False
                self.save_data()

    def delete_day(self, date_str) -> None:
        with self._lock:
            if date_str in self.daily:
                del self.daily[date_str]
                save_json(daily_path(), self.daily)

    # ---------------- 保存 ----------------
    def save_data(self, force=False) -> None:
        now = time.time()
        if not force and now - self._last_save < 30:
            return
        self._last_save = now
        try:
            save_json(daily_path(), self.daily)
        except Exception as e:
            log("保存数据失败: %s" % e)

    def save_config(self) -> None:
        try:
            save_json(config_path(), self.cfg)
        except Exception as e:
            log("保存配置失败: %s" % e)


# --------------------------------------------------------------------------
# 图标
# --------------------------------------------------------------------------
def make_icon_image(size=64):
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse([1, 1, size - 1, size - 1], fill="#2D7DD2", outline="#FFFFFF")
    cx = cy = size / 2.0
    w = max(2, size // 12)
    d.line([cx, cy, cx, cy - size * 0.30], fill="#FFFFFF", width=w)
    d.line([cx, cy, cx + size * 0.22, cy + size * 0.16], fill="#FFFFFF", width=w)
    d.ellipse([cx - size * 0.07, cy - size * 0.07, cx + size * 0.07, cy + size * 0.07], fill="#FFFFFF")
    return img


# --------------------------------------------------------------------------
# 界面
# --------------------------------------------------------------------------
C_BG = "#EEF2F6"
C_CARD = "#FFFFFF"
C_BLUE = "#2D7DD2"
C_GREEN = "#27AE60"
C_ORANGE = "#E67E22"
C_RED = "#E74C3C"
C_GRAY = "#7F8C8D"
C_DARK = "#26323F"


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.keeper = Keeper()
        self.tray = None
        self.tray_ok = False
        self.mini_win = None
        self.icon_photo = None
        self.prev = None            # 上一次 stats，用于触发提醒
        self._cur_hotkey = None     # 当前已注册的热键
        self.end_alerted = False
        self.qualify_alerted = False
        self.pre_alerted = False
        self.hide_hinted = False
        self.dlg_open = None        # 记录是否已有设置/记录对话框

        self._build_ui()
        self._build_mini()
        self._start_tray()
        self._register_hotkey()
        self._start_lock_watch()
        self.refresh()
        self.root.after(250, self.tick)

    # ================= 主窗口 =================
    def _build_ui(self):
        root = self.root
        root.title("%s v%s" % (APP_NAME, APP_VERSION))
        root.configure(bg=C_BG)
        root.resizable(False, False)

        self.f_title = tkfont.Font(family="Microsoft YaHei UI", size=12, weight="bold")
        self.f_big = tkfont.Font(family="Microsoft YaHei UI", size=38, weight="bold")
        self.f_badge = tkfont.Font(family="Microsoft YaHei UI", size=13, weight="bold")
        self.f_norm = tkfont.Font(family="Microsoft YaHei UI", size=10)
        self.f_small = tkfont.Font(family="Microsoft YaHei UI", size=9)
        self.f_btn = tkfont.Font(family="Microsoft YaHei UI", size=13, weight="bold")
        self.f_card_v = tkfont.Font(family="Microsoft YaHei UI", size=15, weight="bold")

        root.grid_columnconfigure(0, weight=1)

        # --- 头部 ---
        head = tk.Frame(root, bg=C_BG)
        head.grid(row=0, column=0, sticky="ew", padx=16, pady=(12, 4))
        head.grid_columnconfigure(0, weight=1)
        tk.Label(head, text="在线时长助手", font=self.f_title, bg=C_BG, fg="#34495E").grid(row=0, column=0, sticky="w")
        self.l_date = tk.Label(head, text="", font=self.f_norm, bg=C_BG, fg=C_GRAY)
        self.l_date.grid(row=0, column=1, sticky="e")

        # --- 主卡片：时长 ---
        card = tk.Frame(root, bg=C_CARD, highlightbackground="#DDE4EA", highlightthickness=1)
        card.grid(row=1, column=0, sticky="ew", padx=16, pady=4)
        card.grid_columnconfigure(0, weight=1)
        self.l_time = tk.Label(card, text="00:00:00", font=self.f_big, bg=C_CARD, fg="#2C3E50")
        self.l_time.pack(pady=(14, 0))
        self.l_badge = tk.Label(card, text="", font=self.f_badge, bg=C_CARD, fg=C_GREEN)
        self.l_badge.pack(pady=(2, 0))
        self.l_status = tk.Label(card, text="", font=self.f_norm, bg=C_CARD, fg="#2C3E50")
        self.l_status.pack(pady=(4, 0))
        self.l_shift = tk.Label(card, text="", font=self.f_small, bg=C_CARD, fg=C_GRAY)
        self.l_shift.pack(pady=(2, 10))

        # --- 切换按钮 ---
        self.btn_toggle = tk.Button(root, text="", font=self.f_btn, bg=C_GREEN, fg="white",
                                    activebackground="#219A52", activeforeground="white",
                                    relief="flat", cursor="hand2", command=self.on_toggle)
        self.btn_toggle.grid(row=2, column=0, sticky="ew", padx=16, pady=4, ipady=8)

        # --- 提示条 ---
        self.l_tip = tk.Label(root, text="", font=self.f_norm, bg=C_BG, fg=C_GRAY)
        self.l_tip.grid(row=3, column=0, sticky="w", padx=18, pady=(2, 0))

        # --- 统计卡片 ---
        stats = tk.Frame(root, bg=C_BG)
        stats.grid(row=4, column=0, sticky="ew", padx=16, pady=(6, 2))
        for i in range(4):
            stats.grid_columnconfigure(i, weight=1)
        self.cards = []
        labels = [("--", "剩余需在线"), ("--", "可离线额度"), ("--", "预计达标"), ("--", "下班时预计")]
        for i, (v, cap) in enumerate(labels):
            c = tk.Frame(stats, bg=C_CARD, highlightbackground="#DDE4EA", highlightthickness=1)
            c.grid(row=0, column=i, sticky="ew", padx=4, pady=2)
            lv = tk.Label(c, text=v, font=self.f_card_v, bg=C_CARD, fg="#2C3E50")
            lv.pack(pady=(8, 0))
            lc = tk.Label(c, text=cap, font=self.f_small, bg=C_CARD, fg=C_GRAY)
            lc.pack(pady=(0, 8))
            self.cards.append((lv, lc))

        # --- 底部按钮 ---
        btns = tk.Frame(root, bg=C_BG)
        btns.grid(row=5, column=0, sticky="ew", padx=16, pady=(8, 12))
        for i in range(5):
            btns.grid_columnconfigure(i, weight=1)

        def mk_btn(text, cmd):
            return tk.Button(btns, text=text, font=self.f_norm, bg=C_CARD, fg="#34495E",
                             activebackground="#E4EAF0", relief="flat", cursor="hand2", command=cmd)

        mk_btn("班次/设置", self.open_settings).grid(row=0, column=0, sticky="ew", padx=3)
        mk_btn("今日记录", self.open_today).grid(row=0, column=1, sticky="ew", padx=3)
        mk_btn("历史记录", self.open_history).grid(row=0, column=2, sticky="ew", padx=3)
        mk_btn("悬浮窗", self.toggle_mini).grid(row=0, column=3, sticky="ew", padx=3)
        mk_btn("隐藏到托盘", self.hide_to_tray).grid(row=0, column=4, sticky="ew", padx=3)

        root.protocol("WM_DELETE_WINDOW", self.hide_to_tray)
        root.attributes("-topmost", bool(self.keeper.cfg.get("topmost", False)))

        # 图标
        try:
            import base64, io as _io
            from PIL import Image
            img = make_icon_image(64)
            buf = _io.BytesIO()
            img.save(buf, format="PNG")
            self.icon_photo = tk.PhotoImage(data=base64.b64encode(buf.getvalue()).decode("ascii"))
            root.iconphoto(True, self.icon_photo)
        except Exception as e:
            log("窗口图标生成失败: %s" % e)

    # ================= 刷新 =================
    def refresh(self):
        st = self.keeper.stats()
        self.l_date.config(text=st["dtnow"].strftime("%Y年%m月%d日 %A"))
        self.l_time.config(text=fmt_hms(st["online_sec"]))
        req_txt = "%d:%02d" % (st["required_min"] // 60, st["required_min"] % 60)

        if st["qualified"]:
            self.l_badge.config(text="✓ 今日已达标（目标 %s）" % req_txt, fg=C_GREEN)
        else:
            if st["need_overtime"]:
                self.l_badge.config(text="还差 %s（已过下班时间）" % fmt_hms(st["remaining_min"] * 60), fg=C_RED)
            else:
                self.l_badge.config(text="还差 %s" % fmt_hms(st["remaining_min"] * 60), fg=C_ORANGE)

        if st["status"] == "online":
            since = datetime.datetime.fromtimestamp(st["session_start_ts"]).strftime("%H:%M")
            self.l_status.config(text="● 在线中（自 %s 开始）" % since, fg=C_GREEN)
            self.btn_toggle.config(text="开始离线（去吃饭 / 休息 / 上厕所）", bg="#F39C12",
                                   activebackground="#D68910", fg="white", activeforeground="white")
        else:
            since = st["offline_since_sec"]
            t = fmt_hms(since) if since is not None else "--:--:--"
            self.l_status.config(text="○ 离线中（本次已离线 %s）" % t, fg=C_RED)
            self.btn_toggle.config(text="回到在线（继续接待）", bg=C_GREEN,
                                   activebackground="#219A52", fg="white", activeforeground="white")

        self.l_shift.config(text="班次：%s %s - %s   |   目标在线 %s   |   班次时长 %s"
                            % (self.keeper.cfg["preset"], self.keeper.cfg["start"], self.keeper.cfg["end"],
                               req_txt, fmt_hm(st["shift_len_min"])))

        # 提示条
        if st["need_overtime"]:
            self.l_tip.config(text="⚠ 已到下班时间，还差 %s，建议持续在线至约 %s 再达标"
                              % (fmt_hms(st["remaining_min"] * 60), st["ot_finish_dt"].strftime("%H:%M")), fg=C_RED)
        elif not st["qualified"]:
            self.l_tip.config(text="还差 %s 未达标；预计 %s 达标（若保持在线）"
                              % (fmt_hms(st["remaining_min"] * 60), st["est_qualify_dt"].strftime("%H:%M")), fg=C_ORANGE)
        else:
            self.l_tip.config(text="今日已达标 ✓ 剩余时间可自由离线休息", fg=C_GREEN)

        # 统计卡片
        r1 = self.cards[0]
        if st["qualified"]:
            r1[0].config(text="0:00:00", fg=C_GREEN)
        else:
            r1[0].config(text=fmt_hms(st["remaining_min"] * 60), fg=C_ORANGE)
        r1[1].config(text="距目标 %s" % req_txt)

        r2 = self.cards[1]
        if st["over_used_min"] > 0:
            r2[0].config(text="超用 %d 分" % int(st["over_used_min"]), fg=C_RED)
            r2[1].config(text="额度共 %d 分" % int(st["budget_min"]))
        else:
            r2[0].config(text="%d 分钟" % int(st["free_left_min"]), fg="#2C3E50")
            r2[1].config(text="额度共 %d 分（已用 %d）" % (int(st["budget_min"]), int(st["used_offline_min"])))

        r3 = self.cards[2]
        if st["est_qualify_dt"] is not None:
            r3[0].config(text=st["est_qualify_dt"].strftime("%H:%M"), fg="#2C3E50")
            r3[1].config(text="达标时间（若持续在线）")
        else:
            r3[0].config(text="已达标", fg=C_GREEN)
            r3[1].config(text="达标时间")

        r4 = self.cards[3]
        if st["need_overtime"]:
            r4[0].config(text="加班至 %s" % st["ot_finish_dt"].strftime("%H:%M"), fg=C_RED)
            r4[1].config(text="已过下班时间，还差 %s" % fmt_hms(st["remaining_min"] * 60))
        else:
            r4[0].config(text="%s / %s" % (fmt_hm(st["at_end_online_min"]), req_txt),
                         fg=C_RED if st["shortfall_at_end_min"] > 0 else C_GREEN)
            r4[1].config(text="下班 %s（还有 %s）" % (st["end_dt"].strftime("%H:%M"),
                          fmt_hm(max(0, (st["end_dt"].timestamp() - st["now"]) / 60.0))))

        self._refresh_mini(st)

    # ================= 事件循环 =================
    def tick(self):
        try:
            while True:
                ev = self.keeper.events.get_nowait()
                self.handle_event(ev)
        except queue.Empty:
            pass
        try:
            self.refresh()
            self._alerts()
        except Exception as e:
            log("刷新界面异常: %s" % e)
        st = self.keeper.stats()
        if st["status"] == "online" and time.time() - self.keeper._last_save > 60:
            self.keeper.save_data(force=True)
        self.root.after(250, self.tick)

    def handle_event(self, ev):
        name = ev[0]
        if name == "hotkey" or name == "tray_toggle":
            self.on_toggle(quiet=True)
        elif name == "tray_show":
            self.show_window()
        elif name == "tray_mini":
            self.toggle_mini()
        elif name == "tray_settings":
            self.open_settings()
        elif name == "tray_quit":
            self.keeper.save_data(force=True)
            try:
                if self.tray is not None:
                    self.tray.stop()
            except Exception:
                pass
            self.root.destroy()
        elif name == "auto_offline":
            self.notify("已自动记为离线", "检测到电脑锁屏，已自动记录为离线。\n解锁后将自动回到在线（可在设置中关闭）。")
        elif name == "auto_online":
            self.notify("已自动回到在线", "检测到电脑解锁，已自动记录为在线。")
        elif name == "notify":
            self.notify(ev[1], ev[2])
        elif name == "tray_title":
            self._set_tray_title(ev[1])
        elif name == "menu_update":
            self._update_tray_menu()

    def on_toggle(self, quiet=False):
        if self.keeper.status() == "online":
            st = self.keeper.stats()
            if not st["qualified"] and self.keeper.cfg.get("confirm_offline", True):
                msg = ("当前还差 %s 未达标。\n确定现在离线吗？\n（下班时可能需加班补时长）" % fmt_hms(st["remaining_min"] * 60))
                parent = self.root if self.root.state() == "normal" else None
                if not messagebox.askyesno("确认离线", msg, parent=parent):
                    return
        self.keeper.toggle("manual")
        self.refresh()
        if self.keeper.cfg.get("sound", True) and winsound is not None:
            try:
                winsound.MessageBeep(winsound.MB_OK)
            except Exception:
                pass

    # ---------------- 提醒 ----------------
    def _alerts(self):
        st = self.keeper.stats()
        prev = self.prev
        self.prev = st
        if prev is None:
            return
        sound = self.keeper.cfg.get("sound", True)
        # 达标瞬间
        if not prev["qualified"] and st["qualified"] and not self.qualify_alerted:
            self.qualify_alerted = True
            self.notify("🎉 今日已达标！", "在线时长已达 %s，可以放心离线休息了。" % fmt_hms(st["online_sec"]))
            if sound and winsound is not None:
                try:
                    winsound.MessageBeep(winsound.MB_ICONASTERISK)
                except Exception:
                    pass
        # 下班瞬间
        if not prev["past_end"] and st["past_end"] and not self.end_alerted:
            self.end_alerted = True
            if st["qualified"]:
                self.notify("已到下班时间", "今日已达标 ✓ 可以准时下班啦。")
            else:
                self.notify("已到下班时间，还差时长", "还差 %s，预计需工作到约 %s 才达标。"
                            % (fmt_hms(st["remaining_min"] * 60), st["ot_finish_dt"].strftime("%H:%M")))
        # 下班前10分钟
        if self.keeper.cfg.get("pre_alert", True) and not st["past_end"] and not st["qualified"] and not self.pre_alerted:
            left = (st["end_dt"].timestamp() - st["now"]) / 60.0
            if left <= 10:
                self.pre_alerted = True
                self.notify("距离下班还有10分钟",
                            "已在线 %s，还差 %s。若需达标，建议保持在线或准备加班。"
                            % (fmt_hms(st["online_sec"]), fmt_hms(st["remaining_min"] * 60)))

    # ================= 悬浮窗 =================
    def _build_mini(self):
        self.mini_visible = self.keeper.cfg.get("mini", True)
        win = tk.Toplevel(self.root)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.configure(bg=C_DARK)
        frame = tk.Frame(win, bg=C_DARK)
        frame.pack(fill="both", expand=True)
        self.mini_dot = tk.Label(frame, text="●", font=("Microsoft YaHei UI", 11, "bold"), bg=C_DARK, fg=C_GREEN)
        self.mini_dot.pack(side="left", padx=(10, 4), pady=10)
        self.mini_text = tk.Label(frame, text="在线 00:00:00", font=("Microsoft YaHei UI", 10, "bold"),
                                  bg=C_DARK, fg="white")
        self.mini_text.pack(side="left", padx=2)
        tk.Button(frame, text="切", font=("Microsoft YaHei UI", 9, "bold"), bg="#3D5164", fg="white",
                  relief="flat", cursor="hand2", width=3, command=self.on_toggle).pack(side="right", padx=(2, 2), pady=4)
        tk.Button(frame, text="×", font=("Microsoft YaHei UI", 9, "bold"), bg="#3D5164", fg="white",
                  relief="flat", cursor="hand2", width=2, command=self.hide_mini).pack(side="right", pady=4)

        # 拖动（标签区域也可拖动）
        self._drag_off = None
        for w in (frame, self.mini_dot, self.mini_text):
            w.bind("<ButtonPress-1>", self._mini_press)
            w.bind("<B1-Motion>", self._mini_drag)
            w.bind("<ButtonRelease-1>", self._mini_release)
            w.bind("<Double-Button-1>", lambda e: self.on_toggle())
        self.mini_win = win

        pos = self.keeper.cfg.get("mini_pos")
        if pos:
            win.geometry("+%d+%d" % (pos[0], pos[1]))
        else:
            win.update_idletasks()
            sw = win.winfo_screenwidth()
            sh = win.winfo_screenheight()
            win.geometry("+%d+%d" % (sw - 360, sh - 120))
        if self.mini_visible:
            win.deiconify()
        else:
            win.withdraw()

    def _mini_press(self, e):
        self._drag_off = (e.x_root - self.mini_win.winfo_x(), e.y_root - self.mini_win.winfo_y())

    def _mini_drag(self, e):
        if self._drag_off:
            self.mini_win.geometry("+%d+%d" % (e.x_root - self._drag_off[0], e.y_root - self._drag_off[1]))

    def _mini_release(self, e):
        if self._drag_off:
            self._drag_off = None
            try:
                self.keeper.cfg["mini_pos"] = [self.mini_win.winfo_x(), self.mini_win.winfo_y()]
                self.keeper.save_config()
            except Exception:
                pass

    def _refresh_mini(self, st):
        if self.mini_win is None:
            return
        if st["status"] == "online":
            self.mini_dot.config(fg=C_GREEN)
            text = "在线 %s" % fmt_hms(st["online_sec"])
        else:
            self.mini_dot.config(fg="#E74C3C")
            off = st["offline_since_sec"]
            text = "离线 %s" % (fmt_hms(off) if off is not None else "--:--:--")
        if st["qualified"]:
            text += "  ✓达标"
        self.mini_text.config(text=text)

    def toggle_mini(self):
        self.mini_visible = not self.mini_visible
        if self.mini_visible:
            self.mini_win.deiconify()
        else:
            self.mini_win.withdraw()
        self.keeper.cfg["mini"] = self.mini_visible
        self.keeper.save_config()

    def hide_mini(self):
        self.mini_visible = False
        self.mini_win.withdraw()
        self.keeper.cfg["mini"] = False
        self.keeper.save_config()

    # ================= 托盘 =================
    def _build_menu(self):
        import pystray
        st = self.keeper.stats()
        status_text = "在线中" if st["status"] == "online" else "离线中"
        return pystray.Menu(
            pystray.MenuItem("显示主窗口", lambda i, t: self.keeper.events.put(("tray_show", None)), default=True),
            pystray.MenuItem("切换在线/离线（当前：%s）" % status_text,
                             lambda i, t: self.keeper.events.put(("tray_toggle", None))),
            pystray.MenuItem("显示/隐藏悬浮窗", lambda i, t: self.keeper.events.put(("tray_mini", None))),
            pystray.MenuItem("设置", lambda i, t: self.keeper.events.put(("tray_settings", None))),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出", lambda i, t: self.keeper.events.put(("tray_quit", None))),
        )

    def _start_tray(self):
        try:
            import pystray
            self.tray = pystray.Icon("online_time_keeper", make_icon_image(64), APP_NAME, self._build_menu())
            self.tray.run_detached()
            self.tray_ok = True
            log("托盘已启动")
        except Exception as e:
            self.tray_ok = False
            log("托盘启动失败: %s" % e)

    def _update_tray_menu(self):
        try:
            if self.tray is not None:
                self.tray.menu = self._build_menu()
                self.tray.update_menu()
        except Exception:
            pass

    def _set_tray_title(self, text):
        try:
            if self.tray is not None:
                self.tray.title = text
        except Exception:
            pass

    def notify(self, title, msg):
        if self.tray_ok and self.tray is not None:
            try:
                self.tray.notify(msg, title)
            except Exception as e:
                log("托盘通知失败: %s" % e)

    # ================= 热键 =================
    def _register_hotkey(self):
        try:
            import keyboard
        except Exception as e:
            log("keyboard 库不可用: %s" % e)
            return
        try:
            prev = self._cur_hotkey
            if prev:
                try:
                    keyboard.remove_hotkey(prev)
                except Exception:
                    pass
            key = self.keeper.cfg.get("hotkey", "").strip()
            if key:
                keyboard.parse_hotkey(key)   # 校验格式
                keyboard.add_hotkey(key, lambda: self.keeper.events.put(("hotkey", None)))
                self._cur_hotkey = key
            else:
                self._cur_hotkey = None
            log("全局热键已注册: %s" % (key or "（已禁用）"))
        except Exception as e:
            log("全局热键注册失败: %s" % e)

    # ================= 锁屏检测 =================
    def _start_lock_watch(self):
        def run():
            locked = None
            while True:
                try:
                    now_locked = is_workstation_locked()
                except Exception:
                    now_locked = False
                if now_locked != locked:
                    locked = now_locked
                    if locked and self.keeper.cfg.get("lock_detect", True):
                        if self.keeper.mark_offline(auto=True):
                            self.keeper.events.put(("auto_offline", None))
                    elif not locked and self.keeper.cfg.get("unlock_auto", True):
                        if self.keeper.mark_online(auto=True):
                            self.keeper.events.put(("auto_online", None))
                time.sleep(3)
        threading.Thread(target=run, daemon=True).start()

    # ================= 窗口显隐 =================
    def show_window(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def hide_to_tray(self):
        self.root.withdraw()
        if self.tray_ok and not self.keeper.cfg.get("hide_hinted", False):
            self.keeper.cfg["hide_hinted"] = True
            self.keeper.save_config()
            self.notify("仍在后台运行", "主窗口已隐藏，程序在托盘继续计时。\n点托盘图标可恢复显示，或按热键 %s 切换在线/离线。"
                        % self.keeper.cfg.get("hotkey", ""))

    # ================= 设置 =================
    def open_settings(self):
        if self.dlg_open == "settings":
            return
        self.dlg_open = "settings"
        cfg = self.keeper.cfg
        dlg = tk.Toplevel(self.root)
        dlg.title("设置")
        dlg.configure(bg=C_BG)
        dlg.resizable(False, False)
        dlg.transient(self.root)
        dlg.grab_set()
        dlg.geometry("+%d+%d" % (self.root.winfo_rootx() + 80, self.root.winfo_rooty() + 60))

        body = tk.Frame(dlg, bg=C_BG)
        body.pack(padx=18, pady=14, fill="both", expand=True)

        def row(label):
            f = tk.Frame(body, bg=C_BG)
            f.pack(fill="x", pady=4)
            tk.Label(f, text=label, font=self.f_norm, bg=C_BG, fg="#34495E", width=14, anchor="w").pack(side="left")
            return f

        # 班次
        f = row("班次：")
        preset_var = tk.StringVar(value=cfg["preset"])
        cb = ttk.Combobox(f, textvariable=preset_var, values=list(PRESETS.keys()) + ["自定义"],
                          state="readonly", width=14, font=self.f_norm)
        cb.pack(side="left")
        start_var = tk.StringVar(value=cfg["start"])
        end_var = tk.StringVar(value=cfg["end"])
        tk.Label(f, text="开始", font=self.f_small, bg=C_BG, fg=C_GRAY).pack(side="left", padx=(14, 2))
        tk.Entry(f, textvariable=start_var, width=6, font=self.f_norm).pack(side="left")
        tk.Label(f, text="结束", font=self.f_small, bg=C_BG, fg=C_GRAY).pack(side="left", padx=(8, 2))
        tk.Entry(f, textvariable=end_var, width=6, font=self.f_norm).pack(side="left")

        def on_preset(e=None):
            p = preset_var.get()
            if p in PRESETS:
                start_var.set(PRESETS[p][0])
                end_var.set(PRESETS[p][1])
        cb.bind("<<ComboboxSelected>>", on_preset)

        # 目标时长
        f = row("目标在线时长：")
        req_var = tk.StringVar(value="%d:%02d" % (cfg["required_min"] // 60, cfg["required_min"] % 60))
        tk.Entry(f, textvariable=req_var, width=8, font=self.f_norm).pack(side="left")
        tk.Label(f, text="（小时:分钟，如 7:30）", font=self.f_small, bg=C_BG, fg=C_GRAY).pack(side="left", padx=6)

        # 热键
        f = row("切换热键：")
        hotkey_var = tk.StringVar(value=cfg["hotkey"])
        tk.Entry(f, textvariable=hotkey_var, width=18, font=self.f_norm).pack(side="left")
        tk.Label(f, text="（如 ctrl+alt+o，留空禁用）", font=self.f_small, bg=C_BG, fg=C_GRAY).pack(side="left", padx=6)

        # 选项
        opt = tk.Frame(body, bg=C_BG)
        opt.pack(fill="x", pady=6)
        vars_ = {}
        items = [
            ("sound", "提示音与通知"),
            ("lock_detect", "锁屏自动记离线"),
            ("unlock_auto", "解锁自动回到在线"),
            ("confirm_offline", "未达标离线时确认"),
            ("pre_alert", "下班前10分钟提醒"),
            ("topmost", "主窗口置顶"),
        ]
        for i, (key, text) in enumerate(items):
            v = tk.IntVar(value=1 if cfg.get(key, False) else 0)
            vars_[key] = v
            tk.Checkbutton(opt, text=text, variable=v, font=self.f_norm, bg=C_BG, fg="#34495E",
                           activebackground=C_BG).grid(row=i // 2, column=i % 2, sticky="w", padx=(0, 24), pady=2)

        btns = tk.Frame(body, bg=C_BG)
        btns.pack(fill="x", pady=(10, 0))

        def on_save():
            try:
                start_var.set(start_var.get().strip())
                end_var.set(end_var.get().strip())
                sh, sm = parse_hm(start_var.get())
                eh, em = parse_hm(end_var.get())
                if (eh * 60 + em) <= (sh * 60 + sm):
                    messagebox.showerror("设置", "结束时间必须晚于开始时间。", parent=dlg)
                    return
                rq = req_var.get().strip()
                rparts = rq.split(":")
                if len(rparts) != 2:
                    raise ValueError("目标时长格式错误")
                req_min = int(rparts[0]) * 60 + int(rparts[1])
                if not (60 <= req_min <= 1440):
                    raise ValueError("目标时长需在 1 小时到 24 小时之间")
                hk = hotkey_var.get().strip()
                if hk:
                    import keyboard
                    keyboard.parse_hotkey(hk)
            except ImportError:
                if hotkey_var.get().strip():
                    messagebox.showerror("设置", "热键功能不可用，请留空热键。", parent=dlg)
                    return
            except Exception as e:
                messagebox.showerror("设置", "输入有误：%s" % e, parent=dlg)
                return
            p = preset_var.get()
            if p in PRESETS and PRESETS[p] == (start_var.get(), end_var.get()):
                cfg["preset"] = p
            else:
                cfg["preset"] = "自定义" if p not in PRESETS else p
            cfg["start"] = start_var.get()
            cfg["end"] = end_var.get()
            cfg["required_min"] = req_min
            cfg["hotkey"] = hotkey_var.get().strip()
            for key, v in vars_.items():
                cfg[key] = bool(v.get())
            self.keeper.save_config()
            self._register_hotkey()
            self.root.attributes("-topmost", cfg.get("topmost", False))
            self.refresh()
            dlg.destroy()
            self.dlg_open = None

        tk.Button(btns, text="保存", font=self.f_norm, bg=C_BLUE, fg="white", relief="flat", cursor="hand2",
                  command=on_save).pack(side="left", padx=(0, 10), ipadx=18, ipady=4)
        tk.Button(btns, text="取消", font=self.f_norm, bg="#BDC3C7", fg="white", relief="flat", cursor="hand2",
                  command=lambda: (dlg.destroy(), setattr(self, "dlg_open", None))).pack(side="left", ipadx=18, ipady=4)

        dlg.protocol("WM_DELETE_WINDOW", lambda: (dlg.destroy(), setattr(self, "dlg_open", None)))

    # ================= 今日记录 =================
    def open_today(self):
        if self.dlg_open == "today":
            return
        self.dlg_open = "today"
        dlg = tk.Toplevel(self.root)
        dlg.title("今日记录（可修正）")
        dlg.configure(bg=C_BG)
        dlg.transient(self.root)
        dlg.grab_set()
        dlg.geometry("+%d+%d" % (self.root.winfo_rootx() + 60, self.root.winfo_rooty() + 40))

        tree = ttk.Treeview(dlg, columns=("start", "end", "dur"), show="headings", height=10)
        tree.heading("start", text="开始时间")
        tree.heading("end", text="结束时间")
        tree.heading("dur", text="时长")
        tree.column("start", width=120, anchor="center")
        tree.column("end", width=120, anchor="center")
        tree.column("dur", width=90, anchor="center")
        tree.pack(padx=12, pady=10, fill="both", expand=True)

        def load_rows():
            tree.delete(*tree.get_children())
            rec = self.keeper.day_record(create=False)
            if not rec:
                total_lbl.config(text="今日暂无记录")
                return
            st = self.keeper.stats()
            total = 0
            for i, (s, e) in enumerate(rec["sessions"]):
                se = e if e is not None else st["now"]
                dur = max(0, se - s)
                total += dur
                end_txt = datetime.datetime.fromtimestamp(se).strftime("%H:%M:%S") if e is not None else "在线中…"
                tree.insert("", "end", iid=str(i),
                            values=(datetime.datetime.fromtimestamp(s).strftime("%H:%M:%S"),
                                    end_txt, fmt_hms(dur)))
            total_lbl.config(text="共 %d 段，在线合计 %s" % (len(rec["sessions"]), fmt_hms(total)))

        total_lbl = tk.Label(dlg, text="", font=self.f_norm, bg=C_BG, fg="#34495E")
        total_lbl.pack(pady=(0, 6))
        load_rows()

        btns = tk.Frame(dlg, bg=C_BG)
        btns.pack(pady=(0, 12))

        def add_manual():
            sub = tk.Toplevel(dlg)
            sub.title("补记在线时段")
            sub.configure(bg=C_BG)
            sub.transient(dlg)
            sub.grab_set()
            f = tk.Frame(sub, bg=C_BG)
            f.pack(padx=14, pady=12)
            now = datetime.datetime.now()
            s_var = tk.StringVar(value=(now - datetime.timedelta(minutes=30)).strftime("%H:%M"))
            e_var = tk.StringVar(value=now.strftime("%H:%M"))
            tk.Label(f, text="开始", font=self.f_norm, bg=C_BG).grid(row=0, column=0, padx=4)
            tk.Entry(f, textvariable=s_var, width=6, font=self.f_norm).grid(row=0, column=1, padx=4)
            tk.Label(f, text="结束", font=self.f_norm, bg=C_BG).grid(row=0, column=2, padx=4)
            tk.Entry(f, textvariable=e_var, width=6, font=self.f_norm).grid(row=0, column=3, padx=4)

            def ok():
                try:
                    d = datetime.date.today()
                    sh, sm = parse_hm(s_var.get())
                    eh, em = parse_hm(e_var.get())
                    sts = datetime.datetime(d.year, d.month, d.day, sh, sm).timestamp()
                    ets = datetime.datetime(d.year, d.month, d.day, eh, em).timestamp()
                    if ets <= sts:
                        raise ValueError("结束必须晚于开始")
                    if ets > time.time() + 60:
                        raise ValueError("结束时间不能在未来")
                except ValueError as e:
                    messagebox.showerror("补记", str(e), parent=sub)
                    return
                except Exception as e:
                    messagebox.showerror("补记", "时间格式错误：%s" % e, parent=sub)
                    return
                self.keeper.add_session(sts, ets)
                sub.destroy()
                load_rows()
                self.refresh()

            tk.Button(f, text="确定", font=self.f_norm, bg=C_BLUE, fg="white", relief="flat", cursor="hand2",
                      command=ok).grid(row=1, column=1, columnspan=2, pady=(10, 0), ipadx=16, ipady=3)

        def delete_sel():
            sel = tree.selection()
            if not sel:
                messagebox.showinfo("今日记录", "请先选中要删除的时段。", parent=dlg)
                return
            idx = int(sel[0])
            if messagebox.askyesno("删除", "确定删除选中的这一段时间记录吗？", parent=dlg):
                self.keeper.delete_session(idx)
                load_rows()
                self.refresh()

        def close_last():
            rec = self.keeper.day_record(create=False)
            if rec and rec["sessions"] and rec["sessions"][-1][1] is None:
                self.keeper.set_last_end(time.time())
                load_rows()
                self.refresh()

        tk.Button(btns, text="补记一段", font=self.f_norm, bg=C_CARD, fg="#34495E", relief="flat", cursor="hand2",
                  command=add_manual).pack(side="left", padx=4, ipadx=10, ipady=3)
        tk.Button(btns, text="结束最后一段(到现在)", font=self.f_norm, bg=C_CARD, fg="#34495E", relief="flat",
                  cursor="hand2", command=close_last).pack(side="left", padx=4, ipadx=10, ipady=3)
        tk.Button(btns, text="删除选中", font=self.f_norm, bg="#E74C3C", fg="white", relief="flat", cursor="hand2",
                  command=delete_sel).pack(side="left", padx=4, ipadx=10, ipady=3)
        tk.Button(btns, text="关闭", font=self.f_norm, bg="#BDC3C7", fg="white", relief="flat", cursor="hand2",
                  command=lambda: (dlg.destroy(), setattr(self, "dlg_open", None))).pack(side="left", padx=4, ipadx=10, ipady=3)

        dlg.protocol("WM_DELETE_WINDOW", lambda: (dlg.destroy(), setattr(self, "dlg_open", None)))

    # ================= 历史记录 =================
    def open_history(self):
        if self.dlg_open == "history":
            return
        self.dlg_open = "history"
        dlg = tk.Toplevel(self.root)
        dlg.title("历史记录")
        dlg.configure(bg=C_BG)
        dlg.transient(self.root)
        dlg.grab_set()
        dlg.geometry("+%d+%d" % (self.root.winfo_rootx() + 60, self.root.winfo_rooty() + 40))

        tree = ttk.Treeview(dlg, columns=("date", "shift", "req", "online", "diff", "ok"), show="headings", height=14)
        for col, txt, w in [("date", "日期", 110), ("shift", "班次", 150), ("req", "目标", 80),
                            ("online", "已在线", 90), ("diff", "差额", 90), ("ok", "状态", 70)]:
            tree.heading(col, text=txt)
            tree.column(col, width=w, anchor="center")
        tree.pack(padx=12, pady=10, fill="both", expand=True)

        dates = sorted(self.keeper.daily.keys(), reverse=True)
        for d in dates:
            rec = self.keeper.daily[d]
            req = int(rec.get("shift", {}).get("required_min", 450))
            total = 0.0
            for s, e in rec["sessions"]:
                total += max(0, (e if e is not None else time.time()) - s)
            diff_min = total / 60.0 - req
            ok = diff_min >= 0
            shift_txt = "%s %s-%s" % (rec.get("shift", {}).get("preset", "?"),
                                      rec.get("shift", {}).get("start", "?"), rec.get("shift", {}).get("end", "?"))
            tree.insert("", "end", iid=d, values=(
                d, shift_txt, fmt_hm(req), fmt_hm(total / 60.0),
                "富余 %s" % fmt_hm(diff_min) if ok else "差 %s" % fmt_hm(-diff_min),
                "✓ 达标" if ok else "✗ 不足"))

        btns = tk.Frame(dlg, bg=C_BG)
        btns.pack(pady=(0, 12))

        def del_day():
            sel = tree.selection()
            if not sel:
                messagebox.showinfo("历史记录", "请先选中要删除的日期。", parent=dlg)
                return
            d = sel[0]
            if messagebox.askyesno("删除记录", "确定删除 %s 的全部记录吗？（不可恢复）" % d, parent=dlg):
                self.keeper.delete_day(d)
                tree.delete(d)

        tk.Button(btns, text="删除选中日期", font=self.f_norm, bg="#E74C3C", fg="white", relief="flat",
                  cursor="hand2", command=del_day).pack(side="left", padx=4, ipadx=10, ipady=3)
        tk.Button(btns, text="关闭", font=self.f_norm, bg="#BDC3C7", fg="white", relief="flat", cursor="hand2",
                  command=lambda: (dlg.destroy(), setattr(self, "dlg_open", None))).pack(side="left", padx=4, ipadx=10, ipady=3)

        dlg.protocol("WM_DELETE_WINDOW", lambda: (dlg.destroy(), setattr(self, "dlg_open", None)))


# --------------------------------------------------------------------------
# 系统辅助
# --------------------------------------------------------------------------
def is_workstation_locked() -> bool:
    try:
        user32 = ctypes.windll.user32
        h = user32.OpenInputDesktop(0, False, 0x0100)  # DESKTOP_READOBJECTS
        if not h:
            return True
        user32.CloseDesktop(h)
        return False
    except Exception:
        return False


def single_instance_check() -> bool:
    """已运行则返回 False。持有互斥体句柄防止被回收。"""
    global _instance_mutex
    try:
        kernel32 = ctypes.windll.kernel32
        _instance_mutex = kernel32.CreateMutexW(None, False, "Local\\OnlineTimeKeeper_Instance")
        if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
            return False
    except Exception:
        pass
    return True


def msg_box(title, text):
    try:
        ctypes.windll.user32.MessageBoxW(0, text, title, 0x40)
    except Exception:
        pass


# --------------------------------------------------------------------------
# 自测
# --------------------------------------------------------------------------
def selftest() -> bool:
    import shutil
    if sys.stdout is None:  # 打包为无控制台程序时
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    tmp = os.path.join(app_dir(), "selftest_tmp")
    os.makedirs(tmp, exist_ok=True)
    os.environ["KEEPER_DATA_DIR"] = tmp
    results = []

    def check(name, cond):
        results.append(bool(cond))
        line = ("PASS  " if cond else "FAIL  ") + name
        print(line, flush=True)
        log(line)

    def cfg_default():
        k = Keeper()
        return k

    k = cfg_default()
    check("默认配置：目标450分钟/早班", k.cfg["required_min"] == 450 and k.cfg["preset"] == "早班")
    check("初始状态离线", k.status() == "offline")
    check("初始在线时长0", abs(k.online_total() - 0.0) < 0.001)

    now = time.time()
    today = k.today_key()
    # 手工构造：10分钟在线 + 断开 + 现在在线中（开始于30分钟前）
    k.daily[today] = {
        "shift": {"preset": "早班", "start": "08:30", "end": "17:00", "required_min": 450},
        "sessions": [[now - 1800, now - 1200], [now - 600, None]],
        "auto_offline": False,
    }
    st = k.stats(now)
    check("统计：在线600+600=1200秒", abs(st["online_sec"] - 1200.0) < 1.0)
    check("统计：还差 450-20=430 分钟", abs(st["remaining_min"] - 430.0) < 0.1)
    check("统计：未达标", not st["qualified"])
    check("统计：状态在线", st["status"] == "online")
    check("统计：早班额度 510-450=60 分", abs(st["budget_min"] - 60.0) < 0.1)

    k.toggle("manual")   # 现在点离线
    check("toggle后为离线", k.status() == "offline")
    st = k.stats(time.time() + 1)
    check("离线后在线时长不变(约1200秒)", abs(st["online_sec"] - 1200.0) < 2.0)
    check("离线已持续>=1秒", st["offline_since_sec"] is not None and st["offline_since_sec"] >= 1)

    k.toggle("manual")   # 再回到在线
    check("再toggle后为在线", k.status() == "online")

    # 持久化
    k.save_data(force=True)
    k2 = Keeper()
    check("数据持久化：时段数一致", len(k2.daily.get(today, {}).get("sessions", [])) == 3)

    # 修正
    k2.delete_session(0)
    check("删除第一段后剩2段", len(k2.daily[today]["sessions"]) == 2)
    k2.add_session(now - 7200, now - 6600)
    check("补记一段后剩3段", len(k2.daily[today]["sessions"]) == 3)

    # 下班/加班场景
    k2.cfg["start"] = "00:00"
    k2.cfg["end"] = "00:01"
    k2.cfg["required_min"] = 450
    st2 = k2.stats(now)
    check("已过下班时间", st2["past_end"])
    check("需要加班", st2["need_overtime"])
    check("加班到 now+剩余分钟", st2["ot_finish_dt"] is not None and
          abs((st2["ot_finish_dt"].timestamp() - now) / 60.0 - st2["remaining_min"]) < 0.2)

    # 达标场景
    k3 = Keeper()
    k3.cfg["start"], k3.cfg["end"] = "14:00", "22:00"
    k3.daily[k3.today_key()] = {
        "shift": {"preset": "晚班", "start": "14:00", "end": "22:00", "required_min": 450},
        "sessions": [[now - 450 * 60, now]],
        "auto_offline": False,
    }
    st3 = k3.stats(now)
    check("450分钟在线即达标", st3["qualified"] and abs(st3["remaining_min"]) < 0.1)
    check("晚班额度 480-450=30 分", abs(st3["budget_min"] - 30.0) < 0.1)
    check("晚班已用离线 480-450=30", abs(st3["used_offline_min"] - 30.0) < 0.1)

    # 自动离线/上线
    k4 = Keeper()
    k4.daily[k4.today_key()] = {
        "shift": {"preset": "早班", "start": "08:30", "end": "17:00", "required_min": 450},
        "sessions": [[now - 100, None]],
        "auto_offline": False,
    }
    check("锁屏自动离线生效", k4.mark_offline(auto=True) is True)
    check("再次锁屏自动离线不重复", k4.mark_offline(auto=True) is False)
    check("解锁自动上线生效", k4.mark_online(auto=True) is True)
    k4.mark_offline(auto=False)          # 手动离线
    check("手动离线后解锁不会自动上线", k4.mark_online(auto=True) is False)
    check("手动上线生效", k4.mark_online(auto=False) is True)

    # 格式
    check("fmt_hms(450*60)=07:30:00", fmt_hms(450 * 60) == "07:30:00")
    check("fmt_hm(450)=7:30", fmt_hm(450) == "7:30")
    try:
        parse_hm("25:00")
        check("非法时间被拒绝", False)
    except ValueError:
        check("非法时间被拒绝", True)

    shutil.rmtree(tmp, ignore_errors=True)
    ok = all(results)
    print("SELFTEST %s (%d/%d passed)" % ("OK" if ok else "FAILED", sum(results), len(results)), flush=True)
    try:
        with open(os.path.join(data_dir(), "selftest.log"), "w", encoding="utf-8") as f:
            f.write("SELFTEST %s\n" % ("OK" if ok else "FAILED"))
    except Exception:
        pass
    return ok


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------
def main():
    if "--selftest" in sys.argv:
        sys.exit(0 if selftest() else 1)
        return

    # DPI 感知
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

    if not single_instance_check():
        msg_box(APP_NAME, "在线时长助手已在运行。\n可在屏幕右下角托盘图标处打开。")
        return

    try:
        root = tk.Tk()
        app = App(root)
        root.mainloop()
        log("程序退出")
    except Exception as e:
        import traceback
        log("程序异常: %s\n%s" % (e, traceback.format_exc()))
        msg_box(APP_NAME, "程序启动失败：%s\n详情见 data\\app.log" % e)


_instance_mutex = None

if __name__ == "__main__":
    main()
