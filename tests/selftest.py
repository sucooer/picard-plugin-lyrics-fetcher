# -*- coding: utf-8 -*-
"""Offline test for the Lyrics Fetcher v3 plugin.

Stubs out picard/PyQt6 so the plugin module can be imported and its logic
exercised without a Picard installation. Network calls are replaced by canned
responses that are delivered synchronously, so the whole NetEase -> LRCLIB
fallback chain runs inside the test.
"""
import importlib.util
import json
import os
import sys
import tempfile
import types

# The suite lives in <plugin>/tests/, so the plugin itself is one level up.
PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ---------------------------------------------------------------- stub PyQt6
qt = types.ModuleType('PyQt6')
qtw = types.ModuleType('PyQt6.QtWidgets')


class _Widget:
    def __init__(self, *args, **kwargs):
        self._checked = False
        self._text = ''

    def setChecked(self, value):
        self._checked = bool(value)

    def isChecked(self):
        return self._checked

    def setText(self, value):
        self._text = value

    def text(self):
        return self._text

    def setWordWrap(self, value):
        pass


class _Combo(_Widget):
    def __init__(self, *args, **kwargs):
        super().__init__()
        self._items = []
        self._index = -1

    def addItem(self, text, data=None):
        self._items.append((text, data))
        if self._index < 0:
            self._index = 0

    def findData(self, data):
        for i, (_t, d) in enumerate(self._items):
            if d == data:
                return i
        return -1

    def setCurrentIndex(self, index):
        self._index = index

    def currentData(self):
        if 0 <= self._index < len(self._items):
            return self._items[self._index][1]
        return None


for _name in ('QCheckBox', 'QLabel', 'QLineEdit', 'QVBoxLayout'):
    setattr(qtw, _name, _Widget)
qtw.QComboBox = _Combo

# The plugin builds its own Qt network client for NetEase, so the stubs need
# just enough of QtCore/QtNetwork for the module to import.
qtcore = types.ModuleType('PyQt6.QtCore')


class _QUrl:
    def __init__(self, *args, **kwargs):
        pass


class _QUrlQuery:
    def __init__(self, *args, **kwargs):
        pass

    def addQueryItem(self, *args, **kwargs):
        pass


qtcore.QUrl = _QUrl
qtcore.QUrlQuery = _QUrlQuery

qtnet = types.ModuleType('PyQt6.QtNetwork')


class _QNetworkAccessManager:
    def __init__(self, *args, **kwargs):
        pass


class _QNetworkRequest:
    class Attribute:
        Http2AllowedAttribute = 0


class _QNetworkReply:
    class NetworkError:
        NoError = 0


qtnet.QNetworkAccessManager = _QNetworkAccessManager
qtnet.QNetworkRequest = _QNetworkRequest
qtnet.QNetworkReply = _QNetworkReply

qt.QtCore = qtcore
qt.QtNetwork = qtnet
qt.QtWidgets = qtw
sys.modules['PyQt6'] = qt
sys.modules['PyQt6.QtWidgets'] = qtw
sys.modules['PyQt6.QtCore'] = qtcore
sys.modules['PyQt6.QtNetwork'] = qtnet

# --------------------------------------------------------------- stub picard
picard = types.ModuleType('picard')
pconf = types.ModuleType('picard.config')


class BoolOption:
    registry = {}

    @classmethod
    def add_if_missing(cls, section, name, default, *a, **k):
        cls.registry.setdefault((section, name), default)


class TextOption(BoolOption):
    pass


pconf.BoolOption = BoolOption
pconf.TextOption = TextOption
picard.config = pconf

p3 = types.ModuleType('picard.plugin3')
p3api = types.ModuleType('picard.plugin3.api')


class OptionsPage:
    def __init__(self, parent=None):
        pass


class BaseAction:
    TITLE = ''
    api = None

    def __init__(self, parent=None):
        pass

    def callback(self, objs):
        raise NotImplementedError


class PluginApi:
    pass


p3api.OptionsPage = OptionsPage
p3api.BaseAction = BaseAction
p3api.PluginApi = PluginApi
picard.plugin3 = p3
sys.modules['picard'] = picard
sys.modules['picard.config'] = pconf
sys.modules['picard.plugin3'] = p3
sys.modules['picard.plugin3.api'] = p3api

# ------------------------------------------------------------ load the plugin
spec = importlib.util.spec_from_file_location(
    'lyrics_plugin', os.path.join(PLUGIN_DIR, '__init__.py')
)
plugin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plugin)

# ------------------------------------------------------------------- fakes
class FakeLogger:
    def __init__(self):
        self.lines = []

    def _add(self, level, msg, *a):
        try:
            text = msg % a if a else msg
        except Exception:
            text = str(msg)
        self.lines.append('%s: %s' % (level, text))

    def debug(self, msg, *a):
        self._add('DEBUG', msg, *a)

    def info(self, msg, *a):
        self._add('INFO', msg, *a)

    def warning(self, msg, *a):
        self._add('WARN', msg, *a)

    def has(self, needle):
        return any(needle in line for line in self.lines)


class FakeConfig:
    section_name = 'plugin.3431b825'

    def __init__(self, **values):
        self.values = dict(values)

    def __getitem__(self, key):
        return self.values.get(key)

    def __setitem__(self, key, value):
        self.values[key] = value


class FakeWebService:
    """Delivers canned responses synchronously so the chain can be driven.

    A response entry may be a single ``(document, error)`` tuple, or a list of
    them to hand out different answers on successive calls to the same URL
    (the last entry repeats) - that is how retries are exercised.
    """

    def __init__(self, responses=None):
        self.calls = []
        self.responses = responses or {}
        self._counts = {}

    def get_url(self, **kwargs):
        self.calls.append(kwargs)
        url = kwargs.get('url')
        entry = self.responses.get(url, (None, 'no-canned-response'))
        if isinstance(entry, list):
            index = min(self._counts.get(url, 0), len(entry) - 1)
            self._counts[url] = index + 1
            document, error = entry[index]
        else:
            document, error = entry
        handler = kwargs.get('handler')
        if handler is not None:
            handler(document, None, error)
        return 'pending-request'


class FakeNetEaseClient:
    """Stands in for the plugin's dedicated HTTP/1.1 NetEase client."""

    def __init__(self, webservice):
        self.web_service = webservice
        self.calls = []

    def get(self, url, params, handler):
        self.calls.append((url, params))
        return self.web_service.get_url(
            url=url, handler=handler, unencoded_queryargs=params)


class FakeApi:
    def __init__(self, config, responses=None, with_album_task=True):
        self.plugin_config = config
        self.logger = FakeLogger()
        self.web_service = FakeWebService(responses)
        self._lyrics_fetcher_netease_client = FakeNetEaseClient(self.web_service)
        self.tasks = []
        self.completed = []
        self._with_album_task = with_album_task

    def add_album_task(self, album, task_id, description, **kwargs):
        if not self._with_album_task:
            raise RuntimeError('album tasks unavailable')
        self.tasks.append((album, task_id, description))
        factory = kwargs.get('request_factory')
        if factory:
            factory()

    def complete_album_task(self, album, task_id):
        self.completed.append((album, task_id))


class FakeAlbum:
    def __init__(self, name='Album'):
        self.name = name


class FakeTrack:
    def __init__(self, album=None):
        self.album = album


class FakeFile:
    """Mimics Picard's File, including supports_tag() for the format check."""

    ID3_EXTENSIONS = {'.mp3', '.aif', '.aifc', '.aiff', '.wav'}

    def __init__(self, filename, metadata, supports_synced=None):
        self.filename = filename
        self.metadata = metadata
        if supports_synced is None:
            supports_synced = os.path.splitext(filename)[1].lower() in self.ID3_EXTENSIONS
        self._supports_synced = supports_synced
        self.updated = 0

    def supports_tag(self, name):
        if name == 'syncedlyrics':
            return self._supports_synced
        return True

    def update(self, signal=True):
        """Mimics File.update(), which recomputes the changed state."""
        self.updated += 1


DEFAULTS = dict(
    write_tags=True,
    clean_lyrics=True,
    netease_add_translation=False,
    never_replace=True,
    source='auto',
    lrc_mode='never',
)

RESULTS = []


def check(name, condition, detail=''):
    RESULTS.append((name, bool(condition), detail))
    print('%-60s %s %s' % (name, 'PASS' if condition else 'FAIL', detail))


# ---------------------------------------------------------------- sample data
def lrclib_record(title, artist, duration, album='Album', synced=True, plain=True,
                  instrumental=False):
    return {
        'trackName': title,
        'artistName': artist,
        'albumName': album,
        'duration': duration,
        'instrumental': instrumental,
        'syncedLyrics': '[00:12.34] line one\n[00:16.20] line two' if synced else None,
        'plainLyrics': 'line one\nline two' if plain else None,
    }


def ne_song(sid, name, artists, album, milliseconds):
    return {
        'id': sid,
        'name': name,
        'artists': [{'name': a} for a in artists],
        'album': {'name': album},
        'duration': milliseconds,
    }


LRCLIB_HITS = [
    lrclib_record('Lemon', '米津玄師', 256.0),
    lrclib_record('Lemon', '米津玄師', 280.0, album='BOOTLEG'),
]

NE_LRC = (
    '[00:00.000] 作词 : GAK-amazuti-\n'
    '[00:01.016] 作曲 : GAK-amazuti-\n'
    '[00:01.473] 编曲 : GAK-amazuti-\n'
    '[00:23.846] 嵐の中でも 君を探してた\n'
    '[00:28.100] もう一度だけ\n'
)
NE_TLRC = '[00:23.846] 即使在暴风雨中 我一直在寻找你\n[00:28.100] 再一次就好\n'

NE_SEARCH_DOC = {'result': {'songs': [
    ne_song(9999, '嵐の中でも', ['藍井エイル'], 'UNBOUND', 280600),
    ne_song(1111, '嵐の中でも (Instrumental)', ['藍井エイル'], 'UNBOUND', 280000),
    ne_song(2222, '嵐の中でも', ['别人'], '别的专辑', 280600),
]}}
NE_LYRIC_DOC = {'lrc': {'lyric': NE_LRC}, 'tlyric': {'lyric': NE_TLRC},
                'klyric': {'lyric': 'x'}}

REPORTED = {
    plugin.NETEASE_SEARCH_URL: (NE_SEARCH_DOC, None),
    plugin.NETEASE_LYRIC_URL: (NE_LYRIC_DOC, None),
}

print('=== 1. text helpers ===')
check('_normalize casefolds and collapses',
      plugin._normalize('  Lemon   Cake ') == 'lemon cake')
check('_lrc_to_plain drops timestamps',
      plugin._lrc_to_plain('[00:01.00] a\n[00:02.00] b') == 'a\nb')
check('_lrc_to_plain drops blank lines',
      plugin._lrc_to_plain('[00:01.00] a\n[00:02.00] \n[00:03.00] b') == 'a\nb')

print()
print('=== 2. _strip_credits ===')
stripped = plugin._strip_credits(NE_LRC)
check('removes the leading 作词/作曲/编曲 block',
      stripped.startswith('[00:23.846]'), repr(stripped[:40]))
check('keeps every real lyric line', stripped.count('\n') == 1, repr(stripped))
check('removes LRC metadata tags too',
      plugin._strip_credits('[ti:x]\n[by:y]\n[00:01.00] a').strip() == '[00:01.00] a')
check('keeps a lyric line that merely starts with 作曲',
      plugin._strip_credits('[00:01.00] 作曲家的名字').strip() == '[00:01.00] 作曲家的名字')
check('all-credit input yields no lyrics at all',
      plugin._strip_credits('[00:00.00] 作词 : x') == '')
check('input with nothing to strip is returned unchanged',
      plugin._strip_credits('[00:01.00] 歌詞だけ') == '[00:01.00] 歌詞だけ')
check('no credits -> unchanged', plugin._strip_credits('[00:01.00] a') == '[00:01.00] a')
check('strips "混音工程师:" style credits (the reported case)',
      plugin._strip_credits('[00:00.00] 混音工程师: You Yokoi\n[00:01.00] 歌詞').strip()
      == '[00:01.00] 歌詞')
check('strips "录音工程师:" too',
      plugin._strip_credits('[00:00.00] 录音工程师：张三\n[00:01.00] 歌詞').strip()
      == '[00:01.00] 歌詞')
check('strips "OP:" / "SP:" credits',
      plugin._strip_credits('[00:00.00] OP: Sony\n[00:00.50] SP: Lantis\n[00:01.00] a').strip()
      == '[00:01.00] a')
check('does not strip a lyric line merely containing a colon',
      plugin._strip_credits('[00:01.00] 愛は: どこ').strip() == '[00:01.00] 愛は: どこ')
check('strips credits that follow a bare timestamp line',
      plugin._strip_credits(
          '[00:00.00] 作词 : X\n[00:02.55]\n[00:03.00] 作曲 : Y\n[00:23.00] 歌詞'
      ).strip() == '[00:23.00] 歌詞')

print()
print('=== 2b. _drop_empty_lines ===')
check('removes timestamp-only lines (the reported case)',
      plugin._drop_empty_lines('[00:02.552]\n[00:23.846]歌\n[00:25.720]詞')
      == '[00:23.846]歌\n[00:25.720]詞')
check('keeps normal lines',
      plugin._drop_empty_lines('[00:01.00] a\n[00:02.00] b') == '[00:01.00] a\n[00:02.00] b')
check('removes a multi-timestamp line with no text',
      plugin._drop_empty_lines('[00:01.00][00:05.00]\n[00:09.00] a') == '[00:09.00] a')
check('keeps a line that has text after several timestamps',
      plugin._drop_empty_lines('[00:01.00][00:05.00] a') == '[00:01.00][00:05.00] a')
check('blank lines are left alone',
      plugin._drop_empty_lines('[00:01.00] a\n\n[00:02.00] b')
      == '[00:01.00] a\n\n[00:02.00] b')
check('_line_text handles NetEase\'s "[00:00.00-1]" oddity',
      plugin._line_text('[00:00.00-1] 作曲 : X') == '作曲 : X')
check('a credits-only track is recognised despite that timestamp',
      plugin._strip_credits('[00:00.00-1] 作曲 : 小池竜暉/菊池博人') == '')

print()
print('=== 2c. the expanded credit keyword list ===')
# Keywords taken from the MusicMetaCleaner project, checked so that widening
# the list never starts eating real lyric lines.
CREDIT_CASES = [
    '作词 : X', '填词：X', '谱曲：X', '配器：X', '和音：X', '配唱：X', '主唱：X',
    '缩混：X', '混音师：X', '后期：X', '母带处理：X', '文案：X', '企划：X',
    '发行公司：X', '唱片公司：X', '词作者：X', '曲作者：X', '翻译：X', '音译：X',
    '歌词制作：X', '歌词编辑：X', '作詞：X', '詞：X', '編曲：X', '詩曲：X',
    'Produced by: X', 'Lyricist: X', 'Composer: X', 'Arranger: X', 'Written by: X',
    'Mixed by: X', 'Mastering: X', 'Album: X', 'Title: X', 'Song: X', 'Track: X',
    'Disc: X', 'Publisher: X', 'Copyright: X', 'ISRC: X', 'Source: X',
    'Vocals recorded by: X', 'Drum Programming: X', '© 2026 Sony', 'Feat. X',
    'Produced by John', '未经许可，不得翻唱', '版权所有：X', '版权归：X',
    # Performers and instruments (reported by the user for a Japanese release)
    '编程：大森元贵/Ryo Hanai', '电吉他：Hiloto Wakai/大森元贵',
    '键盘：Ryoka Fujisawa', '鼓：Hideyuki Kurakazu',
    '贝斯：X', '小提琴：X', '大提琴：X', '合成器：X', '打击乐：X', '架子鼓：X',
    '木吉他：X', '原声吉他：X', '三味线：X', 'Programming: X',
    'Electric Guitar: X', 'Acoustic Guitar: X', 'Drums: X', 'Keyboards: X',
    'Synthesizer: X', 'Percussion: X',
]
LYRIC_CASES = [
    '凪の海に漂うように', '正解だけが朽ち果てる世の中', '愛は: どこ', '作曲家的名字',
    '混音很重要', 'OPを探して', 'Song of the sea', 'Album of memories',
    'Track my heart', 'Original sin', 'Version of me', 'Source of light',
    '提供给我一点温暖', '作品里的故事', '版权归我们所有', '版权所有 请勿盗用',
    'Chorus of angels', '未经许可我闯进你的世界', 'Featuring you in my dream',
    'Mixed feelings: I cry', 'Music by the sea', 'Words by the river',
    'Vocals of the wind',
    # Bare 鼓 must only match with the colon right after it, otherwise these go.
    '鼓起勇气：往前走', '鼓声：在夜里回响', '鼓励：你做到了吗', '鼓动：我的心跳',
    '键盘上的舞者', '电吉他响起来', '编程人生', '鼓手的心跳', '贝斯的低音线',
]


def _is_credit(text):
    """The rules that apply anywhere in the lyrics (not just the opening)."""
    return bool(
        plugin._CREDIT.match(text)
        or plugin._CREDIT_BARE.match(text)
        or plugin._CREDIT_SINGLE_STRICT.match(text)
    )


missed = [t for t in CREDIT_CASES if not _is_credit(t)]
false_pos = [t for t in LYRIC_CASES if _is_credit(t)]
check('every credit form is recognised', not missed, str(missed))
check('no lyric line is mistaken for a credit', not false_pos, str(false_pos))
check('_CREDIT_BARE is consulted by _strip_credits',
      plugin._strip_credits('[00:00.00] Produced by John\n[00:01.00] 歌詞').strip()
      == '[00:01.00] 歌詞')
check('bare "版权归" alone does not start a strip',
      plugin._strip_credits('[00:00.00] 版权归我们所有\n[00:01.00] 歌詞').strip()
      == '[00:00.00] 版权归我们所有\n[00:01.00] 歌詞')

print()
print('=== 2d. credits are stripped anywhere, not just at the top ===')
# NetEase uploaders scatter this junk through the song and dump it at the end.
MID_AND_END = '\n'.join([
    '[00:00.000] 作词 : A',
    '[00:01.000] 作曲 : B',
    '[00:02.552]',
    '[00:23.846] 凪の海に漂うように',
    '[00:28.100] 正解だけが朽ち果てる世の中',
    '[00:30.000] 词不达意: 我说不出口',
    '[00:35.000] 歌词制作：XXX',
    '[00:40.000] 曲终人散: 谁还记得',
    '[00:45.000] 翻译：YYY',
    '[00:50.000] Featuring you in my dream',
    '[03:50.000] 未经许可，不得翻唱或使用',
    '[03:55.000] 词：ZZZ',
    '[04:00.000] 版权归我们所有',
    '[04:05.000] 曲终人散',
])
result = plugin._strip_credits(MID_AND_END)
kept_lines = [ln for ln in result.splitlines() if ln.strip()]
check('mid-song credit is removed', '歌词制作' not in result)
check('mid-song translation credit is removed', '翻译：YYY' not in result)
check('end-of-song licence notice is removed', '未经许可' not in result)
check('end-of-song "词：" credit is removed', '词：ZZZ' not in result)
check('leading credits still removed', '作词 : A' not in result and '作曲 : B' not in result)
check('bare timestamp line still removed', '[00:02.552]' not in result)
check('lyric "词不达意: ..." survives', '词不达意: 我说不出口' in result)
check('lyric "曲终人散: ..." survives', '曲终人散: 谁还记得' in result)
check('lyric "曲终人散" (no colon) survives', '[04:05.000] 曲终人散' in result)
check('lyric "Featuring you in my dream" survives',
      'Featuring you in my dream' in result)
check('lyric "版权归我们所有" survives', '版权归我们所有' in result)
check('exactly the 7 lyric lines remain', len(kept_lines) == 7, str(len(kept_lines)))

# The leading block still accepts the loose single-character form with a gap.
check('leading "词 : X" (space before colon) is removed',
      plugin._strip_credits('[00:00.00] 词 : X\n[00:01.00] 歌詞').strip()
      == '[00:01.00] 歌詞')
# ...but mid-song the colon has to be immediate.
check('mid-song "词 : X" with a gap is kept',
      plugin._strip_credits('[00:01.00] a\n[00:02.00] 词 不 : X').strip()
      == '[00:01.00] a\n[00:02.00] 词 不 : X')

print()
print('=== 2e. performer credits: leading block and mid-song ===')
# Reported by the user: a Japanese release listed the players as
# "[00:00.363]编程：…", "[00:00.604]电吉他：…" and so on.
PERFORMERS = '\n'.join([
    '[00:00.363]编程：大森元贵/Ryo Hanai',
    '[00:00.604]电吉他：Hiloto Wakai/大森元贵',
    '[00:00.781]键盘：Ryoka Fujisawa',
    '[00:00.964]鼓：Hideyuki Kurakazu',
    '[00:01.200]作词 : 大森元貴',
    '[00:01.400]作曲 : 大森元貴',
    '[00:24.850]拾い集めて',
    '[00:26.100]更に探す東京',
])
out = plugin._strip_credits(PERFORMERS)
check('the performer block is removed', '编程' not in out and '电吉他' not in out)
check('作词/作曲 alongside it are removed too',
      '作词' not in out and '作曲' not in out)
check('the real lyrics survive', out.strip() == '[00:24.850]拾い集めて\n[00:26.100]更に探す東京',
      repr(out))

# Mid-song performer credits are removed as well...
MIXED = '\n'.join([
    '[00:24.850]拾い集めて',
    '[00:30.000]鼓：Hideyuki Kurakazu',
    '[00:32.000]电吉他：Hiloto Wakai',
    '[00:34.000]编程：大森元贵',
    '[00:36.000]更に探す東京',
])
out = plugin._strip_credits(MIXED)
check('mid-song performer credits are removed',
      out.strip() == '[00:24.850]拾い集めて\n[00:36.000]更に探す東京', repr(out))

# ...but lyric lines that merely start with one of those words are untouched.
SINGABLE = '\n'.join([
    '[00:24.850]拾い集めて',
    '[00:30.000]鼓起勇气：往前走',
    '[00:32.000]鼓声：在夜里回响',
    '[00:34.000]鼓励：你做到了吗',
    '[00:36.000]鼓动：我的心跳',
    '[00:38.000]键盘上的舞者',
    '[00:40.000]电吉他响起来',
    '[00:42.000]更に探す東京',
])
out = plugin._strip_credits(SINGABLE)
check('singable lines starting with 鼓/键盘/电吉他 are all kept',
      len([ln for ln in out.splitlines() if ln.strip()]) == 8, repr(out))

print()
print('=== 3. _merge_translation ===')
merged = plugin._merge_translation(NE_LRC, NE_TLRC)
check('original line kept', '[00:23.846] 嵐の中でも 君を探してた' in merged, repr(merged[:120]))
check('translation appended', '即使在暴风雨中 我一直在寻找你' in merged)
check('credit lines without translation left alone',
      '[00:00.000] 作词 : GAK-amazuti-' in merged)
check('empty translation -> unchanged',
      plugin._merge_translation(NE_LRC, '') == NE_LRC)
check('no timestamp overlap -> unchanged',
      plugin._merge_translation('[00:01.00] a', '[00:99.00] b') == '[00:01.00] a')

print()
print('=== 4. _duration_seconds ===')
check('"4:21" -> 261', plugin._duration_seconds({'~length': '4:21'}) == 261)
check('"1:02:03" -> 3723', plugin._duration_seconds({'~length': '1:02:03'}) == 3723)
check('missing -> None', plugin._duration_seconds({}) is None)
check('garbage -> None', plugin._duration_seconds({'~length': 'abc'}) is None)

print()
print('=== 5. _pick_lrclib ===')
best = plugin._pick_lrclib(LRCLIB_HITS, 'Lemon', '米津玄師', 280)
check('picks the exact-duration record',
      best is not None and best['duration'] == 280.0, str(best and best['duration']))
check('rejects a different artist',
      plugin._pick_lrclib([lrclib_record('Lemon', 'Cover Band', 280.0)],
                          'Lemon', '米津玄師', 280) is None)
check('rejects a different title',
      plugin._pick_lrclib([lrclib_record('Lemon (Remix)', '米津玄師', 280.0)],
                          'Lemon', '米津玄師', 280) is None)
check('empty list -> None', plugin._pick_lrclib([], 'Lemon', '米津玄師', 280) is None)
check('artist containment accepted',
      plugin._pick_lrclib([lrclib_record('x', '藍井エイル & ASCA', 10.0)],
                          'x', '藍井エイル', 10) is not None)

print()
print('=== 6. _pick_netease ===')
songs = NE_SEARCH_DOC['result']['songs']
cands = plugin._pick_netease(songs, '嵐の中でも', '藍井エイル', 281)
check('picks the plain-titled exact match',
      cands and cands[0]['id'] == 9999, str(cands[:1]))
check('"(Instrumental)" title variant rejected',
      plugin._pick_netease([songs[1]], '嵐の中でも', '藍井エイル', 281) == [])
check('wrong artist rejected',
      plugin._pick_netease([songs[2]], '嵐の中でも', '藍井エイル', 281) == [])
check('duration is read as milliseconds (280600 -> 280.6s)',
      abs((cands[0]['duration'] / 1000.0) - 280.6) < 0.01)
check('empty list -> []', plugin._pick_netease([], 'x', 'y', 10) == [])
check('artist containment accepted',
      plugin._pick_netease([ne_song(1, '打上花火', ['DAOKO', '米津玄師'], 'A', 1000)],
                           '打上花火', 'DAOKO', 1) != [])
check('several matching releases are all returned (ranked)',
      len(plugin._pick_netease(
          [ne_song(1, 'x', ['a'], 'Single', 1000),
           ne_song(2, 'x', ['a'], 'Album', 1000),
           ne_song(3, 'x', ['a'], 'Comp', 1000)],
          'x', 'a', 1)) == 3)
check('candidate list is capped',
      len(plugin._pick_netease(
          [ne_song(i, 'x', ['a'], 'A%d' % i, 1000) for i in range(10)],
          'x', 'a', 1)) == plugin.NETEASE_CANDIDATES)

print()
print('=== 6b. NetEase: a release without lyrics falls through to the next ===')
# The real case: the album entry carries only a credit line while the single
# release of the same track carries the full lyrics.
TWO_RELEASES = {'result': {'songs': [
    ne_song(3436268212, 'ハナムケ', ['藍井エイル'], 'UNBOUND', 243000),
    ne_song(3415867949, 'ハナムケ', ['藍井エイル'], 'ハナムケ', 243000),
]}}
responses = {
    plugin.NETEASE_SEARCH_URL: (TWO_RELEASES, None),
    plugin.NETEASE_LYRIC_URL: [
        ({'lrc': {'lyric': '[00:00.00-1] 作曲 : 小池竜暉/菊池博人'}}, None),   # album: credits only
        ({'lrc': {'lyric': '[00:06.051] 仄暗い世界に しゃがみ込んでいた\n[00:11.477] 涙も枯れた'}},
         None),                                                                # single: real lyrics
    ],
}
api = FakeApi(FakeConfig(**DEFAULTS), responses)
meta = {'title': 'ハナムケ', 'artist': '藍井エイル', '~length': '4:03'}
plugin._on_file_added(api, FakeTrack(FakeAlbum()),
                      FakeFile(os.path.join(tempfile.mkdtemp(), 'hana.flac'), meta))
check('the second release is tried', len(api.web_service.calls) == 3,
      str(len(api.web_service.calls)))
check('lyrics from the second release are written',
      '仄暗い世界に' in (meta.get('lyrics') or ''), repr((meta.get('lyrics') or '')[:40]))
check('the fallback is logged', api.logger.has('trying the next match'))

# When every release is lyrics-less, the lookup still falls through cleanly.
responses = {
    plugin.NETEASE_SEARCH_URL: (TWO_RELEASES, None),
    plugin.NETEASE_LYRIC_URL: ({'nolyric': True}, None),
    plugin.LRCLIB_SEARCH_URL: (LRCLIB_HITS, None),
}
api = FakeApi(FakeConfig(**DEFAULTS), responses)
meta = {'title': 'Lemon', 'artist': '米津玄師', '~length': '4:40'}
plugin._on_file_added(api, FakeTrack(FakeAlbum()),
                      FakeFile(os.path.join(tempfile.mkdtemp(), 'hana2.mp3'), meta))
check('all releases exhausted -> LRCLIB used',
      meta.get('syncedlyrics') == LRCLIB_HITS[1]['syncedLyrics'])

print()
print('=== 7. _source_order ===')
check('auto -> netease, kugou then lrclib',
      plugin._source_order(FakeApi(FakeConfig(source='auto')))
      == ('netease', 'kugou', 'lrclib'))
check('netease -> netease only',
      plugin._source_order(FakeApi(FakeConfig(source='netease'))) == ('netease',))
check('kugou -> kugou only',
      plugin._source_order(FakeApi(FakeConfig(source='kugou'))) == ('kugou',))
check('lrclib -> lrclib only',
      plugin._source_order(FakeApi(FakeConfig(source='lrclib'))) == ('lrclib',))
check('unset -> auto',
      plugin._source_order(FakeApi(FakeConfig())) == ('netease', 'kugou', 'lrclib'))

print()
print('=== 8. full chain: the reported track via NetEase ===')
tmp = tempfile.mkdtemp()
cfg = FakeConfig(**DEFAULTS)
api = FakeApi(cfg, REPORTED)
meta = {'title': '嵐の中でも', 'artist': '藍井エイル', 'album': 'UNBOUND', '~length': '4:41'}
album = FakeAlbum()
plugin._on_file_added(api, FakeTrack(album), FakeFile(os.path.join(tmp, 'a.mp3'), meta))
check('two requests made (search + lyric)', len(api.web_service.calls) == 2,
      str([c['url'] for c in api.web_service.calls]))
check('first request is the NetEase search',
      api.web_service.calls[0]['url'] == plugin.NETEASE_SEARCH_URL)
check('second request is the NetEase lyric endpoint',
      api.web_service.calls[1]['url'] == plugin.NETEASE_LYRIC_URL)
check('lyric request carries the chosen song id',
      api.web_service.calls[1]['unencoded_queryargs'].get('id') == 9999,
      str(api.web_service.calls[1]['unencoded_queryargs']))
check('both NetEase requests went through the dedicated client',
      len(api._lyrics_fetcher_netease_client.calls) == 2,
      str(api._lyrics_fetcher_netease_client.calls))
check('NetEase headers carry a Referer',
      plugin.NETEASE_HEADERS.get('Referer') == 'https://music.163.com/')
check('syncedlyrics written', bool(meta.get('syncedlyrics')))
check('credits stripped from syncedlyrics',
      '作词' not in (meta.get('syncedlyrics') or ''), repr((meta.get('syncedlyrics') or '')[:40]))
check('syncedlyrics keeps timestamps', '[00:23.846]' in (meta.get('syncedlyrics') or ''))
check('lyrics is the plain version (no timestamps)',
      meta.get('lyrics') and '[00:23' not in meta['lyrics'], repr((meta.get('lyrics') or '')[:40]))
check('album task completed exactly once', len(api.completed) == 1, str(api.completed))
check('success logged with the NetEase description',
      api.logger.has('NetEase "嵐の中でも"'), str(api.logger.lines))

print()
print('=== 9. NetEase options ===')
cfg = FakeConfig(**dict(DEFAULTS, clean_lyrics=False))
api = FakeApi(cfg, REPORTED)
meta = {'title': '嵐の中でも', 'artist': '藍井エイル', '~length': '4:41'}
plugin._on_file_added(api, FakeTrack(FakeAlbum()), FakeFile(os.path.join(tmp, 'b.mp3'), meta))
check('strip_credits=False keeps 作词', '作词' in (meta.get('syncedlyrics') or ''))

cfg = FakeConfig(**dict(DEFAULTS, netease_add_translation=True))
api = FakeApi(cfg, REPORTED)
meta = {'title': '嵐の中でも', 'artist': '藍井エイル', '~length': '4:41'}
plugin._on_file_added(api, FakeTrack(FakeAlbum()), FakeFile(os.path.join(tmp, 'c.mp3'), meta))
check('translation merged when enabled',
      '即使在暴风雨中' in (meta.get('syncedlyrics') or ''),
      repr((meta.get('syncedlyrics') or '')[-60:]))

print()
print('=== 10. fallback chain ===')
# NetEase returns nothing -> Kugou is tried -> then LRCLIB.
responses = {plugin.NETEASE_SEARCH_URL: ({'result': {'songs': []}}, None),
             plugin.KUGOU_SEARCH_URL: ({'data': {'info': []}}, None),
             plugin.LRCLIB_SEARCH_URL: (LRCLIB_HITS, None)}
cfg = FakeConfig(**DEFAULTS)
api = FakeApi(cfg, responses)
meta = {'title': 'Lemon', 'artist': '米津玄師', '~length': '4:40'}
plugin._on_file_added(api, FakeTrack(FakeAlbum()), FakeFile(os.path.join(tmp, 'd.mp3'), meta))
urls = [c['url'] for c in api.web_service.calls]
check('tries NetEase, then Kugou, then LRCLIB',
      urls == [plugin.NETEASE_SEARCH_URL, plugin.KUGOU_SEARCH_URL,
               plugin.LRCLIB_SEARCH_URL], str(urls))
check('LRCLIB result written', meta.get('syncedlyrics') == LRCLIB_HITS[1]['syncedLyrics'])
check('task completed once after fallback', len(api.completed) == 1, str(api.completed))

# NetEase lyric endpoint has no lyrics -> fall through to LRCLIB.
responses = {plugin.NETEASE_SEARCH_URL: (NE_SEARCH_DOC, None),
             plugin.NETEASE_LYRIC_URL: ({'nolyric': True}, None),
             plugin.LRCLIB_SEARCH_URL: (LRCLIB_HITS, None)}
api = FakeApi(FakeConfig(**DEFAULTS), responses)
meta = {'title': 'Lemon', 'artist': '米津玄師', '~length': '4:40'}
plugin._on_file_added(api, FakeTrack(FakeAlbum()), FakeFile(os.path.join(tmp, 'e.mp3'), meta))
check('nolyric falls through to LRCLIB', meta.get('syncedlyrics') == LRCLIB_HITS[1]['syncedLyrics'])
check('task completed once', len(api.completed) == 1)

# Nothing anywhere -> silent, no writes, task still completed.
responses = {plugin.NETEASE_SEARCH_URL: ({'result': {'songs': []}}, None),
             plugin.LRCLIB_SEARCH_URL: ([], None)}
api = FakeApi(FakeConfig(**DEFAULTS), responses)
meta = {'title': '嵐の中でも', 'artist': '藍井エイル'}
plugin._on_file_added(api, FakeTrack(FakeAlbum()), FakeFile(os.path.join(tmp, 'f.mp3'), meta))
check('nothing written when both sources miss',
      'lyrics' not in meta and 'syncedlyrics' not in meta)
check('task still completed', len(api.completed) == 1)
check('no-match logged at info level', api.logger.has('no match'))

print()
print('=== 11. source selection respected ===')
cfg = FakeConfig(**dict(DEFAULTS, source='lrclib'))
api = FakeApi(cfg, {plugin.LRCLIB_SEARCH_URL: (LRCLIB_HITS, None)})
meta = {'title': 'Lemon', 'artist': '米津玄師', '~length': '4:40'}
plugin._on_file_added(api, FakeTrack(FakeAlbum()), FakeFile(os.path.join(tmp, 'g.mp3'), meta))
check('source=lrclib never calls NetEase',
      [c['url'] for c in api.web_service.calls] == [plugin.LRCLIB_SEARCH_URL],
      str([c['url'] for c in api.web_service.calls]))

cfg = FakeConfig(**dict(DEFAULTS, source='netease'))
api = FakeApi(cfg, REPORTED)
meta = {'title': '嵐の中でも', 'artist': '藍井エイル', '~length': '4:41'}
plugin._on_file_added(api, FakeTrack(FakeAlbum()), FakeFile(os.path.join(tmp, 'h.mp3'), meta))
check('source=netease never calls LRCLIB',
      all(c['url'] != plugin.LRCLIB_SEARCH_URL for c in api.web_service.calls))
check('source=netease still writes', bool(meta.get('syncedlyrics')))

print()
print('=== 12. guards ===')
cfg = FakeConfig(**dict(DEFAULTS, write_tags=False))
api = FakeApi(cfg, REPORTED)
plugin._on_file_added(api, FakeTrack(), FakeFile('i.mp3', {'title': 't', 'artist': 'a'}))
check('disabled -> no request', api.web_service.calls == [])

api = FakeApi(FakeConfig(**DEFAULTS), REPORTED)
plugin._on_file_added(api, FakeTrack(), FakeFile('j.mp3', {'title': '', 'artist': 'a'}))
check('missing title -> no request', api.web_service.calls == [])

api = FakeApi(FakeConfig(**DEFAULTS), REPORTED)
plugin._on_file_added(api, FakeTrack(),
                      FakeFile('k.mp3', {'title': 't', 'artist': 'a', 'lyrics': 'x'}))
check('existing lyrics + never_replace -> no request', api.web_service.calls == [])

cfg = FakeConfig(**dict(DEFAULTS, write_tags=False))
api = FakeApi(cfg, REPORTED)
plugin._on_file_added(api, FakeTrack(), FakeFile('l.mp3', {'title': 't', 'artist': 'a'}))
check('both outputs off -> no request', api.web_service.calls == [])

print()
print('=== 13. never_replace and write switches (NetEase path) ===')
cfg = FakeConfig(**DEFAULTS)
api = FakeApi(cfg, REPORTED)
meta = {'title': '嵐の中でも', 'artist': '藍井エイル', '~length': '4:41',
        'lyrics': 'OLD', 'syncedlyrics': 'OLD_SYNCED'}
plugin._on_file_added(api, FakeTrack(FakeAlbum()), FakeFile(os.path.join(tmp, 'm.mp3'), meta))
check('existing tags kept under never_replace', meta['lyrics'] == 'OLD'
      and meta['syncedlyrics'] == 'OLD_SYNCED')

cfg = FakeConfig(**dict(DEFAULTS, never_replace=False))
api = FakeApi(cfg, REPORTED)
meta = {'title': '嵐の中でも', 'artist': '藍井エイル', '~length': '4:41',
        'lyrics': 'OLD', 'syncedlyrics': 'OLD_SYNCED'}
plugin._on_file_added(api, FakeTrack(FakeAlbum()), FakeFile(os.path.join(tmp, 'n.mp3'), meta))
check('overwritten when never_replace is off', meta['lyrics'] != 'OLD')

cfg = FakeConfig(**dict(DEFAULTS, write_tags=False))
api = FakeApi(cfg, REPORTED)
meta = {'title': '嵐の中でも', 'artist': '藍井エイル', '~length': '4:41'}
plugin._on_file_added(api, FakeTrack(FakeAlbum()), FakeFile(os.path.join(tmp, 'o.mp3'), meta))
check('write_tags=False -> no request at all', api.web_service.calls == [])
check('write_tags=False -> nothing written',
      'lyrics' not in meta and 'syncedlyrics' not in meta)

print()
print('=== 14. album-task fallbacks ===')
api = FakeApi(FakeConfig(**DEFAULTS), REPORTED, with_album_task=False)
meta = {'title': '嵐の中でも', 'artist': '藍井エイル', '~length': '4:41'}
plugin._on_file_added(api, FakeTrack(FakeAlbum()), FakeFile(os.path.join(tmp, 'p.mp3'), meta))
check('direct request when album tasks unavailable', len(api.web_service.calls) == 2)
check('no album task recorded', api.tasks == [])
check('still writes', bool(meta.get('syncedlyrics')))

api = FakeApi(FakeConfig(**DEFAULTS), REPORTED)
meta = {'title': '嵐の中でも', 'artist': '藍井エイル', '~length': '4:41'}
plugin._on_file_added(api, FakeTrack(None), FakeFile(os.path.join(tmp, 'q.mp3'), meta))
check('no album on track -> direct request', api.tasks == [])
check('still writes', bool(meta.get('syncedlyrics')))

print()
print('=== 15. _lrc_path and the .lrc export ===')
f = FakeFile(os.path.join(tmp, 'song.mp3'), {})
check('same directory, same base name',
      plugin._lrc_path(f) == os.path.join(tmp, 'song.lrc'), plugin._lrc_path(f))
check('non-ascii names survive',
      plugin._lrc_path(FakeFile(os.path.join(tmp, '01. 嵐の中でも.flac'), {}))
      == os.path.join(tmp, '01. 嵐の中でも.lrc'))

outdir = tempfile.mkdtemp()
audio = os.path.join(outdir, 'track.mp3')
lrc = os.path.join(outdir, 'track.lrc')

# Default is "never": no sidecar at all.
api = FakeApi(FakeConfig(**DEFAULTS))
plugin._on_file_saved(api, FakeFile(audio, {'syncedlyrics': '[00:01.00] hello'}))
check('lrc_mode=never -> no file', not os.path.exists(lrc), lrc)

# "always" writes one for every format.
api = FakeApi(FakeConfig(**dict(DEFAULTS, lrc_mode='always')))
plugin._on_file_saved(api, FakeFile(audio, {'syncedlyrics': '[00:01.00] hello'}))
check('lrc_mode=always -> file created', os.path.exists(lrc), lrc)
check('.lrc holds the synced text',
      open(lrc, encoding='utf-8').read() == '[00:01.00] hello')
plugin._on_file_saved(api, FakeFile(audio, {'syncedlyrics': 'CHANGED'}))
check('an existing .lrc is never overwritten',
      open(lrc, encoding='utf-8').read() == '[00:01.00] hello')

# "unsupported" skips formats that can store syncedlyrics themselves.
os.remove(lrc)
api = FakeApi(FakeConfig(**dict(DEFAULTS, lrc_mode='unsupported')))
plugin._on_file_saved(api, FakeFile(audio, {'syncedlyrics': 'x'}))
check('lrc_mode=unsupported -> no file for MP3', not os.path.exists(lrc))
flac_audio = os.path.join(outdir, 'flac_track.flac')
plugin._on_file_saved(api, FakeFile(flac_audio, {'lyrics': '[00:01.00] x'}))
check('lrc_mode=unsupported -> file for FLAC',
      os.path.exists(os.path.join(outdir, 'flac_track.lrc')))

print()
print('=== 16. enable() and resilience ===')
registered = {'track': [], 'save': [], 'options': [], 'track_action': [], 'album_action': []}


class RecordingApi(FakeApi):
    def register_file_post_addition_to_track_processor(self, fn, priority=0):
        registered['track'].append(fn)

    def register_file_post_save_processor(self, fn, priority=0):
        registered['save'].append(fn)

    def register_options_page(self, page):
        registered['options'].append(page)

    def register_track_action(self, action):
        registered['track_action'].append(action)

    def register_album_action(self, action):
        registered['album_action'].append(action)


BoolOption.registry.clear()
api = RecordingApi(FakeConfig(**DEFAULTS))
plugin.enable(api)
check('track processor registered', registered['track'] == [plugin._on_file_added])
check('save processor registered', registered['save'] == [plugin._on_file_saved])
check('options page registered', registered['options'] == [plugin.LrclibLyricsOptionsPage])
check('track action registered', registered['track_action'] == [plugin.FetchLyricsAction])
check('album action registered', registered['album_action'] == [plugin.FetchLyricsAction])
check('action has a menu title', bool(plugin.FetchLyricsAction.TITLE),
      plugin.FetchLyricsAction.TITLE)
check('all settings declared', len(BoolOption.registry) == 6,
      str(sorted(n for _s, n in BoolOption.registry)))
check('source default declared',
      BoolOption.registry.get(('plugin.3431b825', 'source')) == 'auto')
check('disable() is a no-op', plugin.disable() is None)

api = FakeApi(FakeConfig(), REPORTED)
meta = {'title': '嵐の中でも', 'artist': '藍井エイル', '~length': '4:41'}
plugin._on_file_added(api, FakeTrack(FakeAlbum()), FakeFile(os.path.join(tmp, 'r.mp3'), meta))
check('unregistered settings still fetch (defaults apply)', bool(meta.get('syncedlyrics')))
check('_setting falls back for unknown key', plugin._setting(api, 'write_tags') is True)

print()
print('=== 17. manual action: _track_files and FetchLyricsAction ===')


class TrackWithFiles:
    def __init__(self, album=None):
        self.album = album
        self.files = []


class AlbumWithTracks:
    def __init__(self, tracks):
        self.tracks = tracks


t1 = TrackWithFiles()
t2 = TrackWithFiles()
f1 = FakeFile(os.path.join(tmp, 'x1.mp3'), {})
f2 = FakeFile(os.path.join(tmp, 'x2.mp3'), {})
f3 = FakeFile(os.path.join(tmp, 'x3.mp3'), {})
t1.files = [f1, f2]
t2.files = [f3]

pairs = list(plugin._track_files(t1))
check('Track -> yields its own files', pairs == [(t1, f1), (t1, f2)], str(len(pairs)))
pairs = list(plugin._track_files(AlbumWithTracks([t1, t2])))
check('Album -> yields (track, file) for every track',
      pairs == [(t1, f1), (t1, f2), (t2, f3)], str(len(pairs)))
pairs = list(plugin._track_files(object()))
check('object without files -> empty', pairs == [])

# force=True must override the "never replace" guard.
cfg = FakeConfig(**DEFAULTS)
api = FakeApi(cfg, REPORTED)
meta = {'title': '嵐の中でも', 'artist': '藍井エイル', '~length': '4:41',
        'lyrics': 'OLD', 'syncedlyrics': 'OLD_SYNCED'}
f = FakeFile(os.path.join(tmp, 'y1.mp3'), meta)
started = plugin._begin_lookup(api, None, f, force=True, use_album_task=False)
check('force=True bypasses never_replace', started and meta['lyrics'] != 'OLD')
check('force path issues the requests', len(api.web_service.calls) == 2)

# Without force the guard still applies.
cfg = FakeConfig(**DEFAULTS)
api = FakeApi(cfg, REPORTED)
meta = {'title': '嵐の中でも', 'artist': '藍井エイル', 'lyrics': 'OLD'}
started = plugin._begin_lookup(api, None, FakeFile('y2.mp3', meta))
check('without force the guard applies', started is False and meta['lyrics'] == 'OLD')
check('guard logged at info level', api.logger.has('already has lyrics'))

# Missing tags.
api = FakeApi(FakeConfig(**DEFAULTS), REPORTED)
started = plugin._begin_lookup(api, None, FakeFile('y3.mp3', {'title': '', 'artist': ''}))
check('missing title/artist -> not started', started is False)
check('missing tags logged at warning level', api.logger.has('no title and/or artist'))

# The action itself.
cfg = FakeConfig(**DEFAULTS)
api = FakeApi(cfg, REPORTED)
at1 = TrackWithFiles()
af1 = FakeFile(os.path.join(tmp, 'z1.mp3'),
               {'title': '嵐の中でも', 'artist': '藍井エイル', '~length': '4:41'})
af2 = FakeFile(os.path.join(tmp, 'z2.mp3'),
               {'title': '嵐の中でも', 'artist': '藍井エイル', '~length': '4:41'})
at1.files = [af1, af2]
action = plugin.FetchLyricsAction()
action.api = api
action.callback([at1])
check('action fetches every selected file', len(api.web_service.calls) == 4,
      str(len(api.web_service.calls)))
check('action reports the count', api.logger.has('manual lookup started for 2 file(s)'))

api = FakeApi(FakeConfig(**DEFAULTS), REPORTED)
action = plugin.FetchLyricsAction()
action.api = api
action.callback([AlbumWithTracks([at1]), at1])
check('action de-duplicates files across selected objects',
      len(api.web_service.calls) == 4, str(len(api.web_service.calls)))
check('action still writes lyrics', bool(af1.metadata.get('syncedlyrics')))

api = FakeApi(FakeConfig(**DEFAULTS), REPORTED)
action = plugin.FetchLyricsAction()
action.api = api
action.callback([])
check('action on empty selection is harmless', api.web_service.calls == [])

print()
print('=== 18. enable() never aborts on a settings failure ===')


class BrokenConfig:
    section_name = 'plugin.x'

    def __getitem__(self, key):
        raise RuntimeError('boom')


class BrokenApi(FakeApi):
    def __init__(self):
        super().__init__(BrokenConfig())
        self.registered = []

    def register_file_post_addition_to_track_processor(self, fn, priority=0):
        self.registered.append('track')

    def register_file_post_save_processor(self, fn, priority=0):
        self.registered.append('save')

    def register_track_action(self, action):
        self.registered.append('track_action')

    def register_album_action(self, action):
        self.registered.append('album_action')

    def register_options_page(self, page):
        self.registered.append('options')


class ExplodingSectionApi(BrokenApi):
    def __init__(self):
        # Deliberately bypass FakeApi.__init__ so plugin_config can be a
        # read-only property that raises.
        self.logger = FakeLogger()
        self.web_service = FakeWebService({})
        self.tasks = []
        self.completed = []
        self.registered = []

    @property
    def plugin_config(self):
        raise RuntimeError('no plugin_config')


api = ExplodingSectionApi()
plugin.enable(api)
check('enable() completes when plugin_config raises',
      api.registered == ['track', 'save', 'track_action', 'album_action', 'options'],
      str(api.registered))
check('failure logged', api.logger.has('cannot determine the config section'))

api = BrokenApi()
plugin.enable(api)
check('enable() completes when settings read raises',
      len(api.registered) == 5, str(api.registered))
check('_setting still returns the default', plugin._setting(api, 'write_tags') is True)

print()
print('=== 19. retry on transient network errors ===')
PROTOCOL_ERROR = 'ProtocolFailure'

# The exact case from the user's log: the NetEase search fails with a
# protocol error, then succeeds on the retry.
responses = {
    plugin.NETEASE_SEARCH_URL: [
        (None, PROTOCOL_ERROR),          # 1st attempt fails
        (NE_SEARCH_DOC, None),           # retry succeeds
    ],
    plugin.NETEASE_LYRIC_URL: (NE_LYRIC_DOC, None),
}
cfg = FakeConfig(**DEFAULTS)
api = FakeApi(cfg, responses)
meta = {'title': '嵐の中でも', 'artist': '藍井エイル', '~length': '4:41'}
plugin._on_file_added(api, FakeTrack(FakeAlbum()), FakeFile(os.path.join(tmp, 'rt1.mp3'), meta))
urls = [c['url'] for c in api.web_service.calls]
check('search retried after the protocol error',
      urls == [plugin.NETEASE_SEARCH_URL, plugin.NETEASE_SEARCH_URL,
               plugin.NETEASE_LYRIC_URL], str(len(urls)))
check('lyrics written after the retry succeeded', bool(meta.get('syncedlyrics')))
check('retry attempt logged', api.logger.has('retrying'))
check('no failure warning when the retry works',
      not api.logger.has('after 3 attempts'))

# A failure in the lyric fetch is retried at the lyric stage, not from search.
responses = {
    plugin.NETEASE_SEARCH_URL: (NE_SEARCH_DOC, None),
    plugin.NETEASE_LYRIC_URL: [
        (None, PROTOCOL_ERROR),
        (NE_LYRIC_DOC, None),
    ],
}
api = FakeApi(FakeConfig(**DEFAULTS), responses)
meta = {'title': '嵐の中でも', 'artist': '藍井エイル', '~length': '4:41'}
plugin._on_file_added(api, FakeTrack(FakeAlbum()), FakeFile(os.path.join(tmp, 'rt2.mp3'), meta))
urls = [c['url'] for c in api.web_service.calls]
check('lyric fetch retried without redoing the search',
      urls == [plugin.NETEASE_SEARCH_URL, plugin.NETEASE_LYRIC_URL,
               plugin.NETEASE_LYRIC_URL], str(len(urls)))
check('lyrics written after the lyric retry', bool(meta.get('syncedlyrics')))

# Persistent failure: give up after MAX_REQUEST_RETRIES and fall through.
responses = {
    plugin.NETEASE_SEARCH_URL: (None, PROTOCOL_ERROR),
    plugin.LRCLIB_SEARCH_URL: (LRCLIB_HITS, None),
}
api = FakeApi(FakeConfig(**DEFAULTS), responses)
meta = {'title': 'Lemon', 'artist': '米津玄師', '~length': '4:40'}
plugin._on_file_added(api, FakeTrack(FakeAlbum()), FakeFile(os.path.join(tmp, 'rt3.mp3'), meta))
attempts = len([c for c in api.web_service.calls if c['url'] == plugin.NETEASE_SEARCH_URL])
check('gives up after the retry budget',
      attempts == plugin.MAX_REQUEST_RETRIES + 1, 'attempts=%d' % attempts)
check('falls through to LRCLIB afterwards',
      meta.get('syncedlyrics') == LRCLIB_HITS[1]['syncedLyrics'])
check('persistent failure logged as a warning',
      api.logger.has('after 3 attempts, moving on'))
check('task completed once despite the retries', len(api.completed) == 1)

# LRCLIB failures are retried too.
responses = {
    plugin.NETEASE_SEARCH_URL: ({'result': {'songs': []}}, None),
    plugin.LRCLIB_SEARCH_URL: [(None, PROTOCOL_ERROR), (LRCLIB_HITS, None)],
}
api = FakeApi(FakeConfig(**DEFAULTS), responses)
meta = {'title': 'Lemon', 'artist': '米津玄師', '~length': '4:40'}
plugin._on_file_added(api, FakeTrack(FakeAlbum()), FakeFile(os.path.join(tmp, 'rt4.mp3'), meta))
attempts = len([c for c in api.web_service.calls if c['url'] == plugin.LRCLIB_SEARCH_URL])
check('LRCLIB search retried too', attempts == 2, 'attempts=%d' % attempts)
check('lyrics written after the LRCLIB retry', bool(meta.get('syncedlyrics')))

# An empty result list is NOT an error: it must not be retried. One request per
# source (NetEase, Kugou, LRCLIB), no retries.
responses = {
    plugin.NETEASE_SEARCH_URL: ({'result': {'songs': []}}, None),
    plugin.KUGOU_SEARCH_URL: ({'data': {'info': []}}, None),
    plugin.LRCLIB_SEARCH_URL: ([], None),
}
api = FakeApi(FakeConfig(**DEFAULTS), responses)
meta = {'title': 'x', 'artist': 'y'}
plugin._on_file_added(api, FakeTrack(FakeAlbum()), FakeFile(os.path.join(tmp, 'rt5.mp3'), meta))
attempts = len(api.web_service.calls)
check('empty results are not retried', attempts == 3, 'requests=%d' % attempts)
check('nothing written when all sources are empty', not meta.get('syncedlyrics'))

# A transport failure on ONE candidate must not abandon the other releases:
# the retry budget is per candidate and exhaustion falls to the next one.
print()
print('=== 19b. per-candidate fallback on NetEase lyric errors ===')
TWO_LEMONS = {'result': {'songs': [
    ne_song(555, 'Lemon', ['米津玄師'], 'BOOTLEG', 256000),
    ne_song(666, 'Lemon', ['米津玄師'], 'Lemon', 280000),
]}}
GOOD_LRC = {'lrc': {'lyric': '[00:12.34] 檸檬\n[00:16.20] 夏の終わり'}}
responses = {
    plugin.NETEASE_SEARCH_URL: (TWO_LEMONS, None),
    plugin.NETEASE_LYRIC_URL: [
        (None, PROTOCOL_ERROR),    # candidate 1 (666), attempt 1
        (None, PROTOCOL_ERROR),    # candidate 1, attempt 2
        (None, PROTOCOL_ERROR),    # candidate 1, attempt 3 -> budget spent
        (None, PROTOCOL_ERROR),    # candidate 2 (555), attempt 1
        (GOOD_LRC, None),          # candidate 2, retry succeeds
    ],
}
api = FakeApi(FakeConfig(**DEFAULTS), responses)
meta = {'title': 'Lemon', 'artist': '米津玄師', '~length': '4:40'}
plugin._on_file_added(api, FakeTrack(FakeAlbum()),
                      FakeFile(os.path.join(tmp, 'pc1.mp3'), meta))
ne_calls = [(url, params) for url, params in api._lyrics_fetcher_netease_client.calls
            if url == plugin.NETEASE_LYRIC_URL]
check('candidate 1 is retried three times, then candidate 2 is tried',
      [params.get('id') for _, params in ne_calls] == [666, 666, 666, 555, 555],
      str([params.get('id') for _, params in ne_calls]))
check('candidate 2 gets its own retry budget (error then success)',
      len(ne_calls) == 5, 'lyric calls=%d' % len(ne_calls))
check('lyrics from the second release are written',
      '檸檬' in (meta.get('syncedlyrics') or ''),
      repr((meta.get('syncedlyrics') or '')[:40]))
check('per-candidate exhaustion is logged', api.logger.has('after 3 attempts'))
check('fallthrough to the next match is logged',
      api.logger.has('failed, trying the next match'))
check('never fell through to LRCLIB',
      not any(c['url'] == plugin.LRCLIB_SEARCH_URL for c in api.web_service.calls))
check('task completed once', len(api.completed) == 1, str(api.completed))

# When EVERY candidate errors out, the chain still reaches the next source.
responses = {
    plugin.NETEASE_SEARCH_URL: (TWO_LEMONS, None),
    plugin.NETEASE_LYRIC_URL: (None, PROTOCOL_ERROR),
    plugin.KUGOU_SEARCH_URL: ({'data': {'info': []}}, None),
    plugin.LRCLIB_SEARCH_URL: (LRCLIB_HITS, None),
}
api = FakeApi(FakeConfig(**DEFAULTS), responses)
meta = {'title': 'Lemon', 'artist': '米津玄師', '~length': '4:40'}
plugin._on_file_added(api, FakeTrack(FakeAlbum()),
                      FakeFile(os.path.join(tmp, 'pc2.mp3'), meta))
lyric_calls = len([1 for url, _ in api._lyrics_fetcher_netease_client.calls
                   if url == plugin.NETEASE_LYRIC_URL])
check('each candidate gets the full retry budget before moving on',
      lyric_calls == 2 * (plugin.MAX_REQUEST_RETRIES + 1),
      'lyric calls=%d' % lyric_calls)
check('falls through to LRCLIB after both candidates fail',
      meta.get('syncedlyrics') == LRCLIB_HITS[1]['syncedLyrics'])
check('each exhausted candidate logged a warning',
      sum(1 for line in api.logger.lines if 'after 3 attempts' in line) == 2,
      str([l for l in api.logger.lines if 'after 3 attempts' in l]))
check('task completed once', len(api.completed) == 1, str(api.completed))

print()
print('=== 20. the request chain never fires twice ===')


class ExplodingWebService(FakeWebService):
    def get_url(self, **kwargs):
        self.calls.append(kwargs)
        raise RuntimeError('simulated handler failure')


class ExplodingApi(FakeApi):
    def __init__(self, config):
        super().__init__(config, {})
        self.web_service = ExplodingWebService({})


api = ExplodingApi(FakeConfig(**DEFAULTS))
meta = {'title': 'x', 'artist': 'y'}
result = plugin._begin_lookup(api, FakeTrack(FakeAlbum()), FakeFile('boom.mp3', meta))
check('request attempted exactly once despite the exception',
      len(api.web_service.calls) == 1, 'calls=%d' % len(api.web_service.calls))
check('_begin_lookup still reports success', result is True)

# The lyric stage must be retryable by name (regression guard: the retry path
# referenced a function that did not exist).
check('_request_netease_lyric exists', callable(getattr(plugin, '_request_netease_lyric', None)))
check('_request_netease exists', callable(getattr(plugin, '_request_netease', None)))
check('_request_lrclib exists', callable(getattr(plugin, '_request_lrclib', None)))

print()
print('=== 21. format-aware syncedlyrics handling ===')
check('_supports_synced: mp3 -> True', plugin._supports_synced(FakeFile('a.mp3', {})) is True)
check('_supports_synced: flac -> False', plugin._supports_synced(FakeFile('a.flac', {})) is False)
check('_supports_synced: ogg -> False', plugin._supports_synced(FakeFile('a.ogg', {})) is False)
check('_supports_synced: opus -> False', plugin._supports_synced(FakeFile('a.opus', {})) is False)
check('_supports_synced: m4a -> False', plugin._supports_synced(FakeFile('a.m4a', {})) is False)
check('_supports_synced: aiff -> True', plugin._supports_synced(FakeFile('a.aiff', {})) is True)


class BareFile:
    """No supports_tag() at all: the extension fallback must kick in."""

    def __init__(self, filename):
        self.filename = filename


check('fallback: mp3 -> True', plugin._supports_synced(BareFile('x.mp3')) is True)
check('fallback: flac -> False', plugin._supports_synced(BareFile('x.flac')) is False)
check('fallback: no filename -> False', plugin._supports_synced(BareFile('')) is False)

# FLAC (the user's case): syncedlyrics must NOT be written, plain lyrics must
# be, and the synced text must be cached so the .lrc export still works.
cfg = FakeConfig(**DEFAULTS)
api = FakeApi(cfg, REPORTED)
meta = {'title': '嵐の中でも', 'artist': '藍井エイル', '~length': '4:41'}
plugin._on_file_added(api, FakeTrack(FakeAlbum()),
                      FakeFile(os.path.join(tmp, 'f1.flac'), meta))
check('FLAC: syncedlyrics tag NOT written', 'syncedlyrics' not in meta)
check('FLAC: lyrics tag holds the TIMED text (default)', '[00:23.846]' in (meta.get('lyrics') or ''),
      repr((meta.get('lyrics') or '')[:44]))
check('FLAC: synced text cached for the .lrc export',
      bool(meta.get(plugin.SYNCED_CACHE_TAG)))
check('FLAC: no warning logged for the normal case',
      not any(line.startswith('WARN') for line in api.logger.lines),
      str(api.logger.lines))

# MP3: both tags written and no hint.
cfg = FakeConfig(**DEFAULTS)
api = FakeApi(cfg, REPORTED)
meta = {'title': '嵐の中でも', 'artist': '藍井エイル', '~length': '4:41'}
plugin._on_file_added(api, FakeTrack(FakeAlbum()),
                      FakeFile(os.path.join(tmp, 'f2.mp3'), meta))
check('MP3: syncedlyrics written', bool(meta.get('syncedlyrics')))
check('MP3: lyrics stays PLAIN (it has a real synced field)',
      meta.get('lyrics') and '[00:23' not in meta['lyrics'],
      repr((meta.get('lyrics') or '')[:40]))
check('MP3: no format hint logged',
      not api.logger.has('no field for synced lyrics'))

# FLAC + lrc_mode=always: the sidecar must carry the timestamps from the cache.
outdir2 = tempfile.mkdtemp()
audio = os.path.join(outdir2, 'flac_track.flac')
cfg = FakeConfig(**dict(DEFAULTS, lrc_mode='always'))
api = FakeApi(cfg, REPORTED)
meta = {'title': '嵐の中でも', 'artist': '藍井エイル', '~length': '4:41'}
fobj = FakeFile(audio, meta)
plugin._on_file_added(api, FakeTrack(None), fobj)
plugin._on_file_saved(api, fobj)
lrc = os.path.join(outdir2, 'flac_track.lrc')
check('FLAC + lrc_mode=always: .lrc created', os.path.exists(lrc), lrc)
if os.path.exists(lrc):
    content = open(lrc, encoding='utf-8').read()
    check('.lrc keeps the timestamps', '[00:23.846]' in content, repr(content[:44]))

# write_tags off means no lookup is started at all.
cfg = FakeConfig(**dict(DEFAULTS, write_tags=False))
api = FakeApi(cfg, REPORTED)
meta = {'title': '嵐の中でも', 'artist': '藍井エイル', '~length': '4:41'}
plugin._on_file_added(api, FakeTrack(FakeAlbum()),
                      FakeFile(os.path.join(tmp, 'f3.flac'), meta))
check('write_tags off: nothing written', not meta.get('lyrics'))
check('write_tags off: no request made', api.web_service.calls == [])

print()
print('=== 22. .lrc written straight after the lookup (no save required) ===')


def lookup_only(filename, extra=None):
    """Run just the lookup and return (api, metadata, fileobj, lrc_path)."""
    directory = tempfile.mkdtemp()
    audio = os.path.join(directory, filename)
    cfg = FakeConfig(**dict(DEFAULTS, **(extra or {})))
    api = FakeApi(cfg, REPORTED)
    meta = {'title': '嵐の中でも', 'artist': '藍井エイル', '~length': '4:41'}
    fobj = FakeFile(audio, meta)
    plugin._on_file_added(api, FakeTrack(None), fobj)
    return api, meta, fobj, os.path.splitext(audio)[0] + '.lrc'


# Default lrc_mode is "never": the tag carries the timestamps, no sidecar.
api, meta, fobj, lrc = lookup_only('default.flac')
check('FLAC default: no .lrc written', not os.path.exists(lrc), lrc)
check('FLAC default: timed lyrics in the lyrics tag',
      '[00:23.846]' in (meta.get('lyrics') or ''))
check('FLAC default: File.update() called so Picard shows it as changed',
      fobj.updated == 1, 'updated=%d' % fobj.updated)

# lrc_mode="unsupported" adds a sidecar for FLAC only.
api, meta, fobj, lrc = lookup_only('auto.flac', {'lrc_mode': 'unsupported'})
check('FLAC + lrc_mode=unsupported: .lrc written by the lookup alone',
      os.path.exists(lrc), lrc)
if os.path.exists(lrc):
    check('FLAC: .lrc keeps the timestamps',
          '[00:23.846]' in open(lrc, encoding='utf-8').read())
check('FLAC: .lrc write logged', api.logger.has(lrc), str(api.logger.lines[-2:]))
check('FLAC: timed lyrics also in the tag', '[00:23.846]' in (meta.get('lyrics') or ''))

# MP3 is untouched by "unsupported": it can store the timestamps itself.
api, meta, fobj, lrc = lookup_only('auto.mp3', {'lrc_mode': 'unsupported'})
check('MP3 + lrc_mode=unsupported: no .lrc', not os.path.exists(lrc), lrc)
check('MP3: syncedlyrics tag written instead', bool(meta.get('syncedlyrics')))
check('MP3: File.update() still called', fobj.updated == 1)

# lrc_mode="always" forces a sidecar for MP3 as well.
api, meta, fobj, lrc = lookup_only('forced.mp3', {'lrc_mode': 'always'})
check('MP3 + lrc_mode=always: .lrc written', os.path.exists(lrc), lrc)

# An existing .lrc is never clobbered.
directory = tempfile.mkdtemp()
audio = os.path.join(directory, 'keep.flac')
lrc = os.path.join(directory, 'keep.lrc')
with open(lrc, 'w', encoding='utf-8') as handle:
    handle.write('PRE-EXISTING')
cfg = FakeConfig(**dict(DEFAULTS, lrc_mode='unsupported'))
api = FakeApi(cfg, REPORTED)
meta = {'title': '嵐の中でも', 'artist': '藍井エイル', '~length': '4:41'}
fobj = FakeFile(audio, meta)
plugin._on_file_added(api, FakeTrack(None), fobj)
check('an existing .lrc is never overwritten',
      open(lrc, encoding='utf-8').read() == 'PRE-EXISTING')

# The post-save hook still exists for files Picard renamed during the save.
directory = tempfile.mkdtemp()
audio = os.path.join(directory, 'after_save.flac')
cfg = FakeConfig(**dict(DEFAULTS, lrc_mode='unsupported'))
api = FakeApi(cfg, REPORTED)
meta = {'title': '嵐の中でも', 'artist': '藍井エイル', '~length': '4:41'}
fobj = FakeFile(audio, meta)
plugin._on_file_added(api, FakeTrack(None), fobj)
os.remove(os.path.join(directory, 'after_save.lrc'))
plugin._on_file_saved(api, fobj)
check('post-save hook re-creates a missing .lrc',
      os.path.exists(os.path.join(directory, 'after_save.lrc')))

print()
print('=== 23. NetEase is fetched without a response parser ===')
check('_as_json: bytes -> dict',
      plugin._as_json(b'{"a": 1}') == {'a': 1})
check('_as_json: dict passthrough', plugin._as_json({'a': 1}) == {'a': 1})
check('_as_json: bad bytes -> None', plugin._as_json(b'not json') is None)
check('_as_json: None -> None', plugin._as_json(None) is None)

# Picard sets an Accept: application/json header whenever a response parser is
# requested, and music.163.com answers those with Content-Type: text/plain, so
# we must not ask for one.
api = FakeApi(FakeConfig(**DEFAULTS), REPORTED)
meta = {'title': '嵐の中でも', 'artist': '藍井エイル', '~length': '4:41'}
plugin._on_file_added(api, FakeTrack(FakeAlbum()),
                      FakeFile(os.path.join(tmp, 'p1.flac'), meta))
netease_calls = [c for c in api.web_service.calls if 'music.163.com' in c['url']]
check('two NetEase calls were made', len(netease_calls) == 2, str(len(netease_calls)))
check('no response parser requested (no Accept header)',
      all(c.get('parse_response_type') is None for c in netease_calls),
      str([c.get('parse_response_type') for c in netease_calls]))
check('LRCLIB still uses the json parser',
      all(c.get('parse_response_type') == 'json'
          for c in api.web_service.calls if 'lrclib' in c['url']))
check('chain still works end to end', bool(meta.get('lyrics')))

# The handlers must cope with the raw body they now receive.
raw_responses = {
    plugin.NETEASE_SEARCH_URL: (
        json.dumps(NE_SEARCH_DOC).encode('utf-8'), None),
    plugin.NETEASE_LYRIC_URL: (
        json.dumps(NE_LYRIC_DOC).encode('utf-8'), None),
}
api = FakeApi(FakeConfig(**DEFAULTS), raw_responses)
meta = {'title': '嵐の中でも', 'artist': '藍井エイル', '~length': '4:41'}
plugin._on_file_added(api, FakeTrack(FakeAlbum()),
                      FakeFile(os.path.join(tmp, 'p2.flac'), meta))
check('raw bytes body is decoded and used', bool(meta.get('lyrics')))

# Undecodable bytes must be treated as a miss, not crash.
bad = {plugin.NETEASE_SEARCH_URL: (b'<html>nope</html>', None),
       plugin.LRCLIB_SEARCH_URL: (LRCLIB_HITS, None)}
api = FakeApi(FakeConfig(**DEFAULTS), bad)
meta = {'title': 'Lemon', 'artist': '米津玄師', '~length': '4:40'}
plugin._on_file_added(api, FakeTrack(FakeAlbum()),
                      FakeFile(os.path.join(tmp, 'p3.mp3'), meta))
check('undecodable NetEase body falls through to LRCLIB',
      meta.get('syncedlyrics') == LRCLIB_HITS[1]['syncedLyrics'])

print()
print('=== 24. the NetEase client is per-session and cached ===')
check('NETEASE_HEADERS has Referer + Cookie',
      plugin.NETEASE_HEADERS.get('Referer') == 'https://music.163.com/'
      and 'Cookie' in plugin.NETEASE_HEADERS, str(plugin.NETEASE_HEADERS))
check('_NetEaseClient is a real class', isinstance(plugin._NetEaseClient, type))


class LazyApi(FakeApi):
    """No pre-made client: _netease_client() must build and cache one."""

    def __init__(self, config):
        super().__init__(config, REPORTED)
        del self._lyrics_fetcher_netease_client


api = LazyApi(FakeConfig(**DEFAULTS))
first = plugin._netease_client(api)
second = plugin._netease_client(api)
check('client is created lazily', first is not None)
check('client is cached on the api object', first is second)
check('client is an instance of _NetEaseClient',
      isinstance(first, plugin._NetEaseClient), type(first).__name__)

api = FakeApi(FakeConfig(**DEFAULTS), REPORTED)
pre = api._lyrics_fetcher_netease_client
check('a pre-supplied client is reused',
      plugin._netease_client(api) is pre)

print()
print('=== 25. LRCLIB: a stale search view is refetched by id ===')
# Observed against the live API: /api/search with track_name + artist_name can
# hand back a revision without syncedLyrics for a record whose synced lyrics do
# exist. The same id returns 1330 characters from /api/get/<id>, and from a
# search that also carried album_name or duration.
STALE = [{
    'id': 38948936, 'trackName': '共犯', 'artistName': 'Mrs. GREEN APPLE',
    'albumName': 'POPS', 'duration': 231.0,
    'syncedLyrics': None, 'plainLyrics': '拾い集めて\n更に探す東京\n' * 40,
}]
FRESH = {
    'id': 38948936, 'trackName': '共犯', 'artistName': 'Mrs. GREEN APPLE',
    'albumName': 'POPS', 'duration': 231.0,
    'syncedLyrics': '[00:24.85]拾い集めて\n[00:26.10]更に探す東京\n',
    'plainLyrics': '拾い集めて\n更に探す東京\n',
}
BY_ID = plugin.LRCLIB_GET_URL + '38948936'

responses = {plugin.LRCLIB_SEARCH_URL: (STALE, None), BY_ID: (FRESH, None)}
api = FakeApi(FakeConfig(**dict(DEFAULTS, source='lrclib')), responses)
meta = {'title': '共犯', 'artist': 'Mrs. GREEN APPLE', '~length': '3:51'}
plugin._on_file_added(api, FakeTrack(FakeAlbum()),
                      FakeFile(os.path.join(tmp, 'kyohan.flac'), meta))
urls = [c['url'] for c in api.web_service.calls]
check('the record is refetched by id', urls == [plugin.LRCLIB_SEARCH_URL, BY_ID], str(urls))
check('synced lyrics from the direct fetch are used',
      '[00:24.85]' in (meta.get('lyrics') or ''), repr((meta.get('lyrics') or '')[:40]))
check('the refetch is logged', api.logger.has('supplied them'))
check('task completed once', len(api.completed) == 1, str(api.completed))

# If the direct fetch is no better, the search copy is kept rather than lost.
STILL_PLAIN = dict(FRESH, syncedLyrics=None)
responses = {plugin.LRCLIB_SEARCH_URL: (STALE, None), BY_ID: (STILL_PLAIN, None)}
api = FakeApi(FakeConfig(**dict(DEFAULTS, source='lrclib')), responses)
meta = {'title': '共犯', 'artist': 'Mrs. GREEN APPLE', '~length': '3:51'}
plugin._on_file_added(api, FakeTrack(FakeAlbum()),
                      FakeFile(os.path.join(tmp, 'kyohan2.flac'), meta))
check('plain lyrics still written when the refetch has nothing better',
      bool(meta.get('lyrics')) and '[00:' not in meta['lyrics'])
check('task still completed once', len(api.completed) == 1)

# A failing refetch must not lose the lyrics we already had.
responses = {plugin.LRCLIB_SEARCH_URL: (STALE, None), BY_ID: (None, 'boom')}
api = FakeApi(FakeConfig(**dict(DEFAULTS, source='lrclib')), responses)
meta = {'title': '共犯', 'artist': 'Mrs. GREEN APPLE', '~length': '3:51'}
plugin._on_file_added(api, FakeTrack(FakeAlbum()),
                      FakeFile(os.path.join(tmp, 'kyohan3.flac'), meta))
check('a failed refetch keeps the search copy', bool(meta.get('lyrics')))

# When the search already carries synced lyrics, no extra request is made.
responses = {plugin.LRCLIB_SEARCH_URL: ([FRESH], None)}
api = FakeApi(FakeConfig(**dict(DEFAULTS, source='lrclib')), responses)
meta = {'title': '共犯', 'artist': 'Mrs. GREEN APPLE', '~length': '3:51'}
plugin._on_file_added(api, FakeTrack(FakeAlbum()),
                      FakeFile(os.path.join(tmp, 'kyohan4.flac'), meta))
check('no refetch when synced lyrics are already present',
      [c['url'] for c in api.web_service.calls] == [plugin.LRCLIB_SEARCH_URL],
      str([c['url'] for c in api.web_service.calls]))

print()
print('=== 26. Kugou source ===')
KUGOU_SEARCH_DOC = {'data': {'info': [
    {'songname': '共犯', 'singername': 'Mrs. GREEN APPLE', 'hash': 'good1',
     'duration': 231, 'album_name': 'POPS'},
    {'songname': '共犯 (Live)', 'singername': 'Mrs. GREEN APPLE', 'hash': 'live1',
     'duration': 240, 'album_name': 'Live'},
    {'songname': '共犯', 'singername': 'Someone Else', 'hash': 'other1',
     'duration': 231, 'album_name': 'X'},
    {'songname': '共犯', 'singername': 'Mrs. GREEN APPLE', 'hash': 'far1',
     'duration': 300, 'album_name': 'POPS'},
]}}
KUGOU_LRC_DOC = {'data': {'lrc': (
    '[00:00.00]共犯 - Mrs. GREEN APPLE\n'
    '[00:17.54]词：大森元貴\n'
    '[00:21.93]曲：大森元貴\n'
    '[00:26.31]制作人：大森元貴\n'
    '[00:32.16]こんな処まで\n'
    '[00:35.55]なにをしにきたんだっけ？\n')}}

songs = KUGOU_SEARCH_DOC['data']['info']
cands = plugin._pick_kugou(songs, '共犯', 'Mrs. GREEN APPLE', 231)
check('picks the exact title/artist match', cands and cands[0]['hash'] == 'good1', str(cands[:1]))
check('the closer duration is ranked first', cands[0]['duration'] == 231)
check('wrong artist rejected',
      plugin._pick_kugou([songs[2]], '共犯', 'Mrs. GREEN APPLE', 231) == [])
check('"(Live)" title variant rejected',
      plugin._pick_kugou([songs[1]], '共犯', 'Mrs. GREEN APPLE', 231) == [])
check('empty list -> []', plugin._pick_kugou([], 'x', 'y', 1) == [])
check('candidate list is capped',
      len(plugin._pick_kugou(
          [dict(songs[0], hash='h%d' % i) for i in range(10)],
          '共犯', 'Mrs. GREEN APPLE', 231)) == plugin.KUGOU_CANDIDATES)

# Full chain with source='kugou'.
responses = {
    plugin.KUGOU_SEARCH_URL: (KUGOU_SEARCH_DOC, None),
    plugin.KUGOU_LYRIC_URL: (KUGOU_LRC_DOC, None),
}
api = FakeApi(FakeConfig(**dict(DEFAULTS, source='kugou')), responses)
meta = {'title': '共犯', 'artist': 'Mrs. GREEN APPLE', '~length': '3:51'}
plugin._on_file_added(api, FakeTrack(FakeAlbum()),
                      FakeFile(os.path.join(tmp, 'kg.flac'), meta))
check('source=kugou never calls NetEase',
      all('music.163.com' not in c['url'] for c in api.web_service.calls))
check('kugou lyrics written', 'こんな処まで' in (meta.get('lyrics') or ''),
      repr((meta.get('lyrics') or '')[:50]))
check('the "[00:00.xx] Title - Artist" line is dropped',
      '共犯 - Mrs. GREEN APPLE' not in (meta.get('lyrics') or ''))
check('the 词/曲/制作人 credits are dropped',
      not any(k in (meta.get('lyrics') or '') for k in ('词：', '曲：', '制作人：')))
check('timelength is sent in milliseconds',
      any(c.get('unencoded_queryargs', {}).get('timelength') == 231000
          for c in api.web_service.calls),
      str([c.get('unencoded_queryargs') for c in api.web_service.calls]))
check('all three lyric parameters are sent',
      any(set(c.get('unencoded_queryargs', {})) == {'keyword', 'hash', 'timelength'}
          for c in api.web_service.calls))
check('Kugou answers are read without a response parser',
      all(c.get('parse_response_type') is None
          for c in api.web_service.calls if 'kugou' in c['url']))
check('the log names Kugou as the source', api.logger.has('Kugou "'))
check('task completed once', len(api.completed) == 1)

# An entry without lyrics hands over to the next candidate.
responses = {
    plugin.KUGOU_SEARCH_URL: (KUGOU_SEARCH_DOC, None),
    plugin.KUGOU_LYRIC_URL: [({'data': {'lrc': ''}}, None), (KUGOU_LRC_DOC, None)],
}
api = FakeApi(FakeConfig(**dict(DEFAULTS, source='kugou')), responses)
meta = {'title': '共犯', 'artist': 'Mrs. GREEN APPLE', '~length': '3:51'}
plugin._on_file_added(api, FakeTrack(FakeAlbum()),
                      FakeFile(os.path.join(tmp, 'kg2.flac'), meta))
check('an entry without lyrics falls through to the next match',
      'こんな処まで' in (meta.get('lyrics') or ''))
check('the fallthrough is logged', api.logger.has('trying the next match'))

# source='netease' must not touch Kugou.
api = FakeApi(FakeConfig(**dict(DEFAULTS, source='netease')), REPORTED)
meta = {'title': '嵐の中でも', 'artist': '藍井エイル', '~length': '4:41'}
plugin._on_file_added(api, FakeTrack(FakeAlbum()),
                      FakeFile(os.path.join(tmp, 'kg3.mp3'), meta))
check('source=netease never calls Kugou',
      all('kugou' not in c['url'] for c in api.web_service.calls))

print()
failed = [n for n, ok, _ in RESULTS if not ok]
print('=' * 76)
print('%d checks, %d passed, %d failed' % (len(RESULTS), len(RESULTS) - len(failed), len(failed)))
if failed:
    print('FAILED:')
    for name in failed:
        print('  -', name)
sys.exit(1 if failed else 0)
