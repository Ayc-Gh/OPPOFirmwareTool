"""Push downgrade packages to OPlus devices and trigger official local OTA installation.

Based on reverse engineering of ColorOS Assistant & OTA Assistant APK:
1. Ensure com.color.otaassistant is installed.
2. Push downgrade package (.ozip / .zip) to /sdcard/.
3. Broadcast to wake up OtaAssistantReceiver.
4. Insert local_file_uri and forbid_check_update into DataSharedProvider.
5. Launch com.oplus.ota/com.oplus.otaui.activity.EntryActivity with InstallPackage extra.
"""
from __future__ import annotations
import os
import re
import subprocess
import time
from pathlib import Path
from .common import RESOURCE, HOME, CREATE_NO_WINDOW, check_cancel, Cancelled
from .adb import adb_path, command

PACKAGE_ASSISTANT = "com.color.otaassistant"
RECEIVER_ACTION = "oplus.intent.action.OTA_ASSISTANT_PC_TOOL_RECEIVER"
RECEIVER_COMPONENT = "com.color.otaassistant/.OtaAssistantReceiver"
PROVIDER_URI = "content://com.color.otaassistant.DataSharedProvider/data"

ENTRY_ACTIVITY_NEW = "com.oplus.ota/com.oplus.otaui.activity.EntryActivity"
ENTRY_ACTIVITY_LEGACY = "com.oppo.ota/com.oppo.otaui.activity.EntryActivity"

def inspect_package_safety(local_package: Path, target_device_model="") -> tuple[list[str], list[str]]:
    """Inspect package safety before push. Returns (warnings, errors)."""
    warnings, errors = [], []
    local_package = Path(local_package)
    if not local_package.is_file():
        errors.append(f"固件文件不存在：{local_package}")
        return warnings, errors

    size = local_package.stat().st_size
    if size < 50 * 1024 * 1024:
        errors.append(f"固件文件过小（{size / 1024**2:.1f} MiB），官方完整系统包通常在数 GiB 以上，疑似损坏或非完整系统包！")
        return warnings, errors

    is_zip = False
    try:
        with open(local_package, 'rb') as f:
            header = f.read(16)
        is_zip = header.startswith(b'PK\x03\x04')
        is_ozip = b'OPPO' in header[:12] or header.startswith(b'OZIP')
        if not is_zip and not is_ozip:
            warnings.append("文件头既非标准 ZIP 也非 OZIP 格式，可能不是官方系统固件包！")
    except Exception as e:
        warnings.append(f"文件读取异常: {e}")

    m = re.match(r'^([A-Za-z0-9]+)_', local_package.name)
    pkg_model = m.group(1).upper() if m else ''
    if pkg_model and target_device_model:
        if pkg_model != target_device_model.upper():
            warnings.append(
                f"【机型不匹配警告】固件文件名前缀为【{pkg_model}】，但当前连接手机为【{target_device_model}】！\n"
                "跨机型刷机将导致手机官方系统更新报错或校验拒绝。"
            )

    if is_zip:
        try:
            from .payload import inspect
            info = inspect(str(local_package))
            if info and info.is_delta:
                errors.append(
                    "【拦截警告】检测到此固件为【增量补丁包 (Delta OTA)】！\n"
                    "官方降级必须使用【全量包 (Full OTA)】，增量包缺乏完整分区无法用于降级刷机！"
                )
        except Exception:
            pass

    return warnings, errors

def get_assistant_apk(is_legacy=False, custom_dir="") -> Path:
    """Find the OTA assistant APK matching device Android/ColorOS version."""
    apk_name = "OTA_113.apk" if is_legacy else "OTA_17.apk"
    candidates = []
    if custom_dir:
        candidates.append(Path(custom_dir) / apk_name)
    candidates.extend([
        RESOURCE / "tools/apk" / apk_name,
        HOME / "tools/apk" / apk_name,
        HOME.parent / "tools/apk" / apk_name,
        Path(r"D:\手机工具\coloros助手\coloros-assist\apk") / apk_name,
    ])
    for p in candidates:
        if p.is_file():
            return p
    raise FileNotFoundError(
        f"未找到伴生应用 {apk_name}。请确保 tools/apk 目录下包含该 APK。"
    )

def check_assistant_installed(serial: str, custom_adb="") -> bool:
    """Check if com.color.otaassistant is already installed on device."""
    try:
        out = command(["-s", serial, "shell", "pm", "list", "packages", PACKAGE_ASSISTANT], custom_adb, timeout=10)
        return PACKAGE_ASSISTANT in out
    except Exception:
        return False

def install_assistant(serial: str, apk_path: Path, custom_adb="", log=None) -> None:
    """Install the OTA assistant APK via ADB."""
    if log:
        log(f"正在向设备安装官方降级伴生应用：{apk_path.name}…")
    r = subprocess.run(
        [adb_path(custom_adb), "-s", serial, "install", "-r", "-d", "-g", str(apk_path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        creationflags=CREATE_NO_WINDOW,
    )
    if "Success" not in r.stdout:
        err = r.stderr.strip() or r.stdout.strip()
        raise RuntimeError(f"伴生应用安装失败: {err}")
    if log:
        log("伴生应用安装成功。")

def get_device_free_space(serial: str, custom_adb="", path="/sdcard") -> int:
    """Get free space on device storage in bytes."""
    try:
        out = command(["-s", serial, "shell", "df", "-k", path], custom_adb, timeout=10)
        lines = out.strip().splitlines()
        if len(lines) >= 2:
            parts = lines[-1].split()
            if len(parts) >= 4:
                return int(parts[3]) * 1024
    except Exception:
        pass
    return -1

def push_package_file(serial: str, local_path: Path, remote_path: str, custom_adb="", cancel=None, progress=None, log=None):
    """Push package file to device with progress monitoring and cancel support."""
    local_size = local_path.stat().st_size
    if log:
        log(f"开始推送固件包至手机存储：{remote_path} ({local_size / 1024**3:.2f} GiB)…")

    cmd = [adb_path(custom_adb), "-s", serial, "push", str(local_path), remote_path]
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=CREATE_NO_WINDOW,
    )

    start_time = time.monotonic()
    last_reported = 0
    try:
        while proc.poll() is None:
            if cancel is not None and cancel.is_set():
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except Exception:
                    proc.kill()
                try:
                    command(["-s", serial, "shell", "rm", "-f", remote_path], custom_adb, timeout=10)
                except Exception:
                    pass
                raise Cancelled("降级包推送已被用户取消。")

            now = time.monotonic()
            if now - last_reported >= 1.0:
                last_reported = now
                try:
                    out = command(["-s", serial, "shell", "stat", "-c", "%s", remote_path], custom_adb, timeout=3).strip()
                    if out.isdigit():
                        current = int(out)
                        if progress:
                            speed = current / (now - start_time) if (now - start_time) > 0 else 0
                            progress(current, local_size, f"正在推送固件至手机… {current / 1024**2:.1f} / {local_size / 1024**2:.1f} MiB ({speed / 1024**2:.1f} MiB/s)")
                except Exception:
                    pass
            time.sleep(0.5)

        stdout, stderr = proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f"ADB 推送失败: {stderr.strip() or stdout.strip()}")

        if progress:
            progress(local_size, local_size, "固件推送完成，正在校验手机端文件…")
    except Cancelled:
        raise
    except Exception as e:
        if cancel and cancel.is_set():
            raise Cancelled("已取消推送")
        raise

    try:
        remote_size_str = command(["-s", serial, "shell", "stat", "-c", "%s", remote_path], custom_adb, timeout=10).strip()
        if remote_size_str.isdigit():
            remote_size = int(remote_size_str)
            if remote_size != local_size:
                raise RuntimeError(f"手机端文件大小 ({remote_size}) 与本地 ({local_size}) 不一致，推送不完整。")
    except Exception as e:
        if "不一致" in str(e):
            raise
        if log:
            log(f"已推送完成（校验跳过: {e}）")

def trigger_downgrade(serial: str, remote_path: str, custom_adb="", is_legacy=False, log=None):
    """Wake assistant, write Provider data, and start system OTA EntryActivity."""
    if log:
        log("正在唤醒官方降级伴生应用服务…")

    cmd_broadcast = ["-s", serial, "shell", "am", "broadcast", "-n", RECEIVER_COMPONENT, "-a", RECEIVER_ACTION, "-f", "16777248"]
    command(cmd_broadcast, custom_adb, timeout=10)
    time.sleep(0.5)

    if log:
        log(f"向本地数据通道配置固件参数：{remote_path}…")
    cmd_provider_uri = [
        "-s", serial, "shell", "content", "insert",
        "--uri", PROVIDER_URI,
        "--bind", "name:s:local_file_uri",
        "--bind", f"value:s:{remote_path}"
    ]
    command(cmd_provider_uri, custom_adb, timeout=10)

    cmd_provider_forbid = [
        "-s", serial, "shell", "content", "insert",
        "--uri", PROVIDER_URI,
        "--bind", "name:s:forbid_check_update",
        "--bind", "value:s:true"
    ]
    command(cmd_provider_forbid, custom_adb, timeout=10)
    time.sleep(0.5)

    entry = ENTRY_ACTIVITY_LEGACY if is_legacy else ENTRY_ACTIVITY_NEW
    if log:
        log(f"唤起官方系统更新组件：{entry}…")
    cmd_start = ["-s", serial, "shell", "am", "start", "-n", entry, "-e", "InstallPackage", remote_path]
    out = command(cmd_start, custom_adb, timeout=15)
    if "Error" in out or "Exception" in out:
        if log:
            log(f"主入口唤起异常，尝试备用入口… ({out.strip()})")
        alt_entry = ENTRY_ACTIVITY_NEW if is_legacy else ENTRY_ACTIVITY_LEGACY
        command(["-s", serial, "shell", "am", "start", "-n", alt_entry, "-e", "InstallPackage", remote_path], custom_adb, timeout=15)

    if log:
        log("官方系统更新程序已成功唤起！请在手机屏幕上确认并继续完成降级安装。")

def push_and_install(serial: str, local_package: Path, custom_adb="", cancel=None, progress=None, log=None):
    """Full workflow: preflight -> install assistant -> check storage -> push -> trigger."""
    if cancel is not None: check_cancel(cancel)
    local_package = Path(local_package)
    if not local_package.is_file():
        raise FileNotFoundError(f"降级固件文件不存在：{local_package}")

    try:
        props = command(["-s", serial, "shell", "getprop", "ro.build.version.release"], custom_adb, timeout=10).strip()
        android_ver = int(props) if props.isdigit() else 12
    except Exception:
        android_ver = 12
    is_legacy = android_ver < 11

    if log:
        log(f"目标设备: {serial} (Android {android_ver})，适配模式: {'ColorOS 11.3 早期版本' if is_legacy else 'ColorOS 11.3+ 现代版'}")

    if cancel is not None: check_cancel(cancel)
    if not check_assistant_installed(serial, custom_adb):
        apk_path = get_assistant_apk(is_legacy=is_legacy)
        install_assistant(serial, apk_path, custom_adb, log)
    else:
        if log:
            log("设备已安装降级伴生应用，跳过安装。")

    if cancel is not None: check_cancel(cancel)
    req_size = local_package.stat().st_size
    avail = get_device_free_space(serial, custom_adb)
    if avail > 0:
        if log:
            log(f"手机存储可用空间: {avail / 1024**3:.2f} GiB，固件包所需空间: {req_size / 1024**3:.2f} GiB")
        if avail < req_size + 300 * 1024 * 1024:
            raise RuntimeError(f"手机存储空间不足！剩余空间 {avail / 1024**3:.2f} GiB，需要至少 {(req_size + 300*1024*1024) / 1024**3:.2f} GiB。请清理手机存储后重试。")

    suffix = local_package.suffix if local_package.suffix.lower() in ('.ozip', '.zip') else '.ozip'
    target_remote = f"/sdcard/{local_package.stem}{suffix}"
    push_package_file(serial, local_package, target_remote, custom_adb, cancel, progress, log)

    if cancel is not None: check_cancel(cancel)
    if progress:
        progress(req_size, req_size, "正在唤起手机系统更新程序…")
    trigger_downgrade(serial, target_remote, custom_adb, is_legacy, log)
