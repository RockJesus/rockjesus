# -*- coding: utf-8 -*-
"""QQ 音乐歌单 -> music/playlist.json 同步脚本（GitHub Actions 定时运行）。

依赖：QQ_COOKIE（仓库 Secret，用户自配）。
歌单：'我喜欢' 3787157504（全量元数据匿名可取；音频流地址需 vkey）。

v3 改造（相对 v2）：
1. 【合并旧直链】解析失败时回退用上一份 playlist.json 里的旧直链（按 歌名+歌手 匹配）。
   实测 QQ 直链（purl）有效期远大于 2h（16h+ 的旧链仍 30/30 存活），而 QQ 侧对
   数据中心 IP 的风控会让某些轮次的成功率骤降（匿名通道已 10006 全拒，cookie 通道
   也可能被限流到 4%）。合并策略保证可播数单调不减，坏天气不再吞掉好天气的成果。
2. 统计里加 keep（旧链续用数）。
3. 其余沿用 v2：全量收录、失败不丢弃、cookie→cookie→匿名三段重试、降并发、固定 guid。

匿名通道注意：2026-10-29 起 u.y.qq.com musicu.fcg 匿名请求一律返回 code=10006，
c.y.qq.com mobile3 返回 104003 —— 无 cookie 时 vkey 实际上已不可解析。
"""
import json, os, re, sys, time, uuid, random, urllib.request, concurrent.futures

COOKIE = (sys.argv[1] if len(sys.argv) > 1 else os.environ.get('QQ_COOKIE', '') or '').strip()
PLID = '3787157504'
MAX_TRACKS = int(os.environ.get('MAX_TRACKS', '5000'))
WORKERS = int(os.environ.get('WORKERS', '5'))
RETRY = int(os.environ.get('RETRY', '3'))
BATCH = int(os.environ.get('BATCH', '150'))
UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')


def fetch(url, cookie='', post=None, timeout=25):
    headers = {'User-Agent': UA, 'Referer': 'https://y.qq.com/'}
    if cookie:
        headers['Cookie'] = cookie
    if post is not None:
        req = urllib.request.Request(url, data=json.dumps(post).encode('utf-8'),
                                     headers=dict(headers, **{'Content-Type': 'application/json'}))
    else:
        req = urllib.request.Request(url, headers=headers)
    return urllib.request.urlopen(req, timeout=timeout).read().decode('utf-8', 'ignore')


def main():
    # 1) 歌单全量元数据（匿名可取，3064 首会一次返回）
    txt = fetch('https://c.y.qq.com/qzone/fcg-bin/fcg_ucc_getcdinfo_byids_cp.fcg?type=1&json=1&utf8=1'
                '&onlysong=0&disstid=%s&format=json&g_tk=5381&loginUin=0&hostUin=0&inCharset=utf8'
                '&outCharset=utf-8&notice=0&platform=yqq&needNewCode=0' % PLID)
    i = txt.find('{')
    if i < 0:
        print('playlist meta parse failed')
        sys.exit(0)
    obj = json.loads(txt[i:txt.rfind('}') + 1])
    cd = obj.get('cdlist', [{}])[0]
    songs = cd.get('songlist', [])
    name = cd.get('dissname', '') or '我喜欢'
    if MAX_TRACKS > 0:
        songs = songs[:MAX_TRACKS]
    print('playlist:', name, '| songs returned:', len(songs),
          '| songnum:', cd.get('songnum'), flush=True)

    # 从 cookie 中提取 uin
    uin = '0'
    m = re.search(r'uin=(\d+)', COOKIE)
    if m:
        uin = m.group(1)
    # 固定 guid：vkey 与 guid 绑定，逐曲随机会导致地址提前失效
    guid = str(int(uuid.uuid4().hex, 16))[:10]

    # 【v3 合并基线】上一份歌单里的旧直链：key = (歌名, 歌手)，值 = 旧 purl 完整地址
    prev_urls = {}
    try:
        with open('music/playlist.json', encoding='utf-8') as f:
            old = json.load(f)
        for t in old.get('tracks', []):
            if t.get('url'):
                prev_urls[(t.get('name', ''), t.get('artist', ''))] = t['url']
        print('merge baseline: %d 条旧直链可回退' % len(prev_urls), flush=True)
    except Exception as e:
        print('merge baseline unavailable:', e, flush=True)

    def try_vkey(mid, cookie):
        body = {
            'comm': {'cv': 4747474, 'ct': 24, 'format': 'json', 'inCharset': 'utf-8',
                     'outCharset': 'utf-8', 'notice': 0, 'platform': 'yqq.json',
                     'uin': uin, 'needNewCode': 1},
            'req_0': {'module': 'vkey.GetVkeyServer', 'method': 'CgiGetVkey',
                      'param': {'guid': guid, 'songmid': [mid], 'songtype': [0],
                                'uin': uin, 'loginflag': 1, 'platform': '20'}},
        }
        o = json.loads(fetch('https://u.y.qq.com/cgi-bin/musicu.fcg', cookie, body))
        info = o.get('req_0', {}).get('data', {}).get('midurlinfo', [{}])[0]
        return (info.get('purl', '') or '').strip()

    stat = {'cookie': 0, 'anon': 0, 'keep': 0, 'fail': 0}

    def resolve(song):
        mid = song.get('songmid') or ''
        track = {
            'name': song.get('songname') or song.get('title') or song.get('name') or '',
            'artist': ' / '.join(s.get('name', '') for s in (song.get('singer') or [])),
            'duration': song.get('interval', 0) or 0,
        }
        if not track['name']:
            return None
        purl = ''
        mode = ''
        if mid:
            for attempt in range(RETRY):
                # 前两次带 cookie（会员曲需要），最后一次匿名（cookie 失效时的兜底）
                use_cookie = COOKIE if (attempt < RETRY - 1) else ''
                try:
                    purl = try_vkey(mid, use_cookie)
                except Exception:
                    purl = ''
                if purl:
                    mode = 'cookie' if use_cookie else 'anon'
                    break
                time.sleep(0.25 + random.random() * 0.45)
        if purl:
            albummid = song.get('albummid') or ''
            track['url'] = 'https://dl.stream.qqmusic.qq.com/' + purl
            if albummid:
                track['cover'] = 'https://y.gtimg.cn/music/photo_new/T002R300x300M000%s.jpg' % albummid
            track['playable'] = True
            stat[mode] += 1
        else:
            # 【v3】解析失败 → 回退旧直链（16h+ 旧链实测仍存活）；旧链也没有才标不可播
            old_url = prev_urls.get((track['name'], track['artist']))
            if old_url:
                track['url'] = old_url
                if song.get('albummid'):
                    track.setdefault('cover', 'https://y.gtimg.cn/music/photo_new/T002R300x300M000%s.jpg' % song['albummid'])
                track['playable'] = True
                stat['keep'] += 1
            else:
                track['playable'] = False
                stat['fail'] += 1
        return track

    tracks = []
    total = len(songs)
    done = 0
    t0 = time.time()
    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for start in range(0, total, BATCH):
            chunk = songs[start:start + BATCH]
            for r in ex.map(resolve, chunk):
                if r:
                    tracks.append(r)
            done += len(chunk)
            el = time.time() - t0
            rate = done / el if el > 0 else 0
            eta = (total - done) / rate if rate > 0 else 0
            print('  %d/%d | 可播 %d (cookie %d / anon %d / keep %d) | %.1f 首/秒 | 剩余 ~%ds'
                  % (done, total, stat['cookie'] + stat['anon'] + stat['keep'],
                     stat['cookie'], stat['anon'], stat['keep'], rate, eta), flush=True)
            time.sleep(0.6 + random.random() * 0.6)

    playable = sum(1 for t in tracks if t.get('url'))
    out = {'updated': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
           'source': 'qq', 'playlist': name,
           'total': len(tracks), 'playable': playable, 'tracks': tracks}
    with open('music/playlist.json', 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False)

    print('DONE tracks=%d playable=%d (cookie %d / anon %d / keep %d / fail %d) in %.0fs'
          % (len(tracks), playable, stat['cookie'], stat['anon'], stat['keep'], stat['fail'],
             time.time() - t0), flush=True)
    if len(tracks) < 2:
        print('WARN: too few tracks, keep previous file?')
        sys.exit(1)


if __name__ == '__main__':
    main()
