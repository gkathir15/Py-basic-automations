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
   ___             _ _           _     ___ _           _           
  |   \\ _  _ _ __ | (_)__ __ _ _| |_  | __(_)_ _  __| |___ _ _  
  | |) | || | '_ \\| | / _/ _` (_-<  _| | _|| | ' \\/ _` / -_) '_| 
  |___/ \\_,_| .__/|_|\\__\\__,_/__/\\__| |_| |_|_||_\\__,_\\___|_|   
            |_|                                                  
{Colors.END}{Colors.BLUE}  Lightweight, low-overhead file duplicate analyzer & manager.{Colors.END}
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
    """
    Returns the physical memory (RSS) used by this process in bytes.
    """
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
    - 'max' (Normal priority class on Windows / standard priority on Unix)
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
            
        db_file = Path(drive_root) / "file_registry.db"
        
        # Test write access to the drive root database location
        test_conn = sqlite3.connect(str(db_file))
        test_conn.execute("CREATE TABLE IF NOT EXISTS _write_test (id INTEGER PRIMARY KEY);")
        test_conn.execute("DROP TABLE _write_test;")
        test_conn.close()
        
        return str(db_file)
    except Exception:
        # Fall back to script's directory
        fallback_db = Path(__file__).parent.resolve() / "file_registry.db"
        # Only print warning if we are in CLI mode
        if len(sys.argv) > 1:
            print(f"{Colors.YELLOW}[WARNING] Write permission to drive root '{drive_root}' denied. DB will be stored at: {fallback_db}{Colors.END}")
        return str(fallback_db)

def init_db(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filepath TEXT UNIQUE,
            filename TEXT,
            size INTEGER,
            mtime REAL,
            partial_hash TEXT,
            full_hash TEXT
        )
    """)
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

def get_space_distribution(db_path):
    """
    Analyzes file sizes and grouping in SQLite.
    Returns size totals for Unique, Keeper, and Wasted duplicate spaces.
    """
    db_conn = sqlite3.connect(db_path)
    cursor = db_conn.cursor()
    
    cursor.execute("SELECT COUNT(*), SUM(size) FROM files")
    res = cursor.fetchone()
    total_count = res[0] or 0
    total_size = res[1] or 0
    
    cursor.execute("""
        SELECT SUM((c - 1) * size) FROM (
            SELECT size, COUNT(*) as c FROM files 
            WHERE full_hash IS NOT NULL AND full_hash NOT LIKE 'ERROR_%'
            GROUP BY size, full_hash 
            HAVING c > 1
        )
    """)
    wasted_size = cursor.fetchone()[0] or 0
    
    cursor.execute("""
        SELECT SUM(size) FROM (
            SELECT size, COUNT(*) as c FROM files 
            WHERE full_hash IS NOT NULL AND full_hash NOT LIKE 'ERROR_%'
            GROUP BY size, full_hash 
            HAVING c > 1
        )
    """)
    keeper_size = cursor.fetchone()[0] or 0
    
    unique_size = max(0, total_size - keeper_size - wasted_size)
    
    cursor.execute("""
        SELECT SUM(c - 1) FROM (
            SELECT COUNT(*) as c FROM files 
            WHERE full_hash IS NOT NULL AND full_hash NOT LIKE 'ERROR_%'
            GROUP BY size, full_hash 
            HAVING c > 1
        )
    """)
    wasted_count = cursor.fetchone()[0] or 0
    
    db_conn.close()
    return {
        'total_count': total_count,
        'total_size': total_size,
        'wasted_size': wasted_size,
        'wasted_count': wasted_count,
        'keeper_size': keeper_size,
        'unique_size': unique_size
    }

def run_scan(target_dir, db_path, progress_cb=None):
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
        progress_cb('info', f"Scanning directory: {target_path}...")
        progress_cb('info', f"Using database file: {db_path}")
    else:
        print(f"{Colors.BLUE}Scanning directory: {Colors.BOLD}{target_path}{Colors.END}{Colors.BLUE}...{Colors.END}")
        print(f"{Colors.BLUE}Using database file: {Colors.BOLD}{db_path}{Colors.END}")
    
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
                        stat = entry.stat()
                        batch.append((str(Path(entry.path).resolve()), entry.name, stat.st_size, stat.st_mtime))
                        files_found += 1
                        if len(batch) >= 1000:
                            db_conn.executemany("""
                                INSERT INTO files (filepath, filename, size, mtime) 
                                VALUES (?, ?, ?, ?)
                                ON CONFLICT(filepath) DO UPDATE SET 
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
                                progress_cb('progress_update', files_found, errors, elapsed, speed)
                            else:
                                print_status(f"Indexed {files_found:,} files... ({speed:.0f} files/s)")
                    except (OSError, PermissionError):
                        errors += 1
                elif entry.is_dir():
                    recurse(entry.path)
        except (OSError, PermissionError):
            errors += 1

    recurse(target_path)
    
    if batch:
        db_conn.executemany("""
            INSERT INTO files (filepath, filename, size, mtime) 
            VALUES (?, ?, ?, ?)
            ON CONFLICT(filepath) DO UPDATE SET 
                size = excluded.size,
                mtime = excluded.mtime,
                partial_hash = CASE WHEN size != excluded.size OR mtime != excluded.mtime THEN NULL ELSE partial_hash END,
                full_hash = CASE WHEN size != excluded.size OR mtime != excluded.mtime THEN NULL ELSE full_hash END
        """, batch)
        db_conn.commit()
        
    elapsed = time.time() - start_time
    speed = files_found / elapsed if elapsed > 0 else 0
    
    if progress_cb:
        progress_cb('complete', files_found, errors, elapsed, speed)
    else:
        print(f"\r{Colors.GREEN}Scan complete! Indexed {files_found:,} files in {elapsed:.1f}s. (Skipped {errors:,} due to errors){Colors.END}")
    db_conn.close()
    return True

def find_duplicates(db_path, progress_cb=None, run_quiet=False, throttle_sleep=0.0):
    db_conn = sqlite3.connect(db_path)
    db_conn.execute("PRAGMA journal_mode=WAL;")
    cursor = db_conn.cursor()
    
    init_db(db_conn)
    
    # Step 1: Compute partial hashes for files with matching sizes
    cursor.execute("""
        SELECT filepath FROM files 
        WHERE size IN (
            SELECT size FROM files 
            GROUP BY size 
            HAVING COUNT(*) > 1
        ) AND partial_hash IS NULL
    """)
    to_partial = [r[0] for r in cursor.fetchall()]
    
    if to_partial:
        if progress_cb:
            progress_cb('partial_start', len(to_partial))
        elif not run_quiet:
            print(f"{Colors.BLUE}Computing partial hashes for {len(to_partial):,} size-matching files...{Colors.END}")
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
    
    # Step 2: Compute full hashes for duplicates
    cursor.execute("""
        SELECT filepath, size FROM files 
        WHERE (size, partial_hash) IN (
            SELECT size, partial_hash FROM files 
            WHERE partial_hash NOT LIKE 'ERROR_%'
            GROUP BY size, partial_hash 
            HAVING COUNT(*) > 1
        ) AND full_hash IS NULL
    """)
    to_full_rows = cursor.fetchall()
    to_full = [r[0] for r in to_full_rows]
    total_bytes_to_hash = sum(r[1] for r in to_full_rows)
    
    if to_full:
        if progress_cb:
            progress_cb('full_start', len(to_full), total_bytes_to_hash)
        elif not run_quiet:
            print(f"{Colors.BLUE}Computing full hashes for {len(to_full):,} candidate files ({format_size(total_bytes_to_hash)})...{Colors.END}")
            
        for idx, path in enumerate(to_full):
            # Pass chunk handler to progress callback
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

    # Step 3: Extract final duplicate groups
    cursor.execute("""
        SELECT size, full_hash, COUNT(*) as c FROM files
        WHERE full_hash IS NOT NULL AND full_hash NOT LIKE 'ERROR_%'
        GROUP BY size, full_hash
        HAVING c > 1
        ORDER BY size DESC
    """)
    dup_groups = cursor.fetchall()
    
    if not dup_groups:
        if progress_cb:
            progress_cb('no_duplicates')
        elif not run_quiet:
            print(f"{Colors.GREEN}No duplicate files found.{Colors.END}")
        db_conn.close()
        return []

    total_wasted_space = 0
    total_duplicates_count = 0
    groups_data = []
    
    if progress_cb:
        progress_cb('report_start')
    elif not run_quiet:
        print(f"\n{Colors.BOLD}{Colors.YELLOW}=== DUPLICATE FILES FOUND ==={Colors.END}\n")
    
    for size, f_hash, count in dup_groups:
        cursor.execute("SELECT filepath, mtime FROM files WHERE full_hash = ? ORDER BY filepath", (f_hash,))
        files_in_group = cursor.fetchall()
        
        wasted = (count - 1) * size
        total_wasted_space += wasted
        total_duplicates_count += (count - 1)
        
        size_str = format_size(size)
        wasted_str = format_size(wasted)
        
        if progress_cb:
            progress_cb('report_group', size, f_hash, count, files_in_group)
        elif not run_quiet:
            print(f"{Colors.BOLD}{Colors.CYAN}Hash: {f_hash[:16]}... | Size: {size_str} | Copies: {count} ({Colors.RED}Wasted: {wasted_str}{Colors.CYAN}){Colors.END}")
            for f_path, mtime in files_in_group:
                mtime_str = datetime.fromtimestamp(mtime).strftime('%Y-%m-%d %H:%M:%S')
                print(f"  - {f_path} {Colors.BLUE}(Modified: {mtime_str}){Colors.END}")
            print()
            
        groups_data.append((size, f_hash, [f[0] for f in files_in_group]))
        
    if progress_cb:
        progress_cb('report_summary', total_duplicates_count, total_wasted_space)
    elif not run_quiet:
        print(f"{Colors.BOLD}{Colors.GREEN}Summary: Found {total_duplicates_count} duplicate files. Total reclaimable space: {format_size(total_wasted_space)}{Colors.END}")
        
    db_conn.close()
    return groups_data

def run_isolate(target_dir, db_path, progress_cb=None):
    if progress_cb:
        progress_cb('isolate_start')
        
    groups = find_duplicates(db_path, run_quiet=True)
    if not groups:
        if progress_cb:
            progress_cb('isolate_no_files')
        else:
            print(f"{Colors.YELLOW}No duplicates found to isolate.{Colors.END}")
        return
        
    target_path = Path(target_dir).resolve()
    target_path.mkdir(parents=True, exist_ok=True)
    
    if progress_cb:
        progress_cb('isolate_info', f"Isolating duplicate files to: {target_path}...")
    else:
        print(f"{Colors.BLUE}Isolating duplicate files to: {Colors.BOLD}{target_path}{Colors.END}...")
    
    moved_count = 0
    errors = 0
    
    db_conn = sqlite3.connect(db_path)
    db_conn.execute("PRAGMA journal_mode=WAL;")
    
    for size, f_hash, paths in groups:
        # Keeper heuristic: short path, then alphabetical
        paths_sorted = sorted(paths, key=lambda p: (len(p), p))
        keeper = paths_sorted[0]
        duplicates_to_move = paths_sorted[1:]
        
        if progress_cb:
            progress_cb('isolate_group', size, f_hash, keeper)
        else:
            print(f"\n{Colors.BOLD}{Colors.CYAN}Group {f_hash[:8]}... (Size: {format_size(size)}){Colors.END}")
            print(f"  {Colors.GREEN}[KEEPING]{Colors.END} {keeper}")
        
        for dup in duplicates_to_move:
            try:
                dup_path = Path(dup).resolve()
                if not dup_path.exists():
                    if progress_cb:
                        progress_cb('isolate_move', dup_path, None, 'missing')
                    else:
                        print(f"  {Colors.YELLOW}[MISSING]{Colors.END} {dup_path} (Skipped)")
                    continue
                
                # Reconstruct directory structure to avoid collisions
                parts = list(dup_path.parts)
                if len(parts) > 0:
                    parts[0] = parts[0].replace('\\', '').replace('/', '').replace(':', '_')
                
                dest_path = target_path.joinpath(*parts)
                dest_path.parent.mkdir(parents=True, exist_ok=True)
                
                # If destination file already exists, generate a unique filename
                if dest_path.exists():
                    unique_id = uuid.uuid4().hex[:6]
                    dest_path = dest_path.with_name(f"{dest_path.stem}_{unique_id}{dest_path.suffix}")
                
                # Move the file
                shutil.move(str(dup_path), str(dest_path))
                if progress_cb:
                    progress_cb('isolate_move', dup_path, dest_path, 'moved')
                else:
                    print(f"  {Colors.YELLOW}[MOVING]{Colors.END}  {dup_path} -> {dest_path}")
                
                # Delete from SQLite since it's no longer in original path
                db_conn.execute("DELETE FROM files WHERE filepath = ?", (str(dup_path),))
                moved_count += 1
            except Exception as e:
                if progress_cb:
                    progress_cb('isolate_move', dup_path, None, f"error: {e}")
                else:
                    print(f"  {Colors.RED}[ERROR] Failed to move {dup}: {e}{Colors.END}")
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

def run_executor(scan_path, target_dir):
    """
    All-in-one execution pipeline.
    """
    db_path = get_db_path(scan_path)
    
    print(f"{Colors.BOLD}{Colors.GREEN}>>> STEP 1: Scanning '{scan_path}'...{Colors.END}")
    if not run_scan(scan_path, db_path):
        return
        
    print(f"\n{Colors.BOLD}{Colors.GREEN}>>> STEP 2: Identifying Duplicates...{Colors.END}")
    groups = find_duplicates(db_path)
    
    if not groups:
        print(f"\n{Colors.GREEN}No duplicates found. Process finished successfully.{Colors.END}")
        return
        
    print(f"\n{Colors.BOLD}{Colors.GREEN}>>> STEP 3: Isolation Prompt{Colors.END}")
    try:
        user_input = input(f"{Colors.BOLD}Do you want to isolate the duplicates listed above to '{target_dir}'? (y/n): {Colors.END}").strip().lower()
        if user_input in ('y', 'yes'):
            run_isolate(target_dir, db_path)
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

    # Theme colors definition (Catppuccin Mocha dashboard aesthetic)
    THEME = {
        'bg': '#1e1e2e',
        'card_bg': '#252538',
        'btn_bg': '#313244',
        'btn_blue': '#89b4fa',
        'btn_green': '#a6e3a1',
        'btn_orange': '#fab387',
        'btn_red': '#f38ba8',
        'fg': '#cdd6f4',
        'fg_dim': '#bac2de',
        'fg_dark': '#11111b',
        'font_title': ('Segoe UI', 18, 'bold'),
        'font_header': ('Segoe UI', 11, 'bold'),
        'font_card_val': ('Segoe UI', 14, 'bold'),
        'font_card_lbl': ('Segoe UI', 9),
        'font_body': ('Segoe UI', 10),
        'font_code': ('Consolas', 10)
    }

    class DuplicateFinderApp:
        def __init__(self, root):
            self.root = root
            self.root.title("Duplicate File Finder Dashboard")
            self.root.geometry("950x780")
            self.root.minsize(850, 720)
            self.root.configure(bg=THEME['bg'])

            self.scan_path = tk.StringVar()
            self.isolate_path = tk.StringVar()
            self.status_var = tk.StringVar(value="Ready")
            
            # Metric StringVars for live updates
            self.stat_scanned_files = tk.StringVar(value="0")
            self.stat_scanned_size = tk.StringVar(value="0.00 B")
            self.stat_duplicates = tk.StringVar(value="0 copies")
            self.stat_wasted = tk.StringVar(value="0.00 B")
            
            # Live performance StringVars
            self.perf_speed = tk.StringVar(value="0 B/s")
            self.perf_elapsed = tk.StringVar(value="0.0s")
            self.perf_eta = tk.StringVar(value="--")
            self.perf_ram = tk.StringVar(value="0.0 MB")
            
            # Performance Mode
            self.perf_mode = tk.StringVar(value="balanced")

            # Default paths
            self.scan_path.set(str(Path.cwd()))
            self.isolate_path.set(str(Path.cwd() / "Isolated_Duplicates"))

            self.queue = queue.Queue()
            self.running_thread = None
            self.widgets_to_disable = []
            
            # Tracking variables for speed and ETA calculation
            self.scan_start_time = 0
            self.hash_start_time = 0
            self.hash_total_bytes = 0
            self.hash_bytes_processed = 0

            self.logs_visible = False

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
            # Main container with margins
            container = tk.Frame(self.root, bg=THEME['bg'], padx=20, pady=15)
            container.pack(fill='both', expand=True)

            # 1. Header Frame
            header_frame = tk.Frame(container, bg=THEME['bg'])
            header_frame.pack(fill='x', pady=(0, 8))

            title_lbl = tk.Label(
                header_frame, 
                text="Duplicate File Finder Dashboard", 
                font=THEME['font_title'], 
                bg=THEME['bg'], 
                fg=THEME['btn_blue']
            )
            title_lbl.pack(anchor='w')

            desc_lbl = tk.Label(
                header_frame, 
                text="A lightweight, database-backed utility to scan, identify, and isolate duplicate files.", 
                font=THEME['font_body'], 
                bg=THEME['bg'], 
                fg=THEME['fg_dim']
            )
            desc_lbl.pack(anchor='w', pady=(1, 0))

            # 2. Input Paths Frame (Card)
            input_frame = tk.Frame(container, bg=THEME['card_bg'], padx=15, pady=10)
            input_frame.pack(fill='x', pady=(0, 10))

            # Scan Folder selection
            tk.Label(input_frame, text="Folder to Scan:", font=THEME['font_header'], bg=THEME['card_bg'], fg=THEME['fg']).grid(row=0, column=0, sticky='w', pady=(0, 2))
            scan_entry = tk.Entry(input_frame, textvariable=self.scan_path, bg=THEME['bg'], fg=THEME['fg'], relief='flat', font=THEME['font_body'], insertbackground=THEME['fg'], highlightthickness=1, highlightbackground=THEME['btn_bg'], highlightcolor=THEME['btn_blue'])
            scan_entry.grid(row=1, column=0, sticky='ew', columnspan=2, ipady=4, padx=(0, 10))
            scan_btn = self.make_button(input_frame, "Browse...", self.browse_scan)
            scan_btn.grid(row=1, column=2, sticky='w')

            # Isolate Folder selection
            tk.Label(input_frame, text="Isolation Folder:", font=THEME['font_header'], bg=THEME['card_bg'], fg=THEME['fg']).grid(row=2, column=0, sticky='w', pady=(6, 2))
            iso_entry = tk.Entry(input_frame, textvariable=self.isolate_path, bg=THEME['bg'], fg=THEME['fg'], relief='flat', font=THEME['font_body'], insertbackground=THEME['fg'], highlightthickness=1, highlightbackground=THEME['btn_bg'], highlightcolor=THEME['btn_blue'])
            iso_entry.grid(row=3, column=0, sticky='ew', columnspan=2, ipady=4, padx=(0, 10))
            iso_btn = self.make_button(input_frame, "Browse...", self.browse_isolate)
            iso_btn.grid(row=3, column=2, sticky='w')

            input_frame.columnconfigure(0, weight=1)
            input_frame.columnconfigure(1, weight=1)

            # 3. Middle Dashboard Frame (Donut Chart + Metrics Cards)
            self.dashboard_pane = tk.Frame(container, bg=THEME['bg'])
            self.dashboard_pane.pack(fill='x', pady=(0, 10))

            # Left side: Donut Chart Canvas
            chart_card = tk.Frame(self.dashboard_pane, bg=THEME['card_bg'], padx=15, pady=10)
            chart_card.pack(side='left', fill='both', expand=True, padx=(0, 10))

            tk.Label(chart_card, text="Space Allocation Chart", font=THEME['font_header'], bg=THEME['card_bg'], fg=THEME['fg']).pack(anchor='w', pady=(0, 3))
            self.chart_canvas = tk.Canvas(chart_card, width=170, height=170, bg=THEME['card_bg'], highlightthickness=0)
            self.chart_canvas.pack(pady=2)
            
            # Mini Legend
            legend_frame = tk.Frame(chart_card, bg=THEME['card_bg'])
            legend_frame.pack(fill='x', pady=(2, 0))
            self.draw_legend_box(legend_frame, '#89b4fa', "Unique").pack(side='left', expand=True)
            self.draw_legend_box(legend_frame, '#a6e3a1', "Keeper").pack(side='left', expand=True)
            self.draw_legend_box(legend_frame, '#fab387', "Wasted").pack(side='left', expand=True)

            # Right side: Metric cards grid
            metrics_grid = tk.Frame(self.dashboard_pane, bg=THEME['bg'])
            metrics_grid.pack(side='right', fill='both', expand=True)

            # Row 0: General Stats
            self.build_metric_card(metrics_grid, "Total Files", self.stat_scanned_files, "Total Size", self.stat_scanned_size).grid(row=0, column=0, sticky='nsew', pady=(0, 8), padx=(0, 8))
            self.build_metric_card(metrics_grid, "Duplicate Copies", self.stat_duplicates, "Wasted Space", self.stat_wasted, value_color=THEME['btn_orange']).grid(row=0, column=1, sticky='nsew', pady=(0, 8))

            # Row 1: System Performance and Throttling controls
            perf_card = tk.Frame(metrics_grid, bg=THEME['card_bg'], padx=15, pady=8)
            perf_card.grid(row=1, column=0, columnspan=2, sticky='nsew')
            
            # Title + RAM display
            title_perf_frame = tk.Frame(perf_card, bg=THEME['card_bg'])
            title_perf_frame.pack(fill='x', pady=(0, 4))
            tk.Label(title_perf_frame, text="Performance & Live ETA", font=THEME['font_header'], bg=THEME['card_bg'], fg=THEME['btn_blue']).pack(side='left')
            
            ram_container = tk.Frame(title_perf_frame, bg=THEME['card_bg'])
            ram_container.pack(side='right')
            tk.Label(ram_container, text="Process RAM: ", font=('Segoe UI', 8), bg=THEME['card_bg'], fg=THEME['fg_dim']).pack(side='left')
            tk.Label(ram_container, textvariable=self.perf_ram, font=('Segoe UI', 9, 'bold'), bg=THEME['card_bg'], fg=THEME['btn_green']).pack(side='left')
            
            p_sub_grid = tk.Frame(perf_card, bg=THEME['card_bg'])
            p_sub_grid.pack(fill='x', expand=True, pady=(0, 4))
            
            self.build_perf_item(p_sub_grid, "Speed", self.perf_speed).grid(row=0, column=0, sticky='ew', padx=(0, 10))
            self.build_perf_item(p_sub_grid, "Time Elapsed", self.perf_elapsed).grid(row=0, column=1, sticky='ew', padx=(0, 10))
            self.build_perf_item(p_sub_grid, "ETA (Remaining)", self.perf_eta, value_color=THEME['btn_green']).grid(row=0, column=2, sticky='ew')
            p_sub_grid.columnconfigure(0, weight=1)
            p_sub_grid.columnconfigure(1, weight=1)
            p_sub_grid.columnconfigure(2, weight=1)

            # System resource throttle selector
            throttle_frame = tk.Frame(perf_card, bg=THEME['card_bg'])
            throttle_frame.pack(fill='x', pady=(2, 0))
            tk.Label(throttle_frame, text="System Resource Mode:", font=('Segoe UI', 8, 'bold'), bg=THEME['card_bg'], fg=THEME['fg_dim']).pack(side='left', padx=(0, 10))
            
            modes_frame = tk.Frame(throttle_frame, bg=THEME['card_bg'])
            modes_frame.pack(side='left')
            
            # Customized flat radio buttons
            self.build_radio_button(modes_frame, "Max Speed", "max").pack(side='left', padx=(0, 8))
            self.build_radio_button(modes_frame, "Balanced", "balanced").pack(side='left', padx=(0, 8))
            self.build_radio_button(modes_frame, "Eco (Background)", "eco").pack(side='left')

            metrics_grid.columnconfigure(0, weight=1)
            metrics_grid.columnconfigure(1, weight=1)
            metrics_grid.rowconfigure(0, weight=1)
            metrics_grid.rowconfigure(1, weight=1)

            # 4. Actions & Progress Frame
            actions_and_progress = tk.Frame(container, bg=THEME['bg'])
            actions_and_progress.pack(fill='x', pady=(0, 10))

            # Progress row
            progress_row = tk.Frame(actions_and_progress, bg=THEME['bg'])
            progress_row.pack(fill='x', pady=(0, 6))
            
            self.progress_bar = ttk.Progressbar(progress_row, mode='determinate', orient='horizontal')
            self.progress_bar.pack(side='left', fill='x', expand=True, padx=(0, 15))
            
            self.status_lbl = tk.Label(
                progress_row, 
                textvariable=self.status_var, 
                font=THEME['font_body'], 
                bg=THEME['bg'], 
                fg=THEME['fg_dim']
            )
            self.status_lbl.pack(side='right')

            # Button Actions
            action_row = tk.Frame(actions_and_progress, bg=THEME['bg'])
            action_row.pack(fill='x')

            scan_action_btn = self.make_button(action_row, "🔍 Start Scan & Duplicates Check", self.start_scan, bg_color=THEME['btn_blue'], fg_color=THEME['fg_dark'])
            scan_action_btn.pack(side='left', padx=(0, 10))
            
            isolate_action_btn = self.make_button(action_row, "📦 Move Duplicates to Isolation", self.start_isolate, bg_color=THEME['btn_orange'], fg_color=THEME['fg_dark'])
            isolate_action_btn.pack(side='left', padx=(0, 10))

            prune_action_btn = self.make_button(action_row, "Prune DB Index", self.start_prune)
            prune_action_btn.pack(side='left', padx=(0, 10))

            reset_action_btn = self.make_button(action_row, "Reset Database", self.start_reset, bg_color=THEME['btn_red'], fg_color=THEME['fg_dark'])
            reset_action_btn.pack(side='right')

            # 5. Collapsible Logs Drawer (Bottom)
            self.logs_container = tk.Frame(container, bg=THEME['bg'])
            self.logs_container.pack(fill='both', expand=True)

            self.toggle_btn = tk.Button(
                self.logs_container,
                text="▶ Show Verbose Logs",
                command=self.toggle_logs,
                bg=THEME['btn_bg'],
                fg=THEME['fg_dim'],
                activebackground=THEME['btn_bg'],
                activeforeground=THEME['fg'],
                font=THEME['font_body'],
                relief='flat',
                padx=10,
                pady=4,
                cursor='hand2',
                bd=0
            )
            self.toggle_btn.pack(anchor='w', pady=(3, 3))

            self.log_area = scrolledtext.ScrolledText(
                self.logs_container,
                wrap='word',
                bg=THEME['card_bg'],
                fg=THEME['fg'],
                insertbackground=THEME['fg'],
                font=THEME['font_code'],
                relief='flat',
                borderwidth=0,
                highlightthickness=1,
                highlightbackground=THEME['btn_bg'],
                highlightcolor=THEME['btn_blue']
            )
            # Hidden by default

            # Add font color tags for visual reports
            self.log_area.tag_config('group_header', foreground=THEME['btn_blue'], font=('Consolas', 10, 'bold'))
            self.log_area.tag_config('keeper', foreground=THEME['btn_green'])
            self.log_area.tag_config('dup', foreground=THEME['btn_orange'])
            self.log_area.tag_config('error', foreground=THEME['btn_red'])
            self.log_area.tag_config('success', foreground=THEME['btn_green'], font=('Consolas', 10, 'bold'))
            self.log_area.tag_config('normal', foreground=THEME['fg'])
            self.log_area.tag_config('dim', foreground=THEME['fg_dim'])

        def draw_legend_box(self, parent, color, text):
            frame = tk.Frame(parent, bg=THEME['card_bg'])
            box = tk.Label(frame, bg=color, width=2, height=1, relief='flat')
            box.pack(side='left', padx=(0, 4))
            lbl = tk.Label(frame, text=text, font=('Segoe UI', 8), bg=THEME['card_bg'], fg=THEME['fg_dim'])
            lbl.pack(side='left')
            return frame

        def build_metric_card(self, parent, label1, var1, label2, var2, value_color=THEME['fg']):
            card = tk.Frame(parent, bg=THEME['card_bg'], padx=15, pady=8)
            
            tk.Label(card, text=label1, font=THEME['font_card_lbl'], bg=THEME['card_bg'], fg=THEME['fg_dim']).pack(anchor='w')
            tk.Label(card, textvariable=var1, font=THEME['font_card_val'], bg=THEME['card_bg'], fg=value_color).pack(anchor='w', pady=(0, 4))
            
            tk.Label(card, text=label2, font=THEME['font_card_lbl'], bg=THEME['card_bg'], fg=THEME['fg_dim']).pack(anchor='w')
            tk.Label(card, textvariable=var2, font=THEME['font_card_val'], bg=THEME['card_bg'], fg=value_color).pack(anchor='w')
            
            return card

        def build_perf_item(self, parent, title, variable, value_color=THEME['fg']):
            frame = tk.Frame(parent, bg=THEME['card_bg'])
            tk.Label(frame, text=title, font=THEME['font_card_lbl'], bg=THEME['card_bg'], fg=THEME['fg_dim']).pack(anchor='w')
            tk.Label(frame, textvariable=variable, font=THEME['font_card_val'], bg=THEME['card_bg'], fg=value_color).pack(anchor='w')
            return frame

        def build_radio_button(self, parent, text, val):
            btn = tk.Radiobutton(
                parent,
                text=text,
                variable=self.perf_mode,
                value=val,
                command=self.on_perf_mode_change,
                bg=THEME['card_bg'],
                fg=THEME['fg_dim'],
                activebackground=THEME['card_bg'],
                activeforeground=THEME['fg'],
                selectcolor=THEME['btn_bg'],
                font=('Segoe UI', 8),
                relief='flat',
                bd=0,
                highlightthickness=0,
                cursor='hand2'
            )
            return btn

        def on_perf_mode_change(self):
            mode = self.perf_mode.get()
            set_process_priority(mode)
            self.queue.put(('log', f"[SYSTEM] Resource mode changed to '{mode.upper()}'\n", 'dim'))

        def toggle_logs(self):
            if self.logs_visible:
                self.log_area.pack_forget()
                self.toggle_btn.config(text="▶ Show Verbose Logs")
                self.logs_visible = False
            else:
                self.log_area.pack(fill='both', expand=True, pady=(5, 0))
                self.toggle_btn.config(text="▼ Hide Verbose Logs")
                self.logs_visible = True

        def browse_scan(self):
            dir_path = filedialog.askdirectory(initialdir=self.scan_path.get())
            if dir_path:
                self.scan_path.set(str(Path(dir_path).resolve()))

        def browse_isolate(self):
            dir_path = filedialog.askdirectory(initialdir=self.isolate_path.get())
            if dir_path:
                self.isolate_path.set(str(Path(dir_path).resolve()))

        def disable_widgets(self):
            for widget in self.widgets_to_disable:
                widget.config(state='disabled')

        def enable_widgets(self):
            for widget in self.widgets_to_disable:
                widget.config(state='normal')

        def draw_empty_chart(self):
            self.chart_canvas.delete("all")
            cx, cy = 85, 85
            r = 75
            self.chart_canvas.create_oval(cx-r, cy-r, cx+r, cy+r, fill='#313244', outline="")
            
            r_inner = 48
            self.chart_canvas.create_oval(cx-r_inner, cy-r_inner, cx+r_inner, cy+r_inner, fill=THEME['card_bg'], outline="")
            self.chart_canvas.create_text(cx, cy - 8, text="0%", font=('Segoe UI', 14, 'bold'), fill=THEME['fg_dim'])
            self.chart_canvas.create_text(cx, cy + 12, text="No Scan", font=('Segoe UI', 8), fill=THEME['fg_dim'])

        def update_donut_chart(self, unique, keeper, wasted):
            self.chart_canvas.delete("all")
            cx, cy = 85, 85
            r = 75
            total = unique + keeper + wasted
            
            if total == 0:
                self.draw_empty_chart()
                return
                
            angles = [
                360 * (unique / total),
                360 * (keeper / total),
                360 * (wasted / total)
            ]
            
            colors = ['#89b4fa', '#a6e3a1', '#fab387']
            
            start_ang = 0
            for ang, col in zip(angles, colors):
                if ang > 0:
                    self.chart_canvas.create_arc(cx-r, cy-r, cx+r, cy+r, start=start_ang, extent=ang, fill=col, outline="")
                    start_ang += ang
            
            r_inner = 48
            self.chart_canvas.create_oval(cx-r_inner, cy-r_inner, cx+r_inner, cy+r_inner, fill=THEME['card_bg'], outline="")
            
            wasted_pct = int(100 * wasted / total) if total > 0 else 0
            self.chart_canvas.create_text(cx, cy - 6, text=f"{wasted_pct}%", font=('Segoe UI', 14, 'bold'), fill='#fab387')
            self.chart_canvas.create_text(cx, cy + 12, text="Wasted", font=('Segoe UI', 8), fill=THEME['fg_dim'])

        def update_ram_usage(self):
            ram_bytes = get_current_memory_usage()
            self.perf_ram.set(format_size(ram_bytes))
            self.root.after(1500, self.update_ram_usage)

        def get_progress_cb(self):
            def cb(action, *args):
                if action == 'error':
                    self.queue.put(('log', f"[ERROR] {args[0]}\n", 'error'))
                    self.queue.put(('status', "Error occurred"))
                elif action == 'info':
                    self.queue.put(('log', f"{args[0]}\n", 'normal'))
                elif action == 'progress_update':
                    files_found, errors, elapsed, speed = args
                    self.queue.put(('status', f"Indexed {files_found:,} files... (Errors: {errors:,})"))
                    self.queue.put(('perf_scan', speed, elapsed))
                elif action == 'complete':
                    files_found, errors, elapsed, speed = args
                    self.queue.put(('log', f"Scan complete! Indexed {files_found:,} files in {elapsed:.1f}s. (Skipped {errors:,} due to permission/OS errors)\n\n", 'success'))
                    self.queue.put(('status', "Scan completed"))
                    self.queue.put(('progress', 100))
                    self.queue.put(('perf_scan', speed, elapsed))
                elif action == 'partial_start':
                    self.queue.put(('log', f"Computing partial hashes for {args[0]:,} size-matching files...\n", 'normal'))
                    self.queue.put(('progress', 0))
                elif action == 'partial_progress':
                    curr, total = args
                    percent = int(100 * curr / total) if total > 0 else 0
                    self.queue.put(('progress', percent))
                    self.queue.put(('status', f"Partial Hashing: {curr}/{total} ({percent}%)"))
                elif action == 'full_start':
                    count, bytes_to_hash = args
                    self.hash_total_bytes = bytes_to_hash
                    self.hash_bytes_processed = 0
                    self.hash_start_time = time.time()
                    self.queue.put(('log', f"Computing full hashes for {count:,} candidate matching files ({format_size(bytes_to_hash)})...\n", 'normal'))
                    self.queue.put(('progress', 0))
                elif action == 'hash_chunk':
                    chunk_sz = args[0]
                    self.hash_bytes_processed += chunk_sz
                    elapsed = time.time() - self.hash_start_time
                    speed = self.hash_bytes_processed / elapsed if elapsed > 0 else 0
                    eta = (self.hash_total_bytes - self.hash_bytes_processed) / speed if speed > 0 else 0
                    percent = int(100 * self.hash_bytes_processed / self.hash_total_bytes) if self.hash_total_bytes > 0 else 0
                    
                    self.queue.put(('progress', percent))
                    self.queue.put(('status', f"Full Hashing: {percent}%"))
                    self.queue.put(('perf_hash', speed, elapsed, eta))
                elif action == 'full_progress':
                    curr, total = args
                    pass
                elif action == 'no_duplicates':
                    self.queue.put(('log', "No duplicate files found.\n", 'success'))
                    self.queue.put(('status', "No duplicates found"))
                    self.queue.put(('progress', 100))
                elif action == 'report_start':
                    self.queue.put(('log', "=== DUPLICATE FILES FOUND ===\n\n", 'group_header'))
                elif action == 'report_group':
                    size, f_hash, count, files_in_group = args
                    size_str = format_size(size)
                    wasted = (count - 1) * size
                    wasted_str = format_size(wasted)
                    self.queue.put(('log', f"Hash: {f_hash[:16]}... | Size: {size_str} | Copies: {count} (Wasted: {wasted_str})\n", 'group_header'))
                    for f_path, mtime in files_in_group:
                        mtime_str = datetime.fromtimestamp(mtime).strftime('%Y-%m-%d %H:%M:%S')
                        self.queue.put(('log', f"  [FILE] {f_path} (Modified: {mtime_str})\n", 'dim'))
                    self.queue.put(('log', "\n", 'normal'))
                elif action == 'report_summary':
                    count, wasted = args
                    self.queue.put(('log', f"Summary: Found {count} duplicate files. Reclaimable: {format_size(wasted)}\n", 'success'))
                    self.queue.put(('status', f"Found {count} duplicates ({format_size(wasted)} wasted)"))
                    self.queue.put(('progress', 100))
                elif action == 'isolate_start':
                    self.queue.put(('log', "Starting isolation process...\n", 'normal'))
                    self.queue.put(('progress', 0))
                elif action == 'isolate_no_files':
                    self.queue.put(('log', "No duplicates to isolate.\n", 'normal'))
                    self.queue.put(('status', "Nothing to isolate"))
                elif action == 'isolate_info':
                    self.queue.put(('log', f"{args[0]}\n", 'normal'))
                elif action == 'isolate_group':
                    size, f_hash, keeper = args
                    self.queue.put(('log', f"\nGroup {f_hash[:8]}... (Size: {format_size(size)})\n", 'group_header'))
                    self.queue.put(('log', f"  [KEEPING] {keeper}\n", 'keeper'))
                elif action == 'isolate_move':
                    src, dest, status = args
                    if status == 'moved':
                        self.queue.put(('log', f"  [MOVED]   {src} -> {dest}\n", 'dup'))
                    elif status == 'missing':
                        self.queue.put(('log', f"  [MISSING] {src} (Skipped)\n", 'dim'))
                    else:
                        self.queue.put(('log', f"  [ERROR]   {src} ({status})\n", 'error'))
                elif action == 'isolate_complete':
                    moved_count, errors = args
                    self.queue.put(('log', f"\nIsolation complete! Moved {moved_count} files. (Errors: {errors})\n", 'success'))
                    self.queue.put(('status', f"Isolated {moved_count} duplicates"))
                    self.queue.put(('progress', 100))
            return cb

        def update_metrics(self, db_path):
            try:
                stats = get_space_distribution(db_path)
                self.stat_scanned_files.set(f"{stats['total_count']:,}")
                self.stat_scanned_size.set(format_size(stats['total_size']))
                self.stat_duplicates.set(f"{stats['wasted_count']:,} copies")
                self.stat_wasted.set(format_size(stats['wasted_size']))
                
                # Update chart
                self.update_donut_chart(stats['unique_size'], stats['keeper_size'], stats['wasted_size'])
            except Exception as e:
                self.queue.put(('log', f"Error loading database statistics: {e}\n", 'error'))

        def start_scan(self):
            scan_dir = self.scan_path.get().strip()
            if not scan_dir:
                messagebox.showerror("Error", "Please select a folder to scan.")
                return
                
            self.queue.put(('clear_log',))
            self.queue.put(('disable_controls',))
            self.queue.put(('status', "Starting scan..."))
            
            db_path = get_db_path(scan_dir)
            self.scan_start_time = time.time()
            
            # Determine throttle sleep based on Performance Mode
            mode = self.perf_mode.get()
            # Set Win/Linux process priority class
            set_process_priority(mode)
            
            throttle_sleep = 0.0
            if mode == 'eco':
                throttle_sleep = 0.010  # 10ms pacing delay between 64KB chunks to keep CPU idle
            
            def worker():
                try:
                    success = run_scan(scan_dir, db_path, progress_cb=self.get_progress_cb())
                    if success:
                        find_duplicates(db_path, progress_cb=self.get_progress_cb(), throttle_sleep=throttle_sleep)
                        self.update_metrics(db_path)
                except Exception as e:
                    self.queue.put(('log', f"\nFatal error during scan: {e}\n", 'error'))
                finally:
                    self.queue.put(('enable_controls',))
                    
            self.running_thread = threading.Thread(target=worker, daemon=True)
            self.running_thread.start()

        def start_isolate(self):
            scan_dir = self.scan_path.get().strip()
            iso_dir = self.isolate_path.get().strip()
            if not scan_dir or not iso_dir:
                messagebox.showerror("Error", "Please select scan and isolation folders.")
                return
                
            if not messagebox.askyesno("Confirm Isolation", f"Are you sure you want to move duplicate files from drive '{scan_dir}' to '{iso_dir}'?"):
                return
                
            self.queue.put(('clear_log',))
            self.queue.put(('disable_controls',))
            self.queue.put(('status', "Isolating duplicates..."))
            
            db_path = get_db_path(scan_dir)
            
            def worker():
                try:
                    run_isolate(iso_dir, db_path, progress_cb=self.get_progress_cb())
                    self.update_metrics(db_path)
                except Exception as e:
                    self.queue.put(('log', f"\nFatal error during isolation: {e}\n", 'error'))
                finally:
                    self.queue.put(('enable_controls',))
                    
            self.running_thread = threading.Thread(target=worker, daemon=True)
            self.running_thread.start()

        def start_prune(self):
            scan_dir = self.scan_path.get().strip()
            if not scan_dir:
                messagebox.showerror("Error", "Please select a folder path.")
                return
                
            self.queue.put(('clear_log',))
            self.queue.put(('disable_controls',))
            self.queue.put(('status', "Cleaning up database..."))
            self.queue.put(('progress', 0))
            
            db_path = get_db_path(scan_dir)
            
            def worker():
                try:
                    db_conn = sqlite3.connect(db_path)
                    db_conn.execute("PRAGMA journal_mode=WAL;")
                    cursor = db_conn.cursor()
                    init_db(db_conn)
                    cursor.execute("SELECT filepath FROM files")
                    all_files = [r[0] for r in cursor.fetchall()]
                    
                    if not all_files:
                        self.queue.put(('log', "Database is empty. Nothing to clean.\n", 'success'))
                        self.queue.put(('status', "Cleanup completed"))
                        self.queue.put(('progress', 100))
                        db_conn.close()
                        return

                    self.queue.put(('log', f"Verifying {len(all_files):,} indexed files...\n", 'normal'))
                    missing = []
                    start = time.time()
                    for idx, path in enumerate(all_files):
                        if not os.path.exists(path):
                            missing.append(path)
                        if idx % 100 == 0 or idx == len(all_files) - 1:
                            percent = int(100 * (idx + 1) / len(all_files))
                            self.queue.put(('progress', percent))
                            self.queue.put(('status', f"Verifying: {idx+1}/{len(all_files)}"))
                            elapsed = time.time() - start
                            speed = (idx + 1) / elapsed if elapsed > 0 else 0
                            self.queue.put(('perf_scan', speed, elapsed))
                            
                    if missing:
                        self.queue.put(('log', f"Removing {len(missing):,} missing files from database...\n", 'normal'))
                        db_conn.executemany("DELETE FROM files WHERE filepath = ?", [(p,) for p in missing])
                        db_conn.commit()
                        for m in missing:
                            self.queue.put(('log', f"  [REMOVED INDEX] {m}\n", 'error'))
                        self.queue.put(('log', f"\nCleanup complete! Removed {len(missing)} missing files from database.\n", 'success'))
                    else:
                        self.queue.put(('log', "\nAll indexed files are present in the filesystem. No cleanup needed.\n", 'success'))
                        
                    self.queue.put(('status', "Cleanup completed"))
                    self.queue.put(('progress', 100))
                    db_conn.close()
                    self.update_metrics(db_path)
                except Exception as e:
                    self.queue.put(('log', f"\nFatal error during cleanup: {e}\n", 'error'))
                finally:
                    self.queue.put(('enable_controls',))
                    
            self.running_thread = threading.Thread(target=worker, daemon=True)
            self.running_thread.start()

        def start_reset(self):
            scan_dir = self.scan_path.get().strip()
            if not scan_dir:
                messagebox.showerror("Error", "Please select a folder path to identify the database to reset.")
                return
                
            db_path = get_db_path(scan_dir)
            if not messagebox.askyesno("Confirm Reset", f"Are you sure you want to clear the entire duplicate index database for the drive root at:\n{db_path}?"):
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
                self.queue.put(('status', "Reset failed"))

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
                        self.perf_speed.set(f"{speed:.0f} files/s")
                        self.perf_elapsed.set(f"{elapsed:.1f}s")
                        self.perf_eta.set("Scanning...")
                    elif cmd == 'perf_hash':
                        speed, elapsed, eta = msg[1], msg[2], msg[3]
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
    
    # Configure custom styles for ttk Progressbar
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

    app = DuplicateFinderApp(root)
    root.mainloop()

def main():
    if len(sys.argv) == 1:
        # Launch GUI automatically if no arguments are passed
        launch_gui()
        return

    print_banner()
    
    parser = argparse.ArgumentParser(description="Find and manage duplicate files using SQLite.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    
    # Scan command
    scan_parser = subparsers.add_parser("scan", help="Scan a directory and index file metadata.")
    scan_parser.add_argument("path", help="Directory path to scan")
    
    # Duplicates command
    dup_parser = subparsers.add_parser("duplicates", help="Analyze files and print list of duplicates.")
    dup_parser.add_argument("path", help="Directory or drive path associated with the scan")
    
    # Isolate command
    isolate_parser = subparsers.add_parser("isolate", help="Move duplicates to an isolation directory.")
    isolate_parser.add_argument("path", help="Directory or drive path associated with the scan")
    isolate_parser.add_argument("target_dir", help="Directory where duplicate files will be moved")
    
    # Run command (All-in-one single executor)
    run_parser = subparsers.add_parser("run", help="Perform scan, analysis, and isolation in one single script call.")
    run_parser.add_argument("path", help="Directory path to scan")
    run_parser.add_argument("target_dir", help="Directory where duplicate files will be moved")
    
    # Cleanup command
    cleanup_parser = subparsers.add_parser("cleanup", help="Remove deleted or non-existent files from database.")
    cleanup_parser.add_argument("path", help="Directory or drive path associated with the scan")
    
    # Reset command
    reset_parser = subparsers.add_parser("reset", help="Clear the index database entirely.")
    reset_parser.add_argument("path", help="Directory or drive path associated with the scan")
    
    args = parser.parse_args()
    
    try:
        # Determine database path based on the primary path parameter
        db_path = get_db_path(args.path)
        
        if args.command == "scan":
            run_scan(args.path, db_path)
        elif args.command == "duplicates":
            find_duplicates(db_path)
        elif args.command == "isolate":
            run_isolate(args.target_dir, db_path)
        elif args.command == "run":
            run_executor(args.path, args.target_dir)
        elif args.command == "cleanup":
            run_cleanup(db_path)
        elif args.command == "reset":
            run_reset(db_path)
    except KeyboardInterrupt:
        print(f"\n{Colors.RED}Process interrupted by user.{Colors.END}")
        sys.exit(1)

if __name__ == "__main__":
    main()
