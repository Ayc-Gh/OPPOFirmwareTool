"""Exercise one public firmware query using the same HTTPS client as the GUI."""
import sys
from pathlib import Path

from . import __version__, tracker
from .common import atomic_json


def check_public_query(report_path: Path) -> bool:
    options = tracker.QueryOptions('PHY110', 'cn', '', suffixes='F')
    report = {
        'version': __version__, 'frozen': getattr(sys, 'frozen', False),
        'model': options.model, 'region': options.region, 'mode': 'manual',
        'ok': False, 'outcome': 'query_failed', 'response_code': None, 'packages': [],
    }
    try:
        channel, config = next((channel, cfg) for channel, cfg in tracker.build_queries(options)
                               if channel == '正式版' and cfg.mode == 'manual')
        report['query_ota'] = config.ota_version
        result = tracker.upstream.query_update(config)
        report['response_code'] = result.response_code
        if result.success and result.response_code == 200:
            records = tracker.normalize_result(result, config, channel, options)
            report['packages'] = [{'version': p.version, 'ota': p.ota, 'component': p.component} for p in records]
            if records:
                report.update(ok=True, outcome='packages')
            else:
                report['outcome'] = 'no_package_records'
        elif not result.success and result.response_code == 2004:
            report.update(ok=True, outcome='no_visible_update')
    except Exception as exc:
        # Exception strings can contain proxy credentials or signed download URLs.
        report['error_type'] = type(exc).__name__
    atomic_json(report_path, report)
    return report['ok']
