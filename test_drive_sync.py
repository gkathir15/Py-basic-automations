import os
import shutil
import tempfile
import unittest
import sqlite3
from pathlib import Path

import drive_sync as ds

class TestDriveSync(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.src = Path(self.test_dir) / "src"
        self.dst = Path(self.test_dir) / "dst"
        self.db_path = str(Path(self.test_dir) / "test_registry.db")

        self.src.mkdir()
        self.dst.mkdir()

        # Source content
        (self.src / "a.txt").write_text("alpha content")
        (self.src / "only_here.txt").write_text("unique to the source")
        (self.src / "sub").mkdir()
        (self.src / "sub" / "b.txt").write_text("beta content in a subfolder")

        # Destination already has: the same content stored under another name,
        # a conflicting file at the same relative path, plus an extra file.
        (self.dst / "elsewhere").mkdir()
        (self.dst / "elsewhere" / "renamed_copy.txt").write_text("alpha content")
        (self.dst / "only_here.txt").write_text("a different, older version")
        (self.dst / "extra_only_in_destination.txt").write_text("keep me")

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_analyze_is_a_dry_run(self):
        plan = ds.run_analyze(str(self.src), str(self.dst), self.db_path, run_quiet=True)

        # a.txt is satisfied by content at a different path, so only the two
        # genuinely missing files are planned.
        self.assertEqual(plan['copy_count'], 2)
        copied_rels = {item['rel_path'] for item in plan['to_copy']}
        self.assertIn("only_here.txt", copied_rels)
        self.assertIn(os.path.join("sub", "b.txt"), copied_rels)

        # Nothing was written to the destination by the dry run.
        self.assertFalse((self.dst / "sub" / "b.txt").exists())
        self.assertEqual((self.dst / "only_here.txt").read_text(), "a different, older version")

    def test_sync_copies_missing_files_and_preserves_structure(self):
        plan = ds.run_sync(str(self.src), str(self.dst), self.db_path, run_quiet=True, assume_yes=True)

        self.assertEqual(plan['copied_count'], 2)
        self.assertTrue((self.dst / "sub" / "b.txt").exists())
        self.assertEqual((self.dst / "sub" / "b.txt").read_text(), "beta content in a subfolder")

        # Source is never touched.
        self.assertEqual((self.src / "a.txt").read_text(), "alpha content")

    def test_content_match_anywhere_skips_renamed_files(self):
        ds.run_sync(str(self.src), str(self.dst), self.db_path, run_quiet=True, assume_yes=True)

        # a.txt content already exists as elsewhere/renamed_copy.txt -> not copied.
        self.assertFalse((self.dst / "a.txt").exists())
        self.assertTrue((self.dst / "elsewhere" / "renamed_copy.txt").exists())

    def test_overwrites_conflicting_file_at_same_path(self):
        plan = ds.run_sync(str(self.src), str(self.dst), self.db_path, run_quiet=True, assume_yes=True)

        self.assertEqual(plan['overwritten_count'], 1)
        self.assertEqual((self.dst / "only_here.txt").read_text(), "unique to the source")

    def test_destination_extras_are_never_deleted(self):
        ds.run_sync(str(self.src), str(self.dst), self.db_path, run_quiet=True, assume_yes=True)

        self.assertTrue((self.dst / "extra_only_in_destination.txt").exists())
        self.assertEqual((self.dst / "extra_only_in_destination.txt").read_text(), "keep me")

        plan = ds.run_analyze(str(self.src), str(self.dst), self.db_path, run_quiet=True)
        extras = {item['rel_path'] for item in plan['dest_extras']}
        self.assertIn("extra_only_in_destination.txt", extras)

    def test_second_run_copies_nothing(self):
        ds.run_sync(str(self.src), str(self.dst), self.db_path, run_quiet=True, assume_yes=True)
        plan = ds.run_sync(str(self.src), str(self.dst), self.db_path, run_quiet=True, assume_yes=True)

        self.assertEqual(plan['copy_count'], 0)
        self.assertEqual(plan['copied_count'], 0)

    def test_path_match_mode_reports_instead_of_overwriting(self):
        plan = ds.run_sync(str(self.src), str(self.dst), self.db_path, match='path',
                           run_quiet=True, assume_yes=True)

        # only_here.txt exists at that path in the destination -> left alone.
        self.assertEqual((self.dst / "only_here.txt").read_text(), "a different, older version")
        copied_rels = {item['rel_path'] for item in plan['to_copy']}
        self.assertNotIn("only_here.txt", copied_rels)
        self.assertIn(os.path.join("sub", "b.txt"), copied_rels)

    def test_both_match_mode_overwrites_when_content_differs(self):
        plan = ds.run_sync(str(self.src), str(self.dst), self.db_path, match='both',
                           run_quiet=True, assume_yes=True)

        # Same relative path, different content -> replaced with the source version.
        self.assertEqual((self.dst / "only_here.txt").read_text(), "unique to the source")
        # a.txt is not copied: the destination has no a.txt path at all, but its
        # content lives elsewhere, which 'both' mode does not count as present.
        copied_rels = {item['rel_path'] for item in plan['to_copy']}
        self.assertIn("a.txt", copied_rels)

    def test_copied_files_are_verified_and_indexed(self):
        ds.run_sync(str(self.src), str(self.dst), self.db_path, run_quiet=True, assume_yes=True)

        db_conn = sqlite3.connect(self.db_path)
        cursor = db_conn.cursor()
        cursor.execute("SELECT full_hash FROM files WHERE location_id = ? AND rel_path = ?",
                       (ds.DST, os.path.join("sub", "b.txt")))
        row = cursor.fetchone()
        db_conn.close()

        self.assertIsNotNone(row)
        self.assertEqual(row[0], ds.compute_full_hash(str(self.src / "sub" / "b.txt")))

    def test_missing_source_and_destination_are_rejected(self):
        self.assertIsNone(ds.run_sync(str(self.src / "nope"), str(self.dst), self.db_path, run_quiet=True, assume_yes=True))
        self.assertIsNone(ds.run_sync(str(self.src), str(self.dst / "nope"), self.db_path, run_quiet=True, assume_yes=True))
        self.assertIsNone(ds.run_sync(str(self.src), str(self.src), self.db_path, run_quiet=True, assume_yes=True))

    def test_cleanup_and_reset(self):
        ds.run_scan_location(str(self.src), ds.SRC, self.db_path)

        os.remove(self.src / "only_here.txt")
        ds.run_cleanup(self.db_path)

        db_conn = sqlite3.connect(self.db_path)
        cursor = db_conn.cursor()
        cursor.execute("SELECT filepath FROM files WHERE rel_path = 'only_here.txt' AND location_id = ?", (ds.SRC,))
        self.assertIsNone(cursor.fetchone())
        db_conn.close()

        ds.run_reset(self.db_path)
        db_conn = sqlite3.connect(self.db_path)
        cursor = db_conn.cursor()
        cursor.execute("SELECT count(*) FROM sqlite_master WHERE type='table' AND name='files'")
        self.assertEqual(cursor.fetchone()[0], 0)
        db_conn.close()

    def test_scan_drops_stale_index_entries(self):
        ds.run_scan_location(str(self.src), ds.SRC, self.db_path)
        os.remove(self.src / "a.txt")
        ds.run_scan_location(str(self.src), ds.SRC, self.db_path)

        db_conn = sqlite3.connect(self.db_path)
        cursor = db_conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM files WHERE location_id = ?", (ds.SRC,))
        count = cursor.fetchone()[0]
        db_conn.close()

        self.assertEqual(count, 2)

    def test_sync_metrics(self):
        ds.run_scan_location(str(self.src), ds.SRC, self.db_path)
        ds.run_scan_location(str(self.dst), ds.DST, self.db_path)
        ds.hash_candidates(self.db_path, match='hash', run_quiet=True)

        metrics = ds.get_sync_metrics(self.db_path, match='hash')
        self.assertEqual(metrics['src_count'], 3)
        self.assertEqual(metrics['dst_count'], 3)
        # a.txt is present by content elsewhere; only_here.txt and sub/b.txt are not.
        self.assertEqual(metrics['missing_count'], 2)
        self.assertEqual(metrics['missing_size'],
                         (self.src / "only_here.txt").stat().st_size + (self.src / "sub" / "b.txt").stat().st_size)
        # Destination paths with no source counterpart: the renamed copy and the extra file.
        self.assertEqual(metrics['extra_count'], 2)

if __name__ == '__main__':
    unittest.main()
