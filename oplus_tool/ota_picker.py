"""Select a verified transfer in the stock ColorOS local-update file picker."""
from __future__ import annotations

import re
import subprocess
import time
import xml.etree.ElementTree as ET
from pathlib import PurePosixPath

from .adb import command
from .common import check_cancel


FILE_MANAGER = "com.coloros.filemanager"
MODERN_ENTRY = "com.oplus.ota/com.oplus.otaui.activity.EntryActivity"
LEGACY_ENTRY = "com.oppo.ota/com.oppo.otaui.activity.EntryActivity"


class _ScreenUnavailable(RuntimeError):
    pass


def _bounds(node):
    match = re.fullmatch(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", node.get("bounds", ""))
    if not match:
        raise RuntimeError("手机控件位置无法核实。")
    x1, y1, x2, y2 = map(int, match.groups())
    if x2 <= x1 or y2 <= y1:
        raise RuntimeError("手机控件不在可见区域。")
    return x1, y1, x2, y2


def _node(root, package, resource, *labels):
    for node in root.iter("node"):
        if (node.get("package") != package or node.get("resource-id") != f"{package}:id/{resource}"
                or node.get("visible-to-user") == "false"):
            continue
        text = node.get("text", "").replace("\n", "").replace("\r", "")
        if not labels or text in labels or node.get("content-desc") in labels:
            return node
    return None


def _preview(root, package, expected_size, expected_version):
    if _node(root, package, "local_update_root") is None:
        return None
    button = _node(root, package, "download_button")
    if button is None:
        return None
    text = button.get("text", "").strip().casefold()
    description = button.get("content-desc", "").strip().casefold()
    if text and description and text != description:
        raise RuntimeError("更新器确认按钮的文字和无障碍描述不一致，无法确认交接结果。")
    label = text or description
    if label in ("重新选择", "reselect", "select again"):
        reason = _node(root, package, "version_title")
        raise RuntimeError("系统更新器拒绝了所选固件：" + (reason.get("text", "") if reason is not None else "请查看手机。"))
    if label not in ("立即安装", "install now") or button.get("enabled") != "true":
        return None
    value = _node(root, package, "version_content")
    detail = value.get("text", "").strip() if value is not None else ""
    match = re.fullmatch(r"([^|]+?)\s*\|\s*(\d+(?:\.\d+)?)\s*(B|KB|MB|GB|TB)", detail, re.I)
    if not match:
        return None  # The confirmation button and its version/size label render separately.
    version, amount, unit = match.groups()
    region_suffix = r"\((?:CN|EX|EU|IN)\d{2}\)$"
    expected_version = expected_version.strip()
    if expected_version and version.strip() not in (expected_version, re.sub(region_suffix, "", expected_version)):
        raise RuntimeError(f"更新器显示的固件版本与推送包不符：{detail}")
    scale = 1024 ** ("B", "KB", "MB", "GB", "TB").index(unit.upper())
    decimals = len(amount.partition(".")[2])
    if abs(float(amount) - expected_size / scale) > 0.5 * 10 ** -decimals:
        raise RuntimeError(f"更新器显示的固件大小与推送包不符：{detail}")
    return detail


class LocalUpdatePicker:
    def __init__(self, serial, custom_adb="", cancel=None):
        self.serial = serial
        self.custom_adb = custom_adb
        self.cancel = cancel

    def adb(self, *args, timeout=20):
        if self.cancel is not None:
            check_cancel(self.cancel)
        return command(["-s", self.serial, *args], self.custom_adb, timeout=timeout)

    def screen(self, timeout=20):
        xml = self.adb("exec-out", "uiautomator", "dump", "/dev/tty", timeout=timeout)
        start, end = xml.find("<hierarchy"), xml.rfind("</hierarchy>")
        if start < 0 or end < start:
            raise _ScreenUnavailable("无法读取手机当前界面。请解锁手机并保持屏幕点亮。")
        try:
            return ET.fromstring(xml[start:end + len("</hierarchy>")])
        except ET.ParseError as exc:
            raise _ScreenUnavailable("无法读取手机当前界面。") from exc

    def wait(self, check, description, timeout=20):
        deadline = time.monotonic() + timeout
        remaining = timeout
        while remaining > 0:
            try:
                root = self.screen(timeout=min(20, remaining))
            except (_ScreenUnavailable, subprocess.TimeoutExpired):
                pass  # Retry only observation; input commands are never replayed here.
            else:
                result = check(root)
                if result is not None:
                    return root, result
            remaining = deadline - time.monotonic()
            if remaining > 0:
                time.sleep(min(.4, remaining))
                remaining = deadline - time.monotonic()
        raise RuntimeError("手机未显示" + description + "。请确认手机已解锁并保持亮屏。")

    def require_focus(self, package):
        # A virtual display can be listed first while the phone screen has focus.
        displays = self.adb("shell", "dumpsys window displays | grep -E 'Display: mDisplayId=|mCurrentFocus=|mFocusedApp='")
        match = re.search(r"^\s*Display:\s+mDisplayId=0\b[^\n]*\n(.*?)(?=^\s*Display:\s+mDisplayId=|\Z)",
                          displays, re.M | re.S)
        if not match:
            raise RuntimeError("无法确认手机主屏的前台窗口。")
        lines = match.group(1).splitlines()
        window = next((line for line in lines if "mCurrentFocus=" in line), "")
        component = r"(?:^|[\s{])" + re.escape(package) + "/"
        if re.search(component, window):
            return
        app = next((line for line in lines if "mFocusedApp=" in line), "")
        if "PopupWindow:" in window and re.search(component, app):
            return
        raise RuntimeError("手机前台窗口已变化，已停止选包。")

    @staticmethod
    def dimensions(root):
        frame = root.find("node")
        if frame is None:
            raise RuntimeError("手机界面为空。")
        _, _, w, h = _bounds(frame)
        return w, h

    def tap(self, root, node):
        w, h = self.dimensions(root)
        x1, y1, x2, y2 = _bounds(node)
        x, y = (x1 + x2) // 2, (y1 + y2) // 2
        if not (0 < x < w and 0 < y < h * .78) or node.get("enabled") != "true":
            raise RuntimeError("导航控件超出安全操作区域。")
        self.require_focus(node.get("package"))
        self.adb("shell", "input", "-d", "0", "tap", str(x), str(y))

    def control(self, package, resource, labels):
        root, node = self.wait(lambda r: _node(r, package, resource, *labels), "导航控件 " + (labels[0] if labels else resource))
        self.tap(root, node)

    def open_folder(self, name):
        previous = None
        search_up = False
        for _ in range(30):
            root, _ = self.wait(lambda r: _node(r, FILE_MANAGER, "file_list_item_title"), "存储根目录的文件列表")
            self.require_focus(FILE_MANAGER)
            w, h = self.dimensions(root)
            row = _node(root, FILE_MANAGER, "file_list_item_title", name)
            direction = 1 if search_up else -1
            if row is not None:
                _, y1, _, y2 = _bounds(row)
                top = max((_bounds(n)[3] for n in root.iter("node")
                           if n.get("resource-id") in (f"{FILE_MANAGER}:id/search_bar", f"{FILE_MANAGER}:id/toolbar")), default=0)
                y = (y1 + y2) // 2
                if top < y < .76 * h:
                    self.tap(root, row)
                    return
                if y <= top:
                    direction = 1
            visible = [(n.get("text"), n.get("bounds")) for n in root.iter("node")
                       if n.get("resource-id") == f"{FILE_MANAGER}:id/file_list_item_title"]
            if visible == previous:
                if search_up:
                    break
                # A picker may restore its scroll position from the previous run.
                search_up = True
                direction = 1
            previous = visible
            y1, y2 = (.72, .40) if direction < 0 else (.40, .62)
            self.adb("shell", "input", "-d", "0", "swipe", str(w//2), str(int(h*y1)), str(w//2), str(int(h*y2)), "500")
        raise RuntimeError("文件选择器中找不到本次传输的独立目录。")

    def select_package(self, package, directory, filename):
        def home_menu(root):
            if _node(root, package, "local_update_root") is not None:
                raise RuntimeError("更新器仍显示上次的固件预览，请先返回软件更新首页。")
            return _node(root, package, "coui_toolbar_more_view")

        root, menu = self.wait(home_menu, "软件更新首页的更多选项")
        self.tap(root, menu)
        self.control(package, "popup_list_window_item_title", ("本地安装", "Local install"))
        def start_picker(root):
            storage = _node(root, FILE_MANAGER, "item_title", "全部文件", "All files")
            if storage is not None:
                return "storage", storage
            recent = _node(root, FILE_MANAGER, "title_view", "最近", "Recent", "Recents")
            if recent is not None:
                return "recent", recent
            return None

        root, (page, node) = self.wait(start_picker, "系统文件选择器")
        if page == "recent":
            self.tap(root, node)
            self.control(FILE_MANAGER, "popup_list_window_item_title", ("文件", "Files"))
        self.control(FILE_MANAGER, "item_title", ("全部文件", "All files"))
        self.open_folder(directory)

        def file_in_task_folder(root):
            _, h = self.dimensions(root)
            # The directory name must be in a header, not another file-list row.
            in_folder = any(n.get("package") == FILE_MANAGER and not n.get("resource-id")
                            and n.get("text", "").replace("\n", "") == directory and _bounds(n)[3] < h * .30
                            for n in root.iter("node"))
            if in_folder:
                return _node(root, FILE_MANAGER, "file_list_item_title", filename)
            return None

        root, file_row = self.wait(file_in_task_folder, "本次任务目录中的固件文件")
        self.tap(root, file_row)  # Last input. Only observe the updater from here.

    def handoff(self, path, preferred_entry, expected_size, expected_version, log=None):
        if not re.fullmatch(r"/sdcard/OPPOFirmwareTool-[a-f0-9]{32}/firmware\.(?:zip|ozip)", path):
            raise ValueError("固件必须位于本次传输的独立目录。")
        if expected_size is None:
            expected_size = int(self.adb("shell", "stat", "-c", "%s", path).strip())
        if expected_size <= 0:
            raise ValueError("手机固件大小无效。")
        entries = (preferred_entry, LEGACY_ENTRY if preferred_entry == MODERN_ENTRY else MODERN_ENTRY)
        for entry in entries:
            try:
                display = ("--display", "0") if preferred_entry == MODERN_ENTRY else ()
                out = self.adb("shell", "am", "start", "-W", *display, "-a", "android.intent.action.MAIN", "-n", entry)
            except RuntimeError as exc:
                if "does not exist" in str(exc) or "unable to resolve" in str(exc):
                    continue
                raise
            if re.search(r"^Status:\s*ok\s*$", out, re.M | re.I) and not re.search(r"\b(error|exception)\b", out, re.I):
                break
        else:
            raise RuntimeError("两个系统更新入口均未能唤起。")
        package = entry.split("/")[0]
        target = PurePosixPath(path)
        if log:
            log("正在通过系统更新器的本地安装菜单选择已校验的固件…")
        self.select_package(package, target.parent.name, target.name)
        _, result = self.wait(lambda r: _preview(r, package, expected_size, expected_version),
                              "所选固件的版本、大小和安装确认按钮", timeout=150)
        self.require_focus(package)
        return result

