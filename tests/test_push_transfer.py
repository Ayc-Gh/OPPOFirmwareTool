"""Exercise transfer ownership using a temporary directory as phone storage."""
import hashlib
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path, PurePosixPath
from unittest.mock import Mock, patch
from uuid import UUID

from oplus_tool import push


class TransferTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        self.phone = root / "sdcard"
        self.phone.mkdir()
        self.local = root / "firmware.zip"
        self.local.write_bytes(b"new firmware")
        self.original = self.phone / self.local.name
        self.original.write_bytes(b"original firmware")
        self.commands = []
        self.transfers = []
        self.cancel = threading.Event()
        self.popen = self.mock(push.subprocess, "Popen", side_effect=self.transfer)
        self.digest = hashlib.sha256(self.local.read_bytes()).hexdigest()
        self.mock(push, "adb_path", return_value="mock-adb")
        self.mock(push, "command", side_effect=self.shell)
        self.mock(push, "validate_push_source", return_value=self.digest)
        self.trigger = self.mock(push, "trigger_downgrade")

    def mock(self, obj, name, **kwargs):
        patcher = patch.object(obj, name, **kwargs)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def device_path(self, name):
        relative = PurePosixPath(name).relative_to("/sdcard")
        self.assertNotIn("..", relative.parts)
        return self.phone.joinpath(*relative.parts)

    def shell(self, args, custom="", timeout=10):
        self.assertEqual(args[:3], ["-s", "SERIAL", "shell"])
        cmd = args[3:]
        self.commands.append(cmd)
        if cmd[0] == "getprop":
            return "[ro.product.brand]: [OPPO]\n[ro.product.model]: [PHY110]\n[ro.build.version.release]: [14]"
        if cmd[:3] == ["pm", "list", "packages"]:
            return "package:" + push.PACKAGE_ASSISTANT
        if cmd[0] == "df":
            return "Filesystem 1K-blocks Used Available\n/dev 9999999 123 9000000"
        path = self.device_path(cmd[-1])
        if cmd == ["mkdir", cmd[-1]]:
            path.mkdir()
        elif cmd[:3] == ["stat", "-c", "%s"]:
            return str(path.stat().st_size)
        elif cmd[:2] == ["rm", "-f"]:
            path.unlink(missing_ok=True)
        elif cmd == ["rmdir", cmd[-1]]:
            path.rmdir()
        else:
            self.fail(f"Unexpected device command {cmd!r}")
        return ""

    def transfer(self, args, **kwargs):
        self.assertEqual(args[:3], ["mock-adb", "-s", "SERIAL"])
        destination = self.device_path(args[-1])
        if args[3:5] == ["shell", "sha256sum"]:
            output = hashlib.sha256(destination.read_bytes()).hexdigest() + "  " + args[-1]
        else:
            self.assertEqual(args[3], "push")
            destination.write_bytes(Path(args[-2]).read_bytes())
            self.transfers.append(destination)
            output = ""
        process = Mock(returncode=0)
        process.poll.return_value = 0
        process.communicate.return_value = (output, "")
        return process

    def run_push(self, **kwargs):
        push.push_and_install("SERIAL", self.local, cancel=self.cancel, **kwargs)

    def assert_only_original(self):
        self.assertEqual(list(self.phone.iterdir()), [self.original])
        self.assertEqual(self.original.read_bytes(), b"original firmware")

    def test_cancel_in_flight_waits_for_killed_writer_and_preserves_original(self):
        reaped = False

        def start(args, **kwargs):
            process = self.transfer(args, **kwargs)
            process.poll.return_value = None

            def communicate(timeout=None):
                nonlocal reaped
                if timeout is None:
                    # Removal must happen only after the child finishes writing.
                    self.assertTrue(self.transfers[-1].exists())
                    reaped = True
                    return "", ""
                self.cancel.set()
                raise subprocess.TimeoutExpired("adb", timeout)

            process.communicate.side_effect = communicate
            return process

        self.popen.side_effect = start
        with self.assertRaises(push.Cancelled):
            self.run_push()
        self.assertTrue(reaped)
        self.assert_only_original()
        self.trigger.assert_not_called()

    def test_cancel_after_transfer_and_before_ota_cleans_only_this_attempt(self):
        self.run_push()
        first = self.transfers[-1]
        self.assertEqual(self.device_path(self.trigger.call_args.args[1]), first)
        self.run_push()
        second = self.transfers[-1]
        self.assertNotEqual(first.parent, second.parent)

        def progress(done, total, message):
            if "唤起" in message:
                self.cancel.set()

        with self.assertRaises(push.Cancelled):
            self.run_push(progress=progress)
        self.assertEqual(self.trigger.call_count, 2)
        self.assertEqual(self.original.read_bytes(), b"original firmware")
        self.assertEqual(first.read_bytes(), b"new firmware")
        self.assertEqual(second.read_bytes(), b"new firmware")
        self.assertFalse(self.transfers[-1].parent.exists())

    def test_directory_collision_never_writes_or_cleans_existing_files(self):
        token = UUID(int=1)
        existing = self.phone / ("OPPOFirmwareTool-" + token.hex)
        existing.mkdir()
        stored = existing / "firmware.zip"
        stored.write_bytes(b"other transfer")
        with patch.object(push, "uuid4", return_value=token), self.assertRaises(FileExistsError):
            self.run_push()
        self.popen.assert_not_called()
        self.trigger.assert_not_called()
        self.assertEqual(stored.read_bytes(), b"other transfer")
        self.assertEqual(self.original.read_bytes(), b"original firmware")
        self.assertFalse(any(cmd[0] in {"rm", "rmdir"} for cmd in self.commands))

    def test_overlapping_attempts_keep_the_other_installers_copy(self):
        def interleave(args, **kwargs):
            process = self.transfer(args, **kwargs)
            if args[3] == "push" and len(self.transfers) == 1:
                push.push_and_install("SERIAL", self.local, cancel=threading.Event())
                self.cancel.set()
            return process

        self.popen.side_effect = interleave
        with self.assertRaises(push.Cancelled):
            self.run_push()
        first, second = self.transfers
        self.assertFalse(first.parent.exists())
        self.assertEqual(second.read_bytes(), b"new firmware")
        self.assertEqual(self.device_path(self.trigger.call_args.args[1]), second)
        self.trigger.assert_called_once()
        self.assertEqual(self.original.read_bytes(), b"original firmware")

    def test_transfer_error_cleans_only_its_partial_package(self):
        def fail(args, **kwargs):
            process = self.transfer(args, **kwargs)
            process.returncode = 1
            process.poll.return_value = 1
            process.communicate.return_value = "", "transfer failed"
            return process

        self.popen.side_effect = fail
        with self.assertRaisesRegex(RuntimeError, "transfer failed"):
            self.run_push()
        self.assert_only_original()
        self.trigger.assert_not_called()

    def test_corrupted_or_unverifiable_phone_copy_is_never_handed_off(self):
        for output in ("different content", "not a checksum", "unsupported command"):
            with self.subTest(output=output):
                def respond(args, **kwargs):
                    if "sha256sum" in args and output == "different content":
                        destination = self.device_path(args[-1])
                        destination.write_bytes(b"x" * destination.stat().st_size)
                    process = self.transfer(args, **kwargs)
                    if "sha256sum" in args and output == "not a checksum":
                        process.communicate.return_value = ("", "")
                    if "sha256sum" in args and output == "unsupported command":
                        process.returncode = 127
                        process.communicate.return_value = ("", "sha256sum: not found")
                    return process

                self.popen.side_effect = respond
                with self.assertRaisesRegex(RuntimeError, "SHA256|sha256sum"):
                    self.run_push()
                self.assert_only_original()
                self.trigger.assert_not_called()

    def test_cancel_while_phone_hashes_never_hands_off(self):
        def start(args, **kwargs):
            process = self.transfer(args, **kwargs)
            if "sha256sum" in args:
                process.poll.return_value = None
                def communicate(timeout=None):
                    if timeout == 0.5:
                        self.cancel.set()
                        raise subprocess.TimeoutExpired("sha256sum", timeout)
                    return "", ""
                process.communicate.side_effect = communicate
            return process

        self.popen.side_effect = start
        with self.assertRaises(push.Cancelled):
            self.run_push()
        self.assert_only_original()
        self.trigger.assert_not_called()

    def test_handoff_timeout_retains_the_verified_package_and_reports_its_path(self):
        self.trigger.side_effect = subprocess.TimeoutExpired("adb", 15)
        messages = []
        with self.assertRaisesRegex(RuntimeError, "无法确认.*交接") as failure:
            self.run_push(log=messages.append)
        remote = self.trigger.call_args.args[1]
        self.assertEqual(self.device_path(remote).read_bytes(), self.local.read_bytes())
        self.assertEqual(self.original.read_bytes(), b"original firmware")
        self.assertFalse(any(cmd[0] in {"rm", "rmdir"} for cmd in self.commands))
        self.assertIn(remote, str(failure.exception))
        self.assertTrue(any("保留" in message and remote in message for message in messages))

    def test_zero_free_space_blocks_before_installing_or_writing(self):
        self.mock(push, "get_device_free_space", return_value=0)
        self.mock(push, "check_assistant_installed", return_value=False)
        install = self.mock(push, "install_assistant")
        with self.assertRaisesRegex(RuntimeError, "存储空间不足"):
            self.run_push()
        install.assert_not_called()
        self.popen.assert_not_called()
        self.trigger.assert_not_called()
        self.assertFalse(any(cmd[0] == "mkdir" for cmd in self.commands))
        self.assert_only_original()


class UpdaterStatusTests(unittest.TestCase):
    def test_both_activity_errors_never_report_success(self):
        attempts = []
        def shell(args, custom="", timeout=10):
            if args[3:5] == ["am", "start"]:
                attempts.append(args[-1])
                return "Error type 3: Activity does not exist"
            return ""

        messages = []
        with patch("oplus_tool.ota_picker.command", side_effect=shell):
            with self.assertRaisesRegex(RuntimeError, "两个系统更新入口均未能唤起"):
                path = "/sdcard/OPPOFirmwareTool-" + "1" * 32 + "/firmware.zip"
                push.trigger_downgrade("SERIAL", path, log=messages.append, expected_size=1024)
        self.assertEqual(attempts, [push.MODERN_ENTRY, push.LEGACY_ENTRY])
        self.assertFalse(any("成功唤起" in message for message in messages))
