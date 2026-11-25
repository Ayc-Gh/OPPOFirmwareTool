"""Verify an Android SignApk whole-file signature with the selected device's OTA keys."""
from __future__ import annotations

import hashlib
import io
import struct
import subprocess
import zipfile

from asn1crypto import cms
from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa, utils

from .adb import adb_path
from .common import CREATE_NO_WINDOW, check_cancel


def _device_keys(serial, custom_adb):
    result = subprocess.run(
        [adb_path(custom_adb), "-s", serial, "exec-out", "cat", "/system/etc/security/otacerts.zip"],
        capture_output=True, timeout=15, creationflags=CREATE_NO_WINDOW, check=True,
    )
    with zipfile.ZipFile(io.BytesIO(result.stdout)) as trusted:
        for entry in trusted.infolist():
            if entry.is_dir() or entry.file_size > 65536:
                continue
            data = trusted.read(entry)
            try:
                cert = (x509.load_pem_x509_certificate(data) if b"-----BEGIN CERTIFICATE-----" in data
                        else x509.load_der_x509_certificate(data))
            except ValueError:
                continue
            key = cert.public_key()
            if isinstance(key, rsa.RSAPublicKey) and key.key_size >= 2048:
                yield key


def _signature(file):
    # The last six ZIP comment bytes contain the offset to the DER signature,
    # 0xffff, and the comment length. Android signs through EOCD byte 19.
    size = file.seek(0, 2)
    if size < 28:
        raise ValueError("文件缺少 Android 整包签名")
    file.seek(-6, 2)
    offset, marker, comment_size = struct.unpack("<HHH", file.read(6))
    if marker != 0xffff or not 6 < offset <= comment_size <= size - 22:
        raise ValueError("Android 整包签名尾部无效")
    file.seek(-comment_size - 22, 2)
    eocd = file.read(22 + comment_size)
    if (not eocd.startswith(b"PK\x05\x06") or b"PK\x05\x06" in eocd[4:]
            or int.from_bytes(eocd[20:22], "little") != comment_size):
        raise ValueError("签名中的 ZIP 结束记录无效")
    envelope = cms.ContentInfo.load(eocd[-offset:-6], strict=True)
    if envelope["content_type"].native != "signed_data":
        raise ValueError("签名类型无效")
    signed = envelope["content"]
    content = signed["encap_content_info"]
    if (content["content_type"].native != "data" or content["content"].native is not None
            or len(signed["signer_infos"]) != 1):
        raise ValueError("需要单一分离式 Android 整包签名")
    signer = signed["signer_infos"][0]
    # SignApk's direct RSA signature. Some OEMs still use SHA-1 here; the
    # transfer digest is independently SHA-256 over the entire file.
    digest_name = signer["digest_algorithm"]["algorithm"].native
    digest_types = {"sha1": hashes.SHA1, "sha256": hashes.SHA256}
    if (signer["signed_attrs"].native is not None or digest_name not in digest_types
            or signer["signature_algorithm"]["algorithm"].native not in ("rsassa_pkcs1v15", digest_name + "_rsa")):
        raise ValueError("不支持此 Android 整包签名算法")
    return size - comment_size - 2, digest_types[digest_name](), signer["signature"].native


def verify_device_ota_signature(path, serial, custom_adb, cancel=None, progress=None):
    with path.open("rb") as file:
        signed_size, algorithm, signature = _signature(file)
        try:
            keys = list(_device_keys(serial, custom_adb))
        except (OSError, subprocess.SubprocessError, zipfile.BadZipFile) as exc:
            raise ValueError("无法读取当前设备的 OTA 信任证书") from exc
        if not keys:
            raise ValueError("未读取到本机受信任的 OTA 签名公钥")
        file.seek(0)
        digest = hashes.Hash(algorithm)
        full_file = hashlib.sha256()
        remaining = signed_size
        while remaining:
            if cancel is not None:
                check_cancel(cancel)
            block = file.read(min(4 * 1024 * 1024, remaining))
            if not block:
                raise ValueError("整包签名覆盖的数据被截断")
            digest.update(block)
            full_file.update(block)
            remaining -= len(block)
            if progress:
                progress(signed_size - remaining, signed_size, "正在用手机信任的 OTA 公钥验证同版本包…")
        # Bind the signed bytes to exactly the SHA-256 later checked on the phone.
        full_file.update(file.read(65537))
        if file.read(1):
            raise ValueError("读取签名时文件大小发生变化")
    signed_digest = digest.finalize()
    for key in keys:
        try:
            key.verify(signature, signed_digest, padding.PKCS1v15(), utils.Prehashed(algorithm))
            return full_file.hexdigest()
        except InvalidSignature:
            continue
    raise ValueError("整包签名与本机受信任的 OTA 公钥不匹配")
