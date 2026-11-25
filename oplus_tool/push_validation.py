"""Validate the package and device before any installation or device write."""
from __future__ import annotations

import hashlib
import re
import zipfile
from pathlib import Path

from . import models, payload
from .common import Cancelled, check_cancel
from .ota_signature import verify_device_ota_signature

MIN_PACKAGE_SIZE = 50 * 1024 * 1024
BLOCK_SIZE = 4 * 1024 * 1024


def _check_device_metadata(meta, model, codename, profiles, matched_record):
    accepted = {model, codename} - {""}
    devices = set(re.findall(r"[^|,;\s]+", meta.get("pre-device", "").upper()))
    if devices and devices.isdisjoint(accepted):
        return "OTA 元数据与设备机型不匹配。"
    model_codes = {profile.model.upper() for profile in profiles}
    products = []
    for item in meta.get("post-build", "").upper().split("|"):
        if not item:
            continue
        fields = item.split(":", 1)[0].split("/")
        if len(fields) != 3:
            return "OTA 构建指纹无法识别，请核对固件。"
        _, product, device = fields
        if product in model_codes and product != model:
            continue
        if device not in accepted and (codename or product != model):
            continue
        products.append(product)
    if meta.get("post-build") and not products:
        return "OTA 构建指纹与设备机型不匹配。"
    shared = any(profile.model.upper() != model and
                 codename in (code.upper() for code in profile.codenames) for profile in profiles)
    if (devices or products) and (model in devices or model in products or not shared):
        return ""
    if matched_record:
        return ""
    return "无法从此包唯一核实适用机型，请选择本机型带校验值的官方降级记录。"


def _check_current_build_preview(path, info, device, custom_adb, cancel, progress):
    """A source-independent partial package may be previewed on its exact signed target build."""
    meta = info.metadata
    expected = {
        "ota-type": "AB", "product_name": device.model, "pre-device": device.codename,
        "post-build": device.props.get("ro.build.fingerprint"),
        "post-build-incremental": device.props.get("ro.build.version.incremental"),
        "ota-id": device.ota, "version_name": device.firmware,
    }
    if any(not value or meta.get(key) != value for key, value in expected.items()):
        raise ValueError("部分分区包不能作为全量降级包，预览须与手机当前机型、指纹和构建版本完全一致。")
    for field, prop in (("post-security-patch-level", "ro.build.version.security_patch"),
                        ("post-timestamp", "ro.build.date.utc"), ("post-sdk-level", "ro.build.version.sdk")):
        if field in meta and meta[field] != device.props.get(prop):
            raise ValueError(f"部分分区包的 {field} 与当前设备不匹配。")
    if "oplus_project_numbers" in meta and device.project not in meta["oplus_project_numbers"].split(","):
        raise ValueError("部分分区包的项目编号与当前设备不匹配。")
    if "oplus_hex_nv_id" in meta:
        nv = meta["oplus_hex_nv_id"].lower()
        if (not re.fullmatch(r"[01]{8}", device.nvid) or not re.fullmatch(r"(?:0x)?[0-9a-f]{1,2}", nv)
                or int(device.nvid, 2) != int(nv, 16)):
            raise ValueError("部分分区包的运营商 NV 与当前设备不匹配。")

    if not info.partitions or any(op["type"] not in (0, 1, 6, 7, 8, 14)
            or any(n in (4, 5, 9) for n, _, _ in payload.fields(op["raw"]))
            for partition in info.partitions for op in partition.operations):
        raise ValueError("部分分区包包含未知操作或操作引用了源分区。")
    with zipfile.ZipFile(path) as archive:
        def small_entry(name):
            entries = [e for e in archive.infolist() if e.filename == name]
            if len(entries) != 1 or entries[0].file_size > 2 * 1024 * 1024:
                raise ValueError(f"同版本预览需要可核验且唯一的 {name}")
            return archive.read(entries[0])

        props = dict(line.split("=", 1) for line in small_entry("payload_properties.txt").decode().splitlines()
                     if "=" in line)
        binary = payload.fields(small_entry("META-INF/com/android/metadata.pb"))
    for source, flags in ((meta, ("wipe", "ota-wipe", "ota-downgrade", "spl-downgrade", "ota-retrofit-dynamic-partitions",
                                 "forbid_ota_local_update")),
                          (props, ("POWERWASH", "forbid_ota_local_update", "oplus_update_engine_verify_disable"))):
        if any(flag in source and source[flag].lower() not in ("0", "no", "false") for flag in flags):
            raise ValueError("部分分区包声明清除数据、降级或限制本地校验。")
    # AOSP OtaMetadata: local preview must agree with both the text and the
    # protobuf consumed by Android, including its pre/postconditions.
    if [v for n, _, v in binary if n == 1] != [1] or any(v for n, _, v in binary if n in (2, 3, 7, 9)):
        raise ValueError("二进制 OTA 元数据声明特殊更新或清除数据。")
    conditions = {n: [value for field, _, value in binary if field == n] for n in (5, 6)}
    if any(len(values) != 1 for values in conditions.values()):
        raise ValueError("二进制 OTA 元数据缺少唯一的源或目标构建条件。")
    pre = payload.fields(conditions[5][0])
    post = payload.fields(conditions[6][0])
    if pre != [(1, 2, device.codename.encode())] or props.get("ota_target_version", device.ota) != device.ota:
        raise ValueError("二进制 OTA 元数据或 payload 属性与当前设备构建不匹配。")
    target = {1: device.codename.encode(), 2: expected["post-build"].encode(),
              3: expected["post-build-incremental"].encode(), 4: device.props.get("ro.build.date.utc"),
              5: device.props.get("ro.build.version.sdk", "").encode(),
              6: device.props.get("ro.build.version.security_patch", "").encode()}
    for field, expected_value in target.items():
        values = [v for n, _, v in post if n == field]
        if field == 4:
            # DeviceState.timestamp is a varint, while getprop returns text.
            values = [str(v) for v in values]
        if (field in (2, 3) or values) and (not expected_value or values != [expected_value]):
            raise ValueError("二进制 OTA 目标机型、指纹、版本或安全补丁与当前设备不匹配。")
    return verify_device_ota_signature(path, device.serial, custom_adb, cancel, progress)


def inspect_package_safety(local_package: Path, target_device_model="", *,
                           codename="", record=None, cancel=None, current_device=None, custom_adb="", progress=None):
    """Return structural errors. A catalog match also requires checking the file digest."""
    _, errors = _inspect(local_package, target_device_model, codename, record, cancel, current_device, custom_adb, progress)
    return [], errors


def _inspect(local_package, target_device_model, codename, record, cancel, current_device, custom_adb, progress):
    """Return the SHA-256 of a verified signed preview, or structural errors."""
    path = Path(local_package)
    model = target_device_model.upper()
    verified_digest = None
    if not path.is_file():
        return None, [f"固件文件不存在：{path}"]
    if path.stat().st_size < MIN_PACKAGE_SIZE:
        return None, ["固件文件过小，不能作为完整系统包推送。"]
    if not model:
        return None, ["无法核实当前连接设备的型号，请刷新设备后再试。"]
    if record is not None:
        if record.model.upper() != model:
            return None, [f"目录记录与设备机型不匹配：{record.model} / {model}。"]
        if record.kind in ("补丁包", "增量包"):
            return None, [f"{record.kind}不能通过整机降级入口推送。"]
        if record.size and path.stat().st_size != record.size:
            return None, ["文件大小与所选固件记录不一致。"]

    # Only interpret a known model code as such. A user may rename the package.
    profiles = models.load_profiles()
    prefix, sep, _ = path.stem.upper().partition("_")
    if sep and prefix != model and any(p.model.upper() == prefix for p in profiles):
        return None, [f"文件名与设备机型不匹配：{prefix} / {model}。"]

    # Opaque/legacy packages can only be identified by an official downgrade
    # record for this device and a digest. validate_push_source compares the bytes.
    matched_record = (record is not None and record.channel == "降级包"
                      and record.kind == "全量包" and record.recipe.get("downgrade") is True
                      and bool(record.md5 or record.sha256))
    need_record = "无法从此包核实适用机型和包类型。请从本机型带校验值的官方降级记录中选择该文件。"
    try:
        with path.open("rb") as source:
            header = source.read(16)
        if header.startswith((b"OPPOENCRYPT!", b"OZIP")):
            return None, [] if matched_record else [need_record]
        if not header.startswith(b"PK\x03\x04"):
            return None, ["文件头不是可识别的 ZIP/OZIP 固件。"]
        with zipfile.ZipFile(path) as archive:
            has_payload = any(i.filename.replace("\\", "/").split("/")[-1] == "payload.bin"
                              for i in archive.infolist())
        if not has_payload:
            return None, [] if matched_record else [need_record]
        info = payload.inspect(str(path), cancel)
        meta = info.metadata
        has_baseline = any(p.has_old_info for p in info.partitions)
        if (info.is_delta or has_baseline or meta.get("pre-build") or meta.get("pre-build-incremental")
                or meta.get("ota-type", "").upper() == "BRICK"):
            return None, ["引用源分区、限定源版本或要求销毁数据的包不能作为全量降级包。"]
        if info.partial_update or info.minor_version:
            if (not info.partial_update or current_device is None or
                    (record is not None and (record.channel == "降级包" or record.recipe.get("downgrade")))):
                return None, ["部分分区包或非零 minor_version 的包不能作为全量降级包。"]
            verified_digest = _check_current_build_preview(path, info, current_device, custom_adb, cancel, progress)
        error = _check_device_metadata(meta, model, codename.upper(), profiles, matched_record)
        if error:
            return None, [error]
    except Cancelled:
        raise
    except Exception as exc:
        return None, [f"无法校验 ZIP 或 payload 清单：{exc}"]
    return verified_digest, []


def validate_push_source(path, device, record=None, cancel=None, progress=None, *, custom_adb=""):
    """Return the SHA256 of the checked source for comparison after transfer."""
    path = Path(path)
    initial = path.stat()
    if record and record.region and device.region and record.region != device.region:
        raise ValueError("固件记录与当前设备的固件地区不匹配。")
    if record and record.nvid and device.nvid and record.nvid != device.nvid:
        raise ValueError("固件记录与当前设备的运营商 NV 不匹配。")
    signed_digest, errors = _inspect(path, device.model, device.codename, record, cancel, device, custom_adb, progress)
    if errors:
        raise ValueError("\n".join(errors))
    sha = None if signed_digest else hashlib.sha256()
    md5 = hashlib.md5() if record and record.md5 else None
    done = 0
    if sha or md5:
        with path.open("rb") as source:
            while chunk := source.read(BLOCK_SIZE):
                if cancel is not None:
                    check_cancel(cancel)
                if sha:
                    sha.update(chunk)
                if md5:
                    md5.update(chunk)
                done += len(chunk)
                if progress:
                    progress(done, initial.st_size, "正在核对本地固件摘要…")
    digest = signed_digest or sha.hexdigest()
    if record and record.sha256 and digest != record.sha256.lower():
        raise ValueError("文件 SHA256 与所选固件记录不一致。")
    if md5 and md5.hexdigest() != record.md5.lower():
        raise ValueError("文件 MD5 与所选固件记录不一致。")
    # Without a published digest, read the entries to the end so that ZIP CRC
    # errors in partition data are detected even when the manifest is readable.
    if not (record and (record.md5 or record.sha256)):
        if progress:
            progress(0, 0, "正在核验固件 ZIP 的 CRC…")
        try:
            with zipfile.ZipFile(path) as archive:
                for entry in archive.infolist():
                    with archive.open(entry) as data:
                        while data.read(BLOCK_SIZE):
                            if cancel is not None:
                                check_cancel(cancel)
        except zipfile.BadZipFile as exc:
            raise ValueError(f"ZIP 完整性检查失败：{exc}") from exc
    final = path.stat()
    if (initial.st_dev, initial.st_ino, initial.st_size, initial.st_mtime_ns) != (
            final.st_dev, final.st_ino, final.st_size, final.st_mtime_ns):
        raise ValueError("校验期间本地文件已变化，请重新选择固件。")
    return digest
