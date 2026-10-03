# -*- coding: utf-8 -*-
"""Lyrics Fetcher — fetch synced and plain lyrics from LRCLIB and NetEase.

Writes the result into the ``syncedlyrics`` tag (LRC text, with timestamps)
and/or the ``lyrics`` tag (plain text) of your audio files, and can optionally
export a ``.lrc`` sidecar file next to the audio file.

Requires MusicBrainz Picard 3.0 or newer: the ``syncedlyrics`` tag only exists
from 3.0 on, and only for formats that can store it (ID3/MP3 and
Vorbis/FLAC/OGG/Opus). MP4/M4A cannot store synced lyrics.
"""

from __future__ import annotations

import os
import re
from functools import partial

from PyQt6.QtWidgets import QCheckBox, QComboBox, QLabel, QLineEdit, QVBoxLayout

from picard.config import BoolOption, TextOption
from picard.plugin3.api import OptionsPage, PluginApi

# --- LRCLIB -----------------------------------------------------------------
# We deliberately use the *search* endpoint rather than /api/get:
# /api/get is an exact-match lookup that answers 404 when it cannot find a
# record, which Picard logs as a network error. /api/search always answers
# 200 (possibly with an empty list), returns the full lyric text inline, and
# tolerates the small artist/album spelling differences that are common with
# non-Latin scripts. The trade-off is that we must pick the right record
# ourselves, which is what _pick_lrclib() does.
LRCLIB_SEARCH_URL = 'https://lrclib.net/api/search'

# --- NetEase Cloud Music ----------------------------------------------------
# These are the public web endpoints the music.163.com player itself calls.
# They are not an officially documented API, so treat them as best-effort:
# they may change or start requiring extra headers without notice.
# Note: /api/search/get/web now returns an encrypted blob, /api/search/get
# still returns plain JSON, so we use the latter.
NETEASE_SEARCH_URL = 'https://music.163.com/api/search/get'
NETEASE_LYRIC_URL = 'https://music.163.com/api/song/lyric'
NETEASE_HEADERS = {
    'Referer': 'https://music.163.com/',
    'Cookie': 'appver=2.0.2; os=pc',
}
NETEASE_SEARCH_LIMIT = 10

DEFAULT_LRC_PATTERN = '%filename%.lrc'
SOURCE_AUTO = 'auto'
SOURCE_NETEASE = 'netease'
SOURCE_LRCLIB = 'lrclib'

# (setting name, default value)
BOOL_SETTINGS = (
    ('enabled', True),
    ('write_synced', True),
    ('write_plain', True),
    ('never_replace', True),
    ('export_lrc', False),
    ('lrc_never_replace', True),
    ('netease_strip_credits', True),
    ('netease_add_translation', False),
)

TEXT_SETTINGS = (
    ('lrc_filename', DEFAULT_LRC_PATTERN),
    ('source', SOURCE_AUTO),
)

# Fallbacks used when a setting is somehow not registered, so that a failure to
# declare the options degrades to the documented defaults instead of silently
# switching the plugin off.
DEFAULTS = dict(BOOL_SETTINGS)
DEFAULTS.update(TEXT_SETTINGS)

# [mm:ss.xx] / [mm:ss.xxx] / [mm:ss:xx] (NetEase uses the last form on occasion)
_LRC_TIME = re.compile(r'\[(\d+):(\d+(?:[.:]\d+)?)\]')
# [ti:...] [ar:...] [al:...] [by:...] [offset:...] etc.
_LRC_META = re.compile(r'^\[[a-zA-Z]+:.*\]$')
# Credit lines such as "作词 : GAK-amazuti-" that NetEase prepends to lyrics.
_CREDIT = re.compile(
    r'^(作词|作曲|编曲|制作人|出品|监制|混音|母带|录音|录音师|录音室|和声|合声|'
    r'吉他|贝斯|鼓|键盘|弦乐|钢琴|策划|统筹|发行|词|曲|编)\s*[:：]'
)
# NetEase covers are titled like "晴天（深情版）" or "Lemon (翻自 米津玄師)".
_NETEASE_NOISE = re.compile(r'(翻自|翻唱|cover|纯音乐|伴奏|remix|钢琴版|吉他版)', re.IGNORECASE)


# ---------------------------------------------------------------------------
# settings
# ---------------------------------------------------------------------------

def _register_settings(api: PluginApi) -> None:
    """Declare the plugin's options so Picard knows their types and defaults."""
    section = api.plugin_config.section_name
    for name, default in BOOL_SETTINGS:
        BoolOption.add_if_missing(section, name, default)
    for name, default in TEXT_SETTINGS:
        TextOption.add_if_missing(section, name, default)


def _setting(api: PluginApi, name: str):
    """Read a plugin setting, falling back to its default when unset.

    Picard returns ``None`` for options that were never registered, so reading
    through this helper keeps the plugin usable even if registration failed.
    """
    try:
        value = api.plugin_config[name]
    except Exception:  # pragma: no cover - defensive
        return DEFAULTS[name]
    return DEFAULTS[name] if value is None else value


def _source_order(api: PluginApi):
    """Return the sources to try, in order."""
    source = (_setting(api, 'source') or SOURCE_AUTO).lower()
    if source == SOURCE_NETEASE:
        return (SOURCE_NETEASE,)
    if source == SOURCE_LRCLIB:
        return (SOURCE_LRCLIB,)
    return (SOURCE_NETEASE, SOURCE_LRCLIB)


# ---------------------------------------------------------------------------
# text helpers
# ---------------------------------------------------------------------------

def _normalize(text) -> str:
    """Casefold and collapse whitespace so that names compare reliably."""
    return ' '.join(str(text or '').casefold().split())


def _artist_matches(want: str, got: str) -> bool:
    """True when two artist strings are the same, or one contains the other.

    Handles the usual "A" vs "A & B" / "A/B" / "A feat. B" differences.
    """
    if not want or not got:
        return False
    return want == got or want in got or got in want


def _duration_seconds(metadata) -> int | None:
    """Return the track length in seconds, or None if unavailable.

    Picard stores the length as a ``mm:ss`` string in the ``~length``
    variable. Both sources hold several versions of the same song (album
    version, single edit, live take), so the duration is what lets us pick the
    right one.
    """
    raw = metadata.get('~length')
    if not raw:
        return None
    try:
        seconds = 0.0
        for part in str(raw).split(':'):
            seconds = seconds * 60 + float(part)
        return int(round(seconds))
    except (TypeError, ValueError):
        return None


def _line_text(line: str) -> str:
    """Strip every timestamp from an LRC line and return the remaining text."""
    return _LRC_TIME.sub('', line).strip()


def _lrc_to_plain(lrc: str) -> str:
    """Turn LRC into plain text by dropping the timestamps."""
    lines = [_line_text(line) for line in lrc.splitlines()]
    return '\n'.join(line for line in lines if line)


def _strip_credits(lrc: str) -> str:
    """Drop the credit block NetEase prepends to the lyrics.

    NetEase lyrics normally open with lines such as
    ``[00:00.000] 作词 : GAK-amazuti-`` before the first real lyric line. Only
    the *leading* block is removed, and only when every line in it looks like a
    credit or an LRC metadata tag, so a song whose first line happens to start
    with "作曲" is not damaged.
    """
    lines = lrc.splitlines()
    index = 0
    while index < len(lines):
        stripped = lines[index].strip()
        if not stripped:
            index += 1
            continue
        text = _line_text(stripped)
        if _LRC_META.match(stripped) or _CREDIT.match(text):
            index += 1
            continue
        break
    if index >= len(lines):
        return lrc  # everything looked like a credit; keep it
    return '\n'.join(lines[index:])


def _merge_translation(lrc: str, translation: str, separator: str = ' / ') -> str:
    """Append each translated line to its original, matching on timestamp."""
    translated = {}
    for line in translation.splitlines():
        match = _LRC_TIME.match(line.strip())
        if not match:
            continue
        text = line[match.end():].strip()
        if text:
            translated.setdefault(match.group(0), text)
    if not translated:
        return lrc

    out = []
    for line in lrc.splitlines():
        match = _LRC_TIME.match(line.strip())
        if match:
            text = line[match.end():].strip()
            extra = translated.get(match.group(0))
            if text and extra and extra != text:
                # Append to the original line so the timestamp and any spacing
                # between it and the text are preserved verbatim.
                line = line.rstrip() + separator + extra
        out.append(line)
    return '\n'.join(out)


def _lrc_path(api: PluginApi, file) -> str:
    """Build the .lrc output path from the configured name pattern."""
    pattern = _setting(api, 'lrc_filename') or DEFAULT_LRC_PATTERN
    base, _extension = os.path.splitext(file.filename)
    directory, name = os.path.split(base)
    path = pattern.replace('%folderpath%', directory).replace('%filename%', name)
    if not os.path.isabs(path):
        path = os.path.join(directory, path)
    return path


# ---------------------------------------------------------------------------
# record selection
# ---------------------------------------------------------------------------

def _pick_lrclib(results, title, artist, duration):
    """Choose the best matching record from a LRCLIB search result list.

    A record is only accepted when the track title matches exactly (after
    normalisation) and the artist name matches. Among the survivors we prefer
    the one whose duration is closest to the file's, and then the one that
    carries synced lyrics.
    """
    want_title = _normalize(title)
    want_artist = _normalize(artist)

    candidates = []
    for item in results:
        if not isinstance(item, dict):
            continue
        if _normalize(item.get('trackName')) != want_title:
            continue
        if not _artist_matches(want_artist, _normalize(item.get('artistName'))):
            continue
        if not (item.get('syncedLyrics') or item.get('plainLyrics')):
            continue
        candidates.append(item)

    if not candidates:
        return None

    def rank(item):
        if duration:
            try:
                delta = abs(float(item.get('duration') or 0) - duration)
            except (TypeError, ValueError):
                delta = float('inf')
        else:
            delta = 0.0
        return (delta, 0 if item.get('syncedLyrics') else 1)

    candidates.sort(key=rank)
    return candidates[0]


def _pick_netease(songs, title, artist, duration):
    """Choose the best matching song from a NetEase search result list.

    NetEase durations are in milliseconds. Covers and edits are titled
    differently ("晴天（深情版）"), so the exact title match already filters out
    most of the noise; an extra penalty demotes anything that still smells like
    a re-recording.
    """
    want_title = _normalize(title)
    want_artist = _normalize(artist)

    candidates = []
    for song in songs:
        if not isinstance(song, dict):
            continue
        if _normalize(song.get('name')) != want_title:
            continue
        artists = song.get('artists') or []
        got_artist = _normalize(
            '/'.join(a.get('name', '') for a in artists if isinstance(a, dict))
        )
        if not _artist_matches(want_artist, got_artist):
            continue
        if not song.get('id'):
            continue
        candidates.append(song)

    if not candidates:
        return None

    def rank(song):
        noisy = 1 if _NETEASE_NOISE.search(song.get('name') or '') else 0
        if duration:
            try:
                delta = abs(float(song.get('duration') or 0) / 1000.0 - duration)
            except (TypeError, ValueError):
                delta = float('inf')
        else:
            delta = 0.0
        return (noisy, delta)

    candidates.sort(key=rank)
    return candidates[0]


# ---------------------------------------------------------------------------
# lookup state
# ---------------------------------------------------------------------------

class _Lookup:
    """Carries the state of one lyrics lookup across its request chain."""

    __slots__ = ('api', 'file', 'album', 'task_id', 'finished')

    def __init__(self, api, file, album, task_id):
        self.api = api
        self.file = file
        self.album = album
        self.task_id = task_id
        self.finished = False

    def finish(self):
        """Mark the album task done, exactly once."""
        if self.finished:
            return
        self.finished = True
        if self.album is not None and self.task_id is not None:
            try:
                self.api.complete_album_task(self.album, self.task_id)
            except Exception:  # pragma: no cover - defensive
                pass


def _write_lyrics(ctx: _Lookup, description: str, synced: str, plain: str) -> None:
    """Write the lyrics into the file metadata and finish the lookup."""
    api = ctx.api
    metadata = ctx.file.metadata

    synced = (synced or '').strip()
    plain = (plain or '').strip()
    if not plain and synced:
        plain = _lrc_to_plain(synced)

    replace = not _setting(api, 'never_replace')
    written = []
    if _setting(api, 'write_synced') and synced and (
        replace or not metadata.get('syncedlyrics')
    ):
        metadata['syncedlyrics'] = synced
        written.append('syncedlyrics')
    if _setting(api, 'write_plain') and plain and (
        replace or not metadata.get('lyrics')
    ):
        metadata['lyrics'] = plain
        written.append('lyrics')

    if written:
        api.logger.info(
            'Lyrics: %s — %s, wrote %s', ctx.file.filename, description, ', '.join(written)
        )
    else:
        api.logger.debug('Lyrics: nothing to write for %s', ctx.file.filename)
    ctx.finish()


# ---------------------------------------------------------------------------
# request chain
# ---------------------------------------------------------------------------

def _dispatch(ctx: _Lookup, sources, index):
    """Fire the request for sources[index], or give up when none is left."""
    if index >= len(sources):
        ctx.api.logger.debug('Lyrics: no match for %s in any source', ctx.file.filename)
        ctx.finish()
        return None
    if sources[index] == SOURCE_NETEASE:
        return _request_netease(ctx, sources, index)
    return _request_lrclib(ctx, sources, index)


def _on_file_added(api: PluginApi, track, file) -> None:
    """Called when a file is added to a track: look the lyrics up."""
    if not _setting(api, 'enabled'):
        return
    if not (_setting(api, 'write_synced') or _setting(api, 'write_plain')):
        return

    metadata = file.metadata
    if not metadata.get('title') or not metadata.get('artist'):
        api.logger.debug(
            'Lyrics: skipping %s, both title and artist are required', file.filename
        )
        return

    if _setting(api, 'never_replace') and (
        metadata.get('syncedlyrics') or metadata.get('lyrics')
    ):
        api.logger.debug('Lyrics: skipping %s, lyrics are already present', file.filename)
        return

    album = getattr(track, 'album', None)
    task_id = None
    if album is not None:
        task_id = 'lyrics_%d' % (abs(hash(file.filename)) & 0x7FFFFFFF)

    ctx = _Lookup(api, file, album, task_id)
    sources = _source_order(api)
    first = partial(_dispatch, ctx, sources, 0)

    if album is not None and task_id is not None:
        try:
            api.add_album_task(album, task_id, 'Fetching lyrics', request_factory=first)
            return
        except Exception as exc:  # pragma: no cover - defensive
            api.logger.debug(
                'Lyrics: album task unavailable (%s), falling back to a direct request', exc
            )
    first()


# --- NetEase ---------------------------------------------------------------

def _request_netease(ctx: _Lookup, sources, index):
    metadata = ctx.file.metadata
    return ctx.api.web_service.get_url(
        url=NETEASE_SEARCH_URL,
        handler=partial(_on_netease_search, ctx, sources, index),
        parse_response_type='json',
        unencoded_queryargs={
            's': '%s %s' % (metadata.get('title'), metadata.get('artist')),
            'type': 1,
            'offset': 0,
            'limit': NETEASE_SEARCH_LIMIT,
        },
        headers=dict(NETEASE_HEADERS),
        priority=True,
    )


def _on_netease_search(ctx: _Lookup, sources, index, document, reply, error):
    if error or not isinstance(document, dict):
        ctx.api.logger.debug('Lyrics: NetEase search failed for %s (%s)', ctx.file.filename, error)
        return _dispatch(ctx, sources, index + 1)

    result = document.get('result')
    songs = result.get('songs') if isinstance(result, dict) else None
    if not songs:
        ctx.api.logger.debug('Lyrics: NetEase returned no results for %s', ctx.file.filename)
        return _dispatch(ctx, sources, index + 1)

    metadata = ctx.file.metadata
    best = _pick_netease(
        songs, metadata.get('title'), metadata.get('artist'), _duration_seconds(metadata)
    )
    if best is None:
        ctx.api.logger.debug(
            'Lyrics: NetEase returned %d results for %s but none matched title/artist',
            len(songs), ctx.file.filename,
        )
        return _dispatch(ctx, sources, index + 1)

    return ctx.api.web_service.get_url(
        url=NETEASE_LYRIC_URL,
        handler=partial(_on_netease_lyric, ctx, best, sources, index),
        parse_response_type='json',
        unencoded_queryargs={'id': best.get('id'), 'lv': -1, 'kv': -1, 'tv': -1},
        headers=dict(NETEASE_HEADERS),
        priority=True,
    )


def _on_netease_lyric(ctx: _Lookup, song, sources, index, document, reply, error):
    if error or not isinstance(document, dict):
        ctx.api.logger.debug('Lyrics: NetEase lyric fetch failed for %s (%s)',
                             ctx.file.filename, error)
        return _dispatch(ctx, sources, index + 1)

    if document.get('nolyric') or document.get('uncollected'):
        ctx.api.logger.debug('Lyrics: NetEase has no lyrics for %s', ctx.file.filename)
        return _dispatch(ctx, sources, index + 1)

    lrc = ((document.get('lrc') or {}).get('lyric') or '').strip()
    if not lrc:
        return _dispatch(ctx, sources, index + 1)

    if _setting(ctx.api, 'netease_strip_credits'):
        lrc = _strip_credits(lrc)

    if _setting(ctx.api, 'netease_add_translation'):
        translation = ((document.get('tlyric') or {}).get('lyric') or '').strip()
        if translation:
            lrc = _merge_translation(lrc, translation)

    artists = '/'.join(
        a.get('name', '') for a in (song.get('artists') or []) if isinstance(a, dict)
    )
    description = 'NetEase "%s" by %s (%ss, album %s)' % (
        song.get('name'),
        artists,
        round((song.get('duration') or 0) / 1000.0, 1),
        (song.get('album') or {}).get('name'),
    )
    return _write_lyrics(ctx, description, lrc, _lrc_to_plain(lrc))


# --- LRCLIB ----------------------------------------------------------------

def _request_lrclib(ctx: _Lookup, sources, index):
    metadata = ctx.file.metadata
    return ctx.api.web_service.get_url(
        url=LRCLIB_SEARCH_URL,
        handler=partial(_on_lrclib_search, ctx, sources, index),
        parse_response_type='json',
        unencoded_queryargs={
            'track_name': metadata.get('title'),
            'artist_name': metadata.get('artist'),
        },
        priority=True,
    )


def _on_lrclib_search(ctx: _Lookup, sources, index, document, reply, error):
    if error or not isinstance(document, list):
        ctx.api.logger.debug('Lyrics: LRCLIB search failed for %s (%s)', ctx.file.filename, error)
        return _dispatch(ctx, sources, index + 1)
    if not document:
        ctx.api.logger.debug('Lyrics: no LRCLIB entry for %s', ctx.file.filename)
        return _dispatch(ctx, sources, index + 1)

    metadata = ctx.file.metadata
    best = _pick_lrclib(
        document, metadata.get('title'), metadata.get('artist'), _duration_seconds(metadata)
    )
    if best is None:
        ctx.api.logger.debug(
            'Lyrics: LRCLIB returned %d results for %s but none matched title/artist',
            len(document), ctx.file.filename,
        )
        return _dispatch(ctx, sources, index + 1)

    if best.get('instrumental'):
        ctx.api.logger.info('Lyrics: %s is marked as instrumental', ctx.file.filename)
        ctx.finish()
        return None

    description = 'LRCLIB "%s" by %s (%ss, album %s)' % (
        best.get('trackName'), best.get('artistName'),
        best.get('duration'), best.get('albumName'),
    )
    return _write_lyrics(
        ctx, description, best.get('syncedLyrics') or '', best.get('plainLyrics') or ''
    )


# ---------------------------------------------------------------------------
# .lrc export
# ---------------------------------------------------------------------------

def _on_file_saved(api: PluginApi, file) -> None:
    """Called after a file is saved: optionally write a .lrc sidecar file."""
    if not _setting(api, 'enabled') or not _setting(api, 'export_lrc'):
        return

    metadata = file.metadata
    lyrics = metadata.get('syncedlyrics') or metadata.get('lyrics')
    if not lyrics:
        return

    path = _lrc_path(api, file)
    if _setting(api, 'lrc_never_replace') and os.path.exists(path):
        return

    try:
        with open(path, 'w', encoding='utf-8') as handle:
            handle.write(lyrics)
    except OSError as exc:
        api.logger.warning('Lyrics: could not write %s (%s)', path, exc)
    else:
        api.logger.info('Lyrics: wrote %s', path)


# ---------------------------------------------------------------------------
# options page
# ---------------------------------------------------------------------------

class LrclibLyricsOptionsPage(OptionsPage):
    NAME = 'lrclib_lyrics'
    TITLE = 'Lyrics Fetcher'
    PARENT = 'plugins'

    def __init__(self, parent=None):
        super().__init__(parent)
        self._api = getattr(self, 'api', None)

        self.cb_enabled = QCheckBox(self._tr('option.enabled', 'Fetch lyrics automatically'))

        self.lbl_source = QLabel(self._tr('option.source', 'Lyrics source:'))
        self.cmb_source = QComboBox()
        self.cmb_source.addItem(
            self._tr('option.source.auto', 'NetEase first, then LRCLIB'), SOURCE_AUTO
        )
        self.cmb_source.addItem(self._tr('option.source.netease', 'NetEase only'), SOURCE_NETEASE)
        self.cmb_source.addItem(self._tr('option.source.lrclib', 'LRCLIB only'), SOURCE_LRCLIB)

        self.cb_write_synced = QCheckBox(self._tr(
            'option.write_synced', 'Write synced lyrics (with timestamps) to "syncedlyrics"'
        ))
        self.cb_write_plain = QCheckBox(
            self._tr('option.write_plain', 'Write plain lyrics to "lyrics"')
        )
        self.cb_never_replace = QCheckBox(
            self._tr('option.never_replace', 'Never replace lyrics that are already present')
        )

        self.cb_strip_credits = QCheckBox(self._tr(
            'option.strip_credits',
            'NetEase: drop the leading credits block (作词/作曲/编曲 …)',
        ))
        self.cb_add_translation = QCheckBox(self._tr(
            'option.add_translation',
            'NetEase: append the Chinese translation to each line (bilingual lyrics)',
        ))

        self.cb_export_lrc = QCheckBox(
            self._tr('option.export_lrc', 'Also export a .lrc file when saving')
        )
        self.cb_lrc_never_replace = QCheckBox(
            self._tr('option.lrc_never_replace', 'Never replace an existing .lrc file')
        )
        self.lbl_pattern = QLabel(
            self._tr('option.lrc_filename', 'Name pattern for the .lrc file:')
        )
        self.txt_pattern = QLineEdit()

        note = QLabel(self._tr(
            'option.note',
            'The "syncedlyrics" tag is only supported for MP3 and FLAC/OGG; MP4/M4A cannot '
            'store synced lyrics, so enable plain lyrics for those. Neither source matches '
            'on MusicBrainz IDs, so mismatches are possible — skim the lyrics before saving. '
            'NetEase uses undocumented web endpoints and may stop working without notice.',
        ))
        note.setWordWrap(True)

        layout = QVBoxLayout(self)
        layout.addWidget(self.cb_enabled)
        layout.addWidget(self.lbl_source)
        layout.addWidget(self.cmb_source)
        layout.addWidget(self.cb_write_synced)
        layout.addWidget(self.cb_write_plain)
        layout.addWidget(self.cb_never_replace)
        layout.addWidget(self.cb_strip_credits)
        layout.addWidget(self.cb_add_translation)
        layout.addWidget(self.cb_export_lrc)
        layout.addWidget(self.cb_lrc_never_replace)
        layout.addWidget(self.lbl_pattern)
        layout.addWidget(self.txt_pattern)
        layout.addWidget(note)
        layout.addStretch()

    def _tr(self, key: str, text: str) -> str:
        api = self._api
        if api is None:
            return text
        try:
            return api.tr(key, text)
        except Exception:  # pragma: no cover - defensive
            return text

    def load(self) -> None:
        api = self._api
        self.cb_enabled.setChecked(bool(_setting(api, 'enabled')))
        self.cb_write_synced.setChecked(bool(_setting(api, 'write_synced')))
        self.cb_write_plain.setChecked(bool(_setting(api, 'write_plain')))
        self.cb_never_replace.setChecked(bool(_setting(api, 'never_replace')))
        self.cb_strip_credits.setChecked(bool(_setting(api, 'netease_strip_credits')))
        self.cb_add_translation.setChecked(bool(_setting(api, 'netease_add_translation')))
        self.cb_export_lrc.setChecked(bool(_setting(api, 'export_lrc')))
        self.cb_lrc_never_replace.setChecked(bool(_setting(api, 'lrc_never_replace')))
        self.txt_pattern.setText(_setting(api, 'lrc_filename') or DEFAULT_LRC_PATTERN)
        source = _setting(api, 'source')
        index = self.cmb_source.findData(source)
        self.cmb_source.setCurrentIndex(index if index >= 0 else 0)

    def save(self) -> None:
        config = self._api.plugin_config
        config['enabled'] = self.cb_enabled.isChecked()
        config['write_synced'] = self.cb_write_synced.isChecked()
        config['write_plain'] = self.cb_write_plain.isChecked()
        config['never_replace'] = self.cb_never_replace.isChecked()
        config['netease_strip_credits'] = self.cb_strip_credits.isChecked()
        config['netease_add_translation'] = self.cb_add_translation.isChecked()
        config['export_lrc'] = self.cb_export_lrc.isChecked()
        config['lrc_never_replace'] = self.cb_lrc_never_replace.isChecked()
        config['lrc_filename'] = self.txt_pattern.text().strip() or DEFAULT_LRC_PATTERN
        config['source'] = self.cmb_source.currentData() or SOURCE_AUTO


# ---------------------------------------------------------------------------
# entry points
# ---------------------------------------------------------------------------

def enable(api: PluginApi) -> None:
    """Called when the plugin is enabled."""
    _register_settings(api)
    api.register_file_post_addition_to_track_processor(_on_file_added)
    api.register_file_post_save_processor(_on_file_saved)
    api.register_options_page(LrclibLyricsOptionsPage)
    api.logger.info('Lyrics Fetcher: enabled')


def disable() -> None:
    """Called when the plugin is disabled."""
