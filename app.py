from flask import (Flask, render_template, request, redirect, url_for,
                   make_response, jsonify, Response)
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from better_profanity import profanity as _profanity
import sqlite3
import secrets
import re
from collections import Counter
from datetime import datetime, timedelta
import os

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', secrets.token_hex(32))

DATABASE  = os.path.join(os.path.dirname(__file__), 'brief30.db')
WORDLIST  = os.path.join(os.path.dirname(__file__), 'brief30_wordlist.txt')

# Admin password — set via environment variable, fallback for dev only
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', 'changeme')

# Set SECURE_COOKIES=true in PythonAnywhere environment variables once HTTPS is live
SECURE_COOKIES = os.environ.get('SECURE_COOKIES', 'false').lower() == 'true'

# ─────────────────────────────────────────
# Profanity Filter
# ─────────────────────────────────────────

def _load_custom_words():
    words = []
    if os.path.exists(WORDLIST):
        with open(WORDLIST, encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#'):
                    words.append(line.lower())
    return words

_profanity.load_censor_words()
_extra_words = _load_custom_words()
if _extra_words:
    _profanity.add_censor_words(_extra_words)

_BYPASS_MAP = str.maketrans({
    '@': 'a', '3': 'e', '1': 'i', '0': 'o',
    '$': 's', '5': 's', '7': 't', '!': 'i',
    '+': 't', '4': 'a',
})

_WHITELIST = {'feck', 'fecking', 'bloody', 'damn', 'hell', 'crap', 'arse'}

def _normalise(text):
    text = re.sub(r'(?<![a-z])f\*{2,4}(?![a-z])', 'fuck',  text)
    text = re.sub(r'(?<![a-z])s\*{2,3}(?![a-z])', 'shit',  text)
    text = re.sub(r'(?<![a-z])c\*{2,4}(?![a-z])', 'cunt',  text)
    text = re.sub(r'(?<![a-z])b\*{4,5}(?![a-z])', 'bitch', text)
    text = re.sub(r'(?<![a-z])([a-z])\*+([a-z]?)(?![a-z])',
                  lambda m: m.group(1) + 'u' * max(1, len(m.group(0)) - len(m.group(1)) - len(m.group(2))) + m.group(2),
                  text)
    text = re.sub(r'\b([a-z](\s[a-z]){2,})\b',
                  lambda m: m.group(0).replace(' ', ''), text)
    text = text.translate(_BYPASS_MAP)
    return text

def contains_profanity(text):
    if not text:
        return False
    lower      = text.lower()
    normalised = _normalise(lower)
    for candidate in (lower, normalised):
        if _profanity.contains_profanity(candidate):
            censored = _profanity.censor(candidate)
            if censored != candidate:
                words_in = set(re.findall(r"[a-z']+", candidate))
                flagged  = words_in - set(re.findall(r"[a-z']+", censored.replace('*', '')))
                if flagged - _WHITELIST:
                    return True
    return False

PROFANITY_MSG = "Please keep your entry family-friendly — Brief is a shared space."

# ─────────────────────────────────────────
# Rate Limiting
# ─────────────────────────────────────────

limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=[],
    storage_uri='memory://'
)

# ─────────────────────────────────────────
# Database
# ─────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys = ON')
    return conn

def init_db():
    with get_db() as db:
        db.executescript('''
            CREATE TABLE IF NOT EXISTS users (
                username        TEXT PRIMARY KEY,
                entry           TEXT,
                mood            TEXT,
                claim_token     TEXT UNIQUE NOT NULL,
                created_at      TEXT NOT NULL,
                updated_at      TEXT NOT NULL,
                in_strangers    INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS snapshots (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                username    TEXT NOT NULL,
                entry       TEXT NOT NULL,
                mood        TEXT,
                taken_at    TEXT NOT NULL,
                FOREIGN KEY (username) REFERENCES users(username)
            );

            CREATE TABLE IF NOT EXISTS groups (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                name         TEXT NOT NULL,
                invite_token TEXT UNIQUE NOT NULL,
                mode         TEXT NOT NULL DEFAULT 'open',
                created_by   TEXT NOT NULL,
                created_at   TEXT NOT NULL,
                FOREIGN KEY (created_by) REFERENCES users(username)
            );

            CREATE TABLE IF NOT EXISTS group_members (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id   INTEGER NOT NULL,
                username   TEXT NOT NULL,
                is_admin   INTEGER NOT NULL DEFAULT 0,
                joined_at  TEXT NOT NULL,
                UNIQUE (group_id, username),
                FOREIGN KEY (group_id) REFERENCES groups(id),
                FOREIGN KEY (username) REFERENCES users(username)
            );

            CREATE TABLE IF NOT EXISTS capsules (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                username     TEXT NOT NULL,
                entry        TEXT NOT NULL,
                reveal_date  TEXT NOT NULL,
                created_at   TEXT NOT NULL,
                revealed     INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY (username) REFERENCES users(username)
            );
        ''')
        try:
            db.execute('ALTER TABLE users ADD COLUMN in_strangers INTEGER NOT NULL DEFAULT 0')
        except Exception:
            pass
        try:
            db.execute('ALTER TABLE users ADD COLUMN mood TEXT')
        except Exception:
            pass
        try:
            db.execute('ALTER TABLE snapshots ADD COLUMN mood TEXT')
        except Exception:
            pass

init_db()

# ─────────────────────────────────────────
# Core Helpers
# ─────────────────────────────────────────

STOPWORDS = {
    'a','an','the','and','or','but','in','on','at','to','for','of','with',
    'is','was','are','were','be','been','being','have','has','had','do',
    'does','did','will','would','could','should','may','might','shall',
    'i','me','my','we','our','you','your','he','his','she','her','it','its',
    'they','their','them','this','that','these','those','so','as','if',
    'not','no','nor','yet','both','either','just','very','too','also','up',
    'out','still','about','after','before','into','from','by','than','then',
    'when','while','who','which','what','how','all','more','some','am','s',
}

def count_words(text):
    return len(text.split()) if text and text.strip() else 0

# ─────────────────────────────────────────
# Community Word Cloud
# ─────────────────────────────────────────

CLOUD_SEEDS = {
    'mother': 3, 'father': 3, 'daughter': 3, 'son': 3, 'husband': 3,
    'wife': 3, 'partner': 3, 'friend': 3, 'family': 3, 'children': 3,
    'parents': 3, 'sister': 3, 'brother': 3, 'baby': 3,
    'home': 3, 'city': 3, 'house': 3, 'garden': 3, 'ireland': 3,
    'dublin': 3, 'country': 3, 'town': 3, 'street': 3, 'sea': 3,
    'work': 3, 'job': 3, 'teacher': 3, 'building': 3, 'making': 3,
    'writing': 3, 'learning': 3, 'studying': 3, 'project': 3,
    'business': 3, 'school': 3, 'career': 3,
    'happy': 3, 'tired': 3, 'grateful': 3, 'lost': 3, 'hopeful': 3,
    'anxious': 3, 'content': 3, 'uncertain': 3, 'calm': 3, 'proud': 3,
    'lonely': 3, 'excited': 3, 'overwhelmed': 3, 'peaceful': 3,
    'finally': 3, 'slowly': 3, 'growing': 3, 'changing': 3, 'starting': 3,
    'trying': 3, 'moving': 3, 'waiting': 3, 'beginning': 3, 'older': 3,
    'recently': 3, 'someday': 3,
    'love': 3, 'health': 3, 'money': 3, 'time': 3, 'nature': 3,
    'music': 3, 'food': 3, 'running': 3, 'reading': 3, 'coffee': 3,
    'dog': 3, 'cat': 3, 'sleep': 3, 'travel': 3, 'adventure': 3,
    'better': 3, 'enough': 3, 'good': 3, 'small': 3, 'quiet': 3,
    'progress': 3, 'purpose': 3, 'wonder': 3, 'ordinary': 3, 'simple': 3,
}

def get_community_cloud(max_words=60):
    freq = Counter(CLOUD_SEEDS)
    with get_db() as db:
        rows = db.execute(
            "SELECT entry FROM users WHERE entry IS NOT NULL AND entry != ''"
        ).fetchall()
    for row in rows:
        words = re.findall(r"[a-z']+", row['entry'].lower())
        for w in words:
            w = w.strip("'")
            if w not in STOPWORDS and len(w) > 2 and not w.isdigit():
                freq[w] += 1
    top = [(w, c) for w, c in freq.most_common(max_words + 20)
           if not contains_profanity(w)][:max_words]
    if not top:
        return []
    min_c = min(c for _, c in top)
    max_c = max(c for _, c in top)
    span  = max_c - min_c
    if span == 0:
        n      = len(top)
        result = [(w, i / max(n - 1, 1)) for i, (w, _) in enumerate(top)]
    else:
        result = [(w, (c - min_c) / span) for w, c in top]
    import random
    random.shuffle(result)
    return result

# ─────────────────────────────────────────
# User Helpers
# ─────────────────────────────────────────

def get_user(username):
    with get_db() as db:
        return db.execute('SELECT * FROM users WHERE username = ?', (username,)).fetchone()

def get_snapshots(username):
    with get_db() as db:
        return db.execute(
            'SELECT * FROM snapshots WHERE username = ? ORDER BY taken_at DESC',
            (username,)
        ).fetchall()

def maybe_take_snapshot(username):
    user = get_user(username)
    if not user or not user['entry']:
        return
    updated = datetime.fromisoformat(user['updated_at'])
    now     = datetime.utcnow()
    if (now - updated).days < 90:
        return
    with get_db() as db:
        last_snap = db.execute(
            'SELECT entry FROM snapshots WHERE username = ? ORDER BY taken_at DESC LIMIT 1',
            (username,)
        ).fetchone()
        if last_snap and last_snap['entry'] == user['entry']:
            db.execute('UPDATE users SET updated_at = ? WHERE username = ?',
                       (now.isoformat(), username))
            return
        db.execute(
            'INSERT INTO snapshots (username, entry, mood, taken_at) VALUES (?, ?, ?, ?)',
            (username, user['entry'], user['mood'], now.isoformat())
        )
        db.execute('UPDATE users SET updated_at = ? WHERE username = ?',
                   (now.isoformat(), username))

def get_username_from_request():
    token    = request.cookies.get('claim_token')
    username = request.cookies.get('username')
    if not token or not username:
        return None
    user = get_user(username)
    if user and user['claim_token'] == token:
        return username
    return None

# ─────────────────────────────────────────
# Stats Helper
# ─────────────────────────────────────────

def compute_user_stats(username):
    snaps    = get_snapshots(username)
    user     = get_user(username)
    all_text = ' '.join(
        [user['entry'] or ''] + [s['entry'] for s in snaps]
    ).lower()
    words    = re.findall(r"[a-z']+", all_text)
    filtered = [w.strip("'") for w in words
                if w.strip("'") not in STOPWORDS and len(w.strip("'")) > 2]
    freq     = Counter(filtered).most_common(25)
    days_member = 0
    if snaps:
        first       = datetime.fromisoformat(snaps[-1]['taken_at'])
        days_member = (datetime.utcnow() - first).days
    return {'word_freq': freq, 'snap_count': len(snaps), 'days_member': days_member}

# ─────────────────────────────────────────
# Group Helpers
# ─────────────────────────────────────────

def get_group(group_id):
    with get_db() as db:
        return db.execute('SELECT * FROM groups WHERE id = ?', (group_id,)).fetchone()

def get_group_by_token(token):
    with get_db() as db:
        return db.execute('SELECT * FROM groups WHERE invite_token = ?', (token,)).fetchone()

def get_group_members(group_id):
    with get_db() as db:
        return db.execute('''
            SELECT gm.username, gm.is_admin, gm.joined_at, u.entry, u.mood, u.updated_at
            FROM group_members gm
            JOIN users u ON u.username = gm.username
            WHERE gm.group_id = ?
            ORDER BY gm.joined_at ASC
        ''', (group_id,)).fetchall()

def get_member_snapshots(group_id):
    with get_db() as db:
        return db.execute('''
            SELECT s.username, s.entry, s.taken_at
            FROM snapshots s
            JOIN group_members gm ON gm.username = s.username AND gm.group_id = ?
            ORDER BY s.taken_at DESC
        ''', (group_id,)).fetchall()

def get_user_groups(username):
    with get_db() as db:
        return db.execute('''
            SELECT g.*, gm.is_admin, gm.joined_at
            FROM groups g
            JOIN group_members gm ON gm.group_id = g.id
            WHERE gm.username = ?
            ORDER BY gm.joined_at ASC
        ''', (username,)).fetchall()

def is_group_member(group_id, username):
    with get_db() as db:
        return db.execute(
            'SELECT 1 FROM group_members WHERE group_id = ? AND username = ?',
            (group_id, username)
        ).fetchone() is not None

def is_group_admin(group_id, username):
    with get_db() as db:
        row = db.execute(
            'SELECT is_admin FROM group_members WHERE group_id = ? AND username = ?',
            (group_id, username)
        ).fetchone()
        return row is not None and row['is_admin'] == 1

def get_member_count(group_id):
    with get_db() as db:
        return db.execute(
            'SELECT COUNT(*) as c FROM group_members WHERE group_id = ?', (group_id,)
        ).fetchone()['c']

def promote_oldest_member(db, group_id, exclude_username):
    oldest = db.execute('''
        SELECT username FROM group_members
        WHERE group_id = ? AND username != ?
        ORDER BY joined_at ASC LIMIT 1
    ''', (group_id, exclude_username)).fetchone()
    if oldest:
        db.execute(
            'UPDATE group_members SET is_admin = 1 WHERE group_id = ? AND username = ?',
            (group_id, oldest['username'])
        )
        return oldest['username']
    return None

def handle_user_leaving_groups(username):
    with get_db() as db:
        memberships = db.execute(
            'SELECT group_id, is_admin FROM group_members WHERE username = ?',
            (username,)
        ).fetchall()
        for m in memberships:
            gid   = m['group_id']
            count = db.execute(
                'SELECT COUNT(*) as c FROM group_members WHERE group_id = ?', (gid,)
            ).fetchone()['c']
            if count <= 1:
                db.execute('DELETE FROM group_members WHERE group_id = ?', (gid,))
                db.execute('DELETE FROM groups WHERE id = ?', (gid,))
            else:
                db.execute(
                    'DELETE FROM group_members WHERE group_id = ? AND username = ?',
                    (gid, username)
                )
                if m['is_admin']:
                    promote_oldest_member(db, gid, username)

# ─────────────────────────────────────────
# Capsule Helpers
# ─────────────────────────────────────────

CAPSULE_YEARS = [1, 5, 10]

def get_capsules(username):
    with get_db() as db:
        return db.execute(
            'SELECT * FROM capsules WHERE username = ? ORDER BY reveal_date ASC',
            (username,)
        ).fetchall()

def get_capsule(capsule_id):
    with get_db() as db:
        return db.execute('SELECT * FROM capsules WHERE id = ?', (capsule_id,)).fetchone()

def check_capsule_reveals(username):
    now = datetime.utcnow().isoformat()
    with get_db() as db:
        db.execute(
            'UPDATE capsules SET revealed = 1 WHERE username = ? AND reveal_date <= ? AND revealed = 0',
            (username, now)
        )

# ─────────────────────────────────────────
# Admin Helper
# ─────────────────────────────────────────

def check_admin_auth():
    return request.cookies.get('admin_auth') == ADMIN_PASSWORD

# ─────────────────────────────────────────
# Cookie Helper
# ─────────────────────────────────────────

def set_auth_cookie(resp, name, value, max_age=60*60*24*365*10):
    """Set an auth cookie with consistent security settings."""
    resp.set_cookie(
        name, value,
        max_age=max_age,
        httponly=True,
        samesite='Lax',
        secure=SECURE_COOKIES,
    )

# ─────────────────────────────────────────
# Mood
# ─────────────────────────────────────────

ALLOWED_MOODS = {
    '😊','😌','🙂','😐','😔','😢','😤','😅','🤔','😴',
    '🥹','🤩','😎','🥰','😬','🫠','😮','🎉','🌱','🔥',
}

def valid_mood(m):
    return m if m in ALLOWED_MOODS else None

# ─────────────────────────────────────────
# Routes — Landing / Home
# ─────────────────────────────────────────

@app.route('/about')
def about():
    return render_template('landing.html', allowed_moods=ALLOWED_MOODS,
                           cloud=get_community_cloud())


@app.route('/api/word-cloud')
def api_word_cloud():
    return jsonify({'words': get_community_cloud()})


@app.route('/')
def index():
    username = get_username_from_request()
    user     = get_user(username) if username else None
    if user:
        maybe_take_snapshot(username)
        snaps = get_snapshots(username)
        return render_template('home.html', user=user, snapshots=snaps, claimed=True)
    return render_template('landing.html', allowed_moods=ALLOWED_MOODS,
                           cloud=get_community_cloud())


@app.route('/claim', methods=['POST'])
@limiter.limit('10 per hour')
def claim():
    username = request.form.get('username', '').strip()
    entry    = request.form.get('entry', '').strip()
    mood     = valid_mood(request.form.get('mood', '').strip())

    if not username or not username.isalnum() or len(username) < 3 or len(username) > 20:
        return render_template('landing.html', allowed_moods=ALLOWED_MOODS,
                               cloud=get_community_cloud(),
                               error="Username must be 3–20 letters/numbers only.")
    if count_words(entry) > 30:
        return render_template('landing.html', allowed_moods=ALLOWED_MOODS,
                               cloud=get_community_cloud(),
                               error="Your entry must be 30 words or fewer.")
    if contains_profanity(entry):
        return render_template('landing.html', allowed_moods=ALLOWED_MOODS,
                               cloud=get_community_cloud(),
                               error=PROFANITY_MSG)
    if get_user(username):
        return render_template('landing.html', allowed_moods=ALLOWED_MOODS,
                               cloud=get_community_cloud(),
                               error=f"'{username}' is already taken. Try another name.")

    token = secrets.token_urlsafe(24)
    now   = datetime.utcnow().isoformat()
    with get_db() as db:
        db.execute(
            'INSERT INTO users (username, entry, mood, claim_token, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)',
            (username, entry, mood, token, now, now)
        )
        if entry:
            db.execute(
                'INSERT INTO snapshots (username, entry, mood, taken_at) VALUES (?, ?, ?, ?)',
                (username, entry, mood, now)
            )

    resp = make_response(redirect(url_for('profile', username=username)))
    set_auth_cookie(resp, 'claim_token', token)
    set_auth_cookie(resp, 'username', username)
    return resp

# ─────────────────────────────────────────
# Routes — Profile
# ─────────────────────────────────────────

@app.route('/profile/<username>')
def profile(username):
    current_user = get_username_from_request()
    user         = get_user(username)
    if not user:
        return render_template('404.html'), 404

    maybe_take_snapshot(username)
    snaps    = get_snapshots(username)
    is_owner = (current_user == username)
    stats    = compute_user_stats(username) if is_owner and snaps else None

    check_capsule_reveals(username)
    capsules = get_capsules(username) if is_owner else []

    error = None
    if request.args.get('error') == 'profanity':
        error = PROFANITY_MSG

    return render_template('profile.html',
                           user=user, snapshots=snaps,
                           is_owner=is_owner, stats=stats,
                           capsules=capsules,
                           capsule_years=CAPSULE_YEARS,
                           allowed_moods=ALLOWED_MOODS,
                           error=error)


@app.route('/update', methods=['POST'])
def update():
    username = get_username_from_request()
    if not username:
        return redirect(url_for('index'))
    entry = request.form.get('entry', '').strip()
    mood  = valid_mood(request.form.get('mood', '').strip())

    def render_profile_error(msg):
        user  = get_user(username)
        snaps = get_snapshots(username)
        return render_template('profile.html', user=user, snapshots=snaps,
                               is_owner=True, stats=compute_user_stats(username),
                               capsules=get_capsules(username),
                               capsule_years=CAPSULE_YEARS,
                               allowed_moods=ALLOWED_MOODS,
                               error=msg)

    if count_words(entry) > 30:
        return render_profile_error("Your entry must be 30 words or fewer.")
    if contains_profanity(entry):
        return render_profile_error(PROFANITY_MSG)

    now = datetime.utcnow().isoformat()
    with get_db() as db:
        db.execute('UPDATE users SET entry = ?, mood = ?, updated_at = ? WHERE username = ?',
                   (entry, mood, now, username))
    return redirect(url_for('profile', username=username))


@app.route('/delete', methods=['POST'])
def delete():
    username = get_username_from_request()
    if not username:
        return redirect(url_for('index'))
    handle_user_leaving_groups(username)
    with get_db() as db:
        db.execute('DELETE FROM capsules  WHERE username = ?', (username,))
        db.execute('DELETE FROM snapshots WHERE username = ?', (username,))
        db.execute('DELETE FROM users     WHERE username = ?', (username,))
    resp = make_response(redirect(url_for('index')))
    resp.delete_cookie('claim_token')
    resp.delete_cookie('username')
    return resp


@app.route('/claim-link')
def claim_link():
    username = get_username_from_request()
    if not username:
        return redirect(url_for('index'))
    user      = get_user(username)
    claim_url = url_for('restore', token=user['claim_token'], _external=True)
    return render_template('claim_link.html', username=username, claim_url=claim_url)


@app.route('/restore/<token>')
def restore(token):
    with get_db() as db:
        user = db.execute('SELECT * FROM users WHERE claim_token = ?', (token,)).fetchone()
    if not user:
        return render_template('home.html', error="Invalid or expired claim link.",
                               user=None, claimed=False)
    resp = make_response(redirect(url_for('profile', username=user['username'])))
    set_auth_cookie(resp, 'claim_token', token)
    set_auth_cookie(resp, 'username', user['username'])
    return resp


@app.route('/api/check-username')
def check_username():
    username = request.args.get('username', '').strip()
    if not username:
        return jsonify({'available': False})
    return jsonify({'available': get_user(username) is None})

# ─────────────────────────────────────────
# Routes — Export
# ─────────────────────────────────────────

@app.route('/export')
def export():
    username = get_username_from_request()
    if not username:
        return redirect(url_for('index'))
    user  = get_user(username)
    snaps = get_snapshots(username)
    lines = [
        f"Brief — {username}",
        f"Member since: {user['created_at'][:10]}",
        f"Exported:     {datetime.utcnow().strftime('%Y-%m-%d')}",
        "", "─" * 40, "",
    ]
    if user['entry']:
        lines += [
            "Current entry:",
            f"  \"{user['entry']}\"",
            f"  (last updated {user['updated_at'][:10]})",
            "", "─" * 40, "",
        ]
    if snaps:
        lines.append("Snapshot history (newest first):")
        lines.append("")
        for s in snaps:
            lines.append(f"  {s['taken_at'][:10]}")
            lines.append(f"  \"{s['entry']}\"")
            lines.append("")
    return Response(
        "\n".join(lines),
        mimetype='text/plain',
        headers={'Content-Disposition': f'attachment; filename="brief-{username}.txt"'}
    )

# ─────────────────────────────────────────
# Routes — Strangers Feed
# ─────────────────────────────────────────

@app.route('/strangers')
def strangers():
    with get_db() as db:
        entries = db.execute('''
            SELECT entry, mood, updated_at FROM users
            WHERE in_strangers = 1 AND entry IS NOT NULL AND entry != ''
            ORDER BY RANDOM()
        ''').fetchall()
    return render_template('strangers.html', entries=entries)


@app.route('/strangers/toggle', methods=['POST'])
def strangers_toggle():
    username = get_username_from_request()
    if not username:
        return redirect(url_for('index'))
    user    = get_user(username)
    new_val = 0 if user['in_strangers'] else 1
    with get_db() as db:
        db.execute('UPDATE users SET in_strangers = ? WHERE username = ?',
                   (new_val, username))
    return redirect(url_for('profile', username=username))

# ─────────────────────────────────────────
# Routes — Time Capsules
# ─────────────────────────────────────────

@app.route('/capsule/create', methods=['POST'])
def capsule_create():
    username = get_username_from_request()
    if not username:
        return redirect(url_for('index'))
    entry = request.form.get('capsule_entry', '').strip()
    try:
        years = int(request.form.get('years', ''))
        assert years in CAPSULE_YEARS
    except (ValueError, AssertionError):
        return redirect(url_for('profile', username=username))
    if not entry or count_words(entry) > 30:
        return redirect(url_for('profile', username=username))
    if contains_profanity(entry):
        return redirect(url_for('profile', username=username, error='profanity'))

    now         = datetime.utcnow()
    reveal_date = now.replace(year=now.year + years).isoformat()
    with get_db() as db:
        db.execute(
            'INSERT INTO capsules (username, entry, reveal_date, created_at, revealed) VALUES (?, ?, ?, ?, 0)',
            (username, entry, reveal_date, now.isoformat())
        )
    return redirect(url_for('profile', username=username))


@app.route('/capsule/<int:capsule_id>/delete', methods=['POST'])
def capsule_delete(capsule_id):
    username = get_username_from_request()
    if not username:
        return redirect(url_for('index'))
    capsule = get_capsule(capsule_id)
    if not capsule or capsule['username'] != username:
        return redirect(url_for('profile', username=username))
    with get_db() as db:
        db.execute('DELETE FROM capsules WHERE id = ?', (capsule_id,))
    return redirect(url_for('profile', username=username))

# ─────────────────────────────────────────
# Routes — Groups
# ─────────────────────────────────────────

@app.route('/groups')
def groups_list():
    username = get_username_from_request()
    if not username:
        return redirect(url_for('index'))
    return render_template('groups_list.html', username=username,
                           user_groups=get_user_groups(username))


@app.route('/groups/create', methods=['GET', 'POST'])
def create_group():
    username = get_username_from_request()
    if not username:
        return redirect(url_for('index'))
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        mode = request.form.get('mode', 'open')
        if mode not in ('open', 'closed'):
            mode = 'open'
        if not name or len(name) > 60:
            return render_template('create_group.html', username=username,
                                   error="Group name must be 1–60 characters.")
        now   = datetime.utcnow().isoformat()
        token = secrets.token_urlsafe(16)
        with get_db() as db:
            db.execute(
                'INSERT INTO groups (name, invite_token, mode, created_by, created_at) VALUES (?, ?, ?, ?, ?)',
                (name, token, mode, username, now)
            )
            group_id = db.execute('SELECT last_insert_rowid() as id').fetchone()['id']
            db.execute(
                'INSERT INTO group_members (group_id, username, is_admin, joined_at) VALUES (?, ?, 1, ?)',
                (group_id, username, now)
            )
        return redirect(url_for('group_view', group_id=group_id))
    return render_template('create_group.html', username=username)


@app.route('/groups/<int:group_id>')
def group_view(group_id):
    username  = get_username_from_request()
    group     = get_group(group_id)
    if not group:
        return render_template('404.html'), 404
    is_member = is_group_member(group_id, username) if username else False
    is_admin  = is_group_admin(group_id, username)  if username else False
    if not is_member:
        invite_url = url_for('join_group', token=group['invite_token'], _external=True)
        return render_template('group_join_prompt.html',
                               group=group, invite_url=invite_url,
                               username=username, can_join=(group['mode'] == 'open'))
    members       = get_group_members(group_id)
    all_snaps     = get_member_snapshots(group_id)
    invite_url    = url_for('join_group', token=group['invite_token'], _external=True)
    snaps_by_user = {}
    for s in all_snaps:
        snaps_by_user.setdefault(s['username'], []).append(s)
    return render_template('group_view.html',
                           group=group, members=members,
                           snaps_by_user=snaps_by_user,
                           invite_url=invite_url,
                           username=username, is_admin=is_admin)


@app.route('/groups/join/<token>')
def join_group(token):
    username = get_username_from_request()
    group    = get_group_by_token(token)
    if not group:
        return render_template('404.html'), 404
    if not username:
        return render_template('home.html', user=None, claimed=False,
                               error="Create your entry first, then use the invite link to join the group.")
    if is_group_member(group['id'], username):
        return redirect(url_for('group_view', group_id=group['id']))
    if group['mode'] == 'closed':
        return render_template('group_join_prompt.html', group=group,
                               username=username, can_join=False,
                               invite_url=url_for('join_group', token=token, _external=True))
    now = datetime.utcnow().isoformat()
    with get_db() as db:
        db.execute(
            'INSERT INTO group_members (group_id, username, is_admin, joined_at) VALUES (?, ?, 0, ?)',
            (group['id'], username, now)
        )
    return redirect(url_for('group_view', group_id=group['id']))


@app.route('/groups/<int:group_id>/leave', methods=['POST'])
def leave_group(group_id):
    username  = get_username_from_request()
    if not username or not is_group_member(group_id, username):
        return redirect(url_for('index'))
    was_admin = is_group_admin(group_id, username)
    with get_db() as db:
        count = db.execute(
            'SELECT COUNT(*) as c FROM group_members WHERE group_id = ?', (group_id,)
        ).fetchone()['c']
        if count <= 1:
            db.execute('DELETE FROM group_members WHERE group_id = ?', (group_id,))
            db.execute('DELETE FROM groups WHERE id = ?', (group_id,))
        else:
            db.execute('DELETE FROM group_members WHERE group_id = ? AND username = ?',
                       (group_id, username))
            if was_admin:
                promote_oldest_member(db, group_id, username)
    return redirect(url_for('groups_list'))


@app.route('/groups/<int:group_id>/remove/<member>', methods=['POST'])
def remove_member(group_id, member):
    username = get_username_from_request()
    if not username or not is_group_admin(group_id, username) or member == username:
        return redirect(url_for('group_view', group_id=group_id))
    with get_db() as db:
        db.execute('DELETE FROM group_members WHERE group_id = ? AND username = ?',
                   (group_id, member))
    return redirect(url_for('group_view', group_id=group_id))


@app.route('/groups/<int:group_id>/delete', methods=['POST'])
def delete_group(group_id):
    username = get_username_from_request()
    if not username or not is_group_admin(group_id, username):
        return redirect(url_for('group_view', group_id=group_id))
    with get_db() as db:
        db.execute('DELETE FROM group_members WHERE group_id = ?', (group_id,))
        db.execute('DELETE FROM groups WHERE id = ?', (group_id,))
    return redirect(url_for('groups_list'))


@app.route('/groups/<int:group_id>/toggle-mode', methods=['POST'])
def toggle_group_mode(group_id):
    username = get_username_from_request()
    if not username or not is_group_admin(group_id, username):
        return redirect(url_for('group_view', group_id=group_id))
    group    = get_group(group_id)
    new_mode = 'closed' if group['mode'] == 'open' else 'open'
    with get_db() as db:
        db.execute('UPDATE groups SET mode = ? WHERE id = ?', (new_mode, group_id))
    return redirect(url_for('group_view', group_id=group_id))

# ─────────────────────────────────────────
# Routes — Admin
# ─────────────────────────────────────────

@app.route('/admin', methods=['GET', 'POST'])
def admin():
    if request.method == 'POST':
        password = request.form.get('password', '')
        if password == ADMIN_PASSWORD:
            resp = make_response(redirect(url_for('admin')))
            resp.set_cookie('admin_auth', ADMIN_PASSWORD,
                            httponly=True, samesite='Lax', secure=SECURE_COOKIES)
            return resp
        return render_template('admin_login.html', error="Incorrect password.")

    if not check_admin_auth():
        return render_template('admin_login.html')

    cutoff = (datetime.utcnow() - timedelta(days=30)).isoformat()
    with get_db() as db:
        stats = {
            'total_users':     db.execute('SELECT COUNT(*) as c FROM users').fetchone()['c'],
            'total_entries':   db.execute("SELECT COUNT(*) as c FROM users WHERE entry != '' AND entry IS NOT NULL").fetchone()['c'],
            'total_snapshots': db.execute('SELECT COUNT(*) as c FROM snapshots').fetchone()['c'],
            'total_groups':    db.execute('SELECT COUNT(*) as c FROM groups').fetchone()['c'],
            'total_capsules':  db.execute('SELECT COUNT(*) as c FROM capsules').fetchone()['c'],
            'strangers_count': db.execute('SELECT COUNT(*) as c FROM users WHERE in_strangers = 1').fetchone()['c'],
            'revealed_caps':   db.execute('SELECT COUNT(*) as c FROM capsules WHERE revealed = 1').fetchone()['c'],
            'active_30d':      db.execute('SELECT COUNT(*) as c FROM users WHERE updated_at >= ?', (cutoff,)).fetchone()['c'],
            'recent_users':    db.execute(
                'SELECT username, created_at, updated_at FROM users ORDER BY created_at DESC LIMIT 10'
            ).fetchall(),
        }
    return render_template('admin.html', stats=stats)


@app.route('/admin/reload-wordlist', methods=['POST'])
def admin_reload_wordlist():
    if not check_admin_auth():
        return redirect(url_for('admin'))
    _profanity.load_censor_words()
    extra = _load_custom_words()
    if extra:
        _profanity.add_censor_words(extra)
    return redirect(url_for('admin'))


@app.route('/admin/logout', methods=['POST'])
def admin_logout():
    resp = make_response(redirect(url_for('admin')))
    resp.delete_cookie('admin_auth')
    return resp


if __name__ == '__main__':
    app.run(debug=True)
