"""Validate the package and device before any installation or device write."""
from __future__ import annotations

import hashlib
import re
import zipfile
from pathlib import Path

from . import models, payload
from .common import Cancelled, check_cancel

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


def inspect_package_safety(local_package: Path, target_device_model="", *,
                           codename="", record=None, cancel=None):
    """Return structural errors. A catalog match also requires checking the file digest."""
    path = Path(local_package)
    model = target_device_model.upper()
    if not path.is_file():
        return [], [f"固件文件不存在：{path}"]
    if path.stat().st_size < MIN_PACKAGE_SIZE:
        return [], ["固件文件过小，不能作为完整系统包推送。"]
    if not model:
        return [], ["无法核实当前连接设备的型号，请刷新设备后再试。"]
    if record is not None:
        if record.model.upper() != model:
            return [], [f"目录记录与设备机型不匹配：{record.model} / {model}。"]
        if record.kind in ("补丁包", "增量包"):
            return [], [f"{record.kind}不能通过整机降级入口推送。"]
        if record.size and path.stat().st_size != record.size:
            return [], ["文件大小与所选固件记录不一致。"]

    # Only interpret a known model code as such. A user may rename the package.
    profiles = models.load_profiles()
    prefix, sep, _ = path.stem.upper().partition("_")
    if sep and prefix != model and any(p.model.upper() == prefix for p in profiles):
        return [], [f"文件名与设备机型不匹配：{prefix} / {model}。"]

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
            return ([], []) if matched_record else ([], [need_record])
        if not header.startswith(b"PK\x03\x04"):
            return [], ["文件头不是可识别的 ZIP/OZIP 固件。"]
        with zipfile.ZipFile(path) as archive:
            has_payload = any(i.filename.replace("\\", "/").split("/")[-1] == "payload.bin"
                              for i in archive.infolist())
        if not has_payload:
            return ([], []) if matched_record else ([], [need_record])
        info = payload.inspect(str(path), cancel)
        meta = info.metadata
        # update_metadata.proto: minor_version (12) is zero for full payloads,
        # partial_update (16) marks a subset even when all its ops are REPLACE.
        has_baseline = any(p.has_old_info for p in info.partitions)
        if (info.is_delta or info.minor_version or info.partial_update or has_baseline
                or meta.get("pre-build") or meta.get("pre-build-incremental")
                or meta.get("ota-type", "").upper() == "BRICK"):
            return [], ["增量包、部分分区更新及有源版本限制的包不能作为全量降级包。"]
        error = _check_device_metadata(meta, model, codename.upper(), profiles, matched_record)
        if error:
            return [], [error]
    except Cancelled:
        raise
    except Exception as exc:
        return [], [f"无法校验 ZIP 或 payload 清单：{exc}"]
    return [], []


def validate_push_source(path, device, record=None, cancel=None, progress=None):
    """Return the SHA256 of the checked source for comparison after transfer."""
    path = Path(path)
    initial = path.stat()
    if record and record.region and device.region and record.region != device.region:
        raise ValueError("固件记录与当前设备的固件地区不匹配。")
    if record and record.nvid and device.nvid and record.nvid != device.nvid:
        raise ValueError("固件记录与当前设备的运营商 NV 不匹配。")
    _, errors = inspect_package_safety(path, device.model, codename=device.codename,
                                       record=record, cancel=cancel)
    if errors:
        raise ValueError("\n".join(errors))
    sha = hashlib.sha256()
    md5 = hashlib.md5() if record and record.md5 else None
    done = 0
    with path.open("rb") as source:
        while chunk := source.read(BLOCK_SIZE):
            if cancel is not None:
                check_cancel(cancel)
            sha.update(chunk)
            if md5:
                md5.update(chunk)
            done += len(chunk)
            if progress:
                progress(done, initial.st_size, "正在核对本地固件摘要…")
    digest = sha.hexdigest()
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
