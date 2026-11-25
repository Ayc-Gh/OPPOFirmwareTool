import subprocess
import unittest
import xml.etree.ElementTree as ET
from unittest.mock import patch

from oplus_tool import ota_picker


OTA = "com.oplus.ota"
FM = ota_picker.FILE_MANAGER
DIR = "OPPOFirmwareTool-" + "1234" * 8
SIZE = 9284634542
VERSION = "PHY110_17.0.0.100"


def screen(package, *widgets):
    root = ET.Element("hierarchy")
    frame = ET.SubElement(root, "node", package=package, bounds="[0,0][1080,2376]")
    for name, text, y in widgets:
        rid = f"{package}:id/{name}" if name else ""
        ET.SubElement(frame, "node", {"package": package, "resource-id": rid, "text": text,
                                     "enabled": "true", "bounds": f"[100,{y}][300,{y+80}]"})
    return root


def preview(detail, button="立即安装"):
    return screen(OTA, ("local_update_root", "", 50), ("version_title", "软件版本", 250),
                  ("version_content", detail, 420), ("download_button", button, 2100))


def focus(main, virtual="com.example.other"):
    return (f"Display: mDisplayId=10003\n mCurrentFocus=Window{{abcd u0 {virtual}/.Other}}\n"
            f"Display: mDisplayId=0\n mCurrentFocus=Window{{abcd u0 {main}/.Main}}\n")


class PickerTests(unittest.TestCase):
    def setUp(self):
        self.picker = ota_picker.LocalUpdatePicker("SERIAL")

    def test_home_stale_or_wrong_details_cannot_confirm_a_preview(self):
        self.assertIsNone(ota_picker._preview(screen(OTA, ("version_content", VERSION+" | 8.65 GB", 200)), OTA, SIZE, VERSION))
        self.assertIsNone(ota_picker._preview(preview(VERSION+" | 8.65 GB", button="正在验证"), OTA, SIZE, VERSION))
        for detail in (VERSION+" | 8.80 GB", "PHY110_17.0.0.101 | 8.65 GB"):
            with self.subTest(detail=detail), self.assertRaises(RuntimeError):
                ota_picker._preview(preview(detail), OTA, SIZE, VERSION)
        for detail in ("", "8.65 GB", VERSION + " | "):
            self.assertIsNone(ota_picker._preview(preview(detail), OTA, SIZE, VERSION))
        with self.assertRaisesRegex(RuntimeError, "拒绝"):
            ota_picker._preview(preview("", button="重新选择"), OTA, SIZE, VERSION)
        with self.assertRaisesRegex(RuntimeError, "版本"):
            ota_picker._preview(preview(VERSION + "(CN02) | 8.65 GB"), OTA, SIZE, VERSION + "(CN01)")

    def test_partial_preview_and_slow_screen_read_are_only_observed_again(self):
        detail = VERSION + " | 8.65 GB"
        with patch.object(self.picker, "screen", side_effect=[
                preview(""), subprocess.TimeoutExpired("uiautomator", 20), preview(detail)]) as read, \
             patch.object(ota_picker.time, "sleep"), patch.object(self.picker, "tap") as tap:
            _, observed = self.picker.wait(lambda r: ota_picker._preview(r, OTA, SIZE, VERSION),
                                           "版本和大小", timeout=150)
        self.assertEqual(observed, detail)
        self.assertEqual(read.call_count, 3)
        tap.assert_not_called()

    def test_coloros17_confirmation_button_only_exposes_accessibility_description(self):
        detail = VERSION + " | 8.65 GB"
        root = preview(detail, button="")
        button = root.find(".//*[@resource-id='com.oplus.ota:id/download_button']")
        button.set("content-desc", "立即安装")
        self.assertEqual(ota_picker._preview(root, OTA, SIZE, VERSION + "(CN01)"), detail)
        button.set("enabled", "false")
        self.assertIsNone(ota_picker._preview(root, OTA, SIZE, VERSION))

    def test_accessibility_rejection_and_conflicting_action_labels_fail_closed(self):
        root = preview(VERSION + " | 8.65 GB", button="")
        button = root.find(".//*[@resource-id='com.oplus.ota:id/download_button']")
        button.set("content-desc", "重新选择")
        with self.assertRaisesRegex(RuntimeError, "拒绝"):
            ota_picker._preview(root, OTA, SIZE, VERSION)
        button.set("text", "立即安装")
        with self.assertRaisesRegex(RuntimeError, "不一致"):
            ota_picker._preview(root, OTA, SIZE, VERSION)

    def test_zero_exit_and_home_page_do_not_report_handoff_success(self):
        with patch.object(self.picker, "adb", return_value="Status: ok"), \
             patch.object(self.picker, "screen", return_value=screen(OTA, ("version_content", VERSION, 200))), \
             patch.object(ota_picker.time, "monotonic", side_effect=[0, 300]):
            with self.assertRaisesRegex(RuntimeError, "未显示"):
                self.picker.handoff("/sdcard/"+DIR+"/firmware.zip", ota_picker.MODERN_ENTRY, SIZE, VERSION)

    def test_default_display_focus_must_match_before_input(self):
        node = screen(FM, ("item_title", "全部文件", 600))
        with patch.object(self.picker, "adb", return_value=focus("com.example.other", FM)) as adb:
            with self.assertRaisesRegex(RuntimeError, "前台窗口"):
                self.picker.tap(node, list(node.iter("node"))[1])
            self.assertFalse(any("input" in c.args for c in adb.call_args_list))

    def test_ignores_same_named_file_outside_the_task_directory(self):
        recent = screen(FM, ("title_view", "最近", 180), ("file_list_item_title", "firmware.zip", 600),
                        ("file_list_item_title", DIR, 300))
        home = screen(OTA, ("coui_toolbar_more_view", "", 150))
        with patch.object(self.picker, "control"), patch.object(self.picker, "open_folder"), \
             patch.object(self.picker, "screen", side_effect=[home, recent, recent]), patch.object(self.picker, "tap") as tap, \
             patch.object(ota_picker.time, "monotonic", side_effect=[0, 0, 0, 300]):
            with self.assertRaisesRegex(RuntimeError, "任务目录"):
                self.picker.select_package(OTA, DIR, "firmware.zip")
            self.assertEqual([c.args[1].get("text") for c in tap.call_args_list], ["", "最近"])

    def test_preexisting_matching_preview_is_not_accepted(self):
        with patch.object(self.picker, "screen", return_value=preview(VERSION + " | 8.65 GB")), \
             patch.object(self.picker, "tap") as tap:
            with self.assertRaisesRegex(RuntimeError, "上次"):
                self.picker.select_package(OTA, DIR, "firmware.zip")
            tap.assert_not_called()

    def test_exact_directory_file_and_matching_preview_with_no_install_tap(self):
        screens = [
            screen(OTA, ("coui_toolbar_more_view", "", 150)),
            screen(OTA, ("popup_list_window_item_title", "本地安装", 560)),
            screen(FM, ("title_view", "最近", 180), ("file_list_item_title", "firmware.zip", 600)),
            screen(FM, ("popup_list_window_item_title", "文件", 499)),
            screen(FM, ("item_title", "全部文件", 1180)),
            screen(FM, ("file_list_item_title", DIR[:23]+"\n"+DIR[23:], 1300)),
            screen(FM, ("", DIR, 170), ("file_list_item_title", "firmware.zip", 685)),
            preview(VERSION+" | 8.65 GB"),
        ]
        page = 0
        taps = []

        def adb(*cmd, **kwargs):
            nonlocal page
            if cmd[:3] == ("shell","am","start"):
                self.assertNotIn("InstallPackage", cmd)
                return "Status: ok\n"
            if cmd[0] == "exec-out":
                return ET.tostring(screens[page], encoding="unicode")
            if cmd[1].startswith("dumpsys window"):
                return focus(screens[page].find("node").get("package"))
            self.assertEqual(cmd[:5], ("shell","input","-d","0","tap"))
            taps.append(int(cmd[-1]))
            page += 1
            return ""

        with patch.object(self.picker, "adb", side_effect=adb):
            result = self.picker.handoff("/sdcard/"+DIR+"/firmware.zip", ota_picker.MODERN_ENTRY,
                                         SIZE, VERSION+"(CN01)")
        self.assertEqual(result, VERSION+" | 8.65 GB")
        self.assertEqual(taps, [190, 600, 220, 539, 1220, 1340, 725])

