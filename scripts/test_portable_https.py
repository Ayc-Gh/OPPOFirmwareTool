"""Run a live firmware query from a freshly extracted Windows release ZIP."""
import argparse
import json
import os
import subprocess
import tempfile
import zipfile
from pathlib import Path


def check_archive(archive: Path):
    with tempfile.TemporaryDirectory(prefix='oppo-https-') as scratch:
        root = Path(scratch)
        app = root / 'portable'
        with zipfile.ZipFile(archive) as package:
            version = json.loads(package.read('docs/packaged-check.json'))['version']
            package.extractall(app)
        report = root / 'https-result.json'
        result = subprocess.run(
            [str(app / 'OPPOFirmwareTool.exe'), '--check-ota-https', str(report)],
            cwd=app, env=os.environ | {'LOCALAPPDATA': str(root / 'appdata')}, timeout=90,
        )
        if not report.is_file():
            raise RuntimeError(f'便携程序退出码 {result.returncode}，未生成 HTTPS 查询报告')
        data = json.loads(report.read_text(encoding='utf-8'))
        print(json.dumps(data, ensure_ascii=False, indent=2))
        if result.returncode or data.get('ok') is not True or data.get('frozen') is not True:
            raise RuntimeError('便携程序实际 HTTPS 查询失败')
        if (data.get('version'), data.get('model'), data.get('region'), data.get('mode')) != (
                version, 'PHY110', 'cn', 'manual'):
            raise RuntimeError('查询报告的版本或查询参数与本次测试不符')
        if data.get('outcome') == 'no_visible_update' and data.get('response_code') == 2004 and data.get('packages') == []:
            return
        if data.get('outcome') != 'packages' or data.get('response_code') != 200 or not data.get('packages'):
            raise RuntimeError('查询报告缺少已确认的固件记录或无更新业务码')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('portable_zip', type=Path)
    check_archive(parser.parse_args().portable_zip)
