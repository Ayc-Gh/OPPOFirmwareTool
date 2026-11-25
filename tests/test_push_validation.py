import hashlib
import struct
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from oplus_tool import adb, payload, push, push_validation
from oplus_tool.catalog import Package
from tests.fixtures import make_payload


class PushValidationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        directory = tempfile.TemporaryDirectory()
        cls.addClassCleanup(directory.cleanup)
        make_payload(directory.name)
        cls.payload = (Path(directory.name) / "payload.bin").read_bytes()

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "PHY110_update.zip"
        self.device = adb.Device("SERIAL", model="PHY110", codename="OP565FL1", region="cn", nvid="10010111")
        minimum = patch.object(push_validation, "MIN_PACKAGE_SIZE", 1)
        minimum.start()
        self.addCleanup(minimum.stop)

    def archive(self, metadata, payload=None):
        with zipfile.ZipFile(self.path, "w") as archive:
            archive.writestr("META-INF/com/android/metadata", metadata)
            archive.writestr("payload.bin", self.payload if payload is None else payload)

    def validate(self, record=None):
        return push.validate_push_source(self.path, self.device, record)

    def record(self, **updates):
        values = dict(model="PHY110", region="cn", channel="降级包", kind="全量包",
                      version="16", ota="OTA", component="System", url="",
                      recipe={"downgrade": True}, md5=hashlib.md5(self.path.read_bytes()).hexdigest())
        return Package(**(values | updates))

    def test_full_zip_uses_embedded_device_and_checks_all_data(self):
        self.archive("pre-device=OTHER|OP565FL1\npost-build=OPPO/PHY110/OP565FL1:16/build\n")
        digest = hashlib.sha256(self.path.read_bytes()).hexdigest()
        self.assertEqual(self.validate(), digest)
        with self.assertRaisesRegex(ValueError, "SHA256"):
            self.validate(self.record(sha256="b" * 64))
        # Damage only the last byte of the stored payload, leaving the manifest readable.
        with self.path.open("r+b") as file:
            offset = file.read().index(self.payload) + len(self.payload) - 1
            file.seek(offset)
            file.write(bytes([self.payload[-1] ^ 1]))
        with self.assertRaisesRegex((ValueError, zipfile.BadZipFile), "CRC"):
            self.validate()

    def test_filename_cannot_override_device_or_missing_identity(self):
        for meta, expected in (("pre-device=PKB110", "不匹配"), ("post-timestamp=1", "无法从此包"),
                               ("pre-device=PHY110\npost-build=OPPO/PKB110/OTHER:16/base", "不匹配")):
            with self.subTest(meta=meta):
                self.archive(meta)
                with self.assertRaisesRegex(ValueError, expected):
                    self.validate()

    def test_unreadable_or_incremental_payload_stops_before_device_writes(self):
        props = "[ro.product.brand]: [OPPO]\n[ro.product.model]: [PHY110]"
        delta_dir = self.path.parent / "delta"
        make_payload(delta_dir, delta=True)
        for body in (b"invalid payload", (delta_dir / "payload.bin").read_bytes()):
            with self.subTest(size=len(body)):
                self.archive("pre-device=PHY110", body)
                with patch.object(push, "command", return_value=props) as command, \
                     patch.object(push, "install_assistant") as install, \
                     patch.object(push, "_push_package_file") as transfer, \
                     patch.object(push, "trigger_downgrade") as trigger:
                    with self.assertRaises(ValueError):
                        push.push_and_install("SERIAL", self.path, record=self.record())
                    self.assertEqual([c.args[0][2:] for c in command.call_args_list], [["shell", "getprop"]])
                    install.assert_not_called()
                    transfer.assert_not_called()
                    trigger.assert_not_called()

    def test_replace_only_operations_cannot_hide_a_restricted_manifest(self):
        end = 24 + struct.unpack_from(">Q", self.payload, 12)[0]
        original = payload.fields(self.payload[24:end])
        for flag in ("partial", "minor", "old_info"):
            with self.subTest(flag=flag):
                fields = [f for f in original if f[0] != 12]
                if flag == "old_info":
                    i = next(i for i,f in enumerate(fields) if f[0] == 13)
                    fields[i] = (13, 2, fields[i][2] + payload.pack_fields([(6, 2, b"")]))
                else:
                    fields.append((16 if flag == "partial" else 12, 0, 1))
                manifest = payload.pack_fields(fields)
                body = b"CrAU" + struct.pack(">QQI", 2, len(manifest), 0) + manifest + self.payload[end:]
                self.archive("pre-device=PHY110", body)
                with self.assertRaisesRegex(ValueError, "全量降级包"):
                    self.validate()

    def test_shared_device_code_does_not_override_a_conflicting_product(self):
        self.device.model = "CPH2009"
        self.device.codename = "OP4C2DL1"
        self.path = self.path.with_name("firmware.zip")
        other = "OPPO/A001OP/OP4C2DL1:12/old"
        actual = "OPPO/CPH2009/OP4C2DL1:12/current"
        for metadata in ("pre-device=OP4C2DL1", f"pre-device=OP4C2DL1\npost-build={other}"):
            self.archive(metadata)
            with self.assertRaises(ValueError):
                self.validate()
        self.archive(f"pre-device=OP4C2DL1\npost-build={other}|{actual}")
        self.assertEqual(self.validate(), hashlib.sha256(self.path.read_bytes()).hexdigest())

    def test_opaque_package_requires_device_scoped_downgrade_digest(self):
        self.path.write_bytes(b"OPPOENCRYPT!" + bytes(128))
        valid = self.record()
        self.assertEqual(self.validate(valid), hashlib.sha256(self.path.read_bytes()).hexdigest())
        for obj in (None, self.record(model="PKB110"), self.record(region="eu"),
                    self.record(recipe={"downgrade": True, "nvid": "10011000"}),
                    self.record(md5=""), self.record(channel="正式版"), self.record(kind="补丁包"),
                    self.record(md5="a" * 32), self.record(size=999)):
            with self.subTest(record=obj):
                with self.assertRaises(ValueError):
                    self.validate(obj)
