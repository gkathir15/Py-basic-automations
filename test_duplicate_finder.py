import os
import shutil
import tempfile
import unittest
import sqlite3
from pathlib import Path

import duplicate_finder as df

class TestDuplicateFinder(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.scan_dir = Path(self.test_dir) / "scan_folder"
        self.iso_dir = Path(self.test_dir) / "isolated_folder"
        self.db_path = str(Path(self.test_dir) / "test_duplicate_registry.db")

        self.scan_dir.mkdir()
        self.iso_dir.mkdir()

        # Create files
        # Duplicate group 1 (content "Dup content 1")
        (self.scan_dir / "file1a.txt").write_text("Dup content 1 - " + "A" * 10000)
        (self.scan_dir / "file1b.txt").write_text("Dup content 1 - " + "A" * 10000)

        # Duplicate group 2 (content "Dup content 2")
        (self.scan_dir / "subfolder").mkdir()
        (self.scan_dir / "file2a.txt").write_text("Dup content 2 - " + "B" * 10000)
        (self.scan_dir / "subfolder" / "file2b.txt").write_text("Dup content 2 - " + "B" * 10000)

        # Unique file
        (self.scan_dir / "unique.txt").write_text("Unique content - " + "C" * 5000)

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    def test_run_scan_and_find_duplicates(self):
        scan_ok = df.run_scan(str(self.scan_dir), self.db_path)
        self.assertTrue(scan_ok)

        # Test space distribution stats
        dist = df.get_space_distribution(self.db_path)
        self.assertEqual(dist['total_count'], 5)

        # Run find_duplicates with multithreaded hashing
        groups = df.find_duplicates(self.db_path, run_quiet=True)
        self.assertEqual(len(groups), 2)

        dist_after = df.get_space_distribution(self.db_path)
        self.assertEqual(dist_after['wasted_count'], 2)
        self.assertGreater(dist_after['wasted_size'], 0)
        self.assertGreater(dist_after['keeper_size'], 0)
        self.assertGreater(dist_after['unique_size'], 0)

    def test_run_isolate(self):
        df.run_scan(str(self.scan_dir), self.db_path)

        # Isolate duplicate files
        df.run_isolate(str(self.iso_dir), self.db_path)

        # Check isolated files exist
        isolated_files = list(self.iso_dir.glob("**/*"))
        file_count = sum(1 for f in isolated_files if f.is_file())
        self.assertEqual(file_count, 2)

    def test_cleanup_and_reset(self):
        df.run_scan(str(self.scan_dir), self.db_path)

        # Delete a file manually
        os.remove(self.scan_dir / "unique.txt")

        df.run_cleanup(self.db_path)

        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT filepath FROM files WHERE filename = 'unique.txt'")
        self.assertIsNone(cursor.fetchone())
        conn.close()

        df.run_reset(self.db_path)

        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT count(*) FROM sqlite_master WHERE type='table' AND name='files'")
        self.assertEqual(cursor.fetchone()[0], 0)
        conn.close()

    def test_format_helpers(self):
        self.assertEqual(df.format_size(500), "500 B")
        self.assertEqual(df.format_size(2048), "2.00 KB")
        self.assertEqual(df.format_size(1048576 * 5), "5.00 MB")
        self.assertEqual(df.format_size(1073741824 * 3), "3.00 GB")

        self.assertEqual(df.format_time(-1), "Unknown")
        self.assertEqual(df.format_time(45.2), "45.2s")
        self.assertEqual(df.format_time(125), "2m 5s")

if __name__ == '__main__':
    unittest.main()
