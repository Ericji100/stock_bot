"""Prepare immutable backup acceptance evidence; never complete or restore a backup."""
from __future__ import annotations

import argparse
import copy
import importlib.util
import json
from pathlib import Path
import re
from uuid import uuid4

MANAGER = Path('D:/workspace/projects/infrastructure/system_management/scripts/codex_backup_manager.py')


def prepare_completion(module, manager, project, backup_id, verification, reference_files, reason):
    project = Path(project)
    checks = verification.get('checks')
    if verification.get('passed') is not True or not isinstance(checks, list) or not checks:
        raise ValueError('VERIFICATION_CHECKS_NOT_PASSED')
    if any(not isinstance(c, dict) or not c.get('name') or c.get('passed') is not True for c in checks):
        raise ValueError('VERIFICATION_CHECKS_NOT_PASSED')
    if not reason.strip() or not re.fullmatch(r'[A-Za-z0-9_-]+', backup_id):
        raise ValueError('BACKUP_ID_OR_REASON_INVALID')
    registry = manager.registry()
    original = next((entry for entry in registry['entries'] if entry['id'] == backup_id), None)
    if original is None or original['status'] != 'retained':
        raise ValueError('BACKUP_NOT_RETAINED')
    if any(op.get('entry_id') == backup_id for op in registry.get('operations', [])):
        raise ValueError('PRIOR_RECYCLE_OPERATION_REQUIRES_INSPECTION')
    source = manager.source_path(original['source'])
    if not source.is_relative_to(project):
        raise ValueError('SOURCE_OUTSIDE_SELECTED_PROJECT')
    backup = module.file_snapshot(manager.backup_path(original['path']), check_exclusive=True)
    if backup['sha256'] != original['sha256']:
        raise ValueError('ORIGINAL_BACKUP_CHANGED')
    replacement = module.file_snapshot(source)
    if verification.get('source_sha256') != replacement['sha256'] or verification.get('backup_sha256') != backup['sha256']:
        raise ValueError('VERIFIED_FILE_SNAPSHOT_CHANGED')
    references = [str(manager.source_path(str(Path(path)))) for path in reference_files]
    if not references:
        raise ValueError('REFERENCE_FILES_REQUIRED')
    for path in references:
        module.file_snapshot(Path(path))
    # Check before creating: do not resolve aliases and silently retry a rejected path.
    out = manager.source_path(str(project / 'verification/backup-acceptance' / backup_id / uuid4().hex))
    out.mkdir(parents=True, exist_ok=False)
    accepted_at = module.utcnow().isoformat()
    acceptance = copy.deepcopy(verification)
    acceptance['accepted_at'] = accepted_at
    acceptance_path = out / 'acceptance.json'
    module.write(acceptance_path, acceptance, exclusive=True)
    entry = copy.deepcopy(original)
    entry.update(
        acceptance={'path': str(acceptance_path), 'sha256': module.digest(acceptance_path), 'accepted_at': accepted_at},
        replacement={'path': str(source), 'sha256': replacement['sha256']},
        reference_files=references,
        retention={'kind': 'ordinary', 'reason': reason},
        hold=original.get('hold', False), recovery_required=True,
        review={'references_checked': True, 'writers_quiescent': True, 'purpose_confirmed': True},
    )
    record = out / 'completion-record.json'
    module.write(record, {'schema': 'codex-backup-registration-v1', 'entries': [entry]}, exclusive=True)
    return {'record': str(record), 'sha256': module.digest(record), 'acceptance': str(acceptance_path),
            'backup_id': backup_id, 'status': 'PREPARED_ONLY'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', required=True, type=Path)
    parser.add_argument('--backup-id', required=True)
    parser.add_argument('--verification-json', required=True, type=Path)
    parser.add_argument('--reference-file', required=True, action='append', type=Path)
    parser.add_argument('--reason', required=True)
    args = parser.parse_args()
    spec = importlib.util.spec_from_file_location('backup_acceptance_manager', MANAGER)
    module = importlib.util.module_from_spec(spec)
    import sys
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    manager = module.BackupManager()
    verification = module.load(manager.source_path(str(args.verification_json)))
    print(json.dumps(prepare_completion(module, manager, args.project, args.backup_id, verification,
                                        args.reference_file, args.reason), ensure_ascii=False))


if __name__ == '__main__':
    main()
