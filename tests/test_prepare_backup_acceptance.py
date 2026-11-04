import copy
import importlib.util
import os
from pathlib import Path
import unittest

from tools.prepare_backup_acceptance import prepare_completion


class BackupAcceptanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not os.environ.get('CODEX_BACKUP_TEST_ROOT'):
            raise unittest.SkipTest('Requires a newly owned system-management synthetic test root')
        path = Path('D:/workspace/projects/infrastructure/system_management/scripts/test_codex_backup_manager.py')
        spec = importlib.util.spec_from_file_location('backup_acceptance_synthetic_fixtures', path)
        cls.fixtures = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.fixtures)
        cls.module = cls.fixtures.m

    def setUp(self):
        self.manager = self.fixtures.fixture()
        self.root = self.manager.project
        self.source = self.root / 'docs/formal.txt'
        self.entry = self.manager.create(str(self.source), 'test', 'acceptance', 'synthetic verification')['entry']
        self.proof = {'passed': True, 'checks': [{'name': 'synthetic verified source', 'passed': True}],
                      'source_sha256': self.module.digest(self.source), 'backup_sha256': self.entry['sha256']}

    def prepare(self, proof=None, project=None):
        return prepare_completion(self.module, self.manager, project or self.root, self.entry['id'],
                                  proof or self.proof, [self.root / 'docs/references.txt'], 'synthetic verified purpose')

    def test_new_plain_path_completes_with_original_manager(self):
        old = self.manager.registry()
        prepared = self.prepare()
        self.assertEqual(self.manager.registry(), old)
        self.assertTrue(Path(prepared['acceptance']).is_relative_to(self.root / 'verification/backup-acceptance'))
        result = self.manager.complete(Path(prepared['record']), prepared['sha256'])
        self.assertEqual(result['status'], 'COMPLETED')
        updated = next(e for e in self.manager.registry()['entries'] if e['id'] == self.entry['id'])
        for key in ('id', 'path', 'source', 'sha256', 'backed_up_at', 'provenance'):
            self.assertEqual(updated[key], self.entry[key])
        self.assertTrue(updated['recovery_required'])
        self.assertEqual(self.module.digest(Path(updated['path'])), self.entry['sha256'])

    def test_existing_logs_junction_still_rejected(self):
        with self.assertRaisesRegex(self.module.Retain, 'LINK_OR_REPARSE_POINT'):
            self.module.absolute('D:/code/stock_ai_bot/logs')

    def test_failed_checks_produce_no_completion(self):
        proof = copy.deepcopy(self.proof)
        proof['checks'][0]['passed'] = False
        with self.assertRaises(ValueError):
            self.prepare(proof)
        self.assertFalse((self.root / 'verification').exists())

    def test_changed_source_stops_preparation(self):
        self.source.write_text('changed after verification', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'SNAPSHOT_CHANGED'):
            self.prepare()

    def test_changed_proof_stops_completion(self):
        prepared = self.prepare()
        self.module.write(Path(prepared['acceptance']), {'passed': True, 'checks': []})
        before = self.manager.registry_path.read_bytes()
        with self.assertRaises(self.module.Retain):
            self.manager.complete(Path(prepared['record']), prepared['sha256'])
        self.assertEqual(self.manager.registry_path.read_bytes(), before)

    def test_project_mismatch_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'SOURCE_OUTSIDE_SELECTED_PROJECT'):
            self.prepare(project=self.root / 'other-project')

    def test_changed_source_after_prepare_stops_completion(self):
        prepared = self.prepare()
        self.source.write_text('new version', encoding='utf-8')
        before = self.manager.registry_path.read_bytes()
        with self.assertRaises(self.module.Retain):
            self.manager.complete(Path(prepared['record']), prepared['sha256'])
        self.assertEqual(self.manager.registry_path.read_bytes(), before)
