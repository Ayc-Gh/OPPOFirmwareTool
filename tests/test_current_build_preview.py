import datetime
import hashlib
import io
import struct
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from asn1crypto import cms
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.serialization import pkcs7
from cryptography.x509.oid import NameOID

from oplus_tool import adb, ota_signature, payload, push_validation
from oplus_tool.catalog import Package
from tests.fixtures import make_payload


def certificate():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "OTA test")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now)
            .not_valid_after(now + datetime.timedelta(days=1)).sign(key, hashes.SHA256()))
    return cert, key


def sign_zip(path, cert, key, legacy=False):
    data = path.read_bytes()[:-2]  # Android leaves the EOCD comment length unsigned.
    builder = pkcs7.PKCS7SignatureBuilder().set_data(data).add_signer(cert, key, hashes.SHA256())
    der = builder.sign(serialization.Encoding.DER, [pkcs7.PKCS7Options.DetachedSignature,
                       pkcs7.PKCS7Options.Binary, pkcs7.PKCS7Options.NoAttributes])
    if legacy:
        envelope = cms.ContentInfo.load(der)
        info = envelope["content"]["signer_infos"][0]
        envelope["content"]["digest_algorithms"][0]["algorithm"] = "sha1"
        info["digest_algorithm"]["algorithm"] = "sha1"
        info["signature_algorithm"]["algorithm"] = "rsassa_pkcs1v15"
        info["signature"] = key.sign(data, padding.PKCS1v15(), hashes.SHA1())
        der = envelope.dump()
    signature_len = len(der) + 6
    # SignApk may also prefix the DER with a comment.
    comment_len = signature_len + len(b"OTA")
    path.write_bytes(data + struct.pack("<H", comment_len) + b"OTA" + der
                     + struct.pack("<HHH", signature_len, 0xffff, comment_len))


class CurrentBuildPreviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.trusted = certificate()
        cls.foreign = certificate()

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "firmware.zip"
        make_payload(temp.name)
        self.body = (Path(temp.name) / "payload.bin").read_bytes()
        self.fingerprint = "OPPO/TEST/DEVICE:17/ID/100:user/release-keys"
        self.device = adb.Device("test-serial", model="TEST", codename="DEVICE", firmware="TEST_17.100(CN01)",
                                 ota="TEST_UPDATE_100", project="22111", nvid="10010111",
                                 props={"ro.build.fingerprint": self.fingerprint, "ro.build.version.incremental": "100",
                                        "ro.build.date.utc": "123", "ro.build.version.sdk": "37",
                                        "ro.build.version.security_patch": "2026-10-01"})
        self.meta = {"product_name": "TEST", "pre-device": "DEVICE", "ota-type": "AB", "ota-id": self.device.ota,
                     "version_name": self.device.firmware, "post-build": self.fingerprint,
                     "post-build-incremental": "100", "wipe": "0", "oplus_project_numbers": "22111", "oplus_hex_nv_id": "97"}
        trust = io.BytesIO()
        with zipfile.ZipFile(trust, "w") as certificates:
            certificates.writestr("local_OTA.x509.pem", self.trusted[0].public_bytes(serialization.Encoding.PEM))
        run = patch.object(ota_signature.subprocess, "run")
        self.adb_run = run.start()
        self.adb_run.return_value.stdout = trust.getvalue()
        self.addCleanup(run.stop)
        path = patch.object(ota_signature, "adb_path", side_effect=lambda custom: custom)
        path.start()
        self.addCleanup(path.stop)
        minimum = patch.object(push_validation, "MIN_PACKAGE_SIZE", 1)
        minimum.start()
        self.addCleanup(minimum.stop)

    def package(self, *, meta=None, pb_fields=(), post_fields=(), props="", signer="trusted", legacy=False, op_extra=()):
        pb = payload.pack_fields
        text = self.meta | (meta or {})
        end = 24 + struct.unpack_from(">Q", self.body, 12)[0]
        entries = [(n, w, v) for n, w, v in payload.fields(self.body[24:end]) if n not in (12, 16)]
        if op_extra:
            for i, (n, w, part) in enumerate(entries):
                if n != 13:
                    continue
                fields = [(k, wire, value+pb(op_extra) if k == 8 else value)
                          for k, wire, value in payload.fields(part)]
                entries[i] = (n, w, pb(fields))
        manifest = pb(entries + [(12, 0, 8), (16, 0, 1)])
        post = [(1, 2, b"DEVICE"), (2, 2, self.fingerprint.encode()), (3, 2, b"100")] + list(post_fields)
        binary = pb([(1, 0, 1), (5, 2, pb([(1, 2, b"DEVICE")])), (6, 2, pb(post))] + list(pb_fields))
        with zipfile.ZipFile(self.path, "w") as archive:
            archive.writestr("META-INF/com/android/metadata", "\n".join(k+"="+v for k, v in text.items()))
            archive.writestr("META-INF/com/android/metadata.pb", binary)
            archive.writestr("payload_properties.txt", props)
            archive.writestr("payload.bin", b"CrAU"+struct.pack(">QQI", 2,
                             len(manifest), 0)+manifest+self.body[end:])
            archive.writestr("marker.txt", b"fixture data")
        if signer:
            sign_zip(self.path, *getattr(self, signer), legacy=legacy)

    def validate(self):
        return push_validation.validate_push_source(self.path, self.device, custom_adb="specific-adb")

    def test_signed_current_build_uses_selected_device_keys_and_full_file_digest(self):
        for legacy in (False, True):
            with self.subTest(legacy_sha1=legacy):
                self.package(legacy=legacy, post_fields=[(4, 0, 123), (5, 2, b"37"), (6, 2, b"2026-10-01")])
                self.assertEqual(self.validate(), hashlib.sha256(self.path.read_bytes()).hexdigest())
                args = self.adb_run.call_args.args[0]
                self.assertEqual(args, ["specific-adb", "-s", "test-serial", "exec-out",
                                 "cat", "/system/etc/security/otacerts.zip"])

    def test_partial_stays_blocked_on_other_build_model_nv_or_source_requirement(self):
        for meta in ({"version_name": "TEST_17.99"}, {"product_name": "OTHER"}, {"pre-device": "OTHER"},
                     {"post-build": self.fingerprint+"a"}, {"post-build-incremental": "99"},
                     {"ota-id": "OTHER"}, {"pre-build": self.fingerprint}, {"oplus_hex_nv_id": "96"},
                     {"oplus_project_numbers": "99999"}, {"ota-downgrade": "yes"}, {"ota-wipe": "yes"},
                     {"wipe": "1"}, {"forbid_ota_local_update": "true"}):
            with self.subTest(meta=meta):
                self.package(meta=meta)
                with self.assertRaises(ValueError):
                    self.validate()
        self.adb_run.assert_not_called()

    def test_explicit_binary_target_mismatches_and_duplicate_conditions_are_rejected(self):
        for extra in ((1, 2, b"OTHER"), (2, 2, b"other fingerprint"), (3, 2, b"101"),
                      (4, 0, 124), (5, 2, b"36"), (6, 2, b"2025-10-01")):
            with self.subTest(post_field=extra[0]):
                self.package(post_fields=[extra])
                with self.assertRaisesRegex(ValueError, "二进制 OTA 目标"):
                    self.validate()
        self.package(pb_fields=[(6, 2, b"")])
        with self.assertRaisesRegex(ValueError, "唯一"):
            self.validate()
        self.adb_run.assert_not_called()

    def test_conflicting_binary_flags_source_extents_and_wipe_properties(self):
        for arguments in ({"pb_fields": [(2, 0, 1)]}, {"pb_fields": [(3, 0, 1)]}, {"pb_fields": [(9, 0, 1)]},
                          {"op_extra": [(4, 2, b"")]}, {"props": "POWERWASH=1"}):
            with self.subTest(arguments=arguments):
                self.package(**arguments)
                with self.assertRaises(ValueError):
                    self.validate()
        self.adb_run.assert_not_called()

    def test_unsigned_foreign_and_modified_signed_archives_are_rejected(self):
        for signer in (None, "foreign", "trusted"):
            with self.subTest(signer=signer):
                self.package(signer=signer)
                if signer == "trusted":
                    self.path.write_bytes(self.path.read_bytes().replace(b"fixture data", b"tamper! data"))
                with self.assertRaisesRegex(ValueError, "签名"):
                    self.validate()

    def test_missing_device_trust_store_fails_closed(self):
        self.package()
        self.adb_run.return_value.stdout = b"unavailable"
        with self.assertRaisesRegex(ValueError, "OTA 信任证书"):
            self.validate()

    def test_signed_partial_is_never_a_catalog_downgrade_or_a_model_only_match(self):
        self.package()
        _, errors = push_validation.inspect_package_safety(self.path, self.device.model, codename=self.device.codename)
        self.assertTrue(errors)
        record = Package(model="TEST", region="cn", channel="降级包", kind="全量包", version=self.device.firmware,
                         ota=self.device.ota, component="System", url="", recipe={"downgrade": True})
        with self.assertRaisesRegex(ValueError, "不能作为全量降级包"):
            push_validation.validate_push_source(self.path, self.device, record, custom_adb="specific-adb")
        self.adb_run.assert_not_called()
