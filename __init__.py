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

import json
import os
import re
from functools import partial

from PyQt6 import QtCore, QtNetwork
from PyQt6.QtWidgets import QCheckBox, QComboBox, QLabel, QLineEdit, QVBoxLayout

from picard.config import BoolOption, TextOption
from picard.plugin3.api import BaseAction, OptionsPage, PluginApi

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
    ('timed_lyrics_in_tag', True),
    ('never_replace', True),
    ('export_lrc', False),
    ('auto_lrc_for_unsupported', True),
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
# Credit lines such as "作词 : GAK-amazuti-" or "混音工程师: You Yokoi" that
# NetEase prepends to lyrics. Up to six characters may sit between the keyword
# and the colon, which covers the common "...工程师" / "...制作人" suffixes.
_CREDIT = re.compile(
    r'^(?:作词|作曲|编曲|词曲|制作人|出品人|出品|监制|混音|母带|录音|和声|合声|'
    r'吉他|贝斯|鼓|键盘|弦乐|钢琴|策划|统筹|发行|OP|SP)'
    r'[^:：]{0,6}[:：]',
    re.IGNORECASE,
)
# NetEase covers are titled like "晴天（深情版）" or "Lemon (翻自 米津玄師)".
_NETEASE_NOISE = re.compile(r'(翻自|翻唱|cover|纯音乐|伴奏|remix|钢琴版|吉他版)', re.IGNORECASE)


# ---------------------------------------------------------------------------
# settings
# ---------------------------------------------------------------------------

def _register_settings(api: PluginApi) -> None:
    """Declare the plugin's options so Picard knows their types and defaults.

    Never let a failure here abort ``enable()``: unregistered options simply
    read back as None, and ``_setting()`` turns that into the default, so the
    plugin keeps working.
    """
    try:
        section = api.plugin_config.section_name
    except Exception as exc:  # pragma: no cover - defensive
        api.logger.warning('Lyrics: cannot determine the config section (%s); using defaults', exc)
        return
    for name, default in BOOL_SETTINGS:
        try:
            BoolOption.add_if_missing(section, name, default)
        except Exception as exc:  # pragma: no cover - defensive
            api.logger.warning('Lyrics: could not register option %r (%s)', name, exc)
    for name, default in TEXT_SETTINGS:
        try:
            TextOption.add_if_missing(section, name, default)
        except Exception as exc:  # pragma: no cover - defensive
            api.logger.warning('Lyrics: could not register option %r (%s)', name, exc)


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


def _as_json(document):
    """Return a decoded JSON document, or None when it cannot be decoded.

    NetEase is requested without a response parser, because Picard sets an
    ``Accept: application/json`` header for parsed responses and music.163.com
    answers those with ``Content-Type: text/plain`` — the header appears to
    upset its CDN. Without a parser the handler receives the raw body, so we
    decode it here.
    """
    if isinstance(document, (bytes, bytearray)):
        try:
            return json.loads(document.decode('utf-8', 'replace'))
        except ValueError:
            return None
    return document


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


# Formats that cannot store the syncedlyrics tag. Picard lists it in their
# UNSUPPORTED_TAGS and filters it out in File._tags_to_update(), so anything we
# put there is silently dropped - the file is not even marked as changed.
# Only ID3 (MP3, AIFF, WAV) can hold it; Vorbis (FLAC/OGG/Opus) and MP4 cannot.
_ID3_EXTENSIONS = {'.mp3', '.aif', '.aifc', '.aiff', '.wav'}

# Virtual tag used to keep the synced lyrics around for the .lrc export even
# when the audio format cannot store them. Picard never writes tags whose name
# starts with '~' to the file.
SYNCED_CACHE_TAG = '~lyrics_fetcher_synced'


def _supports_synced(file) -> bool:
    """Whether this file's format can actually store the syncedlyrics tag."""
    checker = getattr(file, 'supports_tag', None)
    if callable(checker):
        try:
            return bool(checker('syncedlyrics'))
        except Exception:  # pragma: no cover - defensive
            pass
    extension = os.path.splitext(getattr(file, 'filename', '') or '')[1].lower()
    return extension in _ID3_EXTENSIONS


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

    __slots__ = ('api', 'file', 'album', 'task_id', 'force', 'finished', 'retries')

    def __init__(self, api, file, album, task_id, force=False):
        self.api = api
        self.file = file
        self.album = album
        self.task_id = task_id
        self.force = force
        self.finished = False
        self.retries = {}

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

    can_store_synced = _supports_synced(ctx.file)
    replace = ctx.force or not _setting(api, 'never_replace')

    # Formats such as FLAC, OGG and M4A have no field for synced lyrics at all.
    # When asked, put the LRC text into the plain lyrics tag instead, so the
    # timestamps are still stored inside the file and not only in the sidecar.
    timed_in_tag = (
        not can_store_synced
        and bool(synced)
        and _setting(api, 'timed_lyrics_in_tag')
    )
    tag_lyrics = synced if timed_in_tag else plain

    # Keep the synced text for the .lrc export even when the format cannot
    # store it in a tag.
    if synced:
        try:
            metadata[SYNCED_CACHE_TAG] = synced
        except Exception:  # pragma: no cover - defensive
            pass

    written = []
    tag_written = False
    if _setting(api, 'write_synced') and synced and can_store_synced:
        if replace or not metadata.get('syncedlyrics'):
            metadata['syncedlyrics'] = synced
            written.append('syncedlyrics')
    if _setting(api, 'write_plain') and tag_lyrics:
        if replace or not metadata.get('lyrics'):
            metadata['lyrics'] = tag_lyrics
            tag_written = True
            written.append('lyrics (with timestamps)' if timed_in_tag else 'lyrics')

    if written:
        api.logger.info(
            'Lyrics: %s — %s, wrote %s', ctx.file.filename, description, ', '.join(written)
        )
    else:
        api.logger.info('Lyrics: nothing to write for %s', ctx.file.filename)

    # Write the .lrc straight away. For formats that cannot store the
    # syncedlyrics tag this is the only place the timestamps can live, and it
    # means the user does not have to remember to save first.
    if _setting(api, 'enabled'):
        _export_lrc(api, ctx.file)

    # Nothing calls File.update() after the addition-to-track processors run, so
    # without this the track would never be shown as changed in Picard.
    try:
        ctx.file.update()
    except Exception:  # pragma: no cover - defensive
        pass

    if synced and not can_store_synced:
        extension = os.path.splitext(ctx.file.filename)[1].lstrip('.').upper() or 'this'
        name = os.path.basename(ctx.file.filename)
        sidecar_enabled = (
            _setting(api, 'auto_lrc_for_unsupported') or _setting(api, 'export_lrc')
        )
        if timed_in_tag and tag_written:
            api.logger.info(
                'Lyrics: %s — %s has no field for synced lyrics, so the timed lyrics went '
                'into the "lyrics" tag%s.',
                name, extension,
                ' (and the .lrc sidecar)' if sidecar_enabled else '',
            )
        elif not sidecar_enabled:
            api.logger.info(
                'Lyrics: %s — %s files cannot store the syncedlyrics tag (Picard drops it). '
                'Enable "Also export a .lrc file when saving" to keep the timestamps.',
                name, extension,
            )
        elif not written:
            api.logger.info(
                'Lyrics: %s — %s files cannot store the syncedlyrics tag; the timestamps are '
                'in the .lrc sidecar. Enable "Write plain lyrics" to also fill the lyrics tag.',
                name, extension,
            )
        else:
            api.logger.info(
                'Lyrics: %s — %s files cannot store the syncedlyrics tag (Picard drops it), '
                'so the timestamps went into the .lrc sidecar file.',
                name, extension,
            )
    ctx.finish()


# ---------------------------------------------------------------------------
# request chain
# ---------------------------------------------------------------------------

class _NetEaseClient:
    """Dedicated HTTP/1.1 client for music.163.com.

    Picard's shared web service negotiates HTTP/2, and music.163.com's CDN
    intermittently drops those streams. Measured on this machine with Qt 6.11:
    HTTP/2 failed one time in three (NetworkError.RemoteHostClosedError, after
    a five second stall) while the identical requests over HTTP/1.1 succeeded
    three times out of three in about half a second each. The same protocol
    errors show up in Picard's log for music.163.com, so NetEase is fetched
    through this client with HTTP/2 turned off. It still honours the
    application proxy, which Qt applies to every QNetworkAccessManager.
    """

    def __init__(self):
        self._manager = None
        self._handlers = {}

    def _manager_instance(self):
        if self._manager is None:
            self._manager = QtNetwork.QNetworkAccessManager()
        return self._manager

    def get(self, url, params, handler):
        """Issue a GET and deliver ``handler(document, reply, error)``."""
        qurl = QtCore.QUrl(url)
        if params:
            query = QtCore.QUrlQuery(qurl)
            for key, value in params.items():
                query.addQueryItem(str(key), str(value))
            qurl.setQuery(query)

        request = QtNetwork.QNetworkRequest(qurl)
        request.setAttribute(
            QtNetwork.QNetworkRequest.Attribute.Http2AllowedAttribute, False
        )
        for name, value in NETEASE_HEADERS.items():
            request.setRawHeader(name.encode('ascii'), value.encode('ascii'))

        reply = self._manager_instance().get(request)
        self._handlers[reply] = handler
        reply.finished.connect(partial(self._handle_finished, reply))
        return reply

    def _handle_finished(self, reply):
        handler = self._handlers.pop(reply, None)
        failure = None
        document = None
        if reply.error() != QtNetwork.QNetworkReply.NetworkError.NoError:
            failure = reply.error()
        else:
            document = _as_json(bytes(reply.readAll()))
            if document is None:
                failure = 'unparseable response'
        if handler is not None:
            try:
                handler(document, reply, failure)
            except Exception:  # pragma: no cover - defensive
                pass
        reply.deleteLater()


def _netease_client(api):
    """Return the NetEase client for this session, creating it on first use."""
    client = getattr(api, '_lyrics_fetcher_netease_client', None)
    if client is None:
        client = _NetEaseClient()
        try:
            api._lyrics_fetcher_netease_client = client
        except Exception:  # pragma: no cover - defensive
            pass
    return client


def _dispatch(ctx: _Lookup, sources, index):
    """Fire the request for sources[index], or give up when none is left."""
    if index >= len(sources):
        ctx.api.logger.info('Lyrics: no match for %s in any source', ctx.file.filename)
        ctx.finish()
        return None
    if sources[index] == SOURCE_NETEASE:
        return _request_netease(ctx, sources, index)
    return _request_lrclib(ctx, sources, index)


# Picard's web service only retries 429 / 503 / ServiceUnavailable on its own.
# A protocol-level failure (an HTTP/2 stream error, a dropped connection) is
# handed straight to our handler, and those are usually transient, so we retry
# a couple of times before giving up on the source.
MAX_REQUEST_RETRIES = 2


def _retry_or_advance(ctx: _Lookup, sources, index, key, again):
    """Retry a failed request, then fall through to the next source."""
    used = ctx.retries.get(key, 0)
    if used < MAX_REQUEST_RETRIES:
        ctx.retries[key] = used + 1
        ctx.api.logger.info(
            'Lyrics: %s failed for %s, retrying (attempt %d of %d)',
            key, ctx.file.filename, used + 2, MAX_REQUEST_RETRIES + 1,
        )
        return again()
    ctx.api.logger.warning(
        'Lyrics: %s failed for %s after %d attempts, moving on',
        key, ctx.file.filename, MAX_REQUEST_RETRIES + 1,
    )
    return _dispatch(ctx, sources, index + 1)


def _begin_lookup(api: PluginApi, track, file, force=False, use_album_task=True) -> bool:
    """Start a lyrics lookup for one file.

    Returns True when a lookup was started, False when it was skipped. ``force``
    ignores the "never replace" setting (used by the manual menu action, where
    the user has explicitly asked for lyrics).
    """
    metadata = file.metadata
    if not metadata.get('title') or not metadata.get('artist'):
        api.logger.warning(
            'Lyrics: %s has no title and/or artist tag, cannot look up lyrics', file.filename
        )
        return False

    if not force and _setting(api, 'never_replace') and (
        metadata.get('syncedlyrics') or metadata.get('lyrics')
    ):
        api.logger.info(
            'Lyrics: %s already has lyrics and "never replace" is enabled, skipping',
            file.filename,
        )
        return False

    album = getattr(track, 'album', None) if track is not None else None
    task_id = None
    if use_album_task and album is not None:
        task_id = 'lyrics_%d' % (abs(hash(file.filename)) & 0x7FFFFFFF)

    ctx = _Lookup(api, file, album, task_id, force=force)
    sources = _source_order(api)

    # Picard calls request_factory() inside add_album_task(). Guard against the
    # chain being started twice if anything after that raises, so a failure can
    # never turn into duplicate network requests.
    started = []

    def fire_once():
        if started:
            return None
        started.append(True)
        return _dispatch(ctx, sources, 0)

    if task_id is not None:
        try:
            api.add_album_task(album, task_id, 'Fetching lyrics', request_factory=fire_once)
            return True
        except Exception as exc:  # pragma: no cover - defensive
            api.logger.debug(
                'Lyrics: album task unavailable (%s), falling back to a direct request', exc
            )
    if not started:
        fire_once()
    return True


def _track_files(obj):
    """Yield ``(track, file)`` pairs for a Track, Album or Cluster object."""
    tracks = getattr(obj, 'tracks', None)
    if tracks:
        for track in tracks:
            for file in list(getattr(track, 'files', None) or []):
                yield track, file
        return
    for file in list(getattr(obj, 'files', None) or []):
        yield obj, file


_auto_hook_reported = False


def _on_file_added(api: PluginApi, track, file) -> None:
    """Called when a file is added to a track: look the lyrics up."""
    global _auto_hook_reported
    if not _setting(api, 'enabled'):
        return
    if not (_setting(api, 'write_synced') or _setting(api, 'write_plain')):
        return

    if not _auto_hook_reported:
        # One line per session proving the automatic hook is wired up. If this
        # never shows up in the log after loading files, the plugin did not
        # register (or was not enabled) and the manual menu action is the way
        # to go.
        _auto_hook_reported = True
        api.logger.info('Lyrics: automatic lookup hook is active')

    _begin_lookup(api, track, file)


class FetchLyricsAction(BaseAction):
    """Right-click action: fetch lyrics for the selected tracks/albums."""

    TITLE = 'Fetch lyrics (NetEase / LRCLIB)'

    def callback(self, objs):
        api = self.api
        started = 0
        seen = set()
        for obj in objs:
            for track, file in _track_files(obj):
                if file.filename in seen:
                    continue
                seen.add(file.filename)
                if _begin_lookup(api, track, file, force=True, use_album_task=False):
                    started += 1
        api.logger.info(
            'Lyrics: manual lookup started for %d file(s) out of %d selected item(s)',
            started, len(objs),
        )


# --- NetEase ---------------------------------------------------------------

def _request_netease(ctx: _Lookup, sources, index):
    metadata = ctx.file.metadata
    return _netease_client(ctx.api).get(
        NETEASE_SEARCH_URL,
        {
            's': '%s %s' % (metadata.get('title'), metadata.get('artist')),
            'type': 1,
            'offset': 0,
            'limit': NETEASE_SEARCH_LIMIT,
        },
        partial(_on_netease_search, ctx, sources, index),
    )


def _on_netease_search(ctx: _Lookup, sources, index, document, reply, error):
    document = _as_json(document)
    if error or not isinstance(document, dict):
        return _retry_or_advance(
            ctx, sources, index, 'NetEase search',
            partial(_request_netease, ctx, sources, index),
        )

    result = document.get('result')
    songs = result.get('songs') if isinstance(result, dict) else None
    if not songs:
        ctx.api.logger.info('Lyrics: NetEase returned no results for %s', ctx.file.filename)
        return _dispatch(ctx, sources, index + 1)

    metadata = ctx.file.metadata
    best = _pick_netease(
        songs, metadata.get('title'), metadata.get('artist'), _duration_seconds(metadata)
    )
    if best is None:
        ctx.api.logger.info(
            'Lyrics: NetEase returned %d results for %s but none matched title/artist',
            len(songs), ctx.file.filename,
        )
        return _dispatch(ctx, sources, index + 1)

    return _request_netease_lyric(ctx, best, sources, index)


def _request_netease_lyric(ctx: _Lookup, song, sources, index):
    return _netease_client(ctx.api).get(
        NETEASE_LYRIC_URL,
        {'id': song.get('id'), 'lv': -1, 'kv': -1, 'tv': -1},
        partial(_on_netease_lyric, ctx, song, sources, index),
    )


def _on_netease_lyric(ctx: _Lookup, song, sources, index, document, reply, error):
    document = _as_json(document)
    if error or not isinstance(document, dict):
        return _retry_or_advance(
            ctx, sources, index, 'NetEase lyrics',
            partial(_request_netease_lyric, ctx, song, sources, index),
        )

    if document.get('nolyric') or document.get('uncollected'):
        ctx.api.logger.info('Lyrics: NetEase has no lyrics for %s', ctx.file.filename)
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
        return _retry_or_advance(
            ctx, sources, index, 'LRCLIB search',
            partial(_request_lrclib, ctx, sources, index),
        )
    if not document:
        ctx.api.logger.info('Lyrics: no LRCLIB entry for %s', ctx.file.filename)
        return _dispatch(ctx, sources, index + 1)

    metadata = ctx.file.metadata
    best = _pick_lrclib(
        document, metadata.get('title'), metadata.get('artist'), _duration_seconds(metadata)
    )
    if best is None:
        ctx.api.logger.info(
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

def _export_lrc(api: PluginApi, file) -> None:
    """Write the .lrc sidecar file, if the settings call for it."""
    metadata = file.metadata
    # Prefer the synced text we cached during the lookup: for formats that
    # cannot store the syncedlyrics tag this is the only copy left.
    synced = metadata.get(SYNCED_CACHE_TAG) or metadata.get('syncedlyrics')
    plain = metadata.get('lyrics')

    if _setting(api, 'export_lrc'):
        lyrics = synced or plain
    elif _setting(api, 'auto_lrc_for_unsupported') and synced and not _supports_synced(file):
        # The audio format cannot keep the timestamps in a tag, so the sidecar
        # file is the only way to preserve them. Do it without being asked.
        lyrics = synced
    else:
        return

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


def _on_file_saved(api: PluginApi, file) -> None:
    """Called after a file is saved: refresh the .lrc sidecar."""
    if not _setting(api, 'enabled'):
        return
    # The sidecar is normally already written when the lookup finished; this
    # run picks up a file that got renamed by Picard during the save.
    _export_lrc(api, file)


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
        self.cb_timed_in_tag = QCheckBox(self._tr(
            'option.timed_lyrics_in_tag',
            'For formats with no synced-lyrics field (FLAC, OGG, M4A), put the timed lyrics '
            'into the "lyrics" tag',
        ))
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
        self.cb_auto_lrc = QCheckBox(self._tr(
            'option.auto_lrc',
            'Automatically export a .lrc sidecar for formats that cannot store synced '
            'lyrics (FLAC, OGG, M4A)',
        ))
        self.cb_lrc_never_replace = QCheckBox(
            self._tr('option.lrc_never_replace', 'Never replace an existing .lrc file')
        )
        self.lbl_pattern = QLabel(
            self._tr('option.lrc_filename', 'Name pattern for the .lrc file:')
        )
        self.txt_pattern = QLineEdit()

        note = QLabel(self._tr(
            'option.note',
            'Only MP3 (ID3) has a real field for synced lyrics. FLAC, OGG/Opus and MP4/M4A '
            'silently drop the "syncedlyrics" tag, so for those the plugin puts the timed '
            'lyrics into the "lyrics" tag and writes a .lrc sidecar file. Neither source '
            'matches on MusicBrainz IDs, so mismatches are possible — skim the lyrics before '
            'saving. NetEase uses undocumented web endpoints and may stop working without '
            'notice.',
        ))
        note.setWordWrap(True)

        layout = QVBoxLayout(self)
        layout.addWidget(self.cb_enabled)
        layout.addWidget(self.lbl_source)
        layout.addWidget(self.cmb_source)
        layout.addWidget(self.cb_write_synced)
        layout.addWidget(self.cb_write_plain)
        layout.addWidget(self.cb_timed_in_tag)
        layout.addWidget(self.cb_never_replace)
        layout.addWidget(self.cb_strip_credits)
        layout.addWidget(self.cb_add_translation)
        layout.addWidget(self.cb_export_lrc)
        layout.addWidget(self.cb_auto_lrc)
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
        self.cb_timed_in_tag.setChecked(bool(_setting(api, 'timed_lyrics_in_tag')))
        self.cb_never_replace.setChecked(bool(_setting(api, 'never_replace')))
        self.cb_strip_credits.setChecked(bool(_setting(api, 'netease_strip_credits')))
        self.cb_add_translation.setChecked(bool(_setting(api, 'netease_add_translation')))
        self.cb_export_lrc.setChecked(bool(_setting(api, 'export_lrc')))
        self.cb_auto_lrc.setChecked(bool(_setting(api, 'auto_lrc_for_unsupported')))
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
        config['timed_lyrics_in_tag'] = self.cb_timed_in_tag.isChecked()
        config['never_replace'] = self.cb_never_replace.isChecked()
        config['netease_strip_credits'] = self.cb_strip_credits.isChecked()
        config['netease_add_translation'] = self.cb_add_translation.isChecked()
        config['export_lrc'] = self.cb_export_lrc.isChecked()
        config['auto_lrc_for_unsupported'] = self.cb_auto_lrc.isChecked()
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
    api.register_track_action(FetchLyricsAction)
    api.register_album_action(FetchLyricsAction)
    api.register_options_page(LrclibLyricsOptionsPage)
    api.logger.info('Lyrics Fetcher: enabled')


def disable() -> None:
    """Called when the plugin is disabled."""
