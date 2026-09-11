import feedparser
import sqlite3
import os
import re
import json
import hashlib
import hmac
import secrets
import threading
import time
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from datetime import datetime, timezone, timedelta
from flask import Flask, render_template, jsonify, request, make_response, has_request_context

app = Flask(__name__)
DB_PATH = os.path.join(os.path.dirname(__file__), 'aihub.db')
REFRESH_INTERVAL_MINUTES = max(1, int(os.getenv('AIHUB_REFRESH_INTERVAL_MINUTES', '30')))
FEED_TIMEOUT_SECONDS = max(3, int(os.getenv('AIHUB_FEED_TIMEOUT_SECONDS', '12')))
DEEPSEEK_API_KEY = os.getenv('DEEPSEEK_API_KEY', '').strip()
DEEPSEEK_API_URL = os.getenv('DEEPSEEK_API_URL', 'https://api.deepseek.com/chat/completions')
DEEPSEEK_MODEL = os.getenv('DEEPSEEK_MODEL', 'deepseek-chat')
DEEPSEEK_TIMEOUT_SECONDS = max(10, int(os.getenv('DEEPSEEK_TIMEOUT_SECONDS', '90')))
SESSION_DAYS = 3
SESSION_SECONDS = SESSION_DAYS * 24 * 60 * 60
SESSION_COOKIE = 'aihub_session'
refresh_lock = threading.Lock()
refresh_state = {
    'status': 'idle', 'started_at': None, 'finished_at': None,
    'count': 0, 'feeds': [], 'error': None
}
deepseek_lock = threading.Lock()
deepseek_state = {
    'status': 'idle', 'scheduled_at': None, 'started_at': None,
    'finished_at': None, 'count': 0, 'categories': {}, 'error': None
}

# ---------- RSS 源配置 ----------
RSS_FEEDS = {
    "ai_news": [
        {"name": "机器之心", "url": "https://www.jiqizhixin.com/rss"},
        {"name": "量子位", "url": "https://www.qbitai.com/feed"},
        {"name": "MIT Tech Review AI", "url": "https://www.technologyreview.com/topic/artificial-intelligence/feed"},
    ],
    "papers": [
        {"name": "arXiv AI", "url": "http://export.arxiv.org/rss/cs.AI"},
        {"name": "arXiv ML", "url": "http://export.arxiv.org/rss/cs.LG"},
    ],
    "learning": [
        {"name": "Google AI Blog", "url": "https://blog.google/technology/ai/rss/"},
    ]
}

SEED_ARTICLES = [
    {
        "title": "生成式 AI 正在重塑知识工作的协作方式",
        "link": "https://www.technologyreview.com/topic/artificial-intelligence/",
        "summary": "从智能助手到多模态工作流，企业正在重新思考人与模型之间的协作边界。",
        "source": "AI Hub 编辑精选", "category": "ai_news", "published": "2026-09-11"
    },
    {
        "title": "Scaling Laws for Neural Language Models",
        "link": "https://arxiv.org/abs/2001.08361",
        "summary": "经典论文：研究模型规模、数据规模与计算量之间的关系，为大模型训练策略提供参考。",
        "source": "arXiv AI", "category": "papers", "published": "2020-01-23"
    },
    {
        "title": "机器学习工程实践路线图",
        "link": "https://developers.google.com/machine-learning",
        "summary": "从数据准备、模型训练到部署监控，适合系统建立机器学习工程能力。",
        "source": "Google AI", "category": "learning", "published": "持续更新"
    },
    {
        "title": "多模态模型的下一站：从理解走向行动",
        "link": "https://www.qbitai.com/",
        "summary": "视觉、语言与工具调用能力逐步融合，AI Agent 的产品形态正在快速演进。",
        "source": "量子位", "category": "ai_news", "published": "2026-09-10"
    },
    {
        "title": "Attention Is All You Need",
        "link": "https://arxiv.org/abs/1706.03762",
        "summary": "Transformer 架构奠基论文，理解现代语言模型工作原理的必读材料。",
        "source": "arXiv ML", "category": "papers", "published": "2017-06-12"
    },
    {
        "title": "Hugging Face NLP 课程",
        "link": "https://huggingface.co/learn/nlp-course/",
        "summary": "结合 Transformers、Datasets 和 Tokenizers，动手掌握自然语言处理完整流程。",
        "source": "Hugging Face", "category": "learning", "published": "持续更新"
    }
]

# ---------- 数据库 ----------
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS articles (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT, link TEXT, summary TEXT,
        source TEXT, category TEXT,
        published TEXT, fetched_at TEXT
    )''')
    c.execute('CREATE INDEX IF NOT EXISTS idx_articles_category ON articles(category)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_articles_link ON articles(link)')
    c.execute('''CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT NOT NULL UNIQUE COLLATE NOCASE,
        email TEXT NOT NULL UNIQUE COLLATE NOCASE,
        phone TEXT,
        password_hash TEXT NOT NULL,
        created_at TEXT NOT NULL,
        last_login_at TEXT
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        token_hash TEXT NOT NULL UNIQUE,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
    )''')
    favorite_columns = [row[1] for row in c.execute('PRAGMA table_info(favorites)').fetchall()]
    if favorite_columns and 'user_id' not in favorite_columns:
        c.execute('ALTER TABLE favorites RENAME TO favorites_legacy')
        favorite_columns = []
    if not favorite_columns:
        c.execute('''CREATE TABLE IF NOT EXISTS favorites (
            user_id INTEGER NOT NULL,
            article_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY(user_id, article_id),
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY(article_id) REFERENCES articles(id) ON DELETE CASCADE
        )''')
    c.execute('CREATE INDEX IF NOT EXISTS idx_sessions_token ON sessions(token_hash)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_sessions_expiry ON sessions(expires_at)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_favorites_user ON favorites(user_id)')
    conn.commit()
    conn.close()

def utc_now():
    return datetime.now(timezone.utc)

def hash_password(password, salt=None):
    salt = salt or secrets.token_bytes(16)
    derived = hashlib.pbkdf2_hmac('sha256', password.encode(), salt, 240000)
    return f'pbkdf2_sha256$240000${salt.hex()}${derived.hex()}'

def verify_password(password, encoded):
    try:
        algorithm, rounds, salt_hex, digest_hex = encoded.split('$')
        if algorithm != 'pbkdf2_sha256':
            return False
        candidate = hashlib.pbkdf2_hmac('sha256', password.encode(),
                                        bytes.fromhex(salt_hex), int(rounds))
        return hmac.compare_digest(candidate.hex(), digest_hex)
    except (ValueError, TypeError):
        return False

def validate_registration(data):
    username = str(data.get('username', '')).strip()
    email = str(data.get('email', '')).strip().lower()
    phone = str(data.get('phone', '')).strip()
    password = str(data.get('password', ''))
    errors = {}
    if not re.fullmatch(r'[A-Za-z0-9_\u4e00-\u9fff]{3,24}', username):
        errors['username'] = '用户名需为 3-24 位中文、字母、数字或下划线'
    if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', email):
        errors['email'] = '请输入有效邮箱地址'
    if phone and not re.fullmatch(r'\+?[0-9\- ]{7,20}', phone):
        errors['phone'] = '请输入有效手机号'
    password_rules = [
        len(password) >= 8,
        bool(re.search(r'[A-Z]', password)),
        bool(re.search(r'[a-z]', password)),
        bool(re.search(r'\d', password)),
        bool(re.search(r'[^A-Za-z0-9]', password)),
    ]
    if not all(password_rules):
        errors['password'] = '密码至少 8 位，且包含大写字母、小写字母、数字和特殊字符'
    return errors, username, email, phone, password

def public_user(user):
    return {'id': user['id'], 'username': user['username'], 'email': user['email'],
            'phone': user['phone'], 'created_at': user['created_at']}

def current_user():
    if not has_request_context():
        return None
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    now = utc_now().isoformat()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    row = conn.execute('''SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id
        WHERE s.token_hash=? AND s.expires_at>?''', (token_hash, now)).fetchone()
    conn.execute('DELETE FROM sessions WHERE expires_at<=?', (now,))
    conn.commit()
    conn.close()
    return dict(row) if row else None

def create_session(user_id):
    token = secrets.token_urlsafe(48)
    now = utc_now()
    conn = sqlite3.connect(DB_PATH)
    conn.execute('''INSERT INTO sessions(user_id, token_hash, created_at, expires_at)
        VALUES (?, ?, ?, ?)''', (user_id, hashlib.sha256(token.encode()).hexdigest(),
                                  now.isoformat(),
                                  datetime.fromtimestamp(now.timestamp() + SESSION_SECONDS,
                                                          timezone.utc).isoformat()))
    conn.commit()
    conn.close()
    return token

def save_articles(articles):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    for a in articles:
        c.execute('''INSERT INTO articles
            (title, link, summary, source, category, published, fetched_at)
            SELECT ?,?,?,?,?,?,?
            WHERE NOT EXISTS (SELECT 1 FROM articles WHERE link=?)''',
            (a['title'], a['link'], a['summary'],
             a['source'], a['category'], a['published'],
             datetime.now().isoformat(), a['link']))
    conn.commit()
    conn.close()

def fetch_all_feeds():
    all_articles = []
    feed_results = []
    for category, feeds in RSS_FEEDS.items():
        for feed_info in feeds:
            try:
                request = Request(feed_info['url'], headers={'User-Agent': 'AIHub/1.0 RSS Reader'})
                with urlopen(request, timeout=FEED_TIMEOUT_SECONDS) as response:
                    feed = feedparser.parse(response.read())
                if getattr(feed, 'bozo', False) and not feed.entries:
                    raise ValueError('RSS 返回内容无法解析')
                for entry in feed.entries[:10]:
                    all_articles.append({
                        'title': entry.get('title', '无标题'),
                        'link': entry.get('link', '#'),
                        'summary': entry.get('summary', '')[:300],
                        'source': feed_info['name'],
                        'category': category,
                        'published': entry.get('published', '')
                    })
                feed_results.append({
                    'source': feed_info['name'], 'category': category,
                    'status': 'ok', 'count': len(feed.entries[:10])
                })
            except Exception as e:
                feed_results.append({
                    'source': feed_info['name'], 'category': category,
                    'status': 'error', 'count': 0, 'error': str(e)
                })
    return all_articles, feed_results

def build_deepseek_context():
    """Use current RSS candidates as grounded context for the daily AI update."""
    articles, _ = fetch_all_feeds()
    context = []
    for article in articles[:30]:
        context.append({
            'category': article['category'], 'title': article['title'],
            'summary': article['summary'][:500], 'link': article['link'],
            'source': article['source']
        })
    return context

def call_deepseek(context):
    if not DEEPSEEK_API_KEY:
        raise RuntimeError('未配置 DEEPSEEK_API_KEY')
    today = datetime.now().astimezone().strftime('%Y-%m-%d')
    prompt = '''你是 AI Hub 的内容编辑。请基于给定的 RSS 候选内容，整理当天最新、最值得阅读的 AI 内容。
必须只返回 JSON 数组，不要 Markdown，不要解释。数组每项必须包含：category、title、summary、link、source、published。
category 只能是 ai_news、papers、learning；每个分类最多 5 项，优先保留新近且信息密度高的候选。
必须优先使用候选中的 link，不得编造链接；如果候选不足，可以补充可信内容，但 link 必须是完整 http/https URL。
summary 使用中文，80-180 字；published 使用 YYYY-MM-DD。
今天日期：%s
候选内容：
%s''' % (today, json.dumps(context, ensure_ascii=False))
    payload = json.dumps({
        'model': DEEPSEEK_MODEL,
        'temperature': 0.2,
        'messages': [
            {'role': 'system', 'content': '你只输出合法 JSON 数组。'},
            {'role': 'user', 'content': prompt}
        ]
    }).encode('utf-8')
    request = Request(DEEPSEEK_API_URL, data=payload, method='POST', headers={
        'Authorization': f'Bearer {DEEPSEEK_API_KEY}',
        'Content-Type': 'application/json', 'User-Agent': 'AIHub/1.0'
    })
    try:
        with urlopen(request, timeout=DEEPSEEK_TIMEOUT_SECONDS) as response:
            result = json.loads(response.read().decode('utf-8'))
    except HTTPError as error:
        detail = error.read().decode('utf-8', errors='replace')[:300]
        raise RuntimeError(f'DeepSeek 请求失败 ({error.code}): {detail}') from error
    except (URLError, TimeoutError) as error:
        raise RuntimeError(f'DeepSeek 网络请求失败: {error}') from error
    try:
        content = result['choices'][0]['message']['content'].strip()
        content = re.sub(r'^```(?:json)?\s*|\s*```$', '', content, flags=re.IGNORECASE)
        return json.loads(content)
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as error:
        raise RuntimeError(f'DeepSeek 返回格式无效: {error}') from error

def validate_deepseek_articles(items):
    valid = []
    today = datetime.now().astimezone().strftime('%Y-%m-%d')
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        category = item.get('category')
        title = str(item.get('title', '')).strip()
        summary = str(item.get('summary', '')).strip()
        link = str(item.get('link', '')).strip()
        source = str(item.get('source', 'DeepSeek 编辑')).strip()[:80]
        if category not in RSS_FEEDS or not title or not summary:
            continue
        if not re.fullmatch(r'https?://[^\s]+', link):
            continue
        valid.append({
            'title': title[:300], 'link': link[:1000], 'summary': summary[:600],
            'source': source or 'DeepSeek 编辑', 'category': category,
            'published': str(item.get('published') or today)[:30]
        })
    return valid

def perform_deepseek_refresh():
    global deepseek_state
    if not deepseek_lock.acquire(blocking=False):
        return False
    deepseek_state = {
        'status': 'running', 'scheduled_at': deepseek_state.get('scheduled_at'),
        'started_at': utc_now().isoformat(), 'finished_at': None,
        'count': 0, 'categories': {}, 'error': None
    }
    try:
        items = validate_deepseek_articles(call_deepseek(build_deepseek_context()))
        save_articles(items)
        counts = {category: sum(1 for item in items if item['category'] == category)
                  for category in RSS_FEEDS}
        deepseek_state.update({'status': 'ok', 'finished_at': utc_now().isoformat(),
                               'count': len(items), 'categories': counts})
    except Exception as error:
        deepseek_state.update({'status': 'error', 'finished_at': utc_now().isoformat(),
                               'error': str(error)})
    finally:
        deepseek_lock.release()
    return True

def start_deepseek_refresh():
    if deepseek_state['status'] == 'running' or not deepseek_lock.acquire(blocking=False):
        return False
    deepseek_lock.release()
    threading.Thread(target=perform_deepseek_refresh, name='aihub-deepseek', daemon=True).start()
    return True

def seconds_until_local_midnight():
    now = datetime.now().astimezone()
    next_day = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(1, int((next_day - now).total_seconds()))

def deepseek_scheduler():
    while True:
        time.sleep(seconds_until_local_midnight())
        start_deepseek_refresh()

def perform_refresh():
    """Fetch all configured feeds and publish one observable refresh result."""
    global refresh_state
    if not refresh_lock.acquire(blocking=False):
        return False
    refresh_state = {
        'status': 'running', 'started_at': datetime.now(timezone.utc).isoformat(),
        'finished_at': None, 'count': 0, 'feeds': [], 'error': None
    }
    try:
        articles, feeds = fetch_all_feeds()
        if articles:
            save_articles(articles)
        failed = sum(1 for feed in feeds if feed['status'] == 'error')
        refresh_state.update({
            'status': 'partial' if failed else 'ok',
            'finished_at': datetime.now(timezone.utc).isoformat(),
            'count': len(articles), 'feeds': feeds
        })
    except Exception as error:
        refresh_state.update({
            'status': 'error', 'finished_at': datetime.now(timezone.utc).isoformat(),
            'error': str(error)
        })
    finally:
        refresh_lock.release()
    return True

def start_refresh():
    if refresh_state['status'] == 'running' or not refresh_lock.acquire(blocking=False):
        return False
    refresh_lock.release()
    threading.Thread(target=perform_refresh, name='aihub-refresh', daemon=True).start()
    return True

def refresh_scheduler():
    while True:
        time.sleep(REFRESH_INTERVAL_MINUTES * 60)
        start_refresh()

def get_articles(category=None, limit=30, offset=0, query='', source=None, favorites_only=False):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    conditions = []
    values = []
    if category:
        conditions.append('a.category=?')
        values.append(category)
    if source:
        conditions.append('a.source=?')
        values.append(source)
    if query:
        conditions.append('(LOWER(a.title) LIKE ? OR LOWER(a.summary) LIKE ? OR LOWER(a.source) LIKE ?)')
        search = f'%{query.lower()}%'
        values.extend([search, search, search])
    user = current_user()
    if favorites_only and not user:
        return []
    if favorites_only:
        conditions.append('f.article_id IS NOT NULL')
    where = ' WHERE ' + ' AND '.join(conditions) if conditions else ''
    join_user = user['id'] if user else -1
    c.execute(f'''SELECT a.*, f.article_id IS NOT NULL AS favorite
        FROM articles a LEFT JOIN favorites f ON f.article_id=a.id AND f.user_id=?
        {where} ORDER BY a.fetched_at DESC LIMIT ? OFFSET ?''',
        (join_user, *values, limit, offset))
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return rows

def get_article(article_id):
    user = current_user()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    row = conn.execute('''SELECT a.*, f.article_id IS NOT NULL AS favorite
        FROM articles a LEFT JOIN favorites f ON f.article_id=a.id AND f.user_id=?
        WHERE a.id=?''', (user['id'] if user else -1, article_id)).fetchone()
    conn.close()
    return dict(row) if row else None

def get_article_count(category=None, query='', source=None, favorites_only=False):
    user = current_user()
    if favorites_only and not user:
        return 0
    conn = sqlite3.connect(DB_PATH)
    conditions = []
    values = []
    if category:
        conditions.append('a.category=?')
        values.append(category)
    if source:
        conditions.append('a.source=?')
        values.append(source)
    if query:
        conditions.append('(LOWER(a.title) LIKE ? OR LOWER(a.summary) LIKE ? OR LOWER(a.source) LIKE ?)')
        search = f'%{query.lower()}%'
        values.extend([search, search, search])
    if favorites_only:
        conditions.append('f.article_id IS NOT NULL')
    where = ' WHERE ' + ' AND '.join(conditions) if conditions else ''
    join_user = user['id'] if user else -1
    count = conn.execute(f'''SELECT COUNT(*) FROM articles a
        LEFT JOIN favorites f ON f.article_id=a.id AND f.user_id=? {where}''',
        (join_user, *values)).fetchone()[0]
    conn.close()
    return count

def get_stats():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT COUNT(*) FROM articles')
    total = c.fetchone()[0]
    c.execute('SELECT COUNT(*) FROM articles WHERE category="ai_news"')
    news = c.fetchone()[0]
    c.execute('SELECT COUNT(*) FROM articles WHERE category="papers"')
    papers = c.fetchone()[0]
    c.execute('SELECT COUNT(*) FROM articles WHERE category="learning"')
    learning = c.fetchone()[0]
    c.execute('SELECT COUNT(*) FROM favorites')
    favorites = c.fetchone()[0]
    conn.close()
    return {"total": total, "ai_news": news, "papers": papers,
            "learning": learning, "favorites": favorites}

init_db()
if not get_articles(limit=1):
    save_articles(SEED_ARTICLES)

# ---------- 路由 ----------
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/auth/register', methods=['POST'])
def api_register():
    data = request.get_json(silent=True) or request.form
    errors, username, email, phone, password = validate_registration(data)
    if errors:
        return jsonify({'error': '注册信息校验失败', 'fields': errors}), 400
    conn = sqlite3.connect(DB_PATH)
    try:
        created_at = utc_now().isoformat()
        cursor = conn.execute('''INSERT INTO users
            (username, email, phone, password_hash, created_at) VALUES (?, ?, ?, ?, ?)''',
            (username, email, phone or None, hash_password(password), created_at))
        user_id = cursor.lastrowid
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        return jsonify({'error': '用户名或邮箱已注册'}), 409
    finally:
        conn.close()
    token = create_session(user_id)
    user = {'id': user_id, 'username': username, 'email': email, 'phone': phone or None,
            'created_at': created_at}
    response = make_response(jsonify({'user': public_user(user), 'expires_in': SESSION_SECONDS}), 201)
    response.set_cookie(SESSION_COOKIE, token, max_age=SESSION_SECONDS,
                        httponly=True, samesite='Lax', secure=request.is_secure)
    return response

@app.route('/api/auth/login', methods=['POST'])
def api_login():
    data = request.get_json(silent=True) or request.form
    account = str(data.get('account', data.get('username', ''))).strip()
    password = str(data.get('password', ''))
    if not account or not password:
        return jsonify({'error': '请输入用户名或邮箱以及密码'}), 400
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    user = conn.execute('''SELECT * FROM users
        WHERE username=? COLLATE NOCASE OR email=? COLLATE NOCASE''',
        (account, account)).fetchone()
    if not user or not verify_password(password, user['password_hash']):
        conn.close()
        return jsonify({'error': '用户名、邮箱或密码错误'}), 401
    conn.execute('UPDATE users SET last_login_at=? WHERE id=?', (utc_now().isoformat(), user['id']))
    conn.commit()
    conn.close()
    token = create_session(user['id'])
    response = make_response(jsonify({'user': public_user(dict(user)), 'expires_in': SESSION_SECONDS}))
    response.set_cookie(SESSION_COOKIE, token, max_age=SESSION_SECONDS,
                        httponly=True, samesite='Lax', secure=request.is_secure)
    return response

@app.route('/api/auth/me')
def api_me():
    user = current_user()
    if not user:
        return jsonify({'authenticated': False, 'user': None}), 401
    return jsonify({'authenticated': True, 'user': public_user(user),
                    'expires_in': SESSION_SECONDS})

@app.route('/api/auth/logout', methods=['POST'])
def api_logout():
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        conn = sqlite3.connect(DB_PATH)
        conn.execute('DELETE FROM sessions WHERE token_hash=?',
                     (hashlib.sha256(token.encode()).hexdigest(),))
        conn.commit()
        conn.close()
    response = make_response(jsonify({'status': 'ok'}))
    response.delete_cookie(SESSION_COOKIE)
    return response

@app.route('/api/articles')
def api_articles():
    category = request.args.get('category') or None
    if category and category not in RSS_FEEDS:
        return jsonify({"error": "无效的内容分类"}), 400
    query = request.args.get('q', '').strip()
    source = request.args.get('source') or None
    favorites_only = request.args.get('favorites', '').lower() in ('1', 'true', 'yes')
    try:
        page = max(1, int(request.args.get('page', 1)))
        limit = min(100, max(1, int(request.args.get('limit', 30))))
    except ValueError:
        return jsonify({"error": "page 和 limit 必须是数字"}), 400
    offset = (page - 1) * limit
    articles = get_articles(category, limit, offset, query, source, favorites_only)
    return jsonify({"items": articles, "page": page, "limit": limit,
                    "total": get_article_count(category, query, source, favorites_only),
                    "has_more": len(articles) == limit})

@app.route('/api/articles/<int:article_id>')
def api_article_detail(article_id):
    article = get_article(article_id)
    if not article:
        return jsonify({"error": "文章不存在"}), 404
    return jsonify(article)

@app.route('/api/articles/<int:article_id>/favorite', methods=['POST', 'DELETE'])
def api_article_favorite(article_id):
    user = current_user()
    if not user:
        return jsonify({"error": "请先登录"}), 401
    if not get_article(article_id):
        return jsonify({"error": "文章不存在"}), 404
    conn = sqlite3.connect(DB_PATH)
    if request.method == 'POST':
        conn.execute('''INSERT OR IGNORE INTO favorites(user_id, article_id, created_at)
            VALUES (?, ?, ?)''', (user['id'], article_id, utc_now().isoformat()))
        favorite = True
    else:
        conn.execute('DELETE FROM favorites WHERE article_id=?', (article_id,))
        favorite = False
    conn.commit()
    conn.close()
    return jsonify({"article_id": article_id, "favorite": favorite})

@app.route('/api/favorites')
def api_favorites():
    if not current_user():
        return jsonify({"error": "请先登录"}), 401
    return jsonify({"items": get_articles(limit=100, favorites_only=True)})

@app.route('/api/sources')
def api_sources():
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute('''SELECT source, category, COUNT(*) AS count,
        MAX(fetched_at) AS last_fetched FROM articles GROUP BY source, category
        ORDER BY source''').fetchall()
    conn.close()
    configured = {feed['name']: (category, feed['url'])
                  for category, feeds in RSS_FEEDS.items() for feed in feeds}
    sources = []
    for name, category, count, last_fetched in rows:
        sources.append({"name": name, "category": category, "count": count,
                        "last_fetched": last_fetched, "url": configured.get(name, ('', ''))[1]})
    return jsonify(sources)

@app.route('/api/health')
def api_health():
    try:
        init_db()
        total = get_stats()['total']
        return jsonify({"status": "ok", "database": "ok", "articles": total,
                        "time": datetime.now(timezone.utc).isoformat()})
    except sqlite3.Error as error:
        return jsonify({"status": "error", "database": str(error)}), 503

@app.route('/api/stats')
def api_stats():
    return jsonify(get_stats())

@app.route('/api/refresh')
def api_refresh():
    started = start_refresh()
    return jsonify({"status": "started" if started else "running",
                    "refresh": refresh_state, "stats": get_stats()}), 202

@app.route('/api/refresh/status')
def api_refresh_status():
    return jsonify({"refresh": refresh_state, "stats": get_stats(),
                    "interval_minutes": REFRESH_INTERVAL_MINUTES})

@app.route('/api/deepseek/status')
def api_deepseek_status():
    return jsonify({"deepseek": deepseek_state,
                    "configured": bool(DEEPSEEK_API_KEY),
                    "model": DEEPSEEK_MODEL,
                    "schedule": "每天 00:00:00（服务器本地时区）"})

@app.route('/api/deepseek/refresh', methods=['POST'])
def api_deepseek_refresh():
    if not DEEPSEEK_API_KEY:
        return jsonify({'error': '服务端未配置 DEEPSEEK_API_KEY'}), 503
    started = start_deepseek_refresh()
    return jsonify({'status': 'started' if started else 'running',
                    'deepseek': deepseek_state}), 202

if __name__ == '__main__':
    threading.Thread(target=refresh_scheduler, name='aihub-scheduler', daemon=True).start()
    threading.Thread(target=deepseek_scheduler, name='aihub-deepseek-scheduler', daemon=True).start()
    app.run(host='0.0.0.0', port=5001, debug=False)
