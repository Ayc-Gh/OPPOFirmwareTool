# Third-party notices

This application is provided under GPL-3.0-only. Its complete editable source and build scripts accompany the Windows distribution.

* **OPlus-Tracker**, Jerry Tse and contributors. https://github.com/JerryTse-OSS/OPlus-Tracker — LGPL-3.0. Vendored sources: `vendor/oplus_tracker/`. Original license included there and under `licenses/`. Modified locally for relative imports, structured raw response access, bounded retry/timeouts, streaming redirect handling, quieter error reporting and enabled TLS verification on downgrade requests. The vendored modified sources are supplied and may be replaced/rebuilt.
* **ssut/payload-dumper-go**, ssut and contributors. https://github.com/ssut/payload-dumper-go — Apache-2.0. Unmodified Windows release 2.1.0 invoked as a separate executable. Source for the release: https://github.com/ssut/payload-dumper-go/tree/2.1.0 . Apache license is included under `licenses/`.
* **xishang0128/payload-dumper-go**, xishang0128 and contributors. https://github.com/xishang0128/payload-dumper-go — GPL-3.0. Unmodified Windows release v0.1.6 invoked as a separate executable. Corresponding source archive is provided with the release distribution; upstream: https://github.com/xishang0128/payload-dumper-go/tree/v0.1.6 . License included under `licenses/`.
* **Android SDK Platform-Tools / ADB**, Android Open Source Project and contributors. Unmodified adb.exe and required Windows DLLs. Upstream: https://developer.android.com/tools/releases/platform-tools . Bundled NOTICE reproduced as `licenses/Android-platform-tools-NOTICE.txt`.
* **PySide6 / Shiboken / Qt**, The Qt Company and contributors, LGPL-3.0 / GPL-3.0 / applicable component licenses. https://www.qt.io/qt-for-python . Package license texts are included under `licenses/python-packages/` when building. PySide/Qt remain dynamically linked in `_internal`; the editable application source and build recipe permit replacement/rebuilding.
* **Python**, Python Software Foundation and contributors, PSF license. https://www.python.org/ . License reproduced from the local Python runtime when building.
* **asn1crypto**, Will Bond and contributors, MIT license. Parses the signed-data envelope for Android OTA package verification. Its license is included in `licenses/python-packages/asn1crypto/`.
* **requests**, Kenneth Reitz and contributors, Apache-2.0; **cryptography**, Python Cryptographic Authority and contributors, Apache-2.0 or BSD; their dependencies retain their respective licenses. Package license texts accompany the distribution.
* **PyInstaller**, PyInstaller development team, GPL-2.0-or-later with the bootloader exception. https://pyinstaller.org/ . No restriction on distributing generated applications is imposed by the bootloader exception. License texts are collected from the installed package when building.
* **ColorOS Upgrade Assistant / OTA Downgrade Components**, OPPO Guangdong Mobile Telecommunications Co., Ltd. The bundled companion APKs (`tools/apk/OTA_17.apk` and `tools/apk/OTA_113.apk`) are official upgrade assistant packages for local install dispatch. Research and reverse engineering analysis of the downgrade trigger flow, `DataSharedProvider` URI injection, and `forbid_check_update` parameters are documented in `docs/项目研究与实现.md`.

Artifact versions and recorded SHA256 values are in `tools/versions.json`. No vendor private keys, device keyboxes, firmware packages or credentials are bundled.

## 公开机型信息来源

`oplus_tool/data/model_catalog.json` 中的机型名称、型号和设备代号事实来自 Google Play 公布的设备 CSV：https://storage.googleapis.com/play_public/supported_devices.csv 。说明页面：https://support.google.com/googleplay/answer/1727131 。目录记录来源、获取时间及原文件 SHA256；只整理 OPPO、OnePlus、realme 条目，不包含设备身份标识。地区默认值由本工具按国行 P 系列代码规则建议，不是 Google 提供的字段。机型收录不保证 OTA 接口支持。
