"""Push downgrade packages to OPlus devices and trigger official local OTA installation.

Based on reverse engineering of ColorOS Assistant & OTA Assistant APK:
1. Ensure com.color.otaassistant is installed.
2. Push downgrade package (.ozip / .zip) to /sdcard/.
3. Broadcast to wake up OtaAssistantReceiver.
4. Insert local_file_uri and forbid_check_update into DataSharedProvider.
5. Launch com.oplus.ota/com.oplus.otaui.activity.EntryActivity with InstallPackage extra.
"""
from __future__ import annotations
import re
import subprocess
import time
from pathlib import Path
from uuid import uuid4
from .common import RESOURCE, HOME, CREATE_NO_WINDOW, check_cancel, Cancelled
from .adb import adb_path, command, device_from_props, parse_props
from .push_validation import inspect_package_safety, validate_push_source

PACKAGE_ASSISTANT = "com.color.otaassistant"
RECEIVER_ACTION = "oplus.intent.action.OTA_ASSISTANT_PC_TOOL_RECEIVER"
RECEIVER_COMPONENT = "com.color.otaassistant/.OtaAssistantReceiver"
PROVIDER_URI = "content://com.color.otaassistant.DataSharedProvider/data"

ENTRY_ACTIVITY_NEW = "com.oplus.ota/com.oplus.otaui.activity.EntryActivity"
ENTRY_ACTIVITY_LEGACY = "com.oppo.ota/com.oppo.otaui.activity.EntryActivity"

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

def _run_transfer_command(cmd, cancel, on_poll=None):
    """Drain adb output while polling for cancellation; always reap the writer."""
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=CREATE_NO_WINDOW,
    )

    try:
        while True:
            if cancel is not None and cancel.is_set():
                raise Cancelled("降级包推送已取消。")
            try:
                stdout, stderr = proc.communicate(timeout=0.5)
                break
            except subprocess.TimeoutExpired:
                if on_poll:
                    on_poll()
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()

    if proc.returncode:
        raise RuntimeError(f"ADB {cmd[3]} 失败：{stderr.strip() or stdout.strip()}")
    return stdout


def _push_package_file(serial: str, local_path: Path, remote_path: str, custom_adb="", cancel=None, progress=None, log=None, *, expected_sha256):
    """Transfer to an exclusively reserved path and verify the phone's copy."""
    local_size = local_path.stat().st_size
    if log:
        log(f"开始推送固件包至手机存储：{remote_path} ({local_size / 1024**3:.2f} GiB)…")
    start_time = time.monotonic()
    last_reported = start_time
    adb = [adb_path(custom_adb), "-s", serial]

    def report_progress():
        nonlocal last_reported
        now = time.monotonic()
        if now - last_reported < 1:
            return
        last_reported = now
        try:
            size = command(["-s", serial, "shell", "stat", "-c", "%s", remote_path], custom_adb, timeout=3).strip()
        except (RuntimeError, OSError, subprocess.TimeoutExpired):
            return  # The file may not be present yet.
        if size.isdigit():
            current = int(size)
            speed = current / (now - start_time) / 1024**2
            progress(current, local_size, f"正在推送固件至手机… {current / 1024**2:.1f} / {local_size / 1024**2:.1f} MiB ({speed:.1f} MiB/s)")

    _run_transfer_command([*adb, "push", str(local_path), remote_path], cancel, report_progress if progress else None)
    if progress:
        progress(0, 0, "固件推送完成，正在核验手机端文件大小和 SHA256…")
    remote_size = command(["-s", serial, "shell", "stat", "-c", "%s", remote_path], custom_adb, timeout=10).strip()
    if not remote_size.isdigit() or int(remote_size) != local_size:
        raise RuntimeError(f"无法确认手机端文件大小与本地一致（手机：{remote_size!r}，本地：{local_size}）。")
    output = _run_transfer_command([*adb, "shell", "sha256sum", remote_path], cancel)
    fields = output.strip().split()
    if len(fields) != 2 or fields[1] != remote_path or not re.fullmatch("[0-9A-Fa-f]{64}", fields[0]):
        raise RuntimeError("手机未返回可核验的固件 SHA256。")
    if fields[0].lower() != expected_sha256:
        raise RuntimeError("手机端固件 SHA256 与本地文件不一致。")

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
    if "error" in out.lower() or "exception" in out.lower():
        if log:
            log(f"主入口唤起异常，尝试备用入口… ({out.strip()})")
        alt_entry = ENTRY_ACTIVITY_NEW if is_legacy else ENTRY_ACTIVITY_LEGACY
        alt_out = command(["-s", serial, "shell", "am", "start", "-n", alt_entry, "-e", "InstallPackage", remote_path], custom_adb, timeout=15)
        if "error" in alt_out.lower() or "exception" in alt_out.lower():
            raise RuntimeError(f"手机报告两个系统更新入口均未能唤起：{alt_out.strip()}")

    if log:
        log("官方系统更新程序已成功唤起！请在手机屏幕上确认并继续完成降级安装。")

def push_and_install(serial: str, local_package: Path, custom_adb="", cancel=None, progress=None, log=None,
                     *, expected_model="", record=None):
    """Full workflow: preflight -> install assistant -> check storage -> push -> trigger."""
    if cancel is not None: check_cancel(cancel)
    local_package = Path(local_package)
    if not local_package.is_file():
        raise FileNotFoundError(f"降级固件文件不存在：{local_package}")

    props = parse_props(command(["-s", serial, "shell", "getprop"], custom_adb, timeout=15))
    device = device_from_props(serial, props)
    if not device or not device.model:
        raise ValueError("无法核实已连接设备的机型，请重新读取设备。")
    if expected_model and device.model.casefold() != expected_model.casefold():
        raise ValueError("设备型号自确认后已变化，请重新选择设备。")
    local_sha256 = validate_push_source(local_package, device, record, cancel, progress)
    release = props.get("ro.build.version.release", "").split(".")[0]
    android_ver = int(release) if release.isdigit() else 12
    is_legacy = android_ver < 11

    if log:
        log(f"目标设备: {serial} (Android {android_ver})，适配模式: {'ColorOS 11.3 早期版本' if is_legacy else 'ColorOS 11.3+ 现代版'}")

    if cancel is not None: check_cancel(cancel)
    req_size = local_package.stat().st_size
    avail = get_device_free_space(serial, custom_adb)
    if avail >= 0:
        if log:
            log(f"手机存储可用空间: {avail / 1024**3:.2f} GiB，固件包所需空间: {req_size / 1024**3:.2f} GiB")
        if avail < req_size + 300 * 1024 * 1024:
            raise RuntimeError(f"手机存储空间不足！剩余空间 {avail / 1024**3:.2f} GiB，需要至少 {(req_size + 300*1024*1024) / 1024**3:.2f} GiB。请清理手机存储后重试。")

    if cancel is not None: check_cancel(cancel)
    if not check_assistant_installed(serial, custom_adb):
        apk_path = get_assistant_apk(is_legacy=is_legacy)
        install_assistant(serial, apk_path, custom_adb, log)
    elif log:
        log("设备已安装降级伴生应用，跳过安装。")

    suffix = '.zip' if local_package.suffix.lower() == '.zip' else '.ozip'
    remote_dir = f"/sdcard/OPPOFirmwareTool-{uuid4().hex}"
    target_remote = f"{remote_dir}/firmware{suffix}"
    shell = ["-s", serial, "shell"]
    if cancel is not None:
        check_cancel(cancel)
    # mkdir without -p is the ownership boundary. On a collision or uncertain
    # result we cannot claim this directory and must never remove anything in it.
    command([*shell, "mkdir", remote_dir], custom_adb, timeout=10)
    try:
        _push_package_file(serial, local_package, target_remote, custom_adb, cancel, progress, log, expected_sha256=local_sha256)
        if progress:
            progress(req_size, req_size, "正在唤起手机系统更新程序…")
        if cancel is not None:
            check_cancel(cancel)
    except Exception:
        try:
            command([*shell, "rm", "-f", target_remote], custom_adb, timeout=10)
            command([*shell, "rmdir", remote_dir], custom_adb, timeout=10)
        except Exception as cleanup_error:
            if log:
                log(f"手机端任务目录可能有残留：{remote_dir}（{cleanup_error}）")
        raise

    # An ADB error can arrive after Android has accepted the path. From this
    # point the updater may still be reading the verified file.
    try:
        trigger_downgrade(serial, target_remote, custom_adb, is_legacy, log)
    except Exception as exc:
        message = (f"无法确认手机系统更新程序的交接结果。已保留核验通过的固件：{target_remote}。"
                   f"请查看手机更新界面，确认安装状态后再处理该文件。详情：{exc}")
        if log:
            log(message)
        raise RuntimeError(message) from exc
