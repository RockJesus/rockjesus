# -*- coding: utf-8 -*-
"""QQ 音乐歌单 -> music/playlist.json 同步脚本（GitHub Actions 定时运行）。

依赖：QQ_COOKIE（仓库 Secret，用户自配）。
歌单：'我喜欢' 3787157504（全量元数据匿名可取；音频流地址需 vkey）。

v2 改造（相对 v1）：
1. 全量收录：v1 用 MAX_TRACKS=300 截断，且 vkey 解析失败的曲目被直接丢弃，
   结果 3064 首的歌单只剩 30 首。v2 把歌单里每一首都写进 JSON。
2. 不再丢弃：拿不到直链的曲目标 playable=false，前端置灰展示并标注原因。
3. 解析失败重试：带 cookie → 带 cookie → 匿名，共 3 次，间隔递增。
   v1 一次失败即丢，是 90% 曲目消失的主因。
4. 降并发 + 批间抖动，避免被 QQ 流控（Actions 出口 IP 更敏感）。
5. 固定 guid：vkey 与 guid 绑定，逐曲随机 guid 会让地址更易失效。
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

    stat = {'cookie': 0, 'anon': 0, 'fail': 0}

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
            print('  %d/%d | 可播 %d (cookie %d / anon %d) | %.1f 首/秒 | 剩余 ~%ds'
                  % (done, total, stat['cookie'] + stat['anon'], stat['cookie'], stat['anon'],
                     rate, eta), flush=True)
            time.sleep(0.6 + random.random() * 0.6)

    playable = sum(1 for t in tracks if t.get('url'))
    out = {'updated': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
           'source': 'qq', 'playlist': name,
           'total': len(tracks), 'playable': playable, 'tracks': tracks}
    with open('music/playlist.json', 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False)

    print('DONE tracks=%d playable=%d (cookie %d / anon %d / fail %d) in %.0fs'
          % (len(tracks), playable, stat['cookie'], stat['anon'], stat['fail'], time.time() - t0),
          flush=True)
    if len(tracks) < 2:
        print('WARN: too few tracks, keep previous file?')
        sys.exit(1)


if __name__ == '__main__':
    main()
