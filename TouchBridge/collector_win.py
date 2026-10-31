# -*- coding: utf-8 -*-
"""
TouchBridge for Windows —— 触控板采集器（Windows 版）
====================================================

把 Windows【精确式触控板 (Precision Touchpad, PTP)】的多点触控数据，
封装成与 macOS 版 TouchBridge 完全一致的 OSC 消息发给本机后端：

  /down  [i slot, f x, f y, f area, i zone]   手指落下
  /move  [i slot, f x, f y, f area]           按住中持续上报(~90Hz)
  /hold  [i slot, f x, f y]                   按住>0.22s 且未滑动
  /drag  [i slot, f x, f y]                   滑动开始(移动>0.02 且 dur>0.12s)
  /tap   [i slot, f x, f y, f dur_ms]         快速轻点(dur<0.2s 且移动<0.02)
  /up    [i slot, f dur_ms, f maxDist]        其它抬起(非 tap)
  /count [i count]                            当前按下的手指数

坐标 x,y ∈ [0,1]（x 向右，y=1 在顶部，与 macOS 版一致）。

技术路线（全部纯标准库，无需安装 / 编译任何东西）：
  * Raw Input API  : RegisterRawInputDevices(UsagePage=0x0D, Usage=0x05, INPUTSINK)
                     —— Windows 通过 HID 数字器页把 PTP 原始触控报告直发给本程序，
                        系统手势（双指滚动等）不受影响，与 macOS 的
                        MultitouchSupport 私有框架地位等价。
  * HidP_* API    : 用设备自带的 preparsed data 解析每份 HID 报告里的
                     TipSwitch / ContactIdentifier / X / Y / ContactCount。
  * 手势引擎      : 与 macOS 版 main.swift 的 Engine.tick() 逐行对齐
                     （同样的阈值 / 槽位 / 消息时序），后端无需任何修改。

可选输入源（自动降级）：
  1. 精确式触控板（首选）
  2. 鼠标模拟：未检测到 PTP 时，在窗口的触控板图上按住鼠标拖动＝单指
  3. --osctest：无任何硬件时发送一组固定 OSC 消息验证管线

用法：
  python collector_win.py                 # 正常启动（GUI）
  python collector_win.py --port 9001     # 指定 OSC 端口
  python collector_win.py --osctest       # 发一组测试消息后退出
  python collector_win.py --list-devices  # 列出原始输入设备（排查问题用）
  python collector_win.py --no-gui        # 控制台模式
"""

import argparse
import math
import socket
import struct
import sys
import threading
import time

IS_WINDOWS = sys.platform == "win32"

# ---------------------------------------------------------------------------
# OSC 发送（与 macOS 版 OSCSender 字节级一致）
# ---------------------------------------------------------------------------
class OSCSender:
    def __init__(self, port=9000, host="127.0.0.1"):
        self.host = host
        self.port = port
        self.sock = None
        self.sent = 0
        self.open(port)

    def open(self, port):
        self.port = port
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sent = 0

    @staticmethod
    def _pad(data):
        r = len(data) % 4
        return data + b"\x00" * (0 if r == 0 else 4 - r)

    def _enc_str(self, s):
        return self._pad(s.encode("utf-8") + b"\x00")

    def send(self, address, args):
        """args: list of ('i', int) / ('f', float)"""
        packet = self._enc_str(address)
        tags = "," + "".join(t for t, _ in args)
        packet += self._enc_str(tags)
        for t, v in args:
            if t == "i":
                packet += struct.pack(">i", int(v))
            else:
                packet += struct.pack(">f", float(v))
        try:
            self.sock.sendto(packet, (self.host, self.port))
            self.sent += 1
            return True
        except Exception:
            return False


# ---------------------------------------------------------------------------
# 触摸快照（原始输入线程写入，引擎线程读取；对应 macOS 版 TouchHub）
# ---------------------------------------------------------------------------
class RawTouch:
    __slots__ = ("id", "x", "y", "area", "ts")

    def __init__(self, tid, x, y, area, ts):
        self.id = tid
        self.x = x
        self.y = y
        self.area = area
        self.ts = ts


class TouchHub:
    def __init__(self):
        self.lock = threading.Lock()
        self.buf = []
        self.report_count = 0

    def ingest(self, touches):
        with self.lock:
            self.buf = touches
            self.report_count += 1

    def snapshot(self):
        with self.lock:
            return list(self.buf)


# ---------------------------------------------------------------------------
# 手势引擎（逐行对齐 macOS 版 main.swift 的 Engine）
# ---------------------------------------------------------------------------
ZONE_NAMES = ["左上", "中上", "右上", "左中", "中心", "右中", "左下", "中下", "右下"]


class Slot:
    __slots__ = ("id", "start", "now", "x", "y", "area", "maxDist", "startX", "startY",
                 "dragging", "holdSent", "phase", "lastMoveSend", "releasedAt")

    def __init__(self, tid, now, x, y, area):
        self.id = tid
        self.start = now
        self.now = now
        self.x = x
        self.y = y
        self.area = area
        self.maxDist = 0.0
        self.startX = x
        self.startY = y
        self.dragging = False
        self.holdSent = False
        self.phase = "按下"
        self.lastMoveSend = now
        self.releasedAt = -1.0


class Engine:
    def __init__(self, hub, sender):
        self.hub = hub
        self.sender = sender
        self.sending = True
        self.slots = {}
        self.lastCount = -1
        self.trackpadOK = False
        self.trackpadName = ""
        self.messageLog = []
        self.status_cb = None  # GUI 刷新回调（可选）
        self.lock = threading.Lock()

    def zone_index(self, x, y):
        cx = min(2, max(0, int(x * 3)))
        cy = 2 - min(2, max(0, int(y * 3)))
        return cy * 3 + cx

    @staticmethod
    def zone_name(z):
        return ZONE_NAMES[max(0, min(8, z))]

    def _free_slot(self):
        for s in range(1, 6):
            if s not in self.slots:
                return s
        return None

    def _log(self, msg):
        self.messageLog.append(msg)
        if len(self.messageLog) > 60:
            del self.messageLog[:len(self.messageLog) - 60]

    def _send(self, address, args):
        self.sender.send(address, args)

    def tick(self):
        now = time.perf_counter()
        snap = self.hub.snapshot()
        pressed = {}
        for t in snap:
            pressed[t.id] = t

        with self.lock:
            # 新手指
            for tid, t in pressed.items():
                if any(s.id == tid for s in self.slots.values()):
                    continue
                slot = self._free_slot()
                if slot is None:
                    continue
                self.slots[slot] = Slot(tid, now, t.x, t.y, t.area)
                if self.sending:
                    z = self.zone_index(t.x, t.y)
                    self._send("/down", [("i", slot), ("f", t.x), ("f", t.y),
                                         ("f", t.area), ("i", z)])
                    self._log(f"down {slot} → {self.zone_name(z)}")

            # 更新 / 抬起
            for slot in list(self.slots.keys()):
                s = self.slots[slot]
                if s.id in pressed:
                    t = pressed[s.id]
                    dist = math.hypot(t.x - s.startX, t.y - s.startY)
                    s.maxDist = max(s.maxDist, dist)
                    s.x, s.y, s.now, s.area = t.x, t.y, now, t.area
                    dur = now - s.start

                    if not s.dragging and dist > 0.02 and dur > 0.12:
                        s.dragging = True
                        s.phase = "滑动"
                        if self.sending:
                            self._send("/drag", [("i", slot), ("f", s.x), ("f", s.y)])
                            self._log(f"drag {slot}")
                    if not s.dragging and not s.holdSent and dur > 0.22:
                        s.holdSent = True
                        s.phase = "长按"
                        if self.sending:
                            self._send("/hold", [("i", slot), ("f", s.x), ("f", s.y)])
                            self._log(f"hold {slot}")
                    if now - s.lastMoveSend > 0.012:
                        s.lastMoveSend = now
                        if self.sending:
                            self._send("/move", [("i", slot), ("f", s.x), ("f", s.y),
                                                 ("f", s.area)])
                elif s.releasedAt < 0:
                    s.releasedAt = now
                    dur = now - s.start
                    is_tap = dur < 0.20 and s.maxDist < 0.02
                    if self.sending:
                        if is_tap:
                            self._send("/tap", [("i", slot), ("f", s.x), ("f", s.y),
                                                ("f", dur * 1000)])
                            self._log(f"tap {slot} {int(dur * 1000)}ms")
                        else:
                            self._send("/up", [("i", slot), ("f", dur * 1000),
                                                ("f", s.maxDist)])
                            self._log(f"up {slot} {int(dur * 1000)}ms")
                    s.phase = "点击" if is_tap else "松开"

            # 清理过期槽位
            for slot in list(self.slots.keys()):
                s = self.slots.get(slot)
                if s is not None and s.releasedAt > 0 and now - s.releasedAt > 0.45:
                    del self.slots[slot]

            c = len(pressed)
            if c != self.lastCount:
                self.lastCount = c
                if self.sending:
                    self._send("/count", [("i", c)])

        if self.status_cb:
            self.status_cb()

    def reset(self):
        with self.lock:
            self.slots.clear()
            self.messageLog.clear()
            self.lastCount = -1


# ---------------------------------------------------------------------------
# Windows Raw Input / HID 层（ctypes）
# ---------------------------------------------------------------------------
if IS_WINDOWS:
    import ctypes
    import ctypes.wintypes as wt

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    hid_dll = ctypes.WinDLL("hid", use_last_error=True)

    WM_INPUT = 0x00FF
    WM_INPUT_DEVICE_CHANGE = 0x00FE
    HWND_MESSAGE = wt.HWND(-3)
    RIDEV_INPUTSINK = 0x00000100
    RIM_TYPEMOUSE, RIM_TYPEKEYBOARD, RIM_TYPEHID = 0, 1, 2
    RID_INPUT = 0x10000003
    RIDI_PREPARSEDDATA = 0x20000005
    HIDP_STATUS_SUCCESS = 0x00110000
    HIDP_INPUT = 0

    DIGITIZER_PAGE = 0x0D
    TOUCHPAD_USAGE = 0x05
    GD_PAGE = 0x01
    U_X, U_Y = 0x30, 0x31
    U_CONTACT_ID = 0x51
    U_CONTACT_COUNT = 0x54
    U_TIP_SWITCH = 0x42
    U_CONFIDENCE = 0x47

    class RAWINPUTDEVICE(ctypes.Structure):
        _fields_ = [("usUsagePage", wt.USHORT), ("usUsage", wt.USHORT),
                    ("dwFlags", wt.DWORD), ("hwndTarget", wt.HWND)]

    class RAWINPUTHEADER(ctypes.Structure):
        _fields_ = [("dwType", wt.DWORD), ("dwSize", wt.DWORD),
                    ("hDevice", wt.HANDLE), ("wParam", wt.WPARAM)]

    class RAWINPUTDEVICELIST(ctypes.Structure):
        _fields_ = [("hDevice", wt.HANDLE), ("dwType", wt.DWORD)]

    class RAWHID(ctypes.Structure):
        _fields_ = [("dwSizeHid", wt.DWORD), ("dwCount", wt.DWORD),
                    ("bRawData", wt.BYTE * 1)]

    class RAWINPUT(ctypes.Structure):
        # HID 报告从结构体偏移 32 处开始（header 24 + RAWHID 头 8，x64）
        _fields_ = [("header", RAWINPUTHEADER), ("hid", RAWHID)]

    class WNDCLASSW(ctypes.Structure):
        _fields_ = [("style", wt.UINT), ("lpfnWndProc", ctypes.c_void_p),
                    ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                    ("hInstance", wt.HINSTANCE), ("hIcon", wt.HICON),
                    ("hCursor", ctypes.c_void_p), ("hbrBackground", wt.HBRUSH),
                    ("lpszMenuName", wt.LPCWSTR), ("lpszClassName", wt.LPCWSTR)]

    class MSG(ctypes.Structure):
        _fields_ = [("hwnd", wt.HWND), ("message", wt.UINT), ("wParam", wt.WPARAM),
                    ("lParam", wt.LPARAM), ("time", wt.DWORD),
                    ("pt", ctypes.c_long * 2)]

    class HIDP_CAPS(ctypes.Structure):
        _fields_ = [("Usage", wt.USHORT), ("UsagePage", wt.USHORT),
                    ("InputReportByteLength", wt.USHORT),
                    ("OutputReportByteLength", wt.USHORT),
                    ("FeatureReportByteLength", wt.USHORT),
                    ("Reserved", wt.USHORT * 17),
                    ("NumberLinkCollectionNodes", wt.USHORT),
                    ("NumberInputButtonCaps", wt.USHORT),
                    ("NumberInputValueCaps", wt.USHORT),
                    ("NumberInputDataIndices", wt.USHORT),
                    ("NumberOutputButtonCaps", wt.USHORT),
                    ("NumberOutputValueCaps", wt.USHORT),
                    ("NumberOutputDataIndices", wt.USHORT),
                    ("NumberFeatureButtonCaps", wt.USHORT),
                    ("NumberFeatureValueCaps", wt.USHORT),
                    ("NumberFeatureDataIndices", wt.USHORT)]

    class HIDP_VALUE_CAPS(ctypes.Structure):
        """经实机校验的布局（72 字节）。

        说明：Windows SDK 的 hidpi.h 里该结构含位域与保留字段，各字段偏移难以
        从头文件直接推导；此处在多台真实 PTP 设备上通过 HidP_GetValueCaps
        原始字节反推并验证：BitSize@18、LogicalMin@40、LogicalMax@44、
        Usage@56（NotRange/Range 的 UsageMin 同偏移）、DataIndex@68。
        启动时会断言 sizeof==72，并在解析 caps 时做合法性检查，若未来
        Windows 变更布局会给出明确报错而不是悄悄输出乱数据。
        """
        _fields_ = [
            ("UsagePage", wt.USHORT),          # +0
            ("ReportID", ctypes.c_ubyte),       # +2
            ("IsAlias", ctypes.c_ubyte),        # +3
            ("BitField", wt.USHORT),            # +4
            ("LinkCollection", wt.USHORT),      # +6
            ("LinkUsage", wt.USHORT),           # +8
            ("LinkUsagePage", wt.USHORT),       # +10
            ("IsRange", ctypes.c_ubyte),        # +12
            ("IsStringRange", ctypes.c_ubyte),  # +13
            ("IsDesignatorRange", ctypes.c_ubyte),  # +14
            ("IsAbsolute", ctypes.c_ubyte),     # +15
            ("HasNull", ctypes.c_ubyte),        # +16
            ("Reserved0", ctypes.c_ubyte),      # +17
            ("BitSize", wt.USHORT),             # +18
            ("ReportCount", wt.USHORT),         # +20
            ("Reserved1", ctypes.c_ubyte * 10),  # +22..31
            ("UnitsExp", wt.ULONG),              # +32
            ("Units", wt.ULONG),                 # +36
            ("LogicalMin", ctypes.c_long),      # +40
            ("LogicalMax", ctypes.c_long),      # +44
            ("PhysicalMin", ctypes.c_long),     # +48
            ("PhysicalMax", ctypes.c_long),     # +52
            ("Usage", wt.USHORT),                # +56（Range 时为 UsageMin）
            ("UsageMax", wt.USHORT),             # +58（Range 时的 UsageMax）
            ("Reserved2", wt.USHORT),            # +60
            ("Reserved3", wt.USHORT),            # +62
            ("Reserved4", wt.USHORT),            # +64
            ("Reserved5", wt.USHORT),            # +66
            ("DataIndex", wt.USHORT),            # +68
            ("Reserved6", wt.USHORT),            # +70
        ]

    LRESULT = ctypes.c_ssize_t
    WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)

    user32.DefWindowProcW.restype = LRESULT
    user32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
    user32.GetRawInputData.argtypes = [wt.HANDLE, wt.UINT, wt.LPVOID,
                                       ctypes.POINTER(wt.UINT), wt.UINT]
    user32.GetRawInputData.restype = wt.UINT
    _G = user32.GetRawInputDeviceInfoW
    _G.argtypes = [wt.HANDLE, wt.UINT, wt.LPVOID, ctypes.POINTER(wt.UINT)]
    _G.restype = wt.UINT

    hid_dll.HidP_GetCaps.argtypes = [ctypes.c_void_p, ctypes.POINTER(HIDP_CAPS)]
    hid_dll.HidP_GetCaps.restype = ctypes.c_int
    hid_dll.HidP_GetValueCaps.argtypes = [ctypes.c_int, ctypes.c_void_p,
                                          ctypes.POINTER(wt.USHORT), ctypes.c_void_p]
    hid_dll.HidP_GetValueCaps.restype = ctypes.c_int
    hid_dll.HidP_GetUsageValue.argtypes = [ctypes.c_int, wt.USHORT, wt.USHORT, wt.USHORT,
                                           ctypes.POINTER(wt.ULONG), ctypes.c_void_p,
                                           ctypes.c_char_p, wt.ULONG]
    hid_dll.HidP_GetUsageValue.restype = ctypes.c_int
    hid_dll.HidP_GetUsages.argtypes = [ctypes.c_int, wt.USHORT, wt.USHORT,
                                       ctypes.POINTER(wt.USHORT), ctypes.POINTER(wt.ULONG),
                                       ctypes.c_void_p, ctypes.c_char_p, wt.ULONG]
    hid_dll.HidP_GetUsages.restype = ctypes.c_int


def get_preparsed_data(hDevice):
    """按设备句柄取 HID preparsed data（返回 bytes 或 None）。"""
    sz = wt.UINT(0)
    if _G(hDevice, RIDI_PREPARSEDDATA, None, ctypes.byref(sz)) == 0xFFFFFFFF \
            or sz.value == 0:
        return None
    buf = ctypes.create_string_buffer(sz.value)
    sz2 = wt.UINT(sz.value)
    if _G(hDevice, RIDI_PREPARSEDDATA, buf, ctypes.byref(sz2)) == 0xFFFFFFFF:
        return None
    return buf


def find_touchpad_devices():
    """枚举原始输入设备，返回所有“数字器-触控板(0x0D/0x05)”的句柄列表。"""
    num = wt.UINT(0)
    if user32.GetRawInputDeviceList(None, ctypes.byref(num),
                                    ctypes.sizeof(RAWINPUTDEVICELIST)) == 0xFFFFFFFF:
        return []
    buf = (RAWINPUTDEVICELIST * num.value)()
    user32.GetRawInputDeviceList(buf, ctypes.byref(num),
                                 ctypes.sizeof(RAWINPUTDEVICELIST))
    out = []
    for i in range(num.value):
        dev = buf[i]
        if dev.dwType != RIM_TYPEHID:
            continue
        pp = get_preparsed_data(dev.hDevice)
        if pp is None:
            continue
        caps = HIDP_CAPS()
        if hid_dll.HidP_GetCaps(pp, ctypes.byref(caps)) != HIDP_STATUS_SUCCESS:
            continue
        if caps.UsagePage == DIGITIZER_PAGE and caps.Usage == TOUCHPAD_USAGE:
            out.append(dev.hDevice)
    return out


class TouchpadParser:
    """解析单个 PTP 设备的 HID 报告 → [(cid, x01, y01), ...]。"""

    def __init__(self, preparsed):
        self.pp = preparsed
        caps = HIDP_CAPS()
        hid_dll.HidP_GetCaps(preparsed, ctypes.byref(caps))
        self.report_len = caps.InputReportByteLength
        self.contacts = []   # [{lc, x_rng, y_rng, cid_rng}]
        self.count_lc = None

        n = caps.NumberInputValueCaps
        if n == 0 or ctypes.sizeof(HIDP_VALUE_CAPS) != 72:
            raise ValueError("HIDP_VALUE_CAPS 布局异常")
        capbuf = ctypes.create_string_buffer(72 * n)
        n_out = wt.USHORT(n)
        nt = hid_dll.HidP_GetValueCaps(HIDP_INPUT, capbuf, ctypes.byref(n_out), preparsed)
        if nt != HIDP_STATUS_SUCCESS:
            raise ValueError("HidP_GetValueCaps 失败")

        per_lc = {}
        count_range = None
        for i in range(n_out.value):
            c = HIDP_VALUE_CAPS.from_buffer_copy(capbuf.raw[72 * i: 72 * i + 72])
            rng = (c.LogicalMin, c.LogicalMax)
            if c.UsagePage == DIGITIZER_PAGE and c.Usage == U_CONTACT_COUNT:
                count_range = rng
                self.count_lc = c.LinkCollection
                continue
            if c.UsagePage != GD_PAGE or c.Usage not in (U_X, U_Y):
                continue
            d = per_lc.setdefault(c.LinkCollection, {})
            if c.Usage == U_X:
                d["x"] = rng
            else:
                d["y"] = rng

        for lc, d in per_lc.items():
            if "x" in d and "y" in d:
                self.contacts.append({
                    "lc": lc, "x_rng": d["x"], "y_rng": d["y"],
                    "cid": None,  # CID 是 0x0D/0x51，见下
                })

        # Contact Identifier（0x0D/0x51）可能在数字器页
        for i in range(n_out.value):
            c = HIDP_VALUE_CAPS.from_buffer_copy(capbuf.raw[72 * i: 72 * i + 72])
            if c.UsagePage == DIGITIZER_PAGE and c.Usage == U_CONTACT_ID:
                for ct in self.contacts:
                    if ct["lc"] == c.LinkCollection:
                        ct["cid"] = (c.LogicalMin, c.LogicalMax)

        self.count_range = count_range

    @staticmethod
    def _norm(v, rng):
        lo, hi = rng
        if hi <= lo:
            return 0.5
        return max(0.0, min(1.0, (v - lo) / (hi - lo)))

    def _usage_value(self, page, lc, usage, report):
        val = wt.ULONG(0)
        nt = hid_dll.HidP_GetUsageValue(HIDP_INPUT, page, lc, usage,
                                        ctypes.byref(val), self.pp, report, len(report))
        return val.value if nt == HIDP_STATUS_SUCCESS else None

    def _usages_on(self, page, lc, report):
        lst = (wt.USHORT * 16)()
        ln = wt.ULONG(16)
        nt = hid_dll.HidP_GetUsages(HIDP_INPUT, page, lc, lst, ctypes.byref(ln),
                                   self.pp, report, len(report))
        if nt != HIDP_STATUS_SUCCESS:
            return []
        return [lst[j] for j in range(ln.value)]

    def parse(self, report):
        """返回 [RawTouch]（仅 Tip=1 的触点；y 已翻转为 macOS 约定：1=顶）。"""
        ts = time.perf_counter()
        out = []
        for ct in self.contacts:
            lc = ct["lc"]
            on_usages = self._usages_on(DIGITIZER_PAGE, lc, report)
            if U_TIP_SWITCH not in on_usages:
                continue
            if U_CONFIDENCE in on_usages:
                pass  # 置信位仅作展示参考，不做掌压过滤
            xv = self._usage_value(GD_PAGE, lc, U_X, report)
            yv = self._usage_value(GD_PAGE, lc, U_Y, report)
            if xv is None or yv is None:
                continue
            x = self._norm(xv, ct["x_rng"])
            y = 1.0 - self._norm(yv, ct["y_rng"])
            if ct["cid"] is not None:
                cidv = self._usage_value(DIGITIZER_PAGE, lc, U_CONTACT_ID, report)
                cid = int(cidv) if cidv is not None else lc
            else:
                cid = lc
            out.append(RawTouch(cid, x, y, 0.5, ts))
        return out


class RawInputThread(threading.Thread):
    """消息泵线程：注册触控板原始输入，接收 WM_INPUT 并解析。"""

    def __init__(self, hub, engine):
        super().__init__(daemon=True)
        self.hub = hub
        self.engine = engine
        self.running = True
        self.parsers = {}        # hDevice -> TouchpadParser
        self.registered_ok = False
        self.last_error = None
        self.first_reports = []  # 前 3 份报告原文（诊断用）
        self._wndproc_ref = None

    def _get_parser(self, hDevice):
        if hDevice in self.parsers:
            return self.parsers[hDevice]
        pp = get_preparsed_data(hDevice)
        if pp is None:
            return None
        caps = HIDP_CAPS()
        if hid_dll.HidP_GetCaps(pp, ctypes.byref(caps)) != HIDP_STATUS_SUCCESS:
            return None
        if not (caps.UsagePage == DIGITIZER_PAGE and caps.Usage == TOUCHPAD_USAGE):
            return None
        try:
            p = TouchpadParser(pp)
        except Exception:
            return None
        self.parsers[hDevice] = p
        return p

    def _wndproc(self, hwnd, msg, wParam, lParam):
        if msg == WM_INPUT:
            try:
                size = wt.UINT(0)
                user32.GetRawInputData(wt.HANDLE(lParam), RID_INPUT, None,
                                       ctypes.byref(size), ctypes.sizeof(RAWINPUTHEADER))
                if size.value:
                    buf = ctypes.create_string_buffer(size.value)
                    user32.GetRawInputData(wt.HANDLE(lParam), RID_INPUT, buf,
                                           ctypes.byref(size),
                                           ctypes.sizeof(RAWINPUTHEADER))
                    raw = ctypes.cast(buf, ctypes.POINTER(RAWINPUT)).contents
                    if raw.header.dwType == RIM_TYPEHID and raw.header.hDevice:
                        hid_part = raw.hid
                        n = hid_part.dwSizeHid * hid_part.dwCount
                        if n > 0:
                            # bRawData 在 RAWINPUT 内的绝对偏移：
                            #   RAWINPUT.hid(CField).offset=24(x64) + RAWHID.bRawData(CField).offset=8
                            # （不能写 RAWINPUT.hid.bRawData：对类取嵌套字段得到的是 CField，无该属性）
                            data_off = RAWINPUT.hid.offset + RAWHID.bRawData.offset
                            report = ctypes.string_at(
                                ctypes.addressof(buf) + data_off, n)
                            if len(self.first_reports) < 3:
                                self.first_reports.append(report[:32].hex())
                            parser = self._get_parser(raw.header.hDevice)
                            if parser:
                                touches = parser.parse(report)
                                self.hub.ingest(touches)
            except Exception as e:  # 消息泵里绝不能崩
                if not self.last_error:
                    import traceback
                    traceback.print_exc()
                self.last_error = str(e)
        elif msg == WM_INPUT_DEVICE_CHANGE:
            self.parsers.clear()
        return user32.DefWindowProcW(hwnd, msg, wParam, lParam)

    def run(self):
        try:
            wndproc = WNDPROC(self._wndproc)
            self._wndproc_ref = wndproc  # 防 GC
            wc = WNDCLASSW()
            wc.lpfnWndProc = ctypes.cast(wndproc, ctypes.c_void_p)
            wc.hInstance = ctypes.windll.kernel32.GetModuleHandleW(None)
            wc.lpszClassName = "TouchBridgeWinCollector"
            if not user32.RegisterClassW(ctypes.byref(wc)):
                self.last_error = "RegisterClassW 失败"
                return
            hwnd = user32.CreateWindowExW(0, "TouchBridgeWinCollector",
                                          "TouchBridgeWin", 0, 0, 0, 0, 0,
                                          HWND_MESSAGE, None, wc.hInstance, 0)
            if not hwnd:
                self.last_error = "CreateWindowExW 失败"
                return

            # 预扫描：确认本机确实有 PTP 设备
            pads = find_touchpad_devices()
            self.engine.trackpadOK = bool(pads)
            if pads:
                for h in pads:
                    self._get_parser(h)

            rid = RAWINPUTDEVICE(DIGITIZER_PAGE, TOUCHPAD_USAGE, RIDEV_INPUTSINK, hwnd)
            ok = user32.RegisterRawInputDevices(ctypes.byref(rid), 1,
                                                ctypes.sizeof(RAWINPUTDEVICE))
            self.registered_ok = bool(ok)
            if not ok:
                self.last_error = "RegisterRawInputDevices 失败"

            msg = MSG()
            while self.running:
                r = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if r in (0, 0xFFFFFFFF):
                    break
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        except Exception as e:
            self.last_error = str(e)

    def stop(self):
        self.running = False
        ctypes.windll.user32.PostThreadMessageW(self.ident, 0x0012, 0, 0)  # WM_QUIT


# ---------------------------------------------------------------------------
# 鼠标模拟输入（无 PTP 时的单指降级方案）
# ---------------------------------------------------------------------------
class MouseSimulator:
    """把 GUI 触控板画布上的按住拖动，注入为“单指触摸”。"""

    def __init__(self, hub):
        self.hub = hub
        self._active = False
        self._last = 0.0

    def down(self, x, y):
        self._active = True
        self._last = time.perf_counter()
        self.hub.ingest([RawTouch(99, x, y, 0.5, self._last)])

    def move(self, x, y):
        if not self._active:
            return
        now = time.perf_counter()
        if now - self._last < 0.008:  # ~120Hz 上限
            return
        self._last = now
        self.hub.ingest([RawTouch(99, x, y, 0.5, now)])

    def up(self):
        self._active = False
        self.hub.ingest([])


# ---------------------------------------------------------------------------
# GUI（tkinter；对齐 macOS 版窗口的信息布局）
# ---------------------------------------------------------------------------
def run_gui(engine, hub, sender):
    import tkinter as tk

    root = tk.Tk()
    root.title("TouchBridge for Windows → OSC")
    root.configure(bg="#0b0b0f")
    root.resizable(False, False)

    state = {"sim": MouseSimulator(hub), "engine": engine, "sender": sender}
    top = tk.Frame(root, bg="#0b0b0f")
    top.pack(fill="x", padx=16, pady=(12, 4))

    status_dot = tk.Label(top, text="●", fg="#66d99a", bg="#0b0b0f",
                          font=("Microsoft YaHei UI", 10))
    status_dot.pack(side="left")
    status_text = tk.StringVar(value="正在发送 → 127.0.0.1:%d" % sender.port)
    tk.Label(top, textvariable=status_text, fg="#b9b9c6", bg="#0b0b0f",
             font=("Microsoft YaHei UI", 10)).pack(side="left", padx=(6, 12))

    def toggle_send():
        engine.sending = not engine.sending
        btn_send.config(text="暂停发送" if engine.sending else "开始发送")

    def apply_port():
        try:
            p = int(entry_port.get().strip())
            if 0 < p < 65536:
                sender.open(p)
                refresh()
        except ValueError:
            pass

    btn_send = tk.Button(top, text="暂停发送", command=toggle_send)
    btn_send.pack(side="left")
    tk.Button(top, text="清空", command=engine.reset).pack(side="left", padx=(6, 0))
    tk.Label(top, text="端口", fg="#b9b9c6", bg="#0b0b0f").pack(side="left", padx=(14, 2))
    entry_port = tk.Entry(top, width=7, justify="center")
    entry_port.insert(0, str(sender.port))
    entry_port.pack(side="left")
    tk.Button(top, text="应用", command=apply_port).pack(side="left", padx=(6, 0))

    body = tk.Frame(root, bg="#0b0b0f")
    body.pack(fill="both", expand=True, padx=16, pady=8)

    # 触控板 3×3 图
    padw, padh = 250, 183
    pad = tk.Canvas(body, width=padw, height=padh + 20, bg="#0b0b0f",
                    highlightthickness=0)
    pad.pack(side="left", fill="y")

    def draw_pad():
        pad.delete("all")
        pad.create_rectangle(0, 0, padw, padh, fill="#141419",
                             outline="#2a2a33")
        for i in range(1, 3):
            x = padw * i / 3
            pad.create_line(x, 0, x, padh, fill="#22222b")
            y = padh * i / 3
            pad.create_line(0, y, padw, y, fill="#22222b")
        pad.create_text(padw / 2, padh + 12, text="触控板 3×3 区域",
                        fill="#71717e", font=("Microsoft YaHei UI", 9))
        now = time.perf_counter()
        with engine.lock:
            for slot, s in engine.slots.items():
                px = s.x * padw
                py = (1 - s.y) * padh
                alpha = 1.0 if s.releasedAt < 0 else max(
                    0.0, 1 - (now - s.releasedAt) / 0.45)
                if alpha <= 0:
                    continue
                c = "#6bd6fa"
                pad.create_oval(px - 16, py - 16, px + 16, py + 16,
                                fill="#15242c", outline=c, dash=(3, 2) if s.releasedAt > 0 else ())
                pad.create_text(px, py, text=str(slot), fill="#e6e6ee",
                                font=("Consolas", 9, "bold"))

    # 鼠标模拟（无 PTP 时）
    def canvas_xy(event):
        x = max(0.0, min(1.0, event.x / padw))
        y = 1.0 - max(0.0, min(1.0, event.y / padh))  # 画布 y=0 在上
        return x, y

    def sim_down(e):
        if not engine.trackpadOK:
            state["sim"].down(*canvas_xy(e))

    def sim_move(e):
        if not engine.trackpadOK:
            state["sim"].move(*canvas_xy(e))

    def sim_up(_e):
        if not engine.trackpadOK:
            state["sim"].up()

    pad.bind("<ButtonPress-1>", sim_down)
    pad.bind("<B1-Motion>", sim_move)
    pad.bind("<ButtonRelease-1>", sim_up)

    # 槽位列表 + 日志
    right = tk.Frame(body, bg="#0b0b0f")
    right.pack(side="left", fill="both", expand=True, padx=(16, 0))
    slot_vars = {}
    tk.Label(right, text="手指槽位", fg="#71717e", bg="#0b0b0f",
             font=("Microsoft YaHei UI", 9)).pack(anchor="w")
    for i in range(1, 6):
        var = tk.StringVar(value=f"{i}   空闲")
        slot_vars[i] = var
        tk.Label(right, textvariable=var, fg="#d9d9e3", bg="#141419",
                 anchor="w", font=("Microsoft YaHei UI", 10),
                 width=34).pack(anchor="w", pady=1, ipadx=6, ipady=2)
    log_var = tk.StringVar(value="")
    tk.Label(right, text="最近消息", fg="#71717e", bg="#0b0b0f",
             font=("Microsoft YaHei UI", 9)).pack(anchor="w", pady=(8, 0))
    tk.Label(right, textvariable=log_var, fg="#71717e", bg="#0b0b0f",
             anchor="w", justify="left", font=("Consolas", 9),
             wraplength=330).pack(anchor="w")

    def refresh():
        with engine.lock:
            for i in range(1, 6):
                s = engine.slots.get(i)
                if s is None:
                    slot_vars[i].set(f"{i}   空闲")
                else:
                    slot_vars[i].set(f"{i}   {Engine.zone_name(engine.zone_index(s.x, s.y))}"
                                     f"   {s.phase}   x {s.x:.2f}  y {s.y:.2f}")
            recent = engine.messageLog[-3:]
        log_var.set("\n".join(reversed(recent)))
        if engine.trackpadOK:
            ok = engine.trackpadOK
            color = "#66d99a" if engine.sending else "#71717e"
            status_dot.config(fg=color)
            status_text.set(("正在发送" if engine.sending else "已暂停")
                            + f"  →  127.0.0.1:{sender.port}")
        else:
            status_dot.config(fg="#e6a54c")
            status_text.set("未检测到精确式触控板 —— 鼠标模拟模式（左图内按住拖动＝单指）"
                            f"  →  127.0.0.1:{sender.port}")
        draw_pad()
        root.after(33, refresh)

    refresh()

    def on_close():
        try:
            engine.sending = False
        finally:
            root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    root.mainloop()


def run_console(engine, hub, sender):
    """无 tkinter 环境的兜底：引擎照常跑，状态打印到控制台。"""
    print("（未安装 tkinter / --no-gui：控制台模式。Ctrl+C 退出。）")
    last = ""
    try:
        while True:
            time.sleep(1.0)
            with engine.lock:
                cur = f"触点={len(hub.snapshot())} 槽位={sorted(engine.slots.keys())} " \
                      f"已发送={sender.sent} 发送中={engine.sending} " \
                      f"PTP={'有' if engine.trackpadOK else '无(鼠标模拟不可用)'}"
            if cur != last:
                print(cur, flush=True)
                last = cur
    except KeyboardInterrupt:
        pass


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def do_osctest(port):
    s = OSCSender(port=port)
    s.send("/down", [("i", 1), ("f", 0.31), ("f", 0.62), ("f", 0.5), ("i", 6)])
    time.sleep(0.05)
    s.send("/move", [("i", 1), ("f", 0.33), ("f", 0.60), ("f", 0.5)])
    time.sleep(0.05)
    s.send("/hold", [("i", 1), ("f", 0.33), ("f", 0.60)])
    time.sleep(0.05)
    s.send("/drag", [("i", 2), ("f", 0.72), ("f", 0.40)])
    time.sleep(0.05)
    s.send("/tap", [("i", 3), ("f", 0.50), ("f", 0.50), ("f", 78.0)])
    time.sleep(0.05)
    s.send("/up", [("i", 1), ("f", 920.0), ("f", 0.12)])
    time.sleep(0.05)
    s.send("/count", [("i", 0)])
    print(f"sent: {s.sent} 条 OSC 测试消息 → 127.0.0.1:{port}")
    print("去网页点「开始监听 OSC」再运行本命令，右下角应能看到这些消息。")


def do_list_devices():
    if not IS_WINDOWS:
        print("仅支持 Windows")
        return
    num = wt.UINT(0)
    user32.GetRawInputDeviceList(None, ctypes.byref(num),
                                 ctypes.sizeof(RAWINPUTDEVICELIST))
    buf = (RAWINPUTDEVICELIST * num.value)()
    user32.GetRawInputDeviceList(buf, ctypes.byref(num),
                                 ctypes.sizeof(RAWINPUTDEVICELIST))
    print(f"共 {num.value} 个原始输入设备：")
    found = False
    for i in range(num.value):
        dev = buf[i]
        if dev.dwType != RIM_TYPEHID:
            continue
        pp = get_preparsed_data(dev.hDevice)
        if pp is None:
            continue
        caps = HIDP_CAPS()
        if hid_dll.HidP_GetCaps(pp, ctypes.byref(caps)) != HIDP_STATUS_SUCCESS:
            continue
        tag = ""
        if caps.UsagePage == DIGITIZER_PAGE and caps.Usage == TOUCHPAD_USAGE:
            tag = "   <<<<< 触控板（PTP）"
            found = True
        print(f"  hDevice={dev.hDevice} usagePage=0x{caps.UsagePage:04x} "
              f"usage=0x{caps.Usage:04x} 输入报告={caps.InputReportByteLength}B{tag}")
    if not found:
        print("  （未发现精确式触控板——若你用的是老式 PS/2 或非 PTP 触控板，无法采集多点触控）")


def main():
    ap = argparse.ArgumentParser(description="TouchBridge for Windows 触控板采集器")
    ap.add_argument("--port", type=int, default=9000, help="OSC 发送端口（默认 9000）")
    ap.add_argument("--osctest", action="store_true", help="发送一组测试 OSC 消息后退出")
    ap.add_argument("--list-devices", action="store_true", help="列出原始输入设备")
    ap.add_argument("--no-gui", action="store_true", help="控制台模式（不用 tkinter）")
    args = ap.parse_args()

    if args.osctest:
        do_osctest(args.port)
        return
    if args.list_devices:
        do_list_devices()
        return
    if not IS_WINDOWS:
        print("本采集器仅支持 Windows（macOS 请用 TouchBridge/build.sh 编译的 .app）")
        sys.exit(1)

    sender = OSCSender(port=args.port)
    hub = TouchHub()
    engine = Engine(hub, sender)

    ri = RawInputThread(hub, engine)
    ri.start()

    # 引擎线程：90Hz，对齐 macOS 版 NSTimer 节奏
    def engine_loop():
        while True:
            engine.tick()
            time.sleep(1.0 / 90.0)

    threading.Thread(target=engine_loop, daemon=True).start()

    # 等待原始输入线程完成注册检测
    for _ in range(50):
        if ri.registered_ok or ri.last_error:
            break
        time.sleep(0.05)
    if not engine.trackpadOK:
        time.sleep(0.3)  # 再给预扫描一点时间
    if ri.last_error:
        print(f"[警告] 原始输入初始化异常：{ri.last_error}")
    if not engine.trackpadOK:
        print("[提示] 未检测到 Windows 精确式触控板（PTP）。")
        print("       - 触控板画布可用鼠标按住拖动模拟单指；")
        print("       - 或运行  python collector_win.py --list-devices  排查；")
        print("       - 老式 PS/2 触控板不支持多点触控原始数据，无法采集。")

    if args.no_gui:
        run_console(engine, hub, sender)
        return
    try:
        import tkinter  # noqa: F401
        run_gui(engine, hub, sender)
    except ImportError:
        run_console(engine, hub, sender)


if __name__ == "__main__":
    main()
