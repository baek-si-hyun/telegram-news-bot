"""STO 과거 뉴스 전체 재전송기 (히스토리 → 새 채널)
사용: python /app/storage/_repost.py <CHAT_ID>
- accepted 기사 URL 중복제거 + 유사제목 중복제거 후 시간순 전송
- 텔레그램 제한 회피: 건당 3초 + 429 백오프
- 진행상황: /app/storage/_repost_progress.txt
"""
import json, glob, time, sys, urllib.request, urllib.error
sys.path.insert(0, '/app')
from storage.cache import _normalize_title, _is_similar

CHAT_ID = sys.argv[1]
ENV = '/app/.env'
HIST_GLOB = '/app/storage/sto/history/*.jsonl'
PROG = '/app/storage/_repost_progress.txt'

env = {}
for line in open(ENV, encoding='utf-8'):
    s = line.strip()
    if s and not s.startswith('#') and '=' in s:
        k, v = s.split('=', 1)
        env[k] = v
TOKEN = env.get('TELEGRAM_BOT_TOKEN_STO') or env.get('TELEGRAM_BOT_TOKEN')

# 1) accepted 기사 수집 + URL 중복제거(가장 이른 수집시각 유지)
seen_url = {}
for fp in sorted(glob.glob(HIST_GLOB)):
    for line in open(fp, encoding='utf-8'):
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue
        if not d.get('accepted'):
            continue
        u = d.get('url')
        if not u:
            continue
        ca = d.get('collected_at') or ''
        if u not in seen_url or ca < seen_url[u]['collected_at']:
            seen_url[u] = {'url': u, 'title': d.get('title', ''), 'collected_at': ca}

items = sorted(seen_url.values(), key=lambda x: x['collected_at'])

# 2) 유사제목 중복제거 (시간순, 먼저 것 유지)
final = []
seen_titles = []
for it in items:
    tk = _normalize_title(it['title'])
    if tk and any(_is_similar(tk, t) for t in seen_titles[-500:]):
        continue
    seen_titles.append(tk)
    final.append(it)


def esc(t):
    return t.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def send(it):
    text = f'📰 <a href="{esc(it["url"])}">{esc(it["title"])}</a>'
    body = json.dumps({'chat_id': CHAT_ID, 'text': text, 'parse_mode': 'HTML',
                       'link_preview_options': {'is_disabled': False}}).encode('ascii')
    req = urllib.request.Request(f'https://api.telegram.org/bot{TOKEN}/sendMessage',
                                 data=body, headers={'Content-Type': 'application/json'})
    for _ in range(5):
        try:
            urllib.request.urlopen(req, timeout=15)
            return True
        except urllib.error.HTTPError as e:
            if e.code == 429:
                try:
                    ra = json.loads(e.read()).get('parameters', {}).get('retry_after', 5)
                except Exception:
                    ra = 5
                time.sleep(ra + 1)
                continue
            time.sleep(3)
        except Exception:
            time.sleep(3)
    return False


total = len(final)
sent = 0
with open(PROG, 'w', encoding='utf-8') as f:
    f.write(f'0/{total} start (unique_url={len(items)})\n')
for i, it in enumerate(final, 1):
    if send(it):
        sent += 1
    if i % 25 == 0 or i == total:
        with open(PROG, 'w', encoding='utf-8') as f:
            f.write(f'{i}/{total} sent={sent}\n')
    time.sleep(3)
with open(PROG, 'a', encoding='utf-8') as f:
    f.write(f'DONE sent={sent}/{total}\n')
