import os
import shutil
import tempfile
import unittest
import sqlite3
from pathlib import Path

import location_comparator as lc

class TestLocationComparator(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.loc_a = Path(self.test_dir) / "loc_a"
        self.loc_b = Path(self.test_dir) / "loc_b"
        self.iso_dir = Path(self.test_dir) / "isolated"
        self.db_path = str(Path(self.test_dir) / "test_registry.db")

        self.loc_a.mkdir()
        self.loc_b.mkdir()
        self.iso_dir.mkdir()

        # Create files in Loc A and Loc B
        # 1. Identical file
        (self.loc_a / "identical.txt").write_text("Hello World! Identical content.")
        (self.loc_b / "identical.txt").write_text("Hello World! Identical content.")

        # 2. Modified file (same relative path, different content)
        (self.loc_a / "modified.txt").write_text("Version A of modified file.")
        (self.loc_b / "modified.txt").write_text("Version B of modified file - altered.")

        # 3. Unique to Location A
        (self.loc_a / "unique_a.txt").write_text("Only in location A.")

        # 4. Unique to Location B in a subfolder
        (self.loc_b / "subfolder").mkdir()
        (self.loc_b / "subfolder" / "unique_b.txt").write_text("Only in location B.")

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    def test_scan_and_compare(self):
        # Scan Location A
        res_a = lc.run_scan_location(str(self.loc_a), 'A', self.db_path)
        self.assertTrue(res_a)

        # Scan Location B
        res_b = lc.run_scan_location(str(self.loc_b), 'B', self.db_path)
        self.assertTrue(res_b)

        # Run Compare
        report = lc.run_compare(str(self.loc_a), str(self.loc_b), self.db_path, run_quiet=True)

        self.assertEqual(len(report['identical']), 1)
        self.assertEqual(report['identical'][0]['rel_path'], "identical.txt")

        self.assertEqual(len(report['modified']), 1)
        self.assertEqual(report['modified'][0]['rel_path'], "modified.txt")

        self.assertEqual(len(report['unique_a']), 1)
        self.assertEqual(report['unique_a'][0]['rel_path'], "unique_a.txt")

        self.assertEqual(len(report['unique_b']), 1)
        self.assertEqual(report['unique_b'][0]['rel_path'], os.path.join("subfolder", "unique_b.txt"))

        metrics = lc.get_comparison_metrics(self.db_path)
        self.assertEqual(metrics['count_a'], 3)
        self.assertEqual(metrics['count_b'], 3)
        self.assertEqual(metrics['identical_count'], 1)
        self.assertEqual(metrics['modified_count'], 1)
        self.assertEqual(metrics['unique_a_count'], 1)
        self.assertEqual(metrics['unique_b_count'], 1)

    def test_isolate_all(self):
        lc.run_scan_location(str(self.loc_a), 'A', self.db_path)
        lc.run_scan_location(str(self.loc_b), 'B', self.db_path)

        # Isolate all differing files from both sources
        lc.run_isolate(str(self.iso_dir), self.db_path, source='both', diff_type='all')

        # Check isolated folder
        self.assertTrue((self.iso_dir / "Location_A" / "modified.txt").exists())
        self.assertTrue((self.iso_dir / "Location_B" / "modified.txt").exists())
        self.assertTrue((self.iso_dir / "Location_A" / "unique_a.txt").exists())
        self.assertTrue((self.iso_dir / "Location_B" / "subfolder" / "unique_b.txt").exists())

        # Check identical file was NOT moved
        self.assertTrue((self.loc_a / "identical.txt").exists())
        self.assertTrue((self.loc_b / "identical.txt").exists())

    def test_isolate_modified_only_a(self):
        lc.run_scan_location(str(self.loc_a), 'A', self.db_path)
        lc.run_scan_location(str(self.loc_b), 'B', self.db_path)

        lc.run_isolate(str(self.iso_dir), self.db_path, source='A', diff_type='modified')

        self.assertTrue((self.iso_dir / "Location_A" / "modified.txt").exists())
        self.assertFalse((self.iso_dir / "Location_B" / "modified.txt").exists())
        self.assertFalse((self.iso_dir / "Location_A" / "unique_a.txt").exists())
        self.assertTrue((self.loc_a / "unique_a.txt").exists())

    def test_cleanup_and_reset(self):
        lc.run_scan_location(str(self.loc_a), 'A', self.db_path)

        # Delete a file manually
        os.remove(self.loc_a / "unique_a.txt")

        lc.run_cleanup(self.db_path)

        db_conn = sqlite3.connect(self.db_path)
        cursor = db_conn.cursor()
        cursor.execute("SELECT filepath FROM files WHERE rel_path = 'unique_a.txt'")
        self.assertIsNone(cursor.fetchone())
        db_conn.close()

        lc.run_reset(self.db_path)
        db_conn = sqlite3.connect(self.db_path)
        cursor = db_conn.cursor()
        cursor.execute("SELECT count(*) FROM sqlite_master WHERE type='table' AND name='files'")
        self.assertEqual(cursor.fetchone()[0], 0)
        db_conn.close()

if __name__ == '__main__':
    unittest.main()
