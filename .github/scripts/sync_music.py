# -*- coding: utf-8 -*-
"""QQ 音乐歌单 -> music/playlist.json 同步脚本（GitHub Actions 定时运行）。

依赖：QQ_COOKIE（仓库 Secret，用户自配）。无 cookie 时只写种子/保留旧数据。
歌单：'我喜欢' 3787157504（全量元数据匿名可取；音频流地址需登录态 vkey）。
"""
import json, os, re, sys, time, uuid, urllib.request, concurrent.futures

COOKIE = (sys.argv[1] if len(sys.argv) > 1 else os.environ.get('QQ_COOKIE', '') or '').strip()
PLID = '3787157504'
MAX_TRACKS = int(os.environ.get('MAX_TRACKS', '300'))
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36'


def fetch(url, cookie='', post=None, timeout=20):
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
    # 1) 歌单全量元数据（匿名可取）
    txt = fetch('https://c.y.qq.com/qzone/fcg-bin/fcg_ucc_getcdinfo_byids_cp.fcg?type=1&json=1&utf8=1'
                '&onlysong=0&disstid=%s&format=json&g_tk=5381&loginUin=0&hostUin=0&inCharset=utf8'
                '&outCharset=utf-8&notice=0&platform=yqq&needNewCode=0' % PLID)
    i = txt.find('{')
    if i < 0:
        print('playlist meta parse failed')
        sys.exit(0)
    obj = json.loads(txt[i:txt.rfind('}') + 1])
    cd = obj.get('cdlist', [{}])[0]
    songs = cd.get('songlist', [])[:MAX_TRACKS]
    name = cd.get('dissname', '') or '我喜欢'
    print('playlist:', name, '| tracks to resolve:', len(songs))

    # 从 cookie 中提取 uin
    uin = '0'
    m = re.search(r'uin=(\d+)', COOKIE)
    if m:
        uin = m.group(1)

    def resolve(song):
        mid = song.get('songmid')
        if not mid or not COOKIE:
            return None
        guid = str(int(uuid.uuid4().hex, 16))[:10]
        body = {
            'comm': {'cv': 4747474, 'ct': 24, 'format': 'json', 'inCharset': 'utf-8',
                     'outCharset': 'utf-8', 'notice': 0, 'platform': 'yqq.json',
                     'uin': uin, 'needNewCode': 1},
            'req_0': {'module': 'vkey.GetVkeyServer', 'method': 'CgiGetVkey',
                      'param': {'guid': guid, 'songmid': [mid], 'songtype': [0],
                                'uin': uin, 'loginflag': 1, 'platform': '20'}},
        }
        try:
            txt3 = fetch('https://u.y.qq.com/cgi-bin/musicu.fcg', COOKIE, body)
            o = json.loads(txt3)
            info = o.get('req_0', {}).get('data', {}).get('midurlinfo', [{}])[0]
            purl = info.get('purl', '') or ''
            if not purl:
                return None
            singer = ' / '.join(s.get('name', '') for s in song.get('singer', []))
            cover = ('https://y.gtimg.cn/music/photo_new/T002R300x300M000%s.jpg' % song.get('albummid', '')) \
                if song.get('albummid') else ''
            return {'name': song.get('songname', ''), 'artist': singer,
                    'url': 'https://dl.stream.qqmusic.qq.com/' + purl,
                    'cover': cover, 'duration': song.get('interval', 0)}
        except Exception as e:
            print('resolve err', mid, type(e).__name__)
            return None

    tracks = []
    if COOKIE:
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as ex:
            for r in ex.map(resolve, songs):
                if r:
                    tracks.append(r)
    else:
        print('NO_COOKIE: QQ_COOKIE secret not configured, keeping seed playlist')

    out = {'updated': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
           'source': 'qq', 'playlist': name,
           'total': len(songs), 'playable': len(tracks), 'tracks': tracks}
    with open('music/playlist.json', 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False)
    print('written music/playlist.json, playable tracks:', len(tracks))


if __name__ == '__main__':
    main()
