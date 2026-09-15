#!/usr/bin/env python3
"""
Drive Sync - one-way file sync between two locations (drives).

Copies files that are missing from the destination. The destination is allowed
to hold extra files; nothing is ever deleted there. Whether a source file counts
as "already present" is decided by content hash (default), relative path, or both.
A dry-run plan is always produced first, and every copied file is re-hashed and
verified against the source after the copy.
"""
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
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

DB_NAME = "drive_sync_registry.db"

# Locations tracked inside the registry database
SRC = 'SRC'
DST = 'DST'

MATCH_MODES = ('hash', 'path', 'both')

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
  ___  ___  ___      _____      _
 |   \\| _ \\| _ \\    / __\\ \\    / /_ _ _ _  __ ___
 | |) |   /|   /    \\__ \\\\ \\/\\/ /| ' \\| ' \\/ _/ -_)
 |___/|_|_\\|_|_\\    |___/ \\_/\\_/ |_||_|_||_\\__\\___|
{Colors.END}{Colors.BLUE}  One-way, hash-verified sync of missing files between two drives.{Colors.END}
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

def list_drives():
    """
    Returns the currently mounted drive roots or mount points.
    On Windows: every logical drive letter that is ready (e.g. 'C:\\').
    Elsewhere: entries under /mnt, /media and /Volumes.
    """
    drives = []
    if sys.platform == 'win32':
        try:
            import ctypes
            bitmask = ctypes.windll.kernel32.GetLogicalDrives()
            for idx in range(26):
                if bitmask & (1 << idx):
                    root = f"{chr(65 + idx)}:\\"
                    if os.path.exists(root):
                        drives.append(root)
        except Exception:
            drives = []
    else:
        for base in ('/mnt', '/media', '/Volumes'):
            try:
                base_path = Path(base)
                if base_path.is_dir():
                    for entry in sorted(base_path.iterdir()):
                        if entry.is_dir():
                            drives.append(str(entry) + os.sep)
            except (OSError, PermissionError):
                continue
    if not drives:
        fallback = Path.cwd().anchor or str(Path.cwd())
        drives = [fallback]
    return drives

def get_db_path(target_path_str):
    """
    Returns the path to the database file.
    Attempts to store it at the root of the target drive.
    Falls back to the script's directory if write permissions are denied.
    """
    drive_root = None
    try:
        resolved_path = Path(target_path_str).resolve()
        drive_root = resolved_path.anchor  # e.g., "C:\\" on Windows, or "/" on Unix
        if not drive_root:
            drive_root = Path.cwd().anchor

        db_file = Path(drive_root) / DB_NAME

        # Test write access
        test_conn = sqlite3.connect(str(db_file))
        test_conn.execute("CREATE TABLE IF NOT EXISTS _write_test (id INTEGER PRIMARY KEY);")
        test_conn.execute("DROP TABLE _write_test;")
        test_conn.close()

        return str(db_file)
    except Exception:
        fallback_db = Path(__file__).parent.resolve() / DB_NAME
        if len(sys.argv) > 1:
            root_label = drive_root if drive_root else 'target drive root'
            print(f"{Colors.YELLOW}[WARNING] Write permission to '{root_label}' denied. DB will be stored at: {fallback_db}{Colors.END}")
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
    conn.execute("CREATE INDEX IF NOT EXISTS idx_loc_size ON files(location_id, size)")
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

def is_valid_hash(hash_value):
    return bool(hash_value) and not str(hash_value).startswith("ERROR_")

def _max_workers():
    return min(32, (os.cpu_count() or 4) * 2)

def run_scan_location(target_dir, location_id, db_path, progress_cb=None):
    """
    Indexes every file below target_dir into the registry under location_id,
    dropping rows for files that disappeared since the previous scan.
    """
    target_path = Path(target_dir).resolve()
    if not target_path.exists():
        message = f"Error: Path {target_path} does not exist."
        if progress_cb:
            progress_cb('error', message)
        else:
            print(f"{Colors.RED}{message}{Colors.END}")
        return False

    db_conn = sqlite3.connect(db_path)
    db_conn.execute("PRAGMA journal_mode=WAL;")
    init_db(db_conn)

    start_time = time.time()

    if progress_cb:
        progress_cb('info', f"Scanning {location_id}: {target_path}...")
        progress_cb('info', f"Using database file: {db_path}")
    else:
        print(f"{Colors.BLUE}Scanning {location_id}: {Colors.BOLD}{target_path}{Colors.END}{Colors.BLUE}...{Colors.END}")
        print(f"{Colors.BLUE}Using database file: {Colors.BOLD}{db_path}{Colors.END}")

    files_found = 0
    errors = 0
    batch = []

    # Temp table of relative paths seen in this pass, used to drop stale rows.
    db_conn.execute("CREATE TEMP TABLE IF NOT EXISTS scan_seen (rel_path TEXT PRIMARY KEY)")
    db_conn.execute("DELETE FROM scan_seen")

    def flush():
        nonlocal batch
        if not batch:
            return
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
        db_conn.executemany("INSERT OR IGNORE INTO scan_seen (rel_path) VALUES (?)",
                            [(row[1],) for row in batch])
        db_conn.commit()
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
                            flush()
                            elapsed = time.time() - start_time
                            speed = files_found / elapsed if elapsed > 0 else 0
                            if progress_cb:
                                progress_cb('progress_update', location_id, files_found, errors, elapsed, speed)
                            else:
                                print_status(f"Indexed {location_id}: {files_found:,} files... ({speed:.0f} files/s)")
                    except (OSError, PermissionError, ValueError):
                        errors += 1
                elif entry.is_dir():
                    recurse(entry.path)
        except (OSError, PermissionError):
            errors += 1

    recurse(target_path)
    flush()

    # Drop stale rows: files that used to exist under this location but are gone.
    cursor = db_conn.cursor()
    cursor.execute("DELETE FROM files WHERE location_id = ? AND rel_path NOT IN (SELECT rel_path FROM scan_seen)",
                   (location_id,))
    removed = cursor.rowcount or 0
    db_conn.commit()
    if removed > 0:
        if progress_cb:
            progress_cb('info', f"Dropped {removed:,} stale index entries for {location_id}.")
        else:
            print_status(f"Dropped {removed:,} stale entries for {location_id}.", end='\n')

    elapsed = time.time() - start_time
    speed = files_found / elapsed if elapsed > 0 else 0

    if progress_cb:
        progress_cb('complete', location_id, files_found, errors, elapsed, speed)
    else:
        print(f"\r{Colors.GREEN}Scan {location_id} complete! Indexed {files_found:,} files in {elapsed:.1f}s. (Skipped {errors:,} errors){Colors.END}")
    db_conn.close()
    return True

def hash_candidates(db_path, match='hash', progress_cb=None, run_quiet=False, throttle_sleep=0.0):
    """
    Two-stage hashing (partial 8KB, then full) limited to files that actually
    have a possible counterpart in the other location:

      * a file whose size appears on the other side, and
      * for the 'path'/'both' modes also files sharing a relative path.

    Files whose size exists on neither side cannot be present in the other
    location, so they are left unhashed and decided by size alone. That keeps a
    full-drive sync from reading every byte twice.
    """
    db_conn = sqlite3.connect(db_path)
    db_conn.execute("PRAGMA journal_mode=WAL;")
    init_db(db_conn)
    cursor = db_conn.cursor()

    def candidate_rows(location):
        other = DST if location == SRC else SRC
        clause = "(size IN (SELECT size FROM files WHERE location_id = ?))"
        params = [location, other]
        if match in ('path', 'both'):
            clause += " OR (rel_path IN (SELECT rel_path FROM files WHERE location_id = ?))"
            params.append(other)
        cursor.execute(f"""
            SELECT id, filepath, size, partial_hash FROM files
            WHERE location_id = ? AND full_hash IS NULL AND {clause}
        """, params)
        return cursor.fetchall()

    candidates = candidate_rows(SRC) + candidate_rows(DST)

    # Stage 1: partial hashes for every candidate (cheap, first 8KB only)
    to_partial = [row for row in candidates if not is_valid_hash(row[3]) and row[3] is None]

    if to_partial:
        if progress_cb:
            progress_cb('partial_start', len(to_partial))
        elif not run_quiet:
            print(f"{Colors.BLUE}Computing partial hashes for {len(to_partial):,} candidate files...{Colors.END}")

        completed = 0
        updates = []

        def _partial_task(row):
            p_hash = compute_partial_hash(row[1])
            if p_hash is None:
                p_hash = f"ERROR_{uuid.uuid4().hex}"
            return row[0], p_hash

        with ThreadPoolExecutor(max_workers=_max_workers()) as executor:
            futures = [executor.submit(_partial_task, row) for row in to_partial]
            for future in as_completed(futures):
                file_id, p_hash = future.result()
                updates.append((p_hash, file_id))
                completed += 1
                if len(updates) >= 200 or completed == len(to_partial):
                    db_conn.executemany("UPDATE files SET partial_hash = ? WHERE id = ?", updates)
                    db_conn.commit()
                    updates = []
                if progress_cb:
                    progress_cb('partial_progress', completed, len(to_partial))
                elif not run_quiet and (completed % 100 == 0 or completed == len(to_partial)):
                    print_progress(completed, len(to_partial), prefix="Partial Hashing", suffix=f"{completed}/{len(to_partial)}")
        if not run_quiet and not progress_cb:
            print()

    # Stage 2: full hashes for candidates whose (size, partial_hash) pair also
    # exists on the other side - only those can still turn out to be identical.
    cursor.execute(f"""
        SELECT c.id, c.filepath, c.size FROM files c
        WHERE c.location_id = ? AND c.full_hash IS NULL
          AND c.partial_hash IS NOT NULL AND c.partial_hash NOT LIKE 'ERROR_%'
          AND EXISTS (
              SELECT 1 FROM files o
              WHERE o.location_id = ? AND o.size = c.size AND o.partial_hash = c.partial_hash
          )
    """, (SRC, DST))
    src_full = cursor.fetchall()
    cursor.execute(f"""
        SELECT c.id, c.filepath, c.size FROM files c
        WHERE c.location_id = ? AND c.full_hash IS NULL
          AND c.partial_hash IS NOT NULL AND c.partial_hash NOT LIKE 'ERROR_%'
          AND EXISTS (
              SELECT 1 FROM files o
              WHERE o.location_id = ? AND o.size = c.size AND o.partial_hash = c.partial_hash
          )
    """, (DST, SRC))
    dst_full = cursor.fetchall()

    to_full = src_full + dst_full
    total_bytes = sum(row[2] or 0 for row in to_full)

    if to_full:
        if progress_cb:
            progress_cb('full_start', len(to_full), total_bytes)
        elif not run_quiet:
            print(f"{Colors.BLUE}Computing full hashes for {len(to_full):,} candidate files ({format_size(total_bytes)})...{Colors.END}")

        completed = 0
        updates = []

        def _full_task(row):
            chunk_cb = (lambda sz: progress_cb('hash_chunk', sz)) if progress_cb else None
            f_hash = compute_full_hash(row[1], chunk_cb=chunk_cb, throttle_sleep=throttle_sleep)
            if f_hash is None:
                f_hash = f"ERROR_{uuid.uuid4().hex}"
            return row[0], f_hash

        with ThreadPoolExecutor(max_workers=_max_workers()) as executor:
            futures = [executor.submit(_full_task, row) for row in to_full]
            for future in as_completed(futures):
                file_id, f_hash = future.result()
                updates.append((f_hash, file_id))
                completed += 1
                if len(updates) >= 50 or completed == len(to_full):
                    db_conn.executemany("UPDATE files SET full_hash = ? WHERE id = ?", updates)
                    db_conn.commit()
                    updates = []
                if progress_cb:
                    progress_cb('full_progress', completed, len(to_full))
                elif not run_quiet and (completed % 10 == 0 or completed == len(to_full)):
                    print_progress(completed, len(to_full), prefix="Full Hashing   ", suffix=f"{completed}/{len(to_full)}")
        if not run_quiet and not progress_cb:
            print()

    db_conn.close()

def build_plan(db_path, src_dir, dst_dir, match='hash', progress_cb=None, run_quiet=False, throttle_sleep=0.0):
    """
    Builds the sync plan (dry run) from the current registry contents.

    Returns a dict with the files to copy, the files already satisfied, the
    overwrites (same relative path, different content) and the destination
    extras (never touched).
    """
    if match not in MATCH_MODES:
        raise ValueError(f"Unknown match mode: {match}")

    hash_candidates(db_path, match=match, progress_cb=progress_cb, run_quiet=run_quiet, throttle_sleep=throttle_sleep)

    db_conn = sqlite3.connect(db_path)
    db_conn.execute("PRAGMA journal_mode=WAL;")
    init_db(db_conn)
    cursor = db_conn.cursor()

    cursor.execute("SELECT rel_path, filepath, size, mtime, full_hash FROM files WHERE location_id = ?", (SRC,))
    src_rows = cursor.fetchall()
    cursor.execute("SELECT rel_path, filepath, size, mtime, full_hash FROM files WHERE location_id = ?", (DST,))
    dst_rows = cursor.fetchall()
    db_conn.close()

    src_root = Path(src_dir).resolve() if src_dir else None
    dst_root = Path(dst_dir).resolve() if dst_dir else None

    dest_by_rel = {row[0]: row for row in dst_rows}
    # Content available anywhere in the destination, keyed by size + full hash.
    dest_content = {(row[2], row[4]) for row in dst_rows if is_valid_hash(row[4])}

    to_copy = []
    synced = []

    for rel_path, filepath, size, mtime, full_hash in src_rows:
        src_valid = is_valid_hash(full_hash)
        dest_row = dest_by_rel.get(rel_path)

        if match == 'path':
            needs_copy = dest_row is None
            overwrite = False
        elif match == 'both':
            if dest_row is None:
                needs_copy = True
                overwrite = False
            else:
                same_content = (
                    src_valid and is_valid_hash(dest_row[4])
                    and dest_row[2] == size and dest_row[4] == full_hash
                )
                needs_copy = not same_content
                overwrite = not same_content
        else:  # 'hash' - content anywhere in the destination counts as synced
            needs_copy = not (src_valid and (size, full_hash) in dest_content)
            overwrite = needs_copy and dest_row is not None

        if needs_copy:
            to_copy.append({
                'rel_path': rel_path,
                'src_path': filepath,
                'dest_path': str(dst_root / rel_path) if dst_root else rel_path,
                'size': size or 0,
                'mtime': mtime,
                'src_hash': full_hash if src_valid else None,
                'overwrite': overwrite,
            })
        else:
            synced.append({
                'rel_path': rel_path,
                'path': filepath,
                'size': size or 0,
                'matched_path': bool(dest_row is not None),
                'src_root': src_root,
            })

    src_rels = {row[0] for row in src_rows}
    dest_extras = []
    for rel_path, filepath, size, mtime, full_hash in dst_rows:
        if rel_path not in src_rels:
            dest_extras.append({
                'rel_path': rel_path,
                'path': filepath,
                'size': size or 0,
            })

    plan = {
        'match': match,
        'src_dir': str(src_root) if src_root else None,
        'dst_dir': str(dst_root) if dst_root else None,
        'to_copy': sorted(to_copy, key=lambda i: i['rel_path']),
        'synced': synced,
        'dest_extras': sorted(dest_extras, key=lambda i: i['rel_path']),
        'copy_count': len(to_copy),
        'copy_size': sum(i['size'] for i in to_copy),
        'overwrite_count': sum(1 for i in to_copy if i['overwrite']),
        'synced_count': len(synced),
        'synced_size': sum(i['size'] for i in synced),
        'copy_elsewhere_count': sum(1 for i in synced if not i['matched_path']),
        'src_count': len(src_rows),
        'src_size': sum((row[2] or 0) for row in src_rows),
        'dst_count': len(dst_rows),
        'dst_size': sum((row[2] or 0) for row in dst_rows),
        'dest_extras_count': len(dest_extras),
        'dest_extras_size': sum(i['size'] for i in dest_extras),
    }
    return plan

def report_plan(plan, limit=10):
    """Prints a human-readable dry-run plan."""
    print(f"\n{Colors.BOLD}{Colors.YELLOW}=== DRIVE SYNC PLAN ({plan['match']} match) ==={Colors.END}\n")
    print(f"  Source:      {plan['src_dir']}")
    print(f"  Destination: {plan['dst_dir']}")
    print(f"  Source files: {plan['src_count']:,} ({format_size(plan['src_size'])})")
    print(f"  Dest files:   {plan['dst_count']:,} ({format_size(plan['dst_size'])}) - extras are never deleted")
    print()
    print(f"{Colors.BOLD}{Colors.GREEN}[1] ALREADY SATISFIED: {plan['synced_count']:,} files "
          f"({format_size(plan['synced_size'])}){Colors.END}")
    if plan['copy_elsewhere_count']:
        print(f"    {plan['copy_elsewhere_count']:,} of them matched by content at a different path "
              f"(treated as present).")
    print()
    print(f"{Colors.BOLD}{Colors.CYAN}[2] TO COPY: {plan['copy_count']:,} files "
          f"({format_size(plan['copy_size'])}){Colors.END}")
    for item in plan['to_copy'][:limit]:
        tag = f"{Colors.YELLOW}(overwrite){Colors.END}" if item['overwrite'] else ''
        print(f"    + {item['rel_path']} ({format_size(item['size'])}) {tag}")
    if plan['copy_count'] > limit:
        print(f"    ... and {plan['copy_count'] - limit:,} more.")
    if plan['overwrite_count']:
        print(f"    {Colors.YELLOW}{plan['overwrite_count']:,} of these replace a destination file at the same path.{Colors.END}")
    print()
    print(f"{Colors.BOLD}{Colors.YELLOW}[3] DESTINATION EXTRAS (kept as-is): {plan['dest_extras_count']:,} files "
          f"({format_size(plan['dest_extras_size'])}){Colors.END}")
    for item in plan['dest_extras'][:limit]:
        print(f"    - {item['rel_path']} ({format_size(item['size'])})")
    if plan['dest_extras_count'] > limit:
        print(f"    ... and {plan['dest_extras_count'] - limit:,} more.")
    print()

def _copy_and_verify(src_file, dest_file, src_size, src_hash, progress_cb=None, throttle_sleep=0.0, attempts=2):
    """
    Copies src_file over dest_file and verifies the result by size and full
    hash. Returns (ok, dest_hash, error_message).
    """
    last_error = None
    dest_hash = None
    for attempt in range(attempts):
        try:
            dest_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(src_file), str(dest_file))
            dest_size = dest_file.stat().st_size
            if dest_size != src_size:
                last_error = f"size mismatch (source {src_size} bytes, copy {dest_size} bytes)"
                continue
            chunk_cb = (lambda sz: progress_cb('hash_chunk', sz)) if progress_cb else None
            dest_hash = compute_full_hash(str(dest_file), chunk_cb=chunk_cb, throttle_sleep=throttle_sleep)
            if dest_hash != src_hash:
                last_error = "hash mismatch after copy"
                continue
            return True, dest_hash, None
        except (OSError, PermissionError, shutil.Error) as exc:
            last_error = str(exc)
        if throttle_sleep > 0.0:
            time.sleep(throttle_sleep)
    return False, dest_hash, last_error or f"copy failed after {attempts} attempts"

def run_sync(src_dir, dst_dir, db_path, match='hash', plan=None, progress_cb=None, run_quiet=False,
             dry_run=False, assume_yes=False, confirm_cb=None, throttle_sleep=0.0):
    """
    One-way sync: copies every source file whose content is missing from the
    destination, verifying each copy by hash. Nothing in the destination is ever
    deleted. Without assume_yes/confirm_cb the plan is shown and confirmation is
    requested interactively.
    """
    src_path = Path(src_dir).resolve()
    dst_path = Path(dst_dir).resolve()

    if not src_path.exists():
        message = f"Error: Source path {src_path} does not exist."
        if progress_cb:
            progress_cb('error', message)
        else:
            print(f"{Colors.RED}{message}{Colors.END}")
        return None
    if not dst_path.exists():
        message = f"Error: Destination path {dst_path} does not exist."
        if progress_cb:
            progress_cb('error', message)
        else:
            print(f"{Colors.RED}{message}{Colors.END}")
        return None
    if src_path == dst_path:
        message = "Error: Source and destination are the same location."
        if progress_cb:
            progress_cb('error', message)
        else:
            print(f"{Colors.RED}{message}{Colors.END}")
        return None
    if dst_path == src_path or src_path in dst_path.parents:
        message = f"Error: Destination {dst_path} is inside the source {src_path}."
        if progress_cb:
            progress_cb('error', message)
        else:
            print(f"{Colors.RED}{message}{Colors.END}")
        return None

    if progress_cb:
        progress_cb('info', f"Source: {src_path}")
        progress_cb('info', f"Destination: {dst_path}")

    if plan is None:
        if progress_cb:
            progress_cb('info', "Building sync plan (scanning and hashing candidates)...")
        if not run_scan_location(str(src_path), SRC, db_path, progress_cb=progress_cb):
            return None
        if not run_scan_location(str(dst_path), DST, db_path, progress_cb=progress_cb):
            return None
        plan = build_plan(db_path, str(src_path), str(dst_path), match=match,
                          progress_cb=progress_cb, run_quiet=run_quiet, throttle_sleep=throttle_sleep)

    if progress_cb:
        progress_cb('plan', plan)
    elif not run_quiet:
        report_plan(plan)

    if dry_run:
        if progress_cb:
            progress_cb('dry_run_complete', plan)
        elif not run_quiet:
            print(f"{Colors.YELLOW}Dry run only - nothing was copied.{Colors.END}")
        return plan

    if plan['copy_count'] == 0:
        if progress_cb:
            progress_cb('sync_complete', 0, 0, 0, 0.0, plan)
        elif not run_quiet:
            print(f"{Colors.GREEN}Destination already contains every source file. Nothing to copy.{Colors.END}")
        empty_result = dict(plan)
        empty_result.update({'failures': [], 'copied_count': 0, 'overwritten_count': 0, 'bytes_copied': 0})
        return empty_result

    confirmed = True
    if not assume_yes:
        if confirm_cb:
            confirmed = confirm_cb(plan)
        else:
            try:
                answer = input(
                    f"{Colors.BOLD}Copy {plan['copy_count']:,} missing file(s) "
                    f"({format_size(plan['copy_size'])}) to '{dst_path}'? (y/n): {Colors.END}"
                ).strip().lower()
                confirmed = answer in ('y', 'yes')
            except KeyboardInterrupt:
                confirmed = False

    if not confirmed:
        if progress_cb:
            progress_cb('sync_cancelled', plan)
        elif not run_quiet:
            print(f"{Colors.YELLOW}Sync cancelled. Nothing was copied.{Colors.END}")
        return plan

    start_time = time.time()
    if progress_cb:
        progress_cb('sync_start', plan['copy_count'], plan['copy_size'])
    else:
        print(f"{Colors.BLUE}Copying {plan['copy_count']:,} file(s) ({format_size(plan['copy_size'])}) "
              f"to {Colors.BOLD}{dst_path}{Colors.END}{Colors.BLUE}...{Colors.END}")

    db_conn = sqlite3.connect(db_path)
    db_conn.execute("PRAGMA journal_mode=WAL;")
    init_db(db_conn)

    copied = 0
    overwritten = 0
    errors = 0
    bytes_copied = 0
    failures = []

    for index, item in enumerate(plan['to_copy'], start=1):
        src_file = Path(item['src_path'])
        dest_file = dst_path / item['rel_path']
        status = 'copied'
        try:
            if not src_file.exists():
                raise FileNotFoundError(f"source file disappeared: {src_file}")

            src_size = src_file.stat().st_size
            src_hash = item['src_hash']
            if not is_valid_hash(src_hash):
                # Not hashed yet (size alone decided it was missing): hash now so
                # the copy can be verified against a known-good value.
                src_hash = compute_full_hash(str(src_file))
                if not is_valid_hash(src_hash):
                    raise OSError("could not read the source file for verification")

            existed = dest_file.exists()
            ok, dest_hash, error = _copy_and_verify(
                src_file, dest_file, src_size, src_hash,
                progress_cb=progress_cb, throttle_sleep=throttle_sleep
            )
            if not ok:
                raise OSError(error)

            if existed:
                status = 'overwritten'
                overwritten += 1
            copied += 1
            bytes_copied += src_size

            # Keep the registry in step with what is now on the destination.
            dest_mtime = dest_file.stat().st_mtime
            db_conn.execute("""
                INSERT INTO files (location_id, rel_path, filepath, filename, size, mtime, partial_hash, full_hash)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(location_id, rel_path) DO UPDATE SET
                    filepath = excluded.filepath,
                    filename = excluded.filename,
                    size = excluded.size,
                    mtime = excluded.mtime,
                    partial_hash = excluded.partial_hash,
                    full_hash = excluded.full_hash
            """, (
                DST, item['rel_path'], str(dest_file), dest_file.name,
                src_size, dest_mtime, compute_partial_hash(str(dest_file)), dest_hash
            ))
            db_conn.commit()

            if progress_cb:
                progress_cb('sync_copy', str(src_file), str(dest_file), status, src_size)
                progress_cb('sync_verify', str(dest_file), True)
            elif not run_quiet:
                print(f"  {Colors.GREEN}[{'OVERWROTE' if existed else 'COPIED'}]{Colors.END} "
                      f"{item['rel_path']} ({format_size(src_size)}) verified")
        except Exception as exc:
            errors += 1
            status = 'failed'
            failures.append((item['rel_path'], str(exc)))
            if progress_cb:
                progress_cb('sync_copy', str(src_file), str(dest_file), status, item['size'], str(exc))
            elif not run_quiet:
                print(f"  {Colors.RED}[FAILED]{Colors.END} {item['rel_path']}: {exc}")

        if progress_cb:
            elapsed = time.time() - start_time
            speed = bytes_copied / elapsed if elapsed > 0 else 0
            progress_cb('sync_progress', index, plan['copy_count'], bytes_copied, speed)

    db_conn.close()
    elapsed = time.time() - start_time

    final_plan = dict(plan)
    final_plan['failures'] = failures
    final_plan['copied_count'] = copied
    final_plan['overwritten_count'] = overwritten
    final_plan['bytes_copied'] = bytes_copied

    if progress_cb:
        progress_cb('sync_complete', copied, bytes_copied, errors, elapsed, plan)
    elif not run_quiet:
        print(f"\n{Colors.GREEN}Sync complete! Copied {copied:,} file(s) ({format_size(bytes_copied)}) "
              f"in {format_time(elapsed)}. (Errors: {errors}){Colors.END}")
        if overwritten:
            print(f"{Colors.YELLOW}{overwritten:,} destination file(s) were replaced with the source version "
                  f"and verified.{Colors.END}")
        for rel_path, error in failures:
            print(f"  {Colors.RED}- {rel_path}: {error}{Colors.END}")
    return final_plan

def run_analyze(src_dir, dst_dir, db_path, match='hash', progress_cb=None, run_quiet=False, throttle_sleep=0.0):
    """Scans both locations and returns the dry-run sync plan."""
    return run_sync(src_dir, dst_dir, db_path, match=match, progress_cb=progress_cb,
                    run_quiet=run_quiet, dry_run=True, throttle_sleep=throttle_sleep)

def get_sync_metrics(db_path, match='hash'):
    """
    Returns the registry statistics used by the dashboard. The 'missing' figures
    are estimates derived from the indexed sizes/hashes, so they can only shrink
    after a hash pass has run.
    """
    db_conn = sqlite3.connect(db_path)
    db_conn.execute("PRAGMA journal_mode=WAL;")
    init_db(db_conn)
    cursor = db_conn.cursor()

    def one(sql, params=()):
        cursor.execute(sql, params)
        res = cursor.fetchone()
        return (res[0] or 0, res[1] or 0)

    src_count, src_size = one("SELECT COUNT(*), SUM(size) FROM files WHERE location_id = ?", (SRC,))
    dst_count, dst_size = one("SELECT COUNT(*), SUM(size) FROM files WHERE location_id = ?", (DST,))

    if match == 'path':
        missing_count, missing_size = one("""
            SELECT COUNT(*), SUM(size) FROM files
            WHERE location_id = ? AND rel_path NOT IN (
                SELECT rel_path FROM files WHERE location_id = ?
            )
        """, (SRC, DST))
    elif match == 'both':
        missing_count, missing_size = one("""
            SELECT COUNT(*), SUM(s.size) FROM files s
            WHERE s.location_id = ? AND (
                s.rel_path NOT IN (SELECT rel_path FROM files WHERE location_id = ?)
                OR EXISTS (
                    SELECT 1 FROM files d
                    WHERE d.location_id = ? AND d.rel_path = s.rel_path
                      AND (d.size != s.size
                           OR d.full_hash IS NULL OR d.full_hash LIKE 'ERROR_%'
                           OR s.full_hash IS NULL OR s.full_hash LIKE 'ERROR_%'
                           OR d.full_hash != s.full_hash)
                )
            )
        """, (SRC, DST, DST))
    else:
        missing_count, missing_size = one("""
            SELECT COUNT(*), SUM(s.size) FROM files s
            WHERE s.location_id = ? AND NOT EXISTS (
                SELECT 1 FROM files d
                WHERE d.location_id = ? AND d.size = s.size
                  AND d.full_hash IS NOT NULL AND d.full_hash NOT LIKE 'ERROR_%'
                  AND d.full_hash = s.full_hash
            )
        """, (SRC, DST))

    extra_count, extra_size = one("""
        SELECT COUNT(*), SUM(size) FROM files
        WHERE location_id = ? AND rel_path NOT IN (
            SELECT rel_path FROM files WHERE location_id = ?
        )
    """, (DST, SRC))

    db_conn.close()
    return {
        'src_count': src_count,
        'src_size': src_size,
        'dst_count': dst_count,
        'dst_size': dst_size,
        'missing_count': missing_count,
        'missing_size': missing_size,
        'extra_count': extra_count,
        'extra_size': extra_size,
        'match': match,
    }

def run_cleanup(db_path):
    db_conn = sqlite3.connect(db_path)
    db_conn.execute("PRAGMA journal_mode=WAL;")
    init_db(db_conn)
    cursor = db_conn.cursor()
    cursor.execute("SELECT filepath FROM files")
    all_files = [r[0] for r in cursor.fetchall()]

    if not all_files:
        print(f"{Colors.GREEN}Database is empty. Nothing to clean.{Colors.END}")
        db_conn.close()
        return 0

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
    return len(missing)

def run_reset(db_path):
    db_conn = sqlite3.connect(db_path)
    db_conn.execute("DROP TABLE IF EXISTS files")
    db_conn.commit()
    db_conn.close()
    if len(sys.argv) > 1:
        print(f"{Colors.GREEN}Database reset completed.{Colors.END}")

def launch_gui():
    try:
        import tkinter as tk
        from tkinter import messagebox, filedialog, scrolledtext
        from tkinter import ttk
        from tkinter import font as tkfont
    except ImportError:
        print("Error: Tkinter library is not installed or graphical interface is not supported on this system.")
        print("Please run this tool in CLI mode by adding arguments.")
        sys.exit(1)

    MATCH_LABELS = {
        'hash': "Content exists anywhere in the destination",
        'path': "Same relative path exists",
        'both': "Same relative path with identical content",
    }

    class DriveSyncApp:
        """
        Windows-native dashboard built from themed ttk widgets: menu bar, group
        boxes, native progress bars and a collapsible log pane.
        """

        def __init__(self, root):
            self.root = root
            root.title("Drive Sync")
            root.geometry("920x700")
            root.minsize(860, 620)

            # Adopt the native Windows visual style and system fonts.
            self.style = ttk.Style(root)
            available_themes = self.style.theme_names()
            for native_theme in ('vista', 'winnative', 'xpnative', 'aqua'):
                if native_theme in available_themes:
                    self.style.theme_use(native_theme)
                    break

            base_font = tkfont.nametofont("TkDefaultFont")
            self.bold_font = base_font.copy()
            self.bold_font.configure(weight="bold")
            self.mono_font = tkfont.nametofont("TkFixedFont")

            # Paths and modes
            self.source_path = tk.StringVar(value=str(Path.cwd()))
            self.dest_path = tk.StringVar(value=str(Path.cwd()))
            self.match_mode = tk.StringVar(value='hash')
            self.perf_mode = tk.StringVar(value='balanced')
            self.show_log = tk.BooleanVar(value=False)

            # Status strings
            self.status_var = tk.StringVar(value="Ready")
            self.task_var = tk.StringVar(value="")

            self.var_src_files = tk.StringVar(value="0")
            self.var_src_size = tk.StringVar(value="0.00 B")
            self.var_dst_files = tk.StringVar(value="0")
            self.var_dst_size = tk.StringVar(value="0.00 B")
            self.var_present = tk.StringVar(value="0")
            self.var_copy = tk.StringVar(value="0")
            self.var_copy_size = tk.StringVar(value="0.00 B")
            self.var_extras = tk.StringVar(value="0")
            self.var_extra_size = tk.StringVar(value="0.00 B")
            self.var_coverage = tk.StringVar(value="0.0%")
            self.var_speed = tk.StringVar(value="0 B/s")
            self.var_elapsed = tk.StringVar(value="0.0s")
            self.var_eta = tk.StringVar(value="--")
            self.var_ram = tk.StringVar(value="0.0 MB")

            self.queue = queue.Queue()
            self.running_thread = None
            self.controls = []
            self.current_plan = None
            self.pending_sync_after_plan = False
            self.run_start_time = 0.0
            self.bytes_done = 0
            self.total_bytes_expected = 0

            self.match_mode.trace_add('write', lambda *a: self.load_path_data())

            self.build_menu()
            self.build_ui()
            self.poll_queue()
            self.update_ram_usage()
            self.load_path_data()

        # ---------- widget helpers ----------
        def _control(self, widget):
            """Registers a widget so it can be disabled while a task runs."""
            self.controls.append(widget)
            return widget

        def _set_enabled(self, enabled):
            state = 'normal' if enabled else 'disabled'
            for widget in self.controls:
                try:
                    widget.configure(state=state)
                except tk.TclError:
                    pass

        def _stat(self, parent, row, column, label, variable):
            ttk.Label(parent, text=label).grid(row=row, column=column * 2, sticky='w', padx=(0, 6), pady=3)
            ttk.Label(parent, textvariable=variable, font=self.bold_font).grid(
                row=row, column=column * 2 + 1, sticky='w', padx=(0, 24), pady=3)

        # ---------- menus ----------
        def build_menu(self):
            menubar = tk.Menu(self.root)

            file_menu = tk.Menu(menubar, tearoff=0)
            file_menu.add_command(label="Analyze (dry run)...", command=self.start_analyze)
            file_menu.add_command(label="Sync missing files...", command=self.start_sync)
            file_menu.add_separator()
            file_menu.add_command(label="Exit", command=self.root.destroy)
            menubar.add_cascade(label="File", menu=file_menu)

            tools_menu = tk.Menu(menubar, tearoff=0)
            tools_menu.add_command(label="Refresh drive list", command=self.refresh_drives)
            tools_menu.add_command(label="Prune index database", command=self.start_prune)
            tools_menu.add_command(label="Reset index database", command=self.start_reset)
            menubar.add_cascade(label="Tools", menu=tools_menu)

            help_menu = tk.Menu(menubar, tearoff=0)
            help_menu.add_command(label="About Drive Sync", command=self.show_about)
            menubar.add_cascade(label="Help", menu=help_menu)

            self.root.config(menu=menubar)

        def show_about(self):
            messagebox.showinfo(
                "About Drive Sync",
                "Drive Sync copies the files a destination is missing.\n\n"
                "The destination keeps every extra file it already holds - nothing\n"
                "is ever deleted. A plan is shown before anything is copied, and\n"
                "each copied file is re-hashed and verified against the source."
            )

        # ---------- layout ----------
        def build_ui(self):
            status_frame = ttk.Frame(self.root, padding=(10, 4))
            status_frame.pack(side='bottom', fill='x')
            ttk.Separator(self.root, orient='horizontal').pack(side='bottom', fill='x')
            ttk.Label(status_frame, textvariable=self.status_var).pack(side='left')
            ttk.Label(status_frame, textvariable=self.task_var).pack(side='right')

            main = ttk.Frame(self.root, padding=10)
            main.pack(fill='both', expand=True)
            main.columnconfigure(0, weight=1)

            # Locations
            locations = ttk.LabelFrame(main, text=" Locations ", padding=10)
            locations.grid(row=0, column=0, sticky='ew')
            locations.columnconfigure(1, weight=1)

            ttk.Label(locations, text="Source drive or folder:").grid(row=0, column=0, sticky='w', pady=(0, 6))
            ttk.Entry(locations, textvariable=self.source_path).grid(row=0, column=1, sticky='ew', padx=6, pady=(0, 6))
            self.src_drive_cb = ttk.Combobox(locations, values=list_drives(), width=8, state='readonly')
            self.src_drive_cb.grid(row=0, column=2, padx=(0, 6), pady=(0, 6))
            self.src_drive_cb.bind('<<ComboboxSelected>>', lambda e: self.source_path.set(self.src_drive_cb.get()))
            self._control(ttk.Button(locations, text="Browse...",
                                     command=lambda: self.browse(self.source_path))).grid(row=0, column=3, pady=(0, 6))

            ttk.Label(locations, text="Destination drive or folder:").grid(row=1, column=0, sticky='w', pady=(0, 6))
            ttk.Entry(locations, textvariable=self.dest_path).grid(row=1, column=1, sticky='ew', padx=6, pady=(0, 6))
            self.dst_drive_cb = ttk.Combobox(locations, values=list_drives(), width=8, state='readonly')
            self.dst_drive_cb.grid(row=1, column=2, padx=(0, 6), pady=(0, 6))
            self.dst_drive_cb.bind('<<ComboboxSelected>>', lambda e: self.dest_path.set(self.dst_drive_cb.get()))
            self._control(ttk.Button(locations, text="Browse...",
                                     command=lambda: self.browse(self.dest_path))).grid(row=1, column=3, pady=(0, 6))

            ttk.Label(locations,
                      text="The destination keeps its extra files: nothing there is ever deleted or moved.").grid(
                row=2, column=0, columnspan=4, sticky='w')

            # Presence rule
            presence = ttk.LabelFrame(main, text=" Copy a source file when ", padding=10)
            presence.grid(row=1, column=0, sticky='ew', pady=(10, 0))
            for idx, mode in enumerate(MATCH_MODES):
                self._control(ttk.Radiobutton(presence, text=MATCH_LABELS[mode], value=mode,
                                              variable=self.match_mode)).grid(row=0, column=idx, sticky='w', padx=(0, 18))
            presence.columnconfigure(len(MATCH_MODES), weight=1)
            self._control(ttk.Button(presence, text="Refresh drives",
                                     command=self.refresh_drives)).grid(row=0, column=len(MATCH_MODES), sticky='e')

            # Sync status
            results = ttk.LabelFrame(main, text=" Sync status ", padding=10)
            results.grid(row=2, column=0, sticky='ew', pady=(10, 0))
            results.columnconfigure(1, weight=1)

            stats = ttk.Frame(results)
            stats.grid(row=0, column=0, columnspan=3, sticky='ew')
            self._stat(stats, 0, 0, "Source files:", self.var_src_files)
            self._stat(stats, 0, 1, "Source size:", self.var_src_size)
            self._stat(stats, 0, 2, "Destination files:", self.var_dst_files)
            self._stat(stats, 0, 3, "Destination size:", self.var_dst_size)
            self._stat(stats, 1, 0, "Already present:", self.var_present)
            self._stat(stats, 1, 1, "To copy:", self.var_copy)
            self._stat(stats, 1, 2, "Bytes to copy:", self.var_copy_size)
            self._stat(stats, 1, 3, "Destination extras:", self.var_extras)

            ttk.Label(results, text="Source coverage:").grid(row=1, column=0, sticky='w', pady=(10, 0))
            self.coverage_bar = ttk.Progressbar(results, mode='determinate', maximum=100.0)
            self.coverage_bar.grid(row=1, column=1, sticky='ew', padx=6, pady=(10, 0))
            ttk.Label(results, textvariable=self.var_coverage, font=self.bold_font).grid(
                row=1, column=2, sticky='w', pady=(10, 0))

            ttk.Label(results, text="Current task:").grid(row=2, column=0, sticky='w', pady=(6, 0))
            self.task_bar = ttk.Progressbar(results, mode='determinate', maximum=100.0)
            self.task_bar.grid(row=2, column=1, sticky='ew', padx=6, pady=(6, 0))

            # Performance
            performance = ttk.LabelFrame(main, text=" Performance ", padding=10)
            performance.grid(row=3, column=0, sticky='ew', pady=(10, 0))
            perf_stats = ttk.Frame(performance)
            perf_stats.grid(row=0, column=0, sticky='ew')
            self._stat(perf_stats, 0, 0, "Speed:", self.var_speed)
            self._stat(perf_stats, 0, 1, "Elapsed:", self.var_elapsed)
            self._stat(perf_stats, 0, 2, "Remaining:", self.var_eta)
            self._stat(perf_stats, 0, 3, "Process RAM:", self.var_ram)

            mode_row = ttk.Frame(performance)
            mode_row.grid(row=1, column=0, sticky='w', pady=(8, 0))
            ttk.Label(mode_row, text="System resource mode:").pack(side='left', padx=(0, 12))
            for mode, label in (('max', "Maximum speed"), ('balanced', "Balanced"), ('eco', "Eco (background)")):
                self._control(ttk.Radiobutton(mode_row, text=label, value=mode,
                                              variable=self.perf_mode)).pack(side='left', padx=(0, 14))

            # Actions
            actions = ttk.Frame(main)
            actions.grid(row=4, column=0, sticky='ew', pady=(12, 0))
            self._control(ttk.Button(actions, text="Analyze (dry run)",
                                     command=self.start_analyze)).pack(side='left')
            self._control(ttk.Button(actions, text="Sync missing files",
                                     command=self.start_sync)).pack(side='left', padx=(8, 0))
            self._control(ttk.Button(actions, text="Prune index",
                                     command=self.start_prune)).pack(side='left', padx=(8, 0))
            self._control(ttk.Button(actions, text="Reset index",
                                     command=self.start_reset)).pack(side='left', padx=(8, 0))
            # Kept out of self.controls: the log stays toggleable while a task runs.
            ttk.Checkbutton(actions, text="Show detailed log", variable=self.show_log,
                            command=self.toggle_logs).pack(side='right')

            # Log pane (hidden until requested)
            self.log_frame = ttk.LabelFrame(main, text=" Log ", padding=6)
            self.log_frame.grid(row=5, column=0, sticky='nsew', pady=(10, 0))
            self.log_frame.columnconfigure(0, weight=1)
            self.log_frame.rowconfigure(0, weight=1)
            main.rowconfigure(5, weight=1)

            self.log_area = scrolledtext.ScrolledText(self.log_frame, wrap='word', height=10,
                                                      font=self.mono_font, relief='solid', borderwidth=1)
            self.log_area.grid(row=0, column=0, sticky='nsew')
            self.log_area.tag_config('error', foreground='#b00020')
            self.log_area.tag_config('ok', foreground='#0a6b2e')
            self.log_area.tag_config('meta', foreground='#4a4a4a')
            self.log_area.configure(state='disabled')
            self.log_frame.grid_remove()

        # ---------- small actions ----------
        def browse(self, variable):
            chosen = filedialog.askdirectory(initialdir=variable.get() or None)
            if chosen:
                variable.set(chosen)
                self.load_path_data()

        def refresh_drives(self):
            drives = list_drives()
            self.src_drive_cb.configure(values=drives)
            self.dst_drive_cb.configure(values=drives)
            self.log("Drive list refreshed.", 'meta')

        def toggle_logs(self):
            if self.show_log.get():
                self.log_frame.grid()
            else:
                self.log_frame.grid_remove()

        def log(self, message, tag=None):
            self.log_area.configure(state='normal')
            self.log_area.insert('end', f"{message}\n", tag or ())
            self.log_area.see('end')
            self.log_area.configure(state='disabled')

        # ---------- status ----------
        def load_path_data(self):
            src = self.source_path.get().strip()
            if not src:
                return
            try:
                self.update_metrics(get_db_path(src))
            except Exception:
                pass

        def update_metrics(self, db_path):
            metrics = get_sync_metrics(db_path, match=self.match_mode.get())
            self.var_src_files.set(f"{metrics['src_count']:,}")
            self.var_src_size.set(format_size(metrics['src_size']))
            self.var_dst_files.set(f"{metrics['dst_count']:,}")
            self.var_dst_size.set(format_size(metrics['dst_size']))
            self.var_present.set(f"{max(0, metrics['src_count'] - metrics['missing_count']):,}")
            self.var_copy.set(f"{metrics['missing_count']:,}")
            self.var_copy_size.set(format_size(metrics['missing_size']))
            self.var_extras.set(f"{metrics['extra_count']:,}")
            self.var_extra_size.set(format_size(metrics['extra_size']))

            coverage = 0.0
            if metrics['src_count']:
                coverage = 100.0 * (metrics['src_count'] - metrics['missing_count']) / metrics['src_count']
            coverage = max(0.0, min(100.0, coverage))
            self.coverage_bar['value'] = coverage
            self.var_coverage.set(f"{coverage:.1f}%")

        def update_ram_usage(self):
            self.var_ram.set(f"{get_current_memory_usage() / (1024 * 1024):.1f} MB")
            self.root.after(2000, self.update_ram_usage)

        # ---------- worker plumbing ----------
        def start_worker(self, target, *args, **kwargs):
            if self.running_thread and self.running_thread.is_alive():
                messagebox.showinfo("Drive Sync", "A task is already running. Please wait for it to finish.")
                return
            self.task_bar['value'] = 0
            self.run_start_time = time.time()
            self.bytes_done = 0
            self.total_bytes_expected = 0
            self._set_enabled(False)
            self.status_var.set("Working...")
            self.running_thread = threading.Thread(target=target, args=args, kwargs=kwargs, daemon=True)
            self.running_thread.start()

        def _worker_analyze(self, db_path):
            set_process_priority(self.perf_mode.get())
            try:
                run_analyze(self.source_path.get().strip(), self.dest_path.get().strip(), db_path,
                            match=self.match_mode.get(), progress_cb=self._cb, run_quiet=True)
            except Exception as exc:
                self.queue.put(('error', f"Analyze failed: {exc}"))
            finally:
                self.queue.put(('task_done',))

        def _worker_copy(self, db_path, plan):
            set_process_priority(self.perf_mode.get())
            try:
                run_sync(self.source_path.get().strip(), self.dest_path.get().strip(), db_path,
                         match=self.match_mode.get(), plan=plan, progress_cb=self._cb,
                         run_quiet=True, assume_yes=True)
            except Exception as exc:
                self.queue.put(('error', f"Sync failed: {exc}"))
            finally:
                self.queue.put(('task_done',))

        def _worker_prune(self, db_path):
            try:
                removed = run_cleanup(db_path)
                self.queue.put(('info', f"Pruned {removed:,} stale entries from the index."))
            except Exception as exc:
                self.queue.put(('error', f"Prune failed: {exc}"))
            finally:
                self.queue.put(('task_done',))

        def _worker_reset(self, db_path):
            try:
                run_reset(db_path)
                self.queue.put(('info', "Index database reset."))
            except Exception as exc:
                self.queue.put(('error', f"Reset failed: {exc}"))
            finally:
                self.queue.put(('task_done',))

        def _cb(self, *args):
            """Progress callback running on a worker thread; forwards into the queue."""
            self.queue.put(args)

        # ---------- main actions ----------
        def _validate_paths(self, src, dst):
            if not src or not os.path.isdir(src):
                messagebox.showerror("Drive Sync", "Please pick an existing source drive or folder.")
                return False
            if not dst or not os.path.isdir(dst):
                messagebox.showerror("Drive Sync", "Please pick an existing destination drive or folder.")
                return False
            if Path(src).resolve() == Path(dst).resolve():
                messagebox.showerror("Drive Sync", "Source and destination must be different locations.")
                return False
            if Path(src).resolve() in Path(dst).resolve().parents:
                messagebox.showerror("Drive Sync", "The destination cannot be inside the source.")
                return False
            return True

        def start_analyze(self):
            src = self.source_path.get().strip()
            dst = self.dest_path.get().strip()
            if not self._validate_paths(src, dst):
                return
            self.pending_sync_after_plan = False
            self.log(f"Analyzing '{src}' -> '{dst}' (dry run, {self.match_mode.get()} match)...", 'meta')
            self.start_worker(self._worker_analyze, get_db_path(dst))

        def start_sync(self):
            src = self.source_path.get().strip()
            dst = self.dest_path.get().strip()
            if not self._validate_paths(src, dst):
                return
            if self.current_plan is None:
                # Dry run first: build the plan, show it, then ask before copying.
                self.pending_sync_after_plan = True
                self.log(f"Building sync plan for '{src}' -> '{dst}'...", 'meta')
                self.start_worker(self._worker_analyze, get_db_path(dst))
                return
            self._confirm_and_copy()

        def _confirm_and_copy(self):
            plan = self.current_plan
            if not plan or plan['copy_count'] == 0:
                messagebox.showinfo("Drive Sync", "The destination already contains every source file.")
                return
            summary = (
                f"Copy {plan['copy_count']:,} file(s) ({format_size(plan['copy_size'])})?\n\n"
                f"Source:      {plan['src_dir']}\n"
                f"Destination: {plan['dst_dir']}\n\n"
                f"Already present:     {plan['synced_count']:,}\n"
                f"To copy:             {plan['copy_count']:,}\n"
                f"Replacing same path: {plan['overwrite_count']:,}\n"
                f"Destination extras:  {plan['dest_extras_count']:,} (left untouched)\n\n"
                f"Every copy is re-hashed and verified afterwards."
            )
            if not messagebox.askyesno("Confirm sync", summary):
                self.log("Sync cancelled by user.", 'meta')
                return
            db_path = get_db_path(self.dest_path.get().strip())
            self.log(f"Copying {plan['copy_count']:,} file(s)...", 'meta')
            self.start_worker(self._worker_copy, db_path, plan)

        def start_prune(self):
            src = self.source_path.get().strip()
            if not src:
                messagebox.showerror("Drive Sync", "Pick a source to locate the index database.")
                return
            self.start_worker(self._worker_prune, get_db_path(src))

        def start_reset(self):
            src = self.source_path.get().strip()
            if not src:
                messagebox.showerror("Drive Sync", "Pick a source to locate the index database.")
                return
            if not messagebox.askyesno("Drive Sync", "Drop the index database? Files on disk are never touched."):
                return
            self.current_plan = None
            self.start_worker(self._worker_reset, get_db_path(src))

        # ---------- queue pumping ----------
        def poll_queue(self):
            try:
                while True:
                    self.handle_event(self.queue.get_nowait())
            except queue.Empty:
                pass
            self.root.after(120, self.poll_queue)

        def _set_task_progress(self, done, total):
            if total:
                self.task_bar['value'] = max(0.0, min(100.0, 100.0 * done / total))

        def handle_event(self, event):
            kind = event[0]
            payload = event[1:]

            if kind == 'info':
                self.log(payload[0], 'meta')
                self.status_var.set(payload[0][:110])
            elif kind == 'error':
                self.log(f"[ERROR] {payload[0]}", 'error')
                self.status_var.set("Error")
            elif kind == 'progress_update':
                location_id, files, errors, elapsed, speed = payload
                self.status_var.set(f"Scanning {location_id}: {files:,} files ({speed:,.0f} files/s)")
                self.task_var.set(f"Scanning {location_id}")
                self.var_elapsed.set(format_time(elapsed))
                self.var_speed.set(f"{speed:,.0f} files/s")
            elif kind == 'partial_start':
                self.log(f"Partial-hashing {payload[0]:,} candidate files...", 'meta')
                self.status_var.set(f"Partial hashing {payload[0]:,} files")
            elif kind == 'partial_progress':
                done, total = payload
                self._set_task_progress(done, total)
                self.var_elapsed.set(format_time(time.time() - self.run_start_time))
            elif kind == 'full_start':
                count, total_bytes = payload
                self.total_bytes_expected = total_bytes
                self.log(f"Full-hashing {count:,} candidates ({format_size(total_bytes)})...", 'meta')
                self.status_var.set(f"Full hashing {count:,} files")
            elif kind == 'hash_chunk':
                self.bytes_done += payload[0]
                elapsed = max(0.001, time.time() - self.run_start_time)
                mbs = (self.bytes_done / (1024 * 1024)) / elapsed
                self.var_speed.set(f"{mbs:,.1f} MB/s")
                if not self.total_bytes_expected:
                    self.total_bytes_expected = self.bytes_done
                self._set_task_progress(self.bytes_done, self.total_bytes_expected)
                self.var_eta.set(format_time(max(0.0, (self.total_bytes_expected - self.bytes_done) / 1048576) / max(mbs, 0.01)))
                self.task_var.set(f"Hashed {format_size(self.bytes_done)}")
            elif kind == 'full_progress':
                done, total = payload
                self._set_task_progress(done, total)
                self.task_var.set(f"Hashing {done:,}/{total:,} files")
            elif kind == 'plan':
                plan = payload[0]
                self.current_plan = plan
                self._report_plan_in_log(plan)
                if self.pending_sync_after_plan:
                    self.pending_sync_after_plan = False
                    self._confirm_and_copy()
            elif kind == 'sync_start':
                total_files, total_bytes = payload
                self.total_bytes_expected = total_bytes
                self.status_var.set(f"Copying {total_files:,} file(s)")
                self.log(f"Sync started: {total_files:,} file(s) / {format_size(total_bytes)} to copy.", 'meta')
            elif kind == 'sync_copy':
                src_path, dest_path, status, size = payload[:4]
                error = payload[4] if len(payload) > 4 else None
                if status == 'copied':
                    self.log(f"  Copied      {dest_path} ({format_size(size)})", 'ok')
                elif status == 'overwritten':
                    self.log(f"  Replaced    {dest_path} ({format_size(size)})", 'ok')
                else:
                    self.log(f"  Failed      {src_path}: {error}", 'error')
            elif kind == 'sync_progress':
                index, total, bytes_copied, speed = payload
                self._set_task_progress(index, total)
                elapsed = max(0.001, time.time() - self.run_start_time)
                self.var_speed.set(f"{speed / (1024 * 1024):,.1f} MB/s")
                self.var_elapsed.set(format_time(elapsed))
                if speed > 0:
                    self.var_eta.set(format_time(max(0.0, self.total_bytes_expected - bytes_copied) / speed))
                self.status_var.set(f"Copying {index:,}/{total:,} files")
            elif kind == 'sync_complete':
                copied, bytes_copied, errors, elapsed = payload[:4]
                self.log(f"Sync complete: {copied:,} file(s) copied ({format_size(bytes_copied)}) "
                         f"in {format_time(elapsed)}. Errors: {errors:,}", 'ok')
                self.status_var.set(f"Done - {copied:,} copied, {errors:,} error(s)")
                self.task_bar['value'] = 100
            elif kind == 'sync_cancelled':
                self.log("Sync cancelled. Nothing was copied.", 'meta')
            elif kind == 'dry_run_complete':
                self.log("Dry run finished - nothing was copied.", 'meta')
                self.status_var.set("Dry run complete")
                self.task_bar['value'] = 0
            elif kind == 'complete':
                location_id, files, errors, elapsed, speed = payload
                self.log(f"Scan {location_id} finished: {files:,} files indexed in {format_time(elapsed)} "
                         f"(errors {errors:,}).", 'meta')
            elif kind == 'task_done':
                self._set_enabled(True)
                if self.status_var.get() == "Working...":
                    self.status_var.set("Ready")
                try:
                    self.update_metrics(get_db_path(self.source_path.get().strip()))
                except Exception:
                    pass

        def _report_plan_in_log(self, plan):
            self.log("")
            self.log(f"Sync plan ({plan['match']} match)", 'meta')
            self.log(f"  Source:          {plan['src_dir']}", 'meta')
            self.log(f"  Destination:     {plan['dst_dir']}", 'meta')
            self.log(f"  Already present: {plan['synced_count']:,} file(s) ({format_size(plan['synced_size'])}) "
                     f"- {plan['copy_elsewhere_count']:,} matched by content at another path", 'ok')
            self.log(f"  To copy:         {plan['copy_count']:,} file(s) ({format_size(plan['copy_size'])}) "
                     f"- {plan['overwrite_count']:,} replace a file at the same path", 'ok')
            for item in plan['to_copy'][:10]:
                self.log(f"    + {item['rel_path']} ({format_size(item['size'])})", 'ok')
            if plan['copy_count'] > 10:
                self.log(f"    ... and {plan['copy_count'] - 10:,} more.", 'meta')
            self.log(f"  Destination extras (kept): {plan['dest_extras_count']:,} file(s) "
                     f"({format_size(plan['dest_extras_size'])})", 'meta')
            for item in plan['dest_extras'][:10]:
                self.log(f"    - {item['rel_path']} ({format_size(item['size'])})", 'meta')
            if plan['dest_extras_count'] > 10:
                self.log(f"    ... and {plan['dest_extras_count'] - 10:,} more.", 'meta')

    root = tk.Tk()
    app = DriveSyncApp(root)
    root.mainloop()

def main():
    if len(sys.argv) == 1:
        launch_gui()
        return

    print_banner()

    parser = argparse.ArgumentParser(
        description="One-way sync of missing files between two drives: the destination is only ever "
                    "added to, never pruned, and every copied file is hash-verified."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_common(sub_parser, paths=False):
        if paths:
            sub_parser.add_argument("src", help="Source drive or folder")
            sub_parser.add_argument("dst", help="Destination drive or folder")
        sub_parser.add_argument("--match", choices=list(MATCH_MODES), default='hash',
                                help="How to decide a source file is already present: "
                                     "'hash' = identical content anywhere in the destination (default), "
                                     "'path' = relative path exists, "
                                     "'both' = relative path exists with identical content")
        sub_parser.add_argument("--mode", choices=['max', 'balanced', 'eco'], default='balanced',
                                help="Process priority while hashing/copying")
        sub_parser.add_argument("--throttle", type=float, default=0.0,
                                help="Seconds to sleep between read/copy operations (I/O throttling)")

    analyze_parser = subparsers.add_parser("analyze", help="Dry run: report what would be copied.")
    add_common(analyze_parser, paths=True)

    sync_parser = subparsers.add_parser("sync", help="Plan, confirm, then copy the missing files.")
    add_common(sync_parser, paths=True)
    sync_parser.add_argument("--dry-run", action="store_true", help="Only show the plan")
    sync_parser.add_argument("-y", "--yes", action="store_true", help="Skip the confirmation prompt")

    run_parser = subparsers.add_parser("run", help="Analyze and then sync with a confirmation prompt.")
    add_common(run_parser, paths=True)
    run_parser.add_argument("-y", "--yes", action="store_true", help="Skip the confirmation prompt")

    cleanup_parser = subparsers.add_parser("cleanup", help="Remove non-existent files from the index.")
    cleanup_parser.add_argument("path", help="Any path on the drive holding the index database")

    reset_parser = subparsers.add_parser("reset", help="Clear the sync index database.")
    reset_parser.add_argument("path", help="Any path on the drive holding the index database")

    args = parser.parse_args()

    try:
        if args.command == 'cleanup':
            run_cleanup(get_db_path(args.path))
            return
        if args.command == 'reset':
            run_reset(get_db_path(args.path))
            return

        # The index lives on the destination drive, which is the drive we write to.
        db_path = get_db_path(args.dst)
        set_process_priority(args.mode)

        if args.command == 'analyze':
            run_analyze(args.src, args.dst, db_path, match=args.match, throttle_sleep=args.throttle)
        elif args.command == 'sync':
            run_sync(args.src, args.dst, db_path, match=args.match, dry_run=args.dry_run,
                     assume_yes=args.yes, throttle_sleep=args.throttle)
        elif args.command == 'run':
            run_sync(args.src, args.dst, db_path, match=args.match,
                     assume_yes=args.yes, throttle_sleep=args.throttle)
    except KeyboardInterrupt:
        print(f"\n{Colors.RED}Process interrupted by user.{Colors.END}")
        sys.exit(1)

if __name__ == "__main__":
    main()
