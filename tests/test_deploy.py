import json
import tempfile
import unittest
from pathlib import Path
from app.config import save_config, load_config
from app.__main__ import seed_demo, restore_backup
from app.store import Store


class DeploymentTest(unittest.TestCase):
    def setUp(self):
        scratch=Path.cwd()/'.test-temp'; scratch.mkdir(exist_ok=True)
        self.tmp=tempfile.TemporaryDirectory(dir=scratch); self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)

    def test_credentials_protected_and_roundtrip(self):
        config={'admin_token':'secret-admin-value','alimail':{'client_secret':'mail-secret-value'},'demo':False}
        path=self.root/'config.json'
        save_config(path,config)
        self.assertNotIn('mail-secret-value',path.read_text())
        self.assertEqual(load_config(path),config)

    def test_demo_is_idempotent_and_backup_restore_is_nondestructive(self):
        db=Store(self.root/'source.db'); seed_demo(db); seed_demo(db)
        self.assertEqual(db.rows('select count(*) n from items')[0]['n'],6)
        db.backup(self.root/'backup.db')
        restore_backup(self.root/'backup.db',self.root/'restored.db')
        self.assertEqual(Store(self.root/'restored.db').rows('select count(*) n from items')[0]['n'],6)
        with self.assertRaises(ValueError): restore_backup(self.root/'backup.db',self.root/'restored.db')

    def test_monthly_archive_preserves_all_operations(self):
        db=Store(self.root/'source.db'); seed_demo(db)
        from app.core import now
        month=now().strftime('%Y-%m')
        db.archive(self.root/'archive',month)
        data=json.loads((self.root/'archive'/f'{month}.json').read_text(encoding='utf-8'))
        self.assertEqual(len(data['items']),6)
        self.assertGreaterEqual(len(data['audit']),8)

if __name__=='__main__': unittest.main()
