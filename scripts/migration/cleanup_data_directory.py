#!/usr/bin/env python3
"""
Data Directory Cleanup Script

Identifies and optionally cleans up:
- Old SQLite database files (legacy migrations)
- Stale lock files
- Old backup files
- Temporary files

Usage:
    python scripts/cleanup_data_directory.py --dry-run  # Preview what would be cleaned
    python scripts/cleanup_data_directory.py --clean     # Actually clean files
"""

import sys
from pathlib import Path
from datetime import datetime
from typing import List, Dict, Tuple

# Add project root to path
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

# Configuration
DATA_DIR = project_root / "data"
DRY_RUN = "--dry-run" in sys.argv
CLEAN = "--clean" in sys.argv

# Active databases (don't delete these)
ACTIVE_DATABASES = {
    "governance_new.db",  # Current PostgreSQL migration target
    "knowledge_graph.db",  # Active knowledge graph
}

# Patterns to identify legacy/migration files
LEGACY_PATTERNS = [
    "*.db-shm.__migrated__",
    "*.db-wal.__migrated__",
]

# Age thresholds (days)
BACKUP_MAX_AGE_DAYS = 90
LOG_MAX_AGE_DAYS = 30
TEMP_MAX_AGE_DAYS = 7


def get_file_age_days(filepath: Path) -> float:
    """Get age of file in days."""
    try:
        mtime = filepath.stat().st_mtime
        age_seconds = datetime.now().timestamp() - mtime
        return age_seconds / 86400
    except (OSError, AttributeError):
        return 0


def find_database_files() -> List[Tuple[Path, Dict]]:
    """Find all database files and categorize them."""
    files = []
    
    for db_file in DATA_DIR.glob("*.db"):
        if db_file.name in ACTIVE_DATABASES:
            status = "active"
        elif "governance.db" in db_file.name and "governance_new.db" not in db_file.name:
            status = "legacy"  # Old governance.db before migration
        elif "knowledge.db" in db_file.name and "knowledge_graph.db" not in db_file.name:
            status = "legacy"  # Old knowledge.db before migration
        else:
            status = "unknown"
        
        age_days = get_file_age_days(db_file)
        files.append((db_file, {
            "type": "database",
            "status": status,
            "age_days": age_days,
            "size_mb": db_file.stat().st_size / (1024 * 1024)
        }))
    
    # Find WAL/SHM files
    for pattern in ["*.db-shm", "*.db-wal"]:
        for wal_file in DATA_DIR.glob(pattern):
            # Check if corresponding .db file exists
            db_name = wal_file.name.replace("-shm", "").replace("-wal", "")
            db_file = DATA_DIR / db_name
            
            if not db_file.exists():
                status = "orphaned"
            elif db_name in ACTIVE_DATABASES:
                status = "active"
            else:
                status = "legacy"
            
            age_days = get_file_age_days(wal_file)
            files.append((wal_file, {
                "type": "wal/shm",
                "status": status,
                "age_days": age_days,
                "size_mb": wal_file.stat().st_size / (1024 * 1024)
            }))
    
    # Find migration markers
    for pattern in LEGACY_PATTERNS:
        for mig_file in DATA_DIR.glob(pattern):
            age_days = get_file_age_days(mig_file)
            files.append((mig_file, {
                "type": "migration_marker",
                "status": "legacy",
                "age_days": age_days,
                "size_mb": mig_file.stat().st_size / (1024 * 1024)
            }))
    
    return files


def find_old_backups() -> List[Tuple[Path, Dict]]:
    """Find old backup files."""
    files = []
    backups_dir = DATA_DIR / "backups"
    
    if not backups_dir.exists():
        return files
    
    for backup_file in backups_dir.rglob("*"):
        if not backup_file.is_file():
            continue
        
        age_days = get_file_age_days(backup_file)
        if age_days > BACKUP_MAX_AGE_DAYS:
            files.append((backup_file, {
                "type": "backup",
                "age_days": age_days,
                "size_mb": backup_file.stat().st_size / (1024 * 1024)
            }))
    
    return files


def find_old_logs() -> List[Tuple[Path, Dict]]:
    """Find old log files."""
    files = []
    logs_dir = DATA_DIR / "logs"
    
    if not logs_dir.exists():
        return files
    
    for log_file in logs_dir.rglob("*.log"):
        if not log_file.is_file():
            continue
        
        age_days = get_file_age_days(log_file)
        if age_days > LOG_MAX_AGE_DAYS:
            files.append((log_file, {
                "type": "log",
                "age_days": age_days,
                "size_mb": log_file.stat().st_size / (1024 * 1024)
            }))
    
    # Also check root data directory for log files
    for log_file in DATA_DIR.glob("*.log"):
        age_days = get_file_age_days(log_file)
        if age_days > LOG_MAX_AGE_DAYS:
            files.append((log_file, {
                "type": "log",
                "age_days": age_days,
                "size_mb": log_file.stat().st_size / (1024 * 1024)
            }))
    
    return files


def find_temp_files() -> List[Tuple[Path, Dict]]:
    """Find temporary files."""
    files = []
    
    temp_patterns = ["*.tmp", "*.bak", "*.old", "*.swp", "*.swo"]
    for pattern in temp_patterns:
        for temp_file in DATA_DIR.rglob(pattern):
            if not temp_file.is_file():
                continue
            
            age_days = get_file_age_days(temp_file)
            if age_days > TEMP_MAX_AGE_DAYS:
                files.append((temp_file, {
                    "type": "temp",
                    "age_days": age_days,
                    "size_mb": temp_file.stat().st_size / (1024 * 1024)
                }))
    
    return files


def format_size(size_mb: float) -> str:
    """Format file size."""
    if size_mb < 1:
        return f"{size_mb * 1024:.1f} KB"
    return f"{size_mb:.2f} MB"


def main():
    """Main cleanup function."""
    print("=" * 80)
    print("UNITARES Data Directory Cleanup")
    print("=" * 80)
    print()
    
    if DRY_RUN:
        print("🔍 DRY RUN MODE - No files will be deleted")
    elif CLEAN:
        print("🧹 CLEAN MODE - Files will be deleted")
    else:
        print("ℹ️  Preview mode - use --dry-run to preview or --clean to actually clean")
        print()
    
    print()
    
    # Collect all files
    all_files = []
    all_files.extend(find_database_files())
    all_files.extend(find_old_backups())
    all_files.extend(find_old_logs())
    all_files.extend(find_temp_files())
    
    # Categorize
    active_files = []
    legacy_files = []
    orphaned_files = []
    old_files = []
    
    for filepath, info in all_files:
        if info["status"] == "active":
            active_files.append((filepath, info))
        elif info["status"] == "orphaned":
            orphaned_files.append((filepath, info))
        elif info["status"] == "legacy":
            legacy_files.append((filepath, info))
        elif info.get("age_days", 0) > max(BACKUP_MAX_AGE_DAYS, LOG_MAX_AGE_DAYS, TEMP_MAX_AGE_DAYS):
            old_files.append((filepath, info))
    
    # Report
    print("📊 Summary:")
    print(f"   Active files: {len(active_files)}")
    print(f"   Legacy files: {len(legacy_files)}")
    print(f"   Orphaned files: {len(orphaned_files)}")
    print(f"   Old files: {len(old_files)}")
    print()
    
    # Show active files
    if active_files:
        print("✅ Active Files (will NOT be deleted):")
        total_size = 0
        for filepath, info in sorted(active_files, key=lambda x: x[0].name):
            size = info["size_mb"]
            total_size += size
            print(f"   {filepath.name:50s} {format_size(size):>10s} ({info['type']})")
        print(f"   Total: {format_size(total_size)}")
        print()
    
    # Show files to clean
    files_to_clean = legacy_files + orphaned_files + old_files
    if files_to_clean:
        print("🗑️  Files to Clean:")
        total_size = 0
        for filepath, info in sorted(files_to_clean, key=lambda x: x[0].name):
            size = info["size_mb"]
            total_size += size
            age = info.get("age_days", 0)
            print(f"   {filepath.name:50s} {format_size(size):>10s} ({info['type']}, {age:.1f} days old)")
        print(f"   Total: {format_size(total_size)}")
        print()
        
        if CLEAN:
            print("🧹 Cleaning files...")
            deleted = 0
            errors = 0
            for filepath, info in files_to_clean:
                try:
                    filepath.unlink()
                    deleted += 1
                    print(f"   ✓ Deleted: {filepath.name}")
                except Exception as e:
                    errors += 1
                    print(f"   ✗ Error deleting {filepath.name}: {e}")
            
            print()
            print(f"✅ Deleted {deleted} files")
            if errors > 0:
                print(f"⚠️  {errors} errors")
        elif DRY_RUN:
            print("💡 Run with --clean to actually delete these files")
    else:
        print("✨ No files to clean!")
    
    print()
    print("=" * 80)


if __name__ == "__main__":
    main()
