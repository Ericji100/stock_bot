"""Run pytest with no external HTTP and owned, isolated test storage."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--task-root', type=Path, required=True)
    parser.add_argument('--result-root', type=Path, required=True)
    parser.add_argument('tests', nargs='*', default=['tests'])
    args = parser.parse_args()
    work = args.task_root.resolve()
    marker = json.loads((work / '.codex-task.json').read_text(encoding='utf-8-sig'))
    if Path(marker['project']).resolve() != ROOT or work != ROOT / '.codex-tmp' / marker['session'] / marker['task']:
        raise ValueError('測試僅可寫入本專案新建且已核對的 owned 暫存。')
    out = args.result_root.resolve()
    if not out.is_relative_to(ROOT / 'verification'):
        raise ValueError('測試證據需保存在專案 verification 目錄。')
    out.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(ROOT))
    os.chdir(ROOT)
    sys.stdout.reconfigure(encoding='utf-8', errors='backslashreplace')
    os.environ.update(OPENBLAS_NUM_THREADS='1', OMP_NUM_THREADS='1', MKL_NUM_THREADS='1',
        CODEX_TASK_TEST_ROOT=str(work/'f'), DEEPSEEK_BUDGET_TEST_ROOT=str(work/'b'),
        DEEPSEEK_API_KEY='offline-test-key', OPENAI_API_KEY='', OPENAI_SECONDARY_API_KEY='',
        TEMP=str(work), TMP=str(work), STOCK_AI_BOT_WORKDATA_ROOT=str(work/'workdata'))
    import httpx
    send, async_send = httpx.Client.send, httpx.AsyncClient.send
    def guarded_send(self, request, *a, **k):
        if not isinstance(self._transport, httpx.MockTransport):
            raise AssertionError('離線驗證禁止外部 HTTP')
        return send(self, request, *a, **k)
    async def guarded_async_send(self, request, *a, **k):
        if not isinstance(self._transport, (httpx.MockTransport, httpx.ASGITransport)):
            raise AssertionError('離線驗證禁止外部 HTTP')
        return await async_send(self, request, *a, **k)
    httpx.Client.send, httpx.AsyncClient.send = guarded_send, guarded_async_send
    def no_external(*a, **k):
        raise AssertionError('離線驗證禁止外部 HTTP')
    import requests
    import urllib.request
    requests.sessions.Session.request = no_external
    urllib.request.urlopen = no_external
    try:
        import curl_cffi.requests
        curl_cffi.requests.Session.request = no_external
    except ImportError:
        pass
    from tests import test_cache_utils
    test_cache_utils._TEST_CACHE_ROOT = work/'t'
    original_dir = test_cache_utils.ensure_test_cache_dir
    def short_dir(name):
        if Path(name).is_absolute() or '..' in Path(name).parts:
            return original_dir(name)
        return original_dir(hashlib.sha256(str(name).encode()).hexdigest()[:8] + uuid4().hex[:5])
    test_cache_utils.ensure_test_cache_dir = short_dir
    test_cache_utils.safe_remove_test_cache = lambda path: None
    test_cache_utils._remove_tree_with_retries = lambda *a, **k: False
    from stock_ai_bot.scanning import stock_scanner
    for name in ['CACHE_DIR', 'MONTHLY_CACHE_DIR', 'PRICE_CACHE_PATH', 'GROSS_MARGIN_CACHE_PATH', 'HISTORICAL_FINANCIAL_CACHE_DIR']:
        setattr(stock_scanner, name, work/'s'/Path(getattr(stock_scanner, name)).relative_to(ROOT))
    from research_center import market_movers_service, prompt_logging, ai_workflow_service
    market_movers_service.CACHE_DIR = work/'market-movers'
    prompt_logging.PROMPT_LOG_DIR = work/'prompts'
    ai_workflow_service.LOW_MODEL_ARTIFACT_DIR = work/'digests'
    from stock_ai_bot.data_sources import official_daily_quotes
    official_daily_quotes.CACHE_DIR = work/'quotes'
    import pytest
    original_mktemp = pytest.TempPathFactory.mktemp
    def short_mktemp(self, basename, numbered=True):
        if Path(basename).name != basename or '..' in Path(basename).parts:
            return original_mktemp(self, basename, numbered=numbered)
        return original_mktemp(self, hashlib.sha256(basename.encode()).hexdigest()[:8], numbered=numbered)
    pytest.TempPathFactory.mktemp = short_mktemp
    source_paths = [ROOT/'main.py', ROOT/'config/research_center.json', ROOT/'config/report_display_terms.json']
    for folder in ['research_center', 'stock_ai_bot', 'tools', 'tests', 'backtests']:
        source_paths.extend((ROOT/folder).rglob('*.py'))
    source_paths.extend((ROOT/'prompt').rglob('*.md'))
    source_paths.extend((ROOT/'docs/strategies/dual_ma').rglob('*.md'))
    source_before = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in source_paths}
    class Results:
        def __init__(self):
            self.started, self.reports = time.monotonic(), []
        def pytest_runtest_logstart(self, nodeid, location):
            (out/'progress.json').write_text(json.dumps({'current_test': nodeid,
                'elapsed_seconds': round(time.monotonic()-self.started, 3)}, indent=2), encoding='utf-8')
        def pytest_runtest_logreport(self, report):
            if report.failed or report.skipped:
                self.reports.append({'nodeid': report.nodeid, 'when': report.when,
                    'outcome': report.outcome, 'details': str(report.longrepr)})
                (out/'details.json').write_text(json.dumps(self.reports, ensure_ascii=False, indent=2), encoding='utf-8')
        def pytest_sessionfinish(self, session, exitstatus):
            changed = [name for name, digest in source_before.items()
                       if not (ROOT/name).is_file() or hashlib.sha256((ROOT/name).read_bytes()).hexdigest() != digest]
            reporter = session.config.pluginmanager.get_plugin('terminalreporter')
            (out/'session.json').write_text(json.dumps({'exit_status': int(exitstatus), 'collected': session.testscollected,
                'failed': session.testsfailed, 'elapsed_seconds': round(time.monotonic()-self.started, 3),
                'counts': {k: len(v) for k, v in reporter.stats.items()},
                'source_unchanged': not changed, 'changed_files': changed, 'source_file_count': len(source_before),
                'http_policy': 'mock/ASGI only; external HTTP denied', 'test_paths': args.tests}, indent=2), encoding='utf-8')
            (out/'source-sha256.json').write_text(json.dumps(source_before, indent=2), encoding='utf-8')
    return pytest.main(['-q', '-o', f'cache_dir={work/"pc"}', '--basetemp', str(work/('p'+uuid4().hex[:4])), *args.tests], plugins=[Results()])


if __name__ == '__main__':
    raise SystemExit(main())
