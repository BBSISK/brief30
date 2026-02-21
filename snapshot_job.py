"""
snapshot_job.py
───────────────
Run this as a PythonAnywhere scheduled task — daily is fine.

It checks every user and takes a snapshot if:
  1. 90+ days have passed since their last snapshot (or account creation)
  2. Their current entry differs from their last snapshot

PythonAnywhere setup:
  Tasks tab → Add task → Daily at 03:00 UTC
  Command: python3 /home/bbsisk/brief30/snapshot_job.py

Output is written to snapshot_job.log in the same directory,
and also printed to stdout (visible in PythonAnywhere task log).
"""

import sqlite3
import os
from datetime import datetime, timedelta, timezone

def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)

DATABASE = os.path.join(os.path.dirname(__file__), 'brief30.db')
LOGFILE  = os.path.join(os.path.dirname(__file__), 'snapshot_job.log')

def run():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys = ON')

    now     = utcnow()
    cutoff  = now - timedelta(days=90)
    snapped = 0
    skipped = 0
    lines   = []

    def log(msg):
        print(msg)
        lines.append(msg)

    log(f"Brief snapshot job — {now.strftime('%Y-%m-%d %H:%M')} UTC")
    log("─" * 48)

    users = conn.execute('SELECT * FROM users').fetchall()

    for user in users:
        username = user['username']

        if not user['entry']:
            skipped += 1
            continue

        last_snap = conn.execute(
            'SELECT entry, taken_at FROM snapshots WHERE username = ? ORDER BY taken_at DESC LIMIT 1',
            (username,)
        ).fetchone()

        if last_snap:
            last_date = datetime.fromisoformat(last_snap['taken_at'])
            if last_date > cutoff:
                skipped += 1
                continue
            if last_snap['entry'] == user['entry']:
                # Entry unchanged — nudge updated_at so we don't re-check for 90 days
                conn.execute('UPDATE users SET updated_at = ? WHERE username = ?',
                             (now.isoformat(), username))
                skipped += 1
                continue
        else:
            created = datetime.fromisoformat(user['created_at'])
            if created > cutoff:
                skipped += 1
                continue

        # Take snapshot — include mood column
        mood = user['mood'] if 'mood' in user.keys() else None
        conn.execute(
            'INSERT INTO snapshots (username, entry, mood, taken_at) VALUES (?, ?, ?, ?)',
            (username, user['entry'], mood, now.isoformat())
        )
        snapped += 1
        log(f"  ✓ {username}")

    conn.commit()
    conn.close()

    log("─" * 48)
    log(f"Done — {snapped} snapshot(s) taken, {skipped} skipped.\n")

    # Append to rolling log file (keeps last 500 lines)
    try:
        existing = []
        if os.path.exists(LOGFILE):
            with open(LOGFILE, 'r') as f:
                existing = f.readlines()
        combined = existing + [l + '\n' for l in lines]
        with open(LOGFILE, 'w') as f:
            f.writelines(combined[-500:])
    except Exception as e:
        print(f"Warning: could not write log file: {e}")

if __name__ == '__main__':
    run()
