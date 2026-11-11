#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Edge 浏览器桥（bridge.py）
========================
作用：在你自己的电脑上运行，通过 CDP（Chrome DevTools Protocol）控制本机
      一个带调试口的 Edge，然后把控制能力以 HTTP API 的形式开放给助手。

前提：Edge 必须带调试口启动（start-edge.bat = 独立小号，
      start-edge-main.bat = 日常 Edge 本人），调试口只绑
      127.0.0.1:9222，外面连不进来；本桥只绑 Tailscale 网卡 IP 对外服务，
      且每个请求都要带 token。

依赖：只有 websocket-client 一个第三方包，其余全是 Python 标准库。
      没装依赖时运行： pip install -r requirements.txt

可选环境变量（一般不用管）：
      BRIDGE_HOST  强制指定监听地址（调试时用）
      BRIDGE_PORT  强制指定监听端口（默认 8765）
      BRIDGE_MODE  运行模式标记（自启脚本会设为 autostart，/health 里回显）
      BRIDGE_NO_AUTOLAUNCH=1  关掉「Edge 没开时自动拉起小号 Edge」

版本 2.1：新增按需自拉 Edge（/launch 端点 + CDP 类请求的自动拉起），
配合 install-autostart.bat 可实现开机自启、免手动开任何文件。

版本 2.2：为自启场景补可观测性——日志同时写 bridge.log 文件、
pythonw 无控制台时防 print 崩溃、启动全过程留痕、主流程异常
捕获写日志；找 Tailscale 地址新增 tailscale.exe 全路径与
ipconfig 解析两条方法。
"""

import hmac
import ipaddress
import json
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    import websocket  # websocket-client
except ImportError:
    print("缺少依赖 websocket-client，请先在本目录运行：")
    print("    pip install -r requirements.txt")
    sys.exit(1)

# ---------------------------------------------------------------- 常量

CDP_BASE = "http://127.0.0.1:9222"          # Edge 调试口，只绑本机
DEFAULT_PORT = 8765                          # 桥的对外端口
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TOKEN_FILE = os.path.join(BASE_DIR, "token.txt")
LOG_FILE = os.path.join(BASE_DIR, "bridge.log")
TAILSCALE_NET = ipaddress.ip_network("100.64.0.0/10")

# pythonw.exe 后台运行时 stdout/stderr 可能是 None 或无效句柄，
# 直接 print 会抛错把进程带走。先把它们兜住。
try:
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")
except Exception:
    pass


# ---------------------------------------------------------------- 工具

def log(msg):
    line = "%s %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:
        print(line, flush=True)
    except Exception:
        pass
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def http_json(url, method="GET", timeout=5):
    """请求一个返回 JSON 的 HTTP 地址（用于和 CDP 的 /json 接口打交道）。"""
    req = urllib.request.Request(url, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


class CDPError(Exception):
    pass


# ---------------------------------------------------------------- CDP 封装

class CDPClient:
    """一条到某个标签页的 CDP WebSocket 连接，按 id 匹配请求/响应。"""

    def __init__(self, ws_url):
        # suppress_origin=True 很关键：新版 Chrome/Edge 会拒绝带 Origin 头的
        # CDP WebSocket 连接（403），去掉 Origin 头才能连上，无需额外启动参数。
        self.ws = websocket.create_connection(ws_url, timeout=30,
                                              suppress_origin=True)
        self._next_id = 0
        self._lock = threading.Lock()

    def call(self, method, params=None):
        with self._lock:
            self._next_id += 1
            mid = self._next_id
            self.ws.send(json.dumps({"id": mid, "method": method,
                                     "params": params or {}}))
            while True:
                raw = self.ws.recv()
                if not raw:
                    raise CDPError("CDP 连接被关闭")
                msg = json.loads(raw)
                if msg.get("id") != mid:
                    continue  # 事件通知，忽略
                if "error" in msg:
                    raise CDPError("CDP 错误: %s" % json.dumps(msg["error"],
                                                               ensure_ascii=False))
                return msg.get("result", {})

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


class CDPManager:
    """管理到各个标签页的 CDP 连接（按 tabId 缓存，坏了自动重连一次）。"""

    def __init__(self):
        self._clients = {}
        self._lock = threading.Lock()
        self._browser_client = None

    # ---- 浏览器级命令（Browser 域，不属于某个标签页） ----

    def browser_call(self, method, params=None):
        """在浏览器级连接上执行命令（如 Browser.setDownloadBehavior）；
        连接坏了就重连再试一次。"""
        try:
            return self._get_browser_client().call(method, params)
        except (CDPError, websocket.WebSocketException, OSError):
            self._drop_browser_client()
            return self._get_browser_client().call(method, params)

    def _get_browser_client(self):
        with self._lock:
            if self._browser_client is not None:
                return self._browser_client
            ver = self.version()
            ws_url = ver.get("webSocketDebuggerUrl")
            if not ws_url:
                raise CDPError("浏览器没有提供浏览器级调试地址")
            self._browser_client = CDPClient(ws_url)
            return self._browser_client

    def _drop_browser_client(self):
        with self._lock:
            client, self._browser_client = self._browser_client, None
        if client:
            client.close()

    # ---- 目标（标签页）列表类操作 ----

    def list_targets(self):
        return http_json(CDP_BASE + "/json", timeout=5)

    def list_tabs(self):
        tabs = []
        for t in self.list_targets():
            if t.get("type") == "page":
                tabs.append({"id": t.get("id"), "title": t.get("title", ""),
                             "url": t.get("url", "")})
        return tabs

    def version(self):
        return http_json(CDP_BASE + "/json/version", timeout=3)

    def open_tab(self, url):
        q = urllib.parse.urlencode({"url": url})
        try:
            target = http_json(CDP_BASE + "/json/new?" + q, method="PUT")
        except urllib.error.HTTPError as e:
            if e.code not in (405, 400):
                raise
            target = http_json(CDP_BASE + "/json/new?" + q, method="GET")
        info = {"id": target.get("id"), "title": target.get("title", ""),
                "url": target.get("url", url)}
        # 有些浏览器版本里 /json/new 只建标签页、不真正导航，
        # 所以再显式用 CDP 的 Page.navigate 跳一次，保证到位。
        try:
            self.call(info["id"], "Page.navigate", {"url": url})
        except Exception:
            pass  # 导航失败不算开标签页失败，后续 /text 会如实反映页面状态
        # 返回时页面往往还没跳完，url/title 是旧的；
        # 稍等再查一次列表，尽量返回真实的当前地址。
        for _ in range(5):
            time.sleep(0.3)
            fresh = self._find_target(info["id"])
            if fresh and fresh.get("url") and fresh["url"] != "about:blank":
                info["title"] = fresh.get("title", info["title"])
                info["url"] = fresh["url"]
                break
        return info

    def close_tab(self, tab_id):
        self.drop_client(tab_id)
        closed = False
        for method in ("PUT", "GET"):
            try:
                req = urllib.request.Request(
                    CDP_BASE + "/json/close/" + tab_id, method=method)
                with urllib.request.urlopen(req, timeout=5) as resp:
                    resp.read()
                closed = True
                break
            except urllib.error.HTTPError as e:
                if e.code == 405:
                    continue
                raise
        if not closed:
            raise CDPError("关闭标签页失败: %s" % tab_id)

    # ---- 单标签页命令 ----

    def _find_target(self, tab_id):
        for t in self.list_targets():
            if t.get("id") == tab_id and t.get("webSocketDebuggerUrl"):
                return t
        return None

    def resolve_tab_id(self, tab_id):
        """没给 tabId 时默认用第一个页面标签页。"""
        if tab_id:
            if not self._find_target(tab_id):
                raise CDPError("找不到标签页: %s" % tab_id)
            return tab_id
        tabs = self.list_tabs()
        if not tabs:
            raise CDPError("当前没有打开的标签页")
        return tabs[0]["id"]

    def _get_client(self, tab_id):
        with self._lock:
            client = self._clients.get(tab_id)
            if client is not None:
                return client
            target = self._find_target(tab_id)
            if not target:
                raise CDPError("找不到标签页: %s" % tab_id)
            client = CDPClient(target["webSocketDebuggerUrl"])
            self._clients[tab_id] = client
            return client

    def drop_client(self, tab_id):
        with self._lock:
            client = self._clients.pop(tab_id, None)
        if client:
            client.close()

    def call(self, tab_id, method, params=None):
        """在指定标签页上执行 CDP 命令；连接坏了就重连再试一次。"""
        tab_id = self.resolve_tab_id(tab_id)
        try:
            return self._get_client(tab_id).call(method, params)
        except (CDPError, websocket.WebSocketException, OSError):
            self.drop_client(tab_id)
            return self._get_client(tab_id).call(method, params)

    def evaluate(self, tab_id, expression):
        """Runtime.evaluate，要求返回可序列化的值；页面抛错时转成 CDPError。"""
        result = self.call(tab_id, "Runtime.evaluate", {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": True,
        })
        if "exceptionDetails" in result:
            detail = result["exceptionDetails"]
            desc = (detail.get("exception", {}).get("description")
                    or detail.get("text", "JS 执行出错"))
            raise CDPError("页面 JS 报错: %s" % desc)
        return result.get("result", {}).get("value")


CDP = CDPManager()


# ---------------------------------------------------------------- 按需拉起 Edge

_EDGE_PROFILE_DIR = os.path.join(
    os.environ.get("LOCALAPPDATA") or "", "MuseEdgeProfile")
_launch_lock = threading.Lock()


def cdp_up():
    try:
        CDP.version()
        return True
    except Exception:
        return False


def find_msedge():
    """在常见安装位置找 msedge.exe（只在 Windows 上找），找不到返回 None。"""
    if os.name != "nt":
        return None
    candidates = []
    for root in (os.environ.get("ProgramFiles(x86)"),
                 os.environ.get("ProgramFiles"),
                 os.environ.get("LOCALAPPDATA")):
        if root:
            candidates.append(os.path.join(
                root, "Microsoft", "Edge", "Application", "msedge.exe"))
    for path in candidates:
        if os.path.isfile(path):
            return path
    return None


def ensure_edge(timeout=25):
    """Edge 调试口没开时，自动用小号配置把 Edge 拉起来并等它就绪。
    返回 {cdp, launched}；拉不起来时抛 CDPError 并说明原因。"""
    if cdp_up():
        return {"cdp": True, "launched": False}
    if os.name != "nt":
        raise CDPError("自动拉起 Edge 只支持 Windows")
    edge = find_msedge()
    if not edge:
        raise CDPError("自动拉起失败: 没找到 msedge.exe")
    with _launch_lock:
        if cdp_up():  # 等锁期间别人已经拉起来了
            return {"cdp": True, "launched": False}
        os.makedirs(_EDGE_PROFILE_DIR, exist_ok=True)
        flags = 0x00000008 | 0x00000200  # DETACHED_PROCESS | NEW_PROCESS_GROUP
        subprocess.Popen(
            [edge, "--remote-debugging-port=9222",
             "--remote-debugging-address=127.0.0.1",
             "--user-data-dir=" + _EDGE_PROFILE_DIR,
             "about:blank"],
            creationflags=flags, close_fds=True)
        deadline = time.time() + timeout
        while time.time() < deadline:
            if cdp_up():
                return {"cdp": True, "launched": True}
            time.sleep(0.5)
    raise CDPError(
        "自动拉起 Edge 后调试口仍不可达。常见原因：小号 Edge 已经开着但"
        "没带调试口（手动打开的那种）——请把 Edge 窗口全部关掉再重试。")


def maybe_autolaunch():
    """CDP 类请求进来时顺手保证 Edge 在跑；关掉开关或 Edge 已在跑时直接返回。"""
    if os.environ.get("BRIDGE_NO_AUTOLAUNCH"):
        return
    if cdp_up():
        return
    ensure_edge()


# ---------------------------------------------------------------- 业务端点

def ep_tabs(params):
    return {"tabs": CDP.list_tabs()}


def ep_open(params):
    url = (params.get("url") or "").strip()
    if not url:
        raise ValueError("缺少参数 url")
    if not (url.startswith("http://") or url.startswith("https://")
            or url.startswith("about:") or url.startswith("data:")):
        url = "https://" + url
    tab = CDP.open_tab(url)
    return {"tab": tab}


def ep_text(params):
    tab_id = CDP.resolve_tab_id(params.get("tabId"))
    value = CDP.evaluate(tab_id,
                         "document.body ? document.body.innerText : ''")
    return {"tabId": tab_id, "text": value or ""}


def ep_click(params):
    selector = params.get("selector")
    if not selector:
        raise ValueError("缺少参数 selector")
    tab_id = CDP.resolve_tab_id(params.get("tabId"))
    expr = ("(() => { const el = document.querySelector(%s);"
            " if (!el) return {ok: false, error: '找不到元素'};"
            " el.scrollIntoView({block: 'center'}); el.click();"
            " return {ok: true}; })()" % json.dumps(selector))
    value = CDP.evaluate(tab_id, expr)
    if isinstance(value, dict) and value.get("ok"):
        return {"tabId": tab_id, "clicked": selector}
    raise CDPError("点击失败: %s" % (value or {}).get("error", "未知原因"))


def ep_type(params):
    selector = params.get("selector")
    text = params.get("text")
    if not selector:
        raise ValueError("缺少参数 selector")
    if text is None:
        raise ValueError("缺少参数 text")
    tab_id = CDP.resolve_tab_id(params.get("tabId"))
    expr = ("(() => { const el = document.querySelector(%s);"
            " if (!el) return false; el.focus(); return true; })()"
            % json.dumps(selector))
    if not CDP.evaluate(tab_id, expr):
        raise CDPError("输入失败: 找不到元素 %s" % selector)
    CDP.call(tab_id, "Input.insertText", {"text": text})
    if params.get("submit"):
        for ev_type in ("keyDown", "keyUp"):
            CDP.call(tab_id, "Input.dispatchKeyEvent", {
                "type": ev_type, "key": "Enter", "code": "Enter",
                "windowsVirtualKeyCode": 13, "nativeVirtualKeyCode": 13,
                # 带 text 才会触发表单提交，原因同 /key 的 Enter
                "text": "\r" if ev_type == "keyDown" else "",
            })
    return {"tabId": tab_id, "typed": len(text)}


def ep_eval(params):
    expression = params.get("expression")
    if not expression:
        raise ValueError("缺少参数 expression")
    tab_id = CDP.resolve_tab_id(params.get("tabId"))
    value = CDP.evaluate(tab_id, expression)
    try:
        json.dumps(value)
    except (TypeError, ValueError):
        value = str(value)
    return {"tabId": tab_id, "result": value}


def ep_close(params):
    tab_id = params.get("tabId")
    if not tab_id:
        raise ValueError("缺少参数 tabId")
    CDP.close_tab(tab_id)
    return {"closed": tab_id}


def ep_screenshot(params):
    tab_id = CDP.resolve_tab_id(params.get("tabId"))
    result = CDP.call(tab_id, "Page.captureScreenshot", {"format": "png"})
    data = result.get("data")
    if not data:
        raise CDPError("截图失败: 浏览器没有返回图片数据")
    return {"tabId": tab_id, "format": "png", "image": data}


def ep_cdp(params):
    """原始 CDP 透传：在指定标签页上执行任意 CDP 方法。
    这是全盘控制的核心口子——调用方等于直接握着调试协议，
    能做的不止是页面操作（但桥刻意不提供导出 cookie 的现成端点）。"""
    method = params.get("method")
    if not method or not isinstance(method, str):
        raise ValueError("缺少参数 method")
    cdp_params = params.get("params") or {}
    if not isinstance(cdp_params, dict):
        raise ValueError("params 必须是 JSON 对象")
    tab_id = CDP.resolve_tab_id(params.get("tabId"))
    result = CDP.call(tab_id, method, cdp_params)
    return {"tabId": tab_id, "result": result}


def ep_scroll(params):
    x = params.get("x", 0)
    y = params.get("y", 0)
    if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
        raise ValueError("x / y 必须是数字（滚动像素数，y 正数向下、x 正数向右）")
    tab_id = CDP.resolve_tab_id(params.get("tabId"))
    CDP.call(tab_id, "Input.dispatchMouseEvent", {
        "type": "mouseWheel", "x": 100, "y": 100,
        "deltaX": x, "deltaY": y,
    })
    return {"tabId": tab_id, "scrolled": {"x": x, "y": y}}


# 常用键名 -> (code, windowsVirtualKeyCode)
_KEY_MAP = {
    "enter": ("Enter", 13), "tab": ("Tab", 9),
    "escape": ("Escape", 27), "esc": ("Escape", 27),
    "backspace": ("Backspace", 8), "delete": ("Delete", 46),
    "insert": ("Insert", 45), "home": ("Home", 36), "end": ("End", 35),
    "pageup": ("PageUp", 33), "pagedown": ("PageDown", 34),
    "arrowup": ("ArrowUp", 38), "arrowdown": ("ArrowDown", 40),
    "arrowleft": ("ArrowLeft", 37), "arrowright": ("ArrowRight", 39),
    " ": ("Space", 32), "space": ("Space", 32), "spacebar": ("Space", 32),
}
for _i in range(1, 13):
    _KEY_MAP["f%d" % _i] = ("F%d" % _i, 111 + _i)

_MOD_BITS = {"alt": 1, "ctrl": 2, "control": 2, "meta": 4,
             "cmd": 4, "command": 4, "win": 4, "shift": 8}


def _modifiers_value(raw):
    """modifiers 可以是整数（CDP 位掩码 Alt=1 Ctrl=2 Meta=4 Shift=8），
    也可以是名字列表/字符串，如 ["ctrl", "shift"] 或 "ctrl"。"""
    if raw is None:
        return 0
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str):
        raw = [raw]
    value = 0
    for name in raw:
        bit = _MOD_BITS.get(str(name).strip().lower())
        if bit is None:
            raise ValueError("不认识的修饰键: %s" % name)
        value |= bit
    return value


def ep_key(params):
    key = params.get("key")
    if not key or not isinstance(key, str):
        raise ValueError("缺少参数 key")
    modifiers = _modifiers_value(params.get("modifiers"))
    tab_id = CDP.resolve_tab_id(params.get("tabId"))
    lowered = key.lower()
    if lowered in _KEY_MAP:
        code, vk = _KEY_MAP[lowered]
        payload = {"key": code, "code": code,
                   "windowsVirtualKeyCode": vk,
                   "nativeVirtualKeyCode": vk}
        if code == "Space":
            payload["key"] = " "
        if code == "Enter":
            # 关键：回车的 keyDown 必须带 text "\r"，浏览器才会把它
            # 当成一次真实回车（触发表单隐式提交等默认动作）；
            # 只发裸 keyDown/rawKeyDown 时页面能收到事件、但不会提交。
            payload["text"] = "\r"
    elif len(key) == 1:
        payload = {"key": key, "code": "Key" + key.upper() if key.isalpha()
                   else key,
                   "windowsVirtualKeyCode": ord(key.upper()),
                   "nativeVirtualKeyCode": ord(key.upper()),
                   "text": key}
    else:
        payload = {"key": key, "code": key}
    payload["modifiers"] = modifiers
    for ev_type in ("keyDown", "keyUp"):
        CDP.call(tab_id, "Input.dispatchKeyEvent",
                 dict(payload, type=ev_type))
    return {"tabId": tab_id, "key": key, "modifiers": modifiers}


def _history_nav(params, action):
    tab_id = CDP.resolve_tab_id(params.get("tabId"))
    CDP.evaluate(tab_id, "history.%s()" % action)
    return {"tabId": tab_id, "action": action}


def ep_back(params):
    return _history_nav(params, "back")


def ep_forward(params):
    return _history_nav(params, "forward")


def ep_reload(params):
    tab_id = CDP.resolve_tab_id(params.get("tabId"))
    CDP.call(tab_id, "Page.reload", {})
    return {"tabId": tab_id, "action": "reload"}


def ep_wait(params):
    selector = params.get("selector")
    if not selector:
        raise ValueError("缺少参数 selector")
    timeout_ms = params.get("timeoutMs", 10000)
    if not isinstance(timeout_ms, (int, float)) or timeout_ms <= 0:
        raise ValueError("timeoutMs 必须是正数")
    tab_id = CDP.resolve_tab_id(params.get("tabId"))
    deadline = time.time() + timeout_ms / 1000.0
    expr = "!!document.querySelector(%s)" % json.dumps(selector)
    while True:
        if CDP.evaluate(tab_id, expr):
            return {"tabId": tab_id, "found": selector}
        if time.time() >= deadline:
            raise CDPError("等待超时: %dms 内没有等到元素 %s"
                           % (timeout_ms, selector))
        time.sleep(0.25)


def ep_download_dir(params):
    path = params.get("path")
    if not path or not isinstance(path, str):
        raise ValueError("缺少参数 path（桥所在电脑上的下载目录）")
    CDP.browser_call("Browser.setDownloadBehavior", {
        "behavior": "allow", "downloadPath": path, "eventsEnabled": True,
    })
    return {"downloadPath": path}


def ep_upload(params):
    selector = params.get("selector")
    file_path = params.get("filePath")
    if not selector:
        raise ValueError("缺少参数 selector")
    if not file_path:
        raise ValueError("缺少参数 filePath（桥所在电脑上的文件路径）")
    if not os.path.isfile(file_path):
        raise ValueError("文件不存在: %s" % file_path)
    tab_id = CDP.resolve_tab_id(params.get("tabId"))
    doc = CDP.call(tab_id, "DOM.getDocument", {"depth": 1})
    root_id = doc.get("root", {}).get("nodeId")
    if not root_id:
        raise CDPError("上传失败: 拿不到页面 DOM 根节点")
    found = CDP.call(tab_id, "DOM.querySelector",
                     {"nodeId": root_id, "selector": selector})
    node_id = found.get("nodeId")
    if not node_id:
        raise CDPError("上传失败: 找不到元素 %s" % selector)
    CDP.call(tab_id, "DOM.setFileInputFiles",
             {"nodeId": node_id, "files": [os.path.abspath(file_path)]})
    return {"tabId": tab_id, "uploaded": file_path}


def ep_launch(params):
    """保证带调试口的小号 Edge 正在运行（没开就自动拉起）。"""
    return ensure_edge()


ENDPOINTS = {
    "/launch": ep_launch,
    "/tabs": ep_tabs,
    "/open": ep_open,
    "/text": ep_text,
    "/click": ep_click,
    "/type": ep_type,
    "/eval": ep_eval,
    "/close": ep_close,
    "/screenshot": ep_screenshot,
    "/cdp": ep_cdp,
    "/scroll": ep_scroll,
    "/key": ep_key,
    "/back": ep_back,
    "/forward": ep_forward,
    "/reload": ep_reload,
    "/wait": ep_wait,
    "/download_dir": ep_download_dir,
    "/upload": ep_upload,
}


# ---------------------------------------------------------------- HTTP 服务

class Handler(BaseHTTPRequestHandler):
    server_version = "EdgeBridge/2.2"

    def log_message(self, fmt, *args):  # 只打简洁日志
        log("[%s] %s" % (self.address_string(), fmt % args))

    def _send_json(self, status, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _check_token(self):
        given = self.headers.get("X-Bridge-Token", "")
        return hmac.compare_digest(given, self.server.bridge_token)

    def do_GET(self):
        if self.path.split("?")[0] == "/health":
            cdp_ok, cdp_info = False, None
            try:
                v = CDP.version()
                cdp_ok = True
                cdp_info = v.get("Browser")
            except Exception as e:  # CDP 没启动也要正常回答
                cdp_info = "CDP 不可达: %s" % e
            self._send_json(200, {"ok": True, "cdp": cdp_ok,
                                  "browser": cdp_info,
                                  "version": "2.2",
                                  "mode": os.environ.get(
                                      "BRIDGE_MODE", "manual")})
        else:
            self._send_json(404, {"ok": False, "error": "未知路径: %s"
                                  % self.path})

    def do_POST(self):
        path = self.path.split("?")[0]
        if path not in ENDPOINTS:
            self._send_json(404, {"ok": False, "error": "未知路径: %s" % path})
            return
        if not self._check_token():
            self._send_json(401, {"ok": False,
                                  "error": "token 校验失败（请求头 X-Bridge-Token）"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length > 0 else b""
            params = json.loads(raw.decode("utf-8")) if raw else {}
            if not isinstance(params, dict):
                raise ValueError("请求体必须是 JSON 对象")
        except (ValueError, UnicodeDecodeError) as e:
            self._send_json(400, {"ok": False, "error": "请求体不是合法 JSON: %s" % e})
            return
        try:
            if path != "/launch":
                maybe_autolaunch()
            result = ENDPOINTS[path](params)
            self._send_json(200, dict({"ok": True}, **result))
        except ValueError as e:      # 参数问题
            self._send_json(400, {"ok": False, "error": str(e)})
        except CDPError as e:        # 页面/标签页层面的问题
            self._send_json(404, {"ok": False, "error": str(e)})
        except (urllib.error.URLError, OSError, websocket.WebSocketException) as e:
            # 多半是 Edge 没开调试口或中途关了
            self._send_json(502, {"ok": False,
                                  "error": "连不上 Edge 调试口(9222)，请确认已用 start-edge.bat 或 start-edge-main.bat 启动带调试口的 Edge。详情: %s" % e})
        except Exception as e:       # 兜底，绝不让服务崩掉
            self._send_json(500, {"ok": False, "error": "内部错误: %s" % e})


# ---------------------------------------------------------------- 启动辅助

def load_or_create_token():
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE, "r", encoding="utf-8") as f:
            token = f.read().strip()
        if token:
            return token
    token = secrets.token_hex(16)
    with open(TOKEN_FILE, "w", encoding="utf-8") as f:
        f.write(token + "\n")
    return token


def _in_tailscale_range(ip):
    try:
        return ipaddress.ip_address(ip) in TAILSCALE_NET
    except ValueError:
        return False


def find_tailscale_ip():
    """想办法找出本机在 Tailscale 网络里的 IP（100.64.0.0/10），找不到返回 None。"""
    # 方法 1：tailscale 命令行（Windows / Linux 都适用）
    # 自启进程的 PATH 可能没有 tailscale，再试一次全路径。
    for cmd in (["tailscale", "ip", "-4"],
                [r"C:\Program Files\Tailscale\tailscale.exe", "ip", "-4"]):
        try:
            out = subprocess.run(cmd, capture_output=True,
                                 text=True, timeout=5)
            for line in (out.stdout or "").splitlines():
                ip = line.strip()
                if _in_tailscale_range(ip):
                    return ip
        except Exception:
            pass
    # 方法 1b：解析 ipconfig 输出，找 100.64/10 网段的地址。
    # TUN 类代理劫持的是路由、动不了网卡本身的地址，这条最抗干扰。
    try:
        out = subprocess.run(["ipconfig"], capture_output=True,
                             text=True, timeout=8)
        import re
        for ip in re.findall(r"\d{1,3}(?:\.\d{1,3}){3}", out.stdout or ""):
            if _in_tailscale_range(ip):
                return ip
    except Exception:
        pass
    # 方法 2：本机主机名解析出的地址里找
    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            if _in_tailscale_range(ip):
                return ip
    except Exception:
        pass
    # 方法 3：试着"连"一下 Tailscale 的服务地址 100.100.100.100，
    #         看系统会选用哪个本地地址出去（UDP connect 不会真发数据）
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("100.100.100.100", 80))
        ip = s.getsockname()[0]
        s.close()
        if _in_tailscale_range(ip):
            return ip
    except Exception:
        pass
    return None


def main():
    log("main 开始，模式=%s，解释器=%s" % (
        os.environ.get("BRIDGE_MODE", "manual"), sys.executable))
    token = load_or_create_token()
    port = int(os.environ.get("BRIDGE_PORT", DEFAULT_PORT))

    forced = os.environ.get("BRIDGE_HOST")
    ts_ip = None if forced else find_tailscale_ip()
    host = forced or ts_ip or "127.0.0.1"
    log("监听地址选定: %s（forced=%s, tailscale=%s）" % (host, forced, ts_ip))

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    server.bridge_token = token

    log("=" * 62)
    log("  Edge 浏览器桥已启动")
    log("  连接地址: http://%s:%d" % (host, port))
    log("  Token:    %s" % token)
    log("  把上面「连接地址」和「Token」两行发给助手即可。")
    if not forced and not ts_ip:
        log("  !! 警告：没找到 Tailscale 网卡，现在只有本机能连。")
        log("  !! 请确认 Tailscale 已安装并登录，再重新运行本桥。")
    log("  停止方法：关掉这个窗口（或按 Ctrl+C），再关掉那个独立 Edge。")
    log("=" * 62)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("已停止。")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # 自启时没有控制台，异常必须落进 bridge.log，否则无声无息
        try:
            log("致命异常，桥退出：\n" + traceback.format_exc())
        except Exception:
            pass
        raise
