#!/usr/bin/env python3
import os
import sys
import sqlite3
import hashlib
import uuid
import argparse
import shutil
import queue
import threading
import time
from pathlib import Path
from datetime import datetime

# Initialize ANSI colors for Windows terminals if needed
if sys.platform == 'win32':
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        # Enable ENABLE_VIRTUAL_TERMINAL_PROCESSING (0x0004) & ENABLE_PROCESSED_OUTPUT (0x0001)
        kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
    except Exception:
        pass

class Colors:
    HEADER = '\033[95m'
    BLUE = '\033[94m'
    CYAN = '\033[96m'
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    RED = '\033[91m'
    END = '\033[0m'
    BOLD = '\033[1m'
    UNDERLINE = '\033[4m'

def print_banner():
    banner = f"""{Colors.BOLD}{Colors.CYAN}
  _                 _   _              ___                                _
 | |   ___  ___ __ _| |_(_)___ _ _    / C \\___ _ __  _ __  __ _ _ _  __ _| |_ ___ _ _
 | |__/ _ \\/ __/ _` |  _| / _ \\ ' \\  | (_) / _ \\ '  \\| '_ \\/ _` | '_|/ _` |  _/ _ \\ '_|
 |____\\___/\\___\\__,_|\\__|_\\___/_||_|  \\___/\\___/_|_|_| .__/\\__,_|_|  \\__,_|\\__\\___/|_|
                                                     |_|
{Colors.END}{Colors.BLUE}  High-performance hash comparison & isolation utility between two directory locations.{Colors.END}
"""
    print(banner)

def print_progress(current, total, prefix='', suffix='', bar_length=30):
    percent = ("{0:.1f}").format(100 * (current / float(total))) if total > 0 else "0.0"
    filled_length = int(round(bar_length * current / float(total))) if total > 0 else 0
    bar = '#' * filled_length + '-' * (bar_length - filled_length)
    sys.stdout.write(f'\r{Colors.BLUE}{prefix} |{bar}| {percent}% {suffix}{Colors.END}')
    sys.stdout.flush()

def print_status(message, end='\r'):
    sys.stdout.write(f'\r{Colors.CYAN}{message}{Colors.END}')
    sys.stdout.flush()
    if end != '\r':
        print()

def format_size(size_bytes):
    if size_bytes is None:
        return "0.00 B"
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.2f} KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.2f} MB"
    else:
        return f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"

def format_time(seconds):
    if seconds < 0:
        return "Unknown"
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes = int(seconds // 60)
    secs = seconds % 60
    return f"{minutes}m {int(secs)}s"

def get_memory_usage_windows():
    try:
        import ctypes
        from ctypes import wintypes

        class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        GetProcessMemoryInfo = ctypes.windll.psapi.GetProcessMemoryInfo
        GetCurrentProcess = ctypes.windll.kernel32.GetCurrentProcess

        process_handle = GetCurrentProcess()
        counters = PROCESS_MEMORY_COUNTERS()
        counters.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS)

        if GetProcessMemoryInfo(process_handle, ctypes.byref(counters), counters.cb):
            return counters.WorkingSetSize  # in bytes
    except Exception:
        pass
    return 0

def get_current_memory_usage():
    """Returns physical memory (RSS) used by this process in bytes."""
    if sys.platform == 'win32':
        return get_memory_usage_windows()
    else:
        try:
            import resource
            return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024
        except Exception:
            pass
    return 0

def set_process_priority(mode):
    """
    Adjusts the process scheduling priority:
    - 'max' (Normal priority class)
    - 'balanced' (Below Normal on Windows / nice 10 on Unix)
    - 'eco' (Idle priority class on Windows / nice 19 on Unix)
    """
    if sys.platform == 'win32':
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.GetCurrentProcess()
            classes = {
                'max': 0x00000020,      # NORMAL_PRIORITY_CLASS
                'balanced': 0x00004000, # BELOW_NORMAL_PRIORITY_CLASS
                'eco': 0x00000040       # IDLE_PRIORITY_CLASS
            }
            kernel32.SetPriorityClass(handle, classes.get(mode, 0x00000020))
        except Exception:
            pass
    else:
        try:
            classes = {
                'max': 0,
                'balanced': 10,
                'eco': 19
            }
            os.nice(classes.get(mode, 0))
        except Exception:
            pass

def get_db_path(target_path_str):
    """
    Returns the path to the database file.
    Attempts to store it at the root of the target drive.
    Falls back to the script's directory if write permissions are denied.
    """
    try:
        resolved_path = Path(target_path_str).resolve()
        drive_root = resolved_path.anchor  # e.g., "C:\" on Windows, or "/" on Unix
        if not drive_root:
            drive_root = Path.cwd().anchor

        db_file = Path(drive_root) / "location_comparator_registry.db"

        # Test write access
        test_conn = sqlite3.connect(str(db_file))
        test_conn.execute("CREATE TABLE IF NOT EXISTS _write_test (id INTEGER PRIMARY KEY);")
        test_conn.execute("DROP TABLE _write_test;")
        test_conn.close()

        return str(db_file)
    except Exception:
        fallback_db = Path(__file__).parent.resolve() / "location_comparator_registry.db"
        if len(sys.argv) > 1:
            print(f"{Colors.YELLOW}[WARNING] Write permission to drive root denied. DB will be stored at: {fallback_db}{Colors.END}")
        return str(fallback_db)

def init_db(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            location_id TEXT,
            rel_path TEXT,
            filepath TEXT,
            filename TEXT,
            size INTEGER,
            mtime REAL,
            partial_hash TEXT,
            full_hash TEXT,
            UNIQUE(location_id, rel_path)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_loc_rel ON files(location_id, rel_path)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_files_size ON files(size)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_files_hashes ON files(partial_hash, full_hash)")
    conn.commit()

def compute_partial_hash(filepath):
    try:
        with open(filepath, 'rb') as f:
            data = f.read(8192)  # Read first 8KB
            return hashlib.sha256(data).hexdigest()
    except (OSError, PermissionError):
        return None

def compute_full_hash(filepath, chunk_cb=None, throttle_sleep=0.0):
    hasher = hashlib.sha256()
    try:
        with open(filepath, 'rb') as f:
            while True:
                chunk = f.read(65536)  # Read 64KB chunks
                if not chunk:
                    break
                hasher.update(chunk)
                if chunk_cb:
                    chunk_cb(len(chunk))
                if throttle_sleep > 0.0:
                    time.sleep(throttle_sleep)
        return hasher.hexdigest()
    except (OSError, PermissionError):
        return None

def get_comparison_metrics(db_path, loc_a_dir=None, loc_b_dir=None):
    """
    Analyzes Location A vs Location B in SQLite database.
    Returns size and count statistics for A, B, Identical, Modified, Unique A, Unique B.
    """
    db_conn = sqlite3.connect(db_path)
    cursor = db_conn.cursor()
    init_db(db_conn)

    cursor.execute("SELECT COUNT(*), SUM(size) FROM files WHERE location_id = 'A'")
    res_a = cursor.fetchone()
    count_a, size_a = res_a[0] or 0, res_a[1] or 0

    cursor.execute("SELECT COUNT(*), SUM(size) FROM files WHERE location_id = 'B'")
    res_b = cursor.fetchone()
    count_b, size_b = res_b[0] or 0, res_b[1] or 0

    # Path-based comparison metrics
    cursor.execute("""
        SELECT a.filepath, a.size, a.full_hash, b.filepath, b.size, b.full_hash, a.rel_path
        FROM files a
        JOIN files b ON a.rel_path = b.rel_path
        WHERE a.location_id = 'A' AND b.location_id = 'B'
    """)
    common_rel = cursor.fetchall()

    identical_count = 0
    identical_size = 0
    modified_count = 0
    modified_size_a = 0
    modified_size_b = 0

    for fa, sa, ha, fb, sb, hb, rel in common_rel:
        if ha is not None and hb is not None and ha == hb and not ha.startswith("ERROR_"):
            identical_count += 1
            identical_size += sa
        else:
            modified_count += 1
            modified_size_a += sa
            modified_size_b += sb

    # Unique paths in A (not in B)
    cursor.execute("""
        SELECT COUNT(*), SUM(size) FROM files
        WHERE location_id = 'A' AND rel_path NOT IN (
            SELECT rel_path FROM files WHERE location_id = 'B'
        )
    """)
    res_uniq_a = cursor.fetchone()
    unique_a_count, unique_a_size = res_uniq_a[0] or 0, res_uniq_a[1] or 0

    # Unique paths in B (not in A)
    cursor.execute("""
        SELECT COUNT(*), SUM(size) FROM files
        WHERE location_id = 'B' AND rel_path NOT IN (
            SELECT rel_path FROM files WHERE location_id = 'A'
        )
    """)
    res_uniq_b = cursor.fetchone()
    unique_b_count, unique_b_size = res_uniq_b[0] or 0, res_uniq_b[1] or 0

    db_conn.close()

    return {
        'count_a': count_a,
        'size_a': size_a,
        'count_b': count_b,
        'size_b': size_b,
        'identical_count': identical_count,
        'identical_size': identical_size,
        'modified_count': modified_count,
        'modified_size_a': modified_size_a,
        'modified_size_b': modified_size_b,
        'unique_a_count': unique_a_count,
        'unique_a_size': unique_a_size,
        'unique_b_count': unique_b_count,
        'unique_b_size': unique_b_size,
        'diff_total_count': modified_count + unique_a_count + unique_b_count,
        'diff_total_size': modified_size_a + modified_size_b + unique_a_size + unique_b_size
    }

def run_scan_location(target_dir, location_id, db_path, progress_cb=None):
    target_path = Path(target_dir).resolve()
    if not target_path.exists():
        if progress_cb:
            progress_cb('error', f"Error: Path {target_path} does not exist.")
        else:
            print(f"{Colors.RED}Error: Path {target_path} does not exist.{Colors.END}")
        return False

    db_conn = sqlite3.connect(db_path)
    db_conn.execute("PRAGMA journal_mode=WAL;")
    init_db(db_conn)

    start_time = time.time()

    if progress_cb:
        progress_cb('info', f"Scanning Location {location_id}: {target_path}...")
        progress_cb('info', f"Using database file: {db_path}")
    else:
        print(f"{Colors.BLUE}Scanning Location {location_id}: {Colors.BOLD}{target_path}{Colors.END}{Colors.BLUE}...{Colors.END}")
        print(f"{Colors.BLUE}Using database file: {Colors.BOLD}{db_path}{Colors.END}")

    # First, delete existing index records for this location_id if directory path changed
    files_found = 0
    errors = 0
    batch = []

    def recurse(path):
        nonlocal files_found, errors, batch
        try:
            for entry in os.scandir(path):
                if entry.is_symlink():
                    continue
                if entry.is_file():
                    try:
                        abs_entry = Path(entry.path).resolve()
                        rel_path = str(abs_entry.relative_to(target_path))
                        stat = entry.stat()
                        batch.append((location_id, rel_path, str(abs_entry), entry.name, stat.st_size, stat.st_mtime))
                        files_found += 1
                        if len(batch) >= 1000:
                            db_conn.executemany("""
                                INSERT INTO files (location_id, rel_path, filepath, filename, size, mtime)
                                VALUES (?, ?, ?, ?, ?, ?)
                                ON CONFLICT(location_id, rel_path) DO UPDATE SET
                                    filepath = excluded.filepath,
                                    filename = excluded.filename,
                                    size = excluded.size,
                                    mtime = excluded.mtime,
                                    partial_hash = CASE WHEN size != excluded.size OR mtime != excluded.mtime THEN NULL ELSE partial_hash END,
                                    full_hash = CASE WHEN size != excluded.size OR mtime != excluded.mtime THEN NULL ELSE full_hash END
                            """, batch)
                            db_conn.commit()
                            batch = []
                            elapsed = time.time() - start_time
                            speed = files_found / elapsed if elapsed > 0 else 0
                            if progress_cb:
                                progress_cb('progress_update', location_id, files_found, errors, elapsed, speed)
                            else:
                                print_status(f"Indexed Location {location_id}: {files_found:,} files... ({speed:.0f} files/s)")
                    except (OSError, PermissionError, ValueError):
                        errors += 1
                elif entry.is_dir():
                    recurse(entry.path)
        except (OSError, PermissionError):
            errors += 1

    recurse(target_path)

    if batch:
        db_conn.executemany("""
            INSERT INTO files (location_id, rel_path, filepath, filename, size, mtime)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(location_id, rel_path) DO UPDATE SET
                filepath = excluded.filepath,
                filename = excluded.filename,
                size = excluded.size,
                mtime = excluded.mtime,
                partial_hash = CASE WHEN size != excluded.size OR mtime != excluded.mtime THEN NULL ELSE partial_hash END,
                full_hash = CASE WHEN size != excluded.size OR mtime != excluded.mtime THEN NULL ELSE full_hash END
        """, batch)
        db_conn.commit()

    elapsed = time.time() - start_time
    speed = files_found / elapsed if elapsed > 0 else 0

    if progress_cb:
        progress_cb('complete', location_id, files_found, errors, elapsed, speed)
    else:
        print(f"\r{Colors.GREEN}Scan Location {location_id} complete! Indexed {files_found:,} files in {elapsed:.1f}s. (Skipped {errors:,} errors){Colors.END}")
    db_conn.close()
    return True

def run_compare(loc_a_dir, loc_b_dir, db_path, progress_cb=None, run_quiet=False, throttle_sleep=0.0):
    db_conn = sqlite3.connect(db_path)
    db_conn.execute("PRAGMA journal_mode=WAL;")
    cursor = db_conn.cursor()
    init_db(db_conn)

    # Step 1: Compute partial hashes for files that need hashing
    # Files need partial hash if full_hash is NULL and partial_hash is NULL
    cursor.execute("""
        SELECT filepath FROM files
        WHERE partial_hash IS NULL AND full_hash IS NULL
    """)
    to_partial = [r[0] for r in cursor.fetchall()]

    if to_partial:
        if progress_cb:
            progress_cb('partial_start', len(to_partial))
        elif not run_quiet:
            print(f"{Colors.BLUE}Computing partial hashes for {len(to_partial):,} files...{Colors.END}")
        for idx, path in enumerate(to_partial):
            p_hash = compute_partial_hash(path)
            if p_hash is None:
                p_hash = f"ERROR_{uuid.uuid4().hex}"
            db_conn.execute("UPDATE files SET partial_hash = ? WHERE filepath = ?", (p_hash, path))
            if idx % 100 == 0 or idx == len(to_partial) - 1:
                db_conn.commit()
                if progress_cb:
                    progress_cb('partial_progress', idx + 1, len(to_partial))
                elif not run_quiet:
                    print_progress(idx + 1, len(to_partial), prefix="Partial Hashing", suffix=f"{idx+1}/{len(to_partial)}")
        if not run_quiet and not progress_cb:
            print()

    # Step 2: Compute full hashes for files whose corresponding counterpart or content needs checking
    # We compute full hashes for all indexed files that don't have one yet.
    cursor.execute("""
        SELECT filepath, size FROM files
        WHERE full_hash IS NULL
    """)
    to_full_rows = cursor.fetchall()
    to_full = [r[0] for r in to_full_rows]
    total_bytes_to_hash = sum(r[1] for r in to_full_rows)

    if to_full:
        if progress_cb:
            progress_cb('full_start', len(to_full), total_bytes_to_hash)
        elif not run_quiet:
            print(f"{Colors.BLUE}Computing full hashes for {len(to_full):,} files ({format_size(total_bytes_to_hash)})...{Colors.END}")

        for idx, path in enumerate(to_full):
            def make_chunk_cb():
                return lambda sz: progress_cb('hash_chunk', sz) if progress_cb else None

            f_hash = compute_full_hash(path, chunk_cb=make_chunk_cb(), throttle_sleep=throttle_sleep)
            if f_hash is None:
                f_hash = f"ERROR_{uuid.uuid4().hex}"
            db_conn.execute("UPDATE files SET full_hash = ? WHERE filepath = ?", (f_hash, path))
            if idx % 10 == 0 or idx == len(to_full) - 1:
                db_conn.commit()
                if progress_cb:
                    progress_cb('full_progress', idx + 1, len(to_full))
                elif not run_quiet:
                    print_progress(idx + 1, len(to_full), prefix="Full Hashing   ", suffix=f"{idx+1}/{len(to_full)}")
        if not run_quiet and not progress_cb:
            print()
        db_conn.commit()

    # Step 3: Detailed comparison query
    # Find files present at same relative path in A and B
    cursor.execute("""
        SELECT a.rel_path, a.filepath, a.size, a.mtime, a.full_hash,
               b.filepath, b.size, b.mtime, b.full_hash
        FROM files a
        JOIN files b ON a.rel_path = b.rel_path
        WHERE a.location_id = 'A' AND b.location_id = 'B'
        ORDER BY a.rel_path
    """)
    common_files = cursor.fetchall()

    identical_files = []
    modified_files = []

    for rel_path, fa, sa, ma, ha, fb, sb, mb, hb in common_files:
        if ha is not None and hb is not None and ha == hb and not ha.startswith("ERROR_"):
            identical_files.append({
                'rel_path': rel_path,
                'path_a': fa, 'size_a': sa, 'mtime_a': ma, 'hash_a': ha,
                'path_b': fb, 'size_b': sb, 'mtime_b': mb, 'hash_b': hb
            })
        else:
            modified_files.append({
                'rel_path': rel_path,
                'path_a': fa, 'size_a': sa, 'mtime_a': ma, 'hash_a': ha,
                'path_b': fb, 'size_b': sb, 'mtime_b': mb, 'hash_b': hb
            })

    # Files unique to Location A (relative path exists in A but not in B)
    cursor.execute("""
        SELECT rel_path, filepath, size, mtime, full_hash FROM files
        WHERE location_id = 'A' AND rel_path NOT IN (
            SELECT rel_path FROM files WHERE location_id = 'B'
        )
        ORDER BY rel_path
    """)
    unique_a = [{
        'rel_path': r[0], 'path': r[1], 'size': r[2], 'mtime': r[3], 'hash': r[4]
    } for r in cursor.fetchall()]

    # Files unique to Location B (relative path exists in B but not in A)
    cursor.execute("""
        SELECT rel_path, filepath, size, mtime, full_hash FROM files
        WHERE location_id = 'B' AND rel_path NOT IN (
            SELECT rel_path FROM files WHERE location_id = 'A'
        )
        ORDER BY rel_path
    """)
    unique_b = [{
        'rel_path': r[0], 'path': r[1], 'size': r[2], 'mtime': r[3], 'hash': r[4]
    } for r in cursor.fetchall()]

    report = {
        'identical': identical_files,
        'modified': modified_files,
        'unique_a': unique_a,
        'unique_b': unique_b
    }

    if progress_cb:
        progress_cb('report_start')
        progress_cb('report_data', report)
    elif not run_quiet:
        print(f"\n{Colors.BOLD}{Colors.YELLOW}=== LOCATION COMPARISON REPORT ==={Colors.END}\n")

        print(f"{Colors.BOLD}{Colors.GREEN}[1] IDENTICAL FILES (Path & Hash match): {len(identical_files)}{Colors.END}")
        for item in identical_files[:10]: # Print top 10 preview
            print(f"  - {item['rel_path']} ({format_size(item['size_a'])})")
        if len(identical_files) > 10:
            print(f"  ... and {len(identical_files) - 10} more identical files.")
        print()

        print(f"{Colors.BOLD}{Colors.RED}[2] MODIFIED / DIFFERENT FILES (Path match, Hash mismatch): {len(modified_files)}{Colors.END}")
        for item in modified_files:
            mtime_a = datetime.fromtimestamp(item['mtime_a']).strftime('%Y-%m-%d %H:%M:%S')
            mtime_b = datetime.fromtimestamp(item['mtime_b']).strftime('%Y-%m-%d %H:%M:%S')
            print(f"  * {item['rel_path']}:")
            print(f"    Loc A: {format_size(item['size_a'])} | Modified: {mtime_a} | Hash: {str(item['hash_a'])[:12]}")
            print(f"    Loc B: {format_size(item['size_b'])} | Modified: {mtime_b} | Hash: {str(item['hash_b'])[:12]}")
        print()

        print(f"{Colors.BOLD}{Colors.CYAN}[3] UNIQUE TO LOCATION A: {len(unique_a)}{Colors.END}")
        for item in unique_a:
            print(f"  + {item['rel_path']} ({format_size(item['size'])})")
        print()

        print(f"{Colors.BOLD}{Colors.YELLOW}[4] UNIQUE TO LOCATION B: {len(unique_b)}{Colors.END}")
        for item in unique_b:
            print(f"  + {item['rel_path']} ({format_size(item['size'])})")
        print()

        tot_diff = len(modified_files) + len(unique_a) + len(unique_b)
        print(f"{Colors.BOLD}{Colors.GREEN}Summary: {len(identical_files)} identical files. {tot_diff} total differing/unique items found.{Colors.END}")

    db_conn.close()
    return report

def run_isolate(isolation_dir, db_path, source='both', diff_type='all', progress_cb=None):
    """
    Moves differing files from Location A and/or Location B to isolation directory.
    - source: 'A', 'B', or 'both'
    - diff_type: 'all' (modified + unique), 'modified' (only modified), 'unique' (only unique)
    """
    if progress_cb:
        progress_cb('isolate_start')

    report = run_compare(None, None, db_path, run_quiet=True)
    if not report:
        if progress_cb:
            progress_cb('isolate_no_files')
        else:
            print(f"{Colors.YELLOW}No differences found to isolate.{Colors.END}")
        return

    target_path = Path(isolation_dir).resolve()
    target_path.mkdir(parents=True, exist_ok=True)

    items_to_move = [] # List of tuples: (source_label, filepath, relative_subpath)

    # Process modified files
    if diff_type in ('all', 'modified'):
        for item in report['modified']:
            if source in ('A', 'both'):
                items_to_move.append(('Location_A', Path(item['path_a']), Path('Location_A') / item['rel_path']))
            if source in ('B', 'both'):
                items_to_move.append(('Location_B', Path(item['path_b']), Path('Location_B') / item['rel_path']))

    # Process unique A
    if diff_type in ('all', 'unique') and source in ('A', 'both'):
        for item in report['unique_a']:
            items_to_move.append(('Location_A', Path(item['path']), Path('Location_A') / item['rel_path']))

    # Process unique B
    if diff_type in ('all', 'unique') and source in ('B', 'both'):
        for item in report['unique_b']:
            items_to_move.append(('Location_B', Path(item['path']), Path('Location_B') / item['rel_path']))

    if not items_to_move:
        if progress_cb:
            progress_cb('isolate_no_files')
        else:
            print(f"{Colors.YELLOW}No matching differing files found for specified criteria.{Colors.END}")
        return

    if progress_cb:
        progress_cb('isolate_info', f"Isolating {len(items_to_move)} differing files to: {target_path}...")
    else:
        print(f"{Colors.BLUE}Isolating {len(items_to_move)} differing files to: {Colors.BOLD}{target_path}{Colors.END}...")

    moved_count = 0
    errors = 0

    db_conn = sqlite3.connect(db_path)
    db_conn.execute("PRAGMA journal_mode=WAL;")

    for src_label, src_file, rel_dest in items_to_move:
        try:
            if not src_file.exists():
                if progress_cb:
                    progress_cb('isolate_move', src_file, None, 'missing')
                else:
                    print(f"  {Colors.YELLOW}[MISSING]{Colors.END} {src_file} (Skipped)")
                continue

            dest_path = target_path / rel_dest
            dest_path.parent.mkdir(parents=True, exist_ok=True)

            if dest_path.exists():
                unique_id = uuid.uuid4().hex[:6]
                dest_path = dest_path.with_name(f"{dest_path.stem}_{unique_id}{dest_path.suffix}")

            shutil.move(str(src_file), str(dest_path))
            if progress_cb:
                progress_cb('isolate_move', src_file, dest_path, 'moved')
            else:
                print(f"  {Colors.YELLOW}[MOVING]{Colors.END} ({src_label}) {src_file} -> {dest_path}")

            db_conn.execute("DELETE FROM files WHERE filepath = ?", (str(src_file),))
            moved_count += 1
        except Exception as e:
            if progress_cb:
                progress_cb('isolate_move', src_file, None, f"error: {e}")
            else:
                print(f"  {Colors.RED}[ERROR] Failed to move {src_file}: {e}{Colors.END}")
            errors += 1

    db_conn.commit()
    db_conn.close()

    if progress_cb:
        progress_cb('isolate_complete', moved_count, errors)
    else:
        print(f"\n{Colors.GREEN}Isolation complete! Moved {moved_count} files to {target_path}. (Errors: {errors}){Colors.END}")

def run_cleanup(db_path):
    db_conn = sqlite3.connect(db_path)
    db_conn.execute("PRAGMA journal_mode=WAL;")
    cursor = db_conn.cursor()
    init_db(db_conn)
    cursor.execute("SELECT filepath FROM files")
    all_files = [r[0] for r in cursor.fetchall()]

    if not all_files:
        print(f"{Colors.GREEN}Database is empty. Nothing to clean.{Colors.END}")
        db_conn.close()
        return

    missing = []
    print(f"{Colors.BLUE}Verifying {len(all_files):,} files in database...{Colors.END}")
    for idx, path in enumerate(all_files):
        if not os.path.exists(path):
            missing.append(path)
        if idx % 1000 == 0 or idx == len(all_files) - 1:
            print_progress(idx + 1, len(all_files), prefix="Verifying", suffix=f"{idx+1}/{len(all_files)}")
    print()

    if missing:
        print(f"{Colors.YELLOW}Removing {len(missing):,} missing files from database...{Colors.END}")
        db_conn.executemany("DELETE FROM files WHERE filepath = ?", [(p,) for p in missing])
        db_conn.commit()
        print(f"{Colors.GREEN}Cleanup complete.{Colors.END}")
    else:
        print(f"{Colors.GREEN}All indexed files are present in the filesystem. No cleanup needed.{Colors.END}")
    db_conn.close()

def run_reset(db_path):
    db_conn = sqlite3.connect(db_path)
    db_conn.execute("DROP TABLE IF EXISTS files")
    db_conn.commit()
    db_conn.close()
    if len(sys.argv) > 1:
        print(f"{Colors.GREEN}Database reset completed.{Colors.END}")

def run_executor(loc_a_path, loc_b_path, target_dir):
    """All-in-one execution pipeline for location comparator."""
    db_path = get_db_path(loc_a_path)

    print(f"{Colors.BOLD}{Colors.GREEN}>>> STEP 1: Scanning Location A '{loc_a_path}'...{Colors.END}")
    if not run_scan_location(loc_a_path, 'A', db_path):
        return

    print(f"\n{Colors.BOLD}{Colors.GREEN}>>> STEP 2: Scanning Location B '{loc_b_path}'...{Colors.END}")
    if not run_scan_location(loc_b_path, 'B', db_path):
        return

    print(f"\n{Colors.BOLD}{Colors.GREEN}>>> STEP 3: Comparing Location Hashes...{Colors.END}")
    report = run_compare(loc_a_path, loc_b_path, db_path)

    tot_diff = len(report['modified']) + len(report['unique_a']) + len(report['unique_b'])
    if tot_diff == 0:
        print(f"\n{Colors.GREEN}Locations are identical! Process finished successfully.{Colors.END}")
        return

    print(f"\n{Colors.BOLD}{Colors.GREEN}>>> STEP 4: Isolation Prompt{Colors.END}")
    try:
        user_input = input(f"{Colors.BOLD}Do you want to isolate differing files to '{target_dir}'? (y/n): {Colors.END}").strip().lower()
        if user_input in ('y', 'yes'):
            run_isolate(target_dir, db_path, source='both', diff_type='all')
        else:
            print(f"{Colors.YELLOW}Isolation skipped. All files left in their original locations.{Colors.END}")
    except KeyboardInterrupt:
        print(f"\n{Colors.RED}Isolation prompt cancelled.{Colors.END}")

def launch_gui():
    try:
        import tkinter as tk
        from tkinter import messagebox, filedialog, scrolledtext
        from tkinter import ttk
    except ImportError:
        print("Error: Tkinter library is not installed or graphical interface is not supported on this system.")
        print("Please run this tool in CLI mode by adding arguments.")
        sys.exit(1)

    # Theme colors definition (Soft pastel dashboard aesthetic)
    THEME = {
        'bg': '#f2f4f8',
        'card_bg': '#ffffff',
        'btn_bg': '#e2e8f0',
        'btn_blue': '#7ba0e4',
        'btn_green': '#7fc99a',
        'btn_orange': '#e89b6b',
        'btn_red': '#e57373',
        'fg': '#2d3748',
        'fg_dim': '#64748b',
        'fg_dark': '#1a202c',
        'font_title': ('Segoe UI', 18, 'bold'),
        'font_header': ('Segoe UI', 11, 'bold'),
        'font_card_val': ('Segoe UI', 14, 'bold'),
        'font_card_lbl': ('Segoe UI', 9),
        'font_body': ('Segoe UI', 10),
        'font_code': ('Consolas', 10)
    }

    class HistoricSpeedChart(tk.Frame):
        """
        Pure Tkinter historic session speed & performance chart.
        Plots Disk Read / Hashing Speed (MB/s), Processing Speed (files/s), and Process RAM (MB)
        on a single time-series canvas graph.
        """
        def __init__(self, parent, bg_color, theme):
            super().__init__(parent, bg=bg_color)
            self.theme = theme

            self.series = {
                'read_hash_mbs': {'label': 'Disk Read / Hash Speed (MB/s)', 'color': '#7ba0e4', 'data': []},
                'file_proc_fps': {'label': 'File Processing (files/s)', 'color': '#7fc99a', 'data': []},
                'ram_mb': {'label': 'Process RAM (MB)', 'color': '#e89b6b', 'data': []}
            }
            self.time_points = []
            self.start_time = time.time()

            # Chart header with title and legends
            header_frame = tk.Frame(self, bg=bg_color)
            header_frame.pack(fill='x', pady=(0, 4))

            title_lbl = tk.Label(header_frame, text="Historic Session Performance", font=self.theme['font_header'], bg=bg_color, fg=self.theme['fg'])
            title_lbl.pack(side='left')

            # Legends
            legend_frame = tk.Frame(header_frame, bg=bg_color)
            legend_frame.pack(side='right')

            for key, sinfo in self.series.items():
                item = tk.Frame(legend_frame, bg=bg_color)
                item.pack(side='left', padx=(8, 0))
                box = tk.Label(item, bg=sinfo['color'], width=2, height=1, relief='flat')
                box.pack(side='left', padx=(0, 3))
                lbl = tk.Label(item, text=sinfo['label'], font=('Segoe UI', 8), bg=bg_color, fg=self.theme['fg_dim'])
                lbl.pack(side='left')

            # Main canvas graph
            self.canvas = tk.Canvas(self, height=130, bg=self.theme['card_bg'], highlightthickness=1, highlightbackground=self.theme['btn_bg'])
            self.canvas.pack(fill='both', expand=True)
            self.canvas.bind("<Configure>", lambda e: self.redraw())

        def reset(self):
            self.time_points.clear()
            for sinfo in self.series.values():
                sinfo['data'].clear()
            self.start_time = time.time()
            self.redraw()

        def add_sample(self, elapsed=None, read_hash_mbs=0.0, file_proc_fps=0.0, ram_mb=0.0):
            if elapsed is None:
                elapsed = time.time() - self.start_time

            # Keep last 120 data points
            if len(self.time_points) >= 120:
                self.time_points.pop(0)
                for sinfo in self.series.values():
                    sinfo['data'].pop(0)

            self.time_points.append(elapsed)
            self.series['read_hash_mbs']['data'].append(max(0.0, float(read_hash_mbs)))
            self.series['file_proc_fps']['data'].append(max(0.0, float(file_proc_fps)))
            self.series['ram_mb']['data'].append(max(0.0, float(ram_mb)))

            self.redraw()

        def redraw(self):
            self.canvas.delete("all")
            width = self.canvas.winfo_width() or 400
            height = self.canvas.winfo_height() or 130

            padding_left = 45
            padding_right = 15
            padding_top = 15
            padding_bottom = 25

            plot_w = max(10, width - padding_left - padding_right)
            plot_h = max(10, height - padding_top - padding_bottom)

            # Draw background grid lines
            for i in range(4):
                y = padding_top + (plot_h * i / 3.0)
                self.canvas.create_line(padding_left, y, padding_left + plot_w, y, fill='#e2e8f0', dash=(2, 2))

            if not self.time_points or len(self.time_points) < 2:
                self.canvas.create_text(width / 2, height / 2, text="Waiting for session metrics...", fill=self.theme['fg_dim'], font=('Segoe UI', 9))
                return

            t_min = self.time_points[0]
            t_max = max(self.time_points[-1], t_min + 1.0)

            # Calculate dynamic Y max across series
            max_val = 1.0
            for sinfo in self.series.values():
                if sinfo['data']:
                    max_val = max(max_val, max(sinfo['data']))

            max_val = max_val * 1.15  # headroom

            # Y axis labels
            self.canvas.create_text(padding_left - 5, padding_top, text=f"{max_val:.1f}", fill=self.theme['fg_dim'], font=('Segoe UI', 7), anchor='e')
            self.canvas.create_text(padding_left - 5, padding_top + plot_h / 2, text=f"{max_val/2:.1f}", fill=self.theme['fg_dim'], font=('Segoe UI', 7), anchor='e')
            self.canvas.create_text(padding_left - 5, padding_top + plot_h, text="0.0", fill=self.theme['fg_dim'], font=('Segoe UI', 7), anchor='e')

            # X axis labels
            self.canvas.create_text(padding_left, padding_top + plot_h + 12, text=f"{t_min:.0f}s", fill=self.theme['fg_dim'], font=('Segoe UI', 7), anchor='n')
            self.canvas.create_text(padding_left + plot_w, padding_top + plot_h + 12, text=f"{t_max:.0f}s", fill=self.theme['fg_dim'], font=('Segoe UI', 7), anchor='n')

            # Draw lines for each series
            n = len(self.time_points)
            for key, sinfo in self.series.items():
                data = sinfo['data']
                points = []
                for idx in range(n):
                    t = self.time_points[idx]
                    v = data[idx]

                    x = padding_left + ((t - t_min) / (t_max - t_min)) * plot_w
                    y = padding_top + plot_h - ((v / max_val) * plot_h)
                    points.append((x, y))

                if len(points) >= 2:
                    flat_coords = [c for pt in points for c in pt]
                    self.canvas.create_line(flat_coords, fill=sinfo['color'], width=2, smooth=True)

    class LocationComparatorApp:
        def __init__(self, root):
            self.root = root
            self.root.title("Location Hash Comparator Dashboard")
            self.root.geometry("980x820")
            self.root.minsize(880, 750)
            self.root.configure(bg=THEME['bg'])

            self.path_a = tk.StringVar(value=str(Path.cwd()))
            self.path_b = tk.StringVar(value=str(Path.cwd()))
            self.isolate_path = tk.StringVar(value=str(Path.cwd() / "Isolated_Differences"))
            self.status_var = tk.StringVar(value="Ready")

            # Metric StringVars
            self.stat_a_count = tk.StringVar(value="0")
            self.stat_a_size = tk.StringVar(value="0.00 B")
            self.stat_b_count = tk.StringVar(value="0")
            self.stat_b_size = tk.StringVar(value="0.00 B")
            self.stat_identical = tk.StringVar(value="0 files")
            self.stat_diff = tk.StringVar(value="0 files")

            # Live performance StringVars
            self.perf_speed = tk.StringVar(value="0 B/s")
            self.perf_elapsed = tk.StringVar(value="0.0s")
            self.perf_eta = tk.StringVar(value="--")
            self.perf_ram = tk.StringVar(value="0.0 MB")

            self.perf_mode = tk.StringVar(value="balanced")

            # Isolation Options
            self.iso_source = tk.StringVar(value="both") # 'A', 'B', or 'both'
            self.iso_diff_type = tk.StringVar(value="all") # 'all', 'modified', 'unique'

            self.queue = queue.Queue()
            self.running_thread = None
            self.widgets_to_disable = []

            self.hash_start_time = 0
            self.hash_total_bytes = 0
            self.hash_bytes_processed = 0

            self.logs_visible = False

            self.current_read_hash_mbs = 0.0
            self.current_file_proc_fps = 0.0

            self.build_ui()
            self.poll_queue()
            self.draw_empty_chart()
            self.update_ram_usage()

        def make_button(self, parent, text, command, bg_color=THEME['btn_bg'], fg_color=THEME['fg']):
            btn = tk.Button(
                parent,
                text=text,
                command=command,
                bg=bg_color,
                fg=fg_color,
                activebackground=bg_color,
                activeforeground=fg_color,
                font=THEME['font_body'],
                relief='flat',
                padx=12,
                pady=6,
                cursor='hand2',
                highlightthickness=0,
                bd=0
            )
            self.widgets_to_disable.append(btn)
            return btn

        def build_ui(self):
            main_frame = tk.Frame(self.root, bg=THEME['bg'])
            main_frame.pack(fill='both', expand=True, padx=20, pady=20)

            # Header
            header_frame = tk.Frame(main_frame, bg=THEME['bg'])
            header_frame.pack(fill='x', pady=(0, 15))

            title = tk.Label(
                header_frame,
                text="Location Hash Comparator",
                font=THEME['font_title'],
                fg=THEME['btn_blue'],
                bg=THEME['bg']
            )
            title.pack(side='left')

            subtitle = tk.Label(
                header_frame,
                text="  |  Compare two directories & isolate differences",
                font=THEME['font_body'],
                fg=THEME['fg_dim'],
                bg=THEME['bg']
            )
            subtitle.pack(side='left', pady=(6, 0))

            # Performance selector in header
            perf_frame = tk.Frame(header_frame, bg=THEME['bg'])
            perf_frame.pack(side='right')

            tk.Label(perf_frame, text="Mode: ", font=THEME['font_card_lbl'], fg=THEME['fg_dim'], bg=THEME['bg']).pack(side='left')
            mode_menu = ttk.Combobox(
                perf_frame,
                textvariable=self.perf_mode,
                values=['max', 'balanced', 'eco'],
                state='readonly',
                width=9
            )
            mode_menu.pack(side='left')
            mode_menu.bind('<<ComboboxSelected>>', lambda e: set_process_priority(self.perf_mode.get()))
            self.widgets_to_disable.append(mode_menu)

            # Input Card
            input_card = tk.Frame(main_frame, bg=THEME['card_bg'], padx=15, pady=15)
            input_card.pack(fill='x', pady=(0, 15))

            # Location A Path Row
            row_a = tk.Frame(input_card, bg=THEME['card_bg'])
            row_a.pack(fill='x', pady=(0, 8))
            tk.Label(row_a, text="Location A:", font=THEME['font_header'], fg=THEME['fg'], bg=THEME['card_bg'], width=12, anchor='w').pack(side='left')
            entry_a = tk.Entry(row_a, textvariable=self.path_a, font=THEME['font_body'], bg=THEME['btn_bg'], fg=THEME['fg'], insertbackground=THEME['fg'], bd=0, relief='flat')
            entry_a.pack(side='left', fill='x', expand=True, padx=(0, 10), ipady=5)
            self.make_button(row_a, "Browse", lambda: self.browse_folder(self.path_a)).pack(side='right')

            # Location B Path Row
            row_b = tk.Frame(input_card, bg=THEME['card_bg'])
            row_b.pack(fill='x', pady=(0, 8))
            tk.Label(row_b, text="Location B:", font=THEME['font_header'], fg=THEME['fg'], bg=THEME['card_bg'], width=12, anchor='w').pack(side='left')
            entry_b = tk.Entry(row_b, textvariable=self.path_b, font=THEME['font_body'], bg=THEME['btn_bg'], fg=THEME['fg'], insertbackground=THEME['fg'], bd=0, relief='flat')
            entry_b.pack(side='left', fill='x', expand=True, padx=(0, 10), ipady=5)
            self.make_button(row_b, "Browse", lambda: self.browse_folder(self.path_b)).pack(side='right')

            # Isolation Path Row
            row_iso = tk.Frame(input_card, bg=THEME['card_bg'])
            row_iso.pack(fill='x')
            tk.Label(row_iso, text="Isolation Path:", font=THEME['font_header'], fg=THEME['fg'], bg=THEME['card_bg'], width=12, anchor='w').pack(side='left')
            entry_iso = tk.Entry(row_iso, textvariable=self.isolate_path, font=THEME['font_body'], bg=THEME['btn_bg'], fg=THEME['fg'], insertbackground=THEME['fg'], bd=0, relief='flat')
            entry_iso.pack(side='left', fill='x', expand=True, padx=(0, 10), ipady=5)
            self.make_button(row_iso, "Browse", lambda: self.browse_folder(self.isolate_path)).pack(side='right')

            # Metrics Dashboard Grid
            metrics_frame = tk.Frame(main_frame, bg=THEME['bg'])
            metrics_frame.pack(fill='x', pady=(0, 15))

            self.create_card(metrics_frame, "LOCATION A", self.stat_a_count, self.stat_a_size, THEME['btn_blue']).grid(row=0, column=0, padx=(0, 10), sticky='nsew')
            self.create_card(metrics_frame, "LOCATION B", self.stat_b_count, self.stat_b_size, THEME['btn_green']).grid(row=0, column=1, padx=(0, 10), sticky='nsew')
            self.create_card(metrics_frame, "IDENTICAL FILES", self.stat_identical, None, THEME['btn_blue']).grid(row=0, column=2, padx=(0, 10), sticky='nsew')
            self.create_card(metrics_frame, "DIFFERENCES", self.stat_diff, None, THEME['btn_red']).grid(row=0, column=3, sticky='nsew')

            metrics_frame.columnconfigure(0, weight=1)
            metrics_frame.columnconfigure(1, weight=1)
            metrics_frame.columnconfigure(2, weight=1)
            metrics_frame.columnconfigure(3, weight=1)

            # Chart Canvas Frame
            chart_frame = tk.Frame(main_frame, bg=THEME['card_bg'], padx=15, pady=10)
            chart_frame.pack(fill='x', pady=(0, 15))

            tk.Label(chart_frame, text="COMPARISON BREAKDOWN", font=THEME['font_card_lbl'], fg=THEME['fg_dim'], bg=THEME['card_bg']).pack(anchor='w')
            self.chart_canvas = tk.Canvas(chart_frame, height=24, bg=THEME['btn_bg'], highlightthickness=0)
            self.chart_canvas.pack(fill='x', pady=(5, 5))

            # Historic Speed & Performance Chart
            self.historic_chart = HistoricSpeedChart(main_frame, THEME['bg'], THEME)
            self.historic_chart.pack(fill='x', pady=(0, 15))

            # Actions Bar
            actions_frame = tk.Frame(main_frame, bg=THEME['bg'])
            actions_frame.pack(fill='x', pady=(0, 15))

            self.make_button(actions_frame, "Run Full Pipeline", self.start_run_pipeline, THEME['btn_blue'], THEME['fg_dark']).pack(side='left', padx=(0, 8))
            self.make_button(actions_frame, "Scan Loc A", self.start_scan_a).pack(side='left', padx=(0, 8))
            self.make_button(actions_frame, "Scan Loc B", self.start_scan_b).pack(side='left', padx=(0, 8))
            self.make_button(actions_frame, "Compare Hashes", self.start_compare, THEME['btn_green'], THEME['fg_dark']).pack(side='left', padx=(0, 8))
            self.make_button(actions_frame, "Isolate Differences", self.start_isolate, THEME['btn_orange'], THEME['fg_dark']).pack(side='left', padx=(0, 8))
            self.make_button(actions_frame, "Cleanup DB", self.start_cleanup).pack(side='left', padx=(0, 8))
            self.make_button(actions_frame, "Reset DB", self.start_reset, THEME['btn_red'], THEME['fg_dark']).pack(side='right')

            # Isolation Options Bar
            iso_opts_frame = tk.Frame(main_frame, bg=THEME['card_bg'], padx=12, pady=8)
            iso_opts_frame.pack(fill='x', pady=(0, 15))

            tk.Label(iso_opts_frame, text="Isolate Options:  Source:", font=THEME['font_card_lbl'], fg=THEME['fg_dim'], bg=THEME['card_bg']).pack(side='left')
            combo_src = ttk.Combobox(iso_opts_frame, textvariable=self.iso_source, values=['both', 'A', 'B'], state='readonly', width=7)
            combo_src.pack(side='left', padx=(5, 15))
            self.widgets_to_disable.append(combo_src)

            tk.Label(iso_opts_frame, text="Difference Type:", font=THEME['font_card_lbl'], fg=THEME['fg_dim'], bg=THEME['card_bg']).pack(side='left')
            combo_diff = ttk.Combobox(iso_opts_frame, textvariable=self.iso_diff_type, values=['all', 'modified', 'unique'], state='readonly', width=10)
            combo_diff.pack(side='left', padx=(5, 0))
            self.widgets_to_disable.append(combo_diff)

            # Progress Section
            prog_card = tk.Frame(main_frame, bg=THEME['card_bg'], padx=15, pady=12)
            prog_card.pack(fill='x', pady=(0, 15))

            status_row = tk.Frame(prog_card, bg=THEME['card_bg'])
            status_row.pack(fill='x', pady=(0, 5))

            self.lbl_status = tk.Label(status_row, textvariable=self.status_var, font=THEME['font_body'], fg=THEME['fg'], bg=THEME['card_bg'])
            self.lbl_status.pack(side='left')

            self.lbl_perf = tk.Label(
                status_row,
                text="0 B/s | 0.0s | ETA: -- | RAM: 0 MB",
                font=THEME['font_code'],
                fg=THEME['fg_dim'],
                bg=THEME['card_bg']
            )
            self.lbl_perf.pack(side='right')

            self.progress_bar = ttk.Progressbar(prog_card, mode='determinate', style="TProgressbar")
            self.progress_bar.pack(fill='x')

            # Log Section Toggle
            log_header = tk.Frame(main_frame, bg=THEME['bg'])
            log_header.pack(fill='x', pady=(0, 5))

            self.btn_toggle_log = tk.Button(
                log_header,
                text="▲ Show Activity Console",
                command=self.toggle_log_console,
                bg=THEME['bg'],
                fg=THEME['btn_blue'],
                activebackground=THEME['bg'],
                activeforeground=THEME['btn_blue'],
                font=THEME['font_card_lbl'],
                bd=0,
                cursor='hand2'
            )
            self.btn_toggle_log.pack(side='left')

            # Console Log Frame
            self.log_container = tk.Frame(main_frame, bg=THEME['card_bg'])

            self.log_area = scrolledtext.ScrolledText(
                self.log_container,
                wrap='word',
                height=12,
                font=THEME['font_code'],
                bg=THEME['card_bg'],
                fg=THEME['fg'],
                insertbackground=THEME['fg'],
                bd=0,
                highlightthickness=0
            )
            self.log_area.pack(fill='both', expand=True, padx=5, pady=5)
            self.log_area.config(state='disabled')

            # Configure console color tags
            self.log_area.tag_config('info', foreground=THEME['btn_blue'])
            self.log_area.tag_config('success', foreground=THEME['btn_green'])
            self.log_area.tag_config('warning', foreground=THEME['btn_orange'])
            self.log_area.tag_config('error', foreground=THEME['btn_red'])
            self.log_area.tag_config('bold', font=('Consolas', 10, 'bold'))

        def create_card(self, parent, title, var_main, var_sub, color):
            card = tk.Frame(parent, bg=THEME['card_bg'], padx=15, pady=12)
            tk.Label(card, text=title, font=THEME['font_card_lbl'], fg=color, bg=THEME['card_bg']).pack(anchor='w')
            tk.Label(card, textvariable=var_main, font=THEME['font_card_val'], fg=THEME['fg'], bg=THEME['card_bg']).pack(anchor='w')
            if var_sub:
                tk.Label(card, textvariable=var_sub, font=THEME['font_card_lbl'], fg=THEME['fg_dim'], bg=THEME['card_bg']).pack(anchor='w')
            return card

        def toggle_log_console(self):
            if self.logs_visible:
                self.log_container.pack_forget()
                self.btn_toggle_log.config(text="▲ Show Activity Console")
                self.logs_visible = False
            else:
                self.log_container.pack(fill='both', expand=True, pady=(0, 10))
                self.btn_toggle_log.config(text="▼ Hide Activity Console")
                self.logs_visible = True

        def draw_empty_chart(self):
            self.chart_canvas.delete("all")
            w = self.chart_canvas.winfo_width() or 800
            self.chart_canvas.create_rectangle(0, 0, w, 24, fill=THEME['btn_bg'], outline="")
            self.chart_canvas.create_text(w // 2, 12, text="No comparison data available", fill=THEME['fg_dim'], font=THEME['font_card_lbl'])

        def draw_chart(self, metrics):
            self.chart_canvas.delete("all")
            w = self.chart_canvas.winfo_width() or 800
            h = 24

            ident_cnt = metrics['identical_count']
            mod_cnt = metrics['modified_count']
            uniq_a = metrics['unique_a_count']
            uniq_b = metrics['unique_b_count']
            tot = ident_cnt + mod_cnt + uniq_a + uniq_b

            if tot == 0:
                self.draw_empty_chart()
                return

            p_ident = ident_cnt / tot
            p_mod = mod_cnt / tot
            p_ua = uniq_a / tot
            p_ub = uniq_b / tot

            x1 = 0
            x2 = x1 + int(w * p_ident)
            if x2 > x1:
                self.chart_canvas.create_rectangle(x1, 0, x2, h, fill=THEME['btn_blue'], outline="")

            x3 = x2 + int(w * p_mod)
            if x3 > x2:
                self.chart_canvas.create_rectangle(x2, 0, x3, h, fill=THEME['btn_red'], outline="")

            x4 = x3 + int(w * p_ua)
            if x4 > x3:
                self.chart_canvas.create_rectangle(x3, 0, x4, h, fill=THEME['btn_orange'], outline="")

            if w > x4:
                self.chart_canvas.create_rectangle(x4, 0, w, h, fill=THEME['btn_green'], outline="")

        def update_ram_usage(self):
            mem_bytes = get_current_memory_usage()
            mem_mb = mem_bytes / (1024 * 1024)
            self.perf_ram.set(f"{mem_mb:.1f} MB")

            perf_text = f"{self.perf_speed.get()} | {self.perf_elapsed.get()} | ETA: {self.perf_eta.get()} | RAM: {self.perf_ram.get()}"
            self.lbl_perf.config(text=perf_text)

            self.historic_chart.add_sample(
                read_hash_mbs=self.current_read_hash_mbs,
                file_proc_fps=self.current_file_proc_fps,
                ram_mb=mem_mb
            )

            self.root.after(1000, self.update_ram_usage)

        def browse_folder(self, string_var):
            folder = filedialog.askdirectory()
            if folder:
                string_var.set(folder)

        def disable_widgets(self):
            for w in self.widgets_to_disable:
                try:
                    w.config(state='disabled')
                except Exception:
                    pass

        def enable_widgets(self):
            for w in self.widgets_to_disable:
                try:
                    w.config(state='normal')
                except Exception:
                    pass

        def update_metrics(self, db_path):
            try:
                metrics = get_comparison_metrics(db_path)
                self.stat_a_count.set(f"{metrics['count_a']:,} files")
                self.stat_a_size.set(format_size(metrics['size_a']))
                self.stat_b_count.set(f"{metrics['count_b']:,} files")
                self.stat_b_size.set(format_size(metrics['size_b']))
                self.stat_identical.set(f"{metrics['identical_count']:,} files")
                self.stat_diff.set(f"{metrics['diff_total_count']:,} files")
                self.draw_chart(metrics)
            except Exception:
                pass

        def start_scan_a(self):
            self._start_scan_single('A', self.path_a.get().strip())

        def start_scan_b(self):
            self._start_scan_single('B', self.path_b.get().strip())

        def _start_scan_single(self, loc_id, target_dir):
            if not target_dir or not os.path.exists(target_dir):
                messagebox.showerror("Error", f"Location {loc_id} directory path does not exist.")
                return

            self.historic_chart.reset()
            self.current_read_hash_mbs = 0.0
            self.current_file_proc_fps = 0.0

            self.queue.put(('clear_log',))
            self.queue.put(('disable_controls',))
            self.queue.put(('status', f"Scanning Location {loc_id}..."))
            self.queue.put(('progress', 0))

            db_path = get_db_path(target_dir)

            def worker():
                def progress_cb(evt_type, *args):
                    if evt_type == 'info':
                        self.queue.put(('log', f"{args[0]}\n", 'info'))
                    elif evt_type == 'progress_update':
                        loc, count, errors, elapsed, speed = args
                        self.queue.put(('status', f"Location {loc}: {count:,} files indexed"))
                        self.queue.put(('perf_scan', speed, elapsed))
                    elif evt_type == 'complete':
                        loc, count, errors, elapsed, speed = args
                        self.queue.put(('log', f"Location {loc} scan complete! {count:,} files in {elapsed:.1f}s. Errors: {errors}\n", 'success'))
                        self.queue.put(('status', f"Scan Location {loc} complete"))
                        self.queue.put(('progress', 100))

                run_scan_location(target_dir, loc_id, db_path, progress_cb=progress_cb)
                self.update_metrics(db_path)
                self.queue.put(('enable_controls',))

            self.running_thread = threading.Thread(target=worker, daemon=True)
            self.running_thread.start()

        def start_compare(self):
            loc_a = self.path_a.get().strip()
            loc_b = self.path_b.get().strip()
            if not loc_a or not os.path.exists(loc_a) or not loc_b or not os.path.exists(loc_b):
                messagebox.showerror("Error", "Please specify valid paths for both Location A and Location B.")
                return

            self.historic_chart.reset()
            self.current_read_hash_mbs = 0.0
            self.current_file_proc_fps = 0.0

            self.queue.put(('clear_log',))
            self.queue.put(('disable_controls',))
            self.queue.put(('status', "Comparing files and hashes..."))
            self.queue.put(('progress', 0))

            db_path = get_db_path(loc_a)

            def worker():
                try:
                    self.hash_start_time = time.time()
                    self.hash_bytes_processed = 0

                    def progress_cb(evt_type, *args):
                        if evt_type == 'partial_start':
                            self.queue.put(('log', f"Computing partial hashes for {args[0]:,} files...\n", 'info'))
                        elif evt_type == 'partial_progress':
                            curr, tot = args
                            pct = int(100 * curr / tot) if tot > 0 else 0
                            self.queue.put(('progress', pct))
                            self.queue.put(('status', f"Partial Hashing: {curr}/{tot}"))
                        elif evt_type == 'full_start':
                            count, total_bytes = args
                            self.hash_total_bytes = total_bytes
                            self.queue.put(('log', f"Computing full hashes for {count:,} files ({format_size(total_bytes)})...\n", 'info'))
                        elif evt_type == 'full_progress':
                            curr, tot = args
                            pct = int(100 * curr / tot) if tot > 0 else 0
                            self.queue.put(('progress', pct))
                        elif evt_type == 'hash_chunk':
                            chunk_sz = args[0]
                            self.hash_bytes_processed += chunk_sz
                            elapsed = time.time() - self.hash_start_time
                            speed = self.hash_bytes_processed / elapsed if elapsed > 0 else 0
                            rem_bytes = max(0, self.hash_total_bytes - self.hash_bytes_processed)
                            eta = rem_bytes / speed if speed > 0 else 0
                            self.queue.put(('perf_hash', speed, elapsed, eta))
                        elif evt_type == 'report_start':
                            self.queue.put(('log', "\n=== LOCATION COMPARISON REPORT ===\n", 'bold'))
                        elif evt_type == 'report_data':
                            report = args[0]
                            ident = report['identical']
                            mod = report['modified']
                            ua = report['unique_a']
                            ub = report['unique_b']

                            self.queue.put(('log', f"Identical Files: {len(ident)}\n", 'success'))
                            self.queue.put(('log', f"Modified/Different Files: {len(mod)}\n", 'error'))
                            self.queue.put(('log', f"Unique to Location A: {len(ua)}\n", 'warning'))
                            self.queue.put(('log', f"Unique to Location B: {len(ub)}\n\n", 'warning'))

                            if mod:
                                self.queue.put(('log', "--- MODIFIED FILES DETAILS ---\n", 'bold'))
                                for m in mod:
                                    self.queue.put(('log', f" * {m['rel_path']}\n   Loc A: {format_size(m['size_a'])} | Loc B: {format_size(m['size_b'])}\n", 'error'))

                    run_compare(loc_a, loc_b, db_path, progress_cb=progress_cb)
                    self.queue.put(('status', "Comparison complete"))
                    self.queue.put(('progress', 100))
                    self.update_metrics(db_path)
                except Exception as e:
                    self.queue.put(('log', f"Error during comparison: {e}\n", 'error'))
                finally:
                    self.queue.put(('enable_controls',))

            self.running_thread = threading.Thread(target=worker, daemon=True)
            self.running_thread.start()

        def start_isolate(self):
            iso_dir = self.isolate_path.get().strip()
            loc_a = self.path_a.get().strip()
            if not iso_dir or not loc_a:
                messagebox.showerror("Error", "Please specify Location A and Isolation directory.")
                return

            src = self.iso_source.get()
            diff_type = self.iso_diff_type.get()

            self.queue.put(('clear_log',))
            self.queue.put(('disable_controls',))
            self.queue.put(('status', "Isolating differing files..."))
            self.queue.put(('progress', 0))

            db_path = get_db_path(loc_a)

            def worker():
                try:
                    def progress_cb(evt_type, *args):
                        if evt_type == 'isolate_info':
                            self.queue.put(('log', f"{args[0]}\n", 'info'))
                        elif evt_type == 'isolate_move':
                            src_p, dest_p, status = args
                            if status == 'moved':
                                self.queue.put(('log', f"  [MOVED] {src_p} -> {dest_p}\n", 'warning'))
                            else:
                                self.queue.put(('log', f"  [FAILED/SKIPPED] {src_p} ({status})\n", 'error'))
                        elif evt_type == 'isolate_complete':
                            moved, errors = args
                            self.queue.put(('log', f"\nIsolation complete! Moved {moved} files. Errors: {errors}\n", 'success'))
                            self.queue.put(('status', "Isolation completed"))
                            self.queue.put(('progress', 100))

                    run_isolate(iso_dir, db_path, source=src, diff_type=diff_type, progress_cb=progress_cb)
                    self.update_metrics(db_path)
                except Exception as e:
                    self.queue.put(('log', f"Error during isolation: {e}\n", 'error'))
                finally:
                    self.queue.put(('enable_controls',))

            self.running_thread = threading.Thread(target=worker, daemon=True)
            self.running_thread.start()

        def start_run_pipeline(self):
            loc_a = self.path_a.get().strip()
            loc_b = self.path_b.get().strip()
            iso_dir = self.isolate_path.get().strip()

            if not loc_a or not os.path.exists(loc_a) or not loc_b or not os.path.exists(loc_b) or not iso_dir:
                messagebox.showerror("Error", "Please specify valid paths for Location A, Location B, and Isolation Directory.")
                return

            self.historic_chart.reset()
            self.current_read_hash_mbs = 0.0
            self.current_file_proc_fps = 0.0

            self.queue.put(('clear_log',))
            self.queue.put(('disable_controls',))
            self.queue.put(('status', "Running full pipeline..."))

            db_path = get_db_path(loc_a)

            def worker():
                try:
                    self.queue.put(('log', ">>> STEP 1: Scanning Location A...\n", 'bold'))
                    run_scan_location(loc_a, 'A', db_path)

                    self.queue.put(('log', ">>> STEP 2: Scanning Location B...\n", 'bold'))
                    run_scan_location(loc_b, 'B', db_path)

                    self.queue.put(('log', ">>> STEP 3: Comparing Hashes...\n", 'bold'))
                    report = run_compare(loc_a, loc_b, db_path, run_quiet=True)

                    tot_diff = len(report['modified']) + len(report['unique_a']) + len(report['unique_b'])
                    self.queue.put(('log', f"Comparison Complete: {len(report['identical'])} identical, {tot_diff} differing/unique files.\n", 'info'))

                    if tot_diff > 0:
                        self.queue.put(('log', ">>> STEP 4: Isolating Differing Files...\n", 'bold'))
                        run_isolate(iso_dir, db_path, source=self.iso_source.get(), diff_type=self.iso_diff_type.get())
                    else:
                        self.queue.put(('log', "No differences found. Locations are identical.\n", 'success'))

                    self.queue.put(('status', "Pipeline complete"))
                    self.queue.put(('progress', 100))
                    self.update_metrics(db_path)
                except Exception as e:
                    self.queue.put(('log', f"Error in pipeline: {e}\n", 'error'))
                finally:
                    self.queue.put(('enable_controls',))

            self.running_thread = threading.Thread(target=worker, daemon=True)
            self.running_thread.start()

        def start_cleanup(self):
            loc_a = self.path_a.get().strip()
            if not loc_a:
                messagebox.showerror("Error", "Please select Location A path.")
                return

            self.queue.put(('clear_log',))
            self.queue.put(('disable_controls',))
            self.queue.put(('status', "Cleaning up database..."))

            db_path = get_db_path(loc_a)

            def worker():
                try:
                    run_cleanup(db_path)
                    self.queue.put(('log', "Cleanup completed successfully.\n", 'success'))
                    self.queue.put(('status', "Cleanup completed"))
                    self.queue.put(('progress', 100))
                    self.update_metrics(db_path)
                except Exception as e:
                    self.queue.put(('log', f"Error during cleanup: {e}\n", 'error'))
                finally:
                    self.queue.put(('enable_controls',))

            self.running_thread = threading.Thread(target=worker, daemon=True)
            self.running_thread.start()

        def start_reset(self):
            loc_a = self.path_a.get().strip()
            if not loc_a:
                messagebox.showerror("Error", "Please select Location A path.")
                return

            db_path = get_db_path(loc_a)
            if not messagebox.askyesno("Confirm Reset", f"Are you sure you want to clear the entire comparison index database at:\n{db_path}?"):
                return

            self.queue.put(('clear_log',))
            self.queue.put(('status', "Resetting database..."))

            try:
                run_reset(db_path)
                self.queue.put(('log', "Database reset completed successfully.\n", 'success'))
                self.queue.put(('status', "Database reset complete"))
                self.queue.put(('progress', 100))
                self.update_metrics(db_path)
            except Exception as e:
                self.queue.put(('log', f"Error resetting database: {e}\n", 'error'))

        def poll_queue(self):
            try:
                while True:
                    msg = self.queue.get_nowait()
                    cmd = msg[0]

                    if cmd == 'status':
                        self.status_var.set(msg[1])
                    elif cmd == 'progress':
                        self.progress_bar['value'] = msg[1]
                    elif cmd == 'perf_scan':
                        speed, elapsed = msg[1], msg[2]
                        self.current_file_proc_fps = speed
                        self.perf_speed.set(f"{speed:.0f} files/s")
                        self.perf_elapsed.set(f"{elapsed:.1f}s")
                        self.perf_eta.set("Scanning...")
                    elif cmd == 'perf_hash':
                        speed, elapsed, eta = msg[1], msg[2], msg[3]
                        self.current_read_hash_mbs = speed / (1024 * 1024)
                        self.perf_speed.set(f"{format_size(speed)}/s")
                        self.perf_elapsed.set(f"{elapsed:.1f}s")
                        self.perf_eta.set(format_time(eta))
                    elif cmd == 'log':
                        text, tag = msg[1], msg[2]
                        self.log_area.config(state='normal')
                        self.log_area.insert(tk.END, text, tag)
                        self.log_area.see(tk.END)
                        self.log_area.config(state='disabled')
                    elif cmd == 'clear_log':
                        self.log_area.config(state='normal')
                        self.log_area.delete('1.0', tk.END)
                        self.log_area.config(state='disabled')
                    elif cmd == 'enable_controls':
                        self.enable_widgets()
                    elif cmd == 'disable_controls':
                        self.disable_widgets()

                    self.queue.task_done()
            except queue.Empty:
                pass
            self.root.after(100, self.poll_queue)

    root = tk.Tk()

    style = ttk.Style(root)
    style.theme_use('clam')
    style.configure(
        "TProgressbar",
        thickness=12,
        troughcolor=THEME['btn_bg'],
        background=THEME['btn_blue'],
        bordercolor=THEME['bg'],
        lightcolor=THEME['btn_blue'],
        darkcolor=THEME['btn_blue']
    )

    app = LocationComparatorApp(root)
    root.mainloop()

def main():
    if len(sys.argv) == 1:
        launch_gui()
        return

    print_banner()

    parser = argparse.ArgumentParser(description="Compare two directory locations by file hash and isolate differences.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Scan A command
    scan_a_parser = subparsers.add_parser("scan_a", help="Scan Location A directory and index file metadata.")
    scan_a_parser.add_argument("path", help="Directory path for Location A")

    # Scan B command
    scan_b_parser = subparsers.add_parser("scan_b", help="Scan Location B directory and index file metadata.")
    scan_b_parser.add_argument("path", help="Directory path for Location B")

    # Scan command (scans both)
    scan_parser = subparsers.add_parser("scan", help="Scan both Location A and Location B.")
    scan_parser.add_argument("path_a", help="Directory path for Location A")
    scan_parser.add_argument("path_b", help="Directory path for Location B")

    # Compare command
    comp_parser = subparsers.add_parser("compare", help="Compare hashes between Location A and Location B.")
    comp_parser.add_argument("path_a", help="Directory path for Location A")
    comp_parser.add_argument("path_b", help="Directory path for Location B")

    # Isolate command
    iso_parser = subparsers.add_parser("isolate", help="Move differing files to isolation directory.")
    iso_parser.add_argument("path_a", help="Directory path for Location A")
    iso_parser.add_argument("target_dir", help="Isolation directory path")
    iso_parser.add_argument("--source", choices=['both', 'A', 'B'], default='both', help="Source location to isolate from")
    iso_parser.add_argument("--diff-type", choices=['all', 'modified', 'unique'], default='all', help="Type of difference to isolate")

    # Run command
    run_parser = subparsers.add_parser("run", help="Perform scan A, scan B, compare, and isolate pipeline.")
    run_parser.add_argument("path_a", help="Directory path for Location A")
    run_parser.add_argument("path_b", help="Directory path for Location B")
    run_parser.add_argument("target_dir", help="Isolation directory path")

    # Cleanup command
    cleanup_parser = subparsers.add_parser("cleanup", help="Remove non-existent files from database index.")
    cleanup_parser.add_argument("path", help="Directory path associated with scan")

    # Reset command
    reset_parser = subparsers.add_parser("reset", help="Clear comparison index database.")
    reset_parser.add_argument("path", help="Directory path associated with scan")

    args = parser.parse_args()

    try:
        ref_path = getattr(args, 'path_a', getattr(args, 'path', None))
        db_path = get_db_path(ref_path)

        if args.command == "scan_a":
            run_scan_location(args.path, 'A', db_path)
        elif args.command == "scan_b":
            run_scan_location(args.path, 'B', db_path)
        elif args.command == "scan":
            run_scan_location(args.path_a, 'A', db_path)
            run_scan_location(args.path_b, 'B', db_path)
        elif args.command == "compare":
            run_compare(args.path_a, args.path_b, db_path)
        elif args.command == "isolate":
            run_isolate(args.target_dir, db_path, source=args.source, diff_type=args.diff_type)
        elif args.command == "run":
            run_executor(args.path_a, args.path_b, args.target_dir)
        elif args.command == "cleanup":
            run_cleanup(db_path)
        elif args.command == "reset":
            run_reset(db_path)
    except KeyboardInterrupt:
        print(f"\n{Colors.RED}Process interrupted by user.{Colors.END}")
        sys.exit(1)

if __name__ == "__main__":
    main()
