# -*- coding: utf-8 -*-
"""LRCLIB Lyrics — fetch synced and plain lyrics from lrclib.net.

Writes the result into the ``syncedlyrics`` tag (LRC text, with timestamps)
and/or the ``lyrics`` tag (plain text) of your audio files, and can optionally
export a ``.lrc`` sidecar file next to the audio file.

Requires MusicBrainz Picard 3.0 or newer: the ``syncedlyrics`` tag only exists
from 3.0 on, and only for formats that can store it (ID3/MP3 and
Vorbis/FLAC/OGG/Opus). MP4/M4A cannot store synced lyrics.
"""

from __future__ import annotations

import os
from functools import partial

from PyQt6.QtWidgets import QCheckBox, QLabel, QLineEdit, QVBoxLayout

from picard.config import BoolOption, TextOption
from picard.plugin3.api import OptionsPage, PluginApi

# We deliberately use the *search* endpoint rather than /api/get:
# /api/get is an exact-match lookup that answers 404 when it cannot find a
# record, which Picard logs as a network error. /api/search always answers
# 200 (possibly with an empty list), returns the full lyric text inline, and
# tolerates the small artist/album spelling differences that are common with
# non-Latin scripts. The trade-off is that we must pick the right record
# ourselves, which is what _pick_best() does.
LRCLIB_SEARCH_URL = 'https://lrclib.net/api/search'
DEFAULT_LRC_PATTERN = '%filename%.lrc'

# (setting name, default value)
BOOL_SETTINGS = (
    ('enabled', True),
    ('write_synced', True),
    ('write_plain', True),
    ('never_replace', True),
    ('export_lrc', False),
    ('lrc_never_replace', True),
)

# Fallbacks used when a setting is somehow not registered, so that a failure to
# declare the options degrades to the documented defaults instead of silently
# switching the plugin off.
DEFAULTS = dict(BOOL_SETTINGS)
DEFAULTS['lrc_filename'] = DEFAULT_LRC_PATTERN


# ---------------------------------------------------------------------------
# settings
# ---------------------------------------------------------------------------

def _register_settings(api: PluginApi) -> None:
    """Declare the plugin's options so Picard knows their types and defaults."""
    section = api.plugin_config.section_name
    for name, default in BOOL_SETTINGS:
        BoolOption.add_if_missing(section, name, default)
    TextOption.add_if_missing(section, 'lrc_filename', DEFAULT_LRC_PATTERN)


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


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _normalize(text) -> str:
    """Casefold and collapse whitespace so that names compare reliably."""
    return ' '.join(str(text or '').casefold().split())


def _duration_seconds(metadata) -> int | None:
    """Return the track length in seconds, or None if unavailable.

    Picard stores the length as a ``mm:ss`` string in the ``~length``
    variable. LRCLIB often holds several versions of the same song (album
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


def _pick_best(results, title, artist, duration):
    """Choose the best matching record from a LRCLIB search result list.

    A record is only accepted when the track title matches exactly (after
    normalisation) and the artist name matches exactly or is contained in the
    other. Among the survivors we prefer the one whose duration is closest to
    the file's, and then the one that carries synced lyrics.

    Returns the chosen record, or None when nothing is a confident match.
    """
    want_title = _normalize(title)
    want_artist = _normalize(artist)

    candidates = []
    for item in results:
        if not isinstance(item, dict):
            continue
        got_title = _normalize(item.get('trackName'))
        got_artist = _normalize(item.get('artistName'))

        if got_title != want_title:
            continue
        if got_artist != want_artist and want_artist not in got_artist and got_artist not in want_artist:
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
# fetching
# ---------------------------------------------------------------------------

def _on_file_added(api: PluginApi, track, file) -> None:
    """Called when a file is added to a track: look the lyrics up on LRCLIB."""
    if not _setting(api, 'enabled'):
        return
    if not (_setting(api, 'write_synced') or _setting(api, 'write_plain')):
        return

    metadata = file.metadata
    title = metadata.get('title')
    artist = metadata.get('artist')
    if not title or not artist:
        api.logger.debug(
            'LRCLIB Lyrics: skipping %s, both title and artist are required', file.filename
        )
        return

    if _setting(api, 'never_replace') and (
        metadata.get('syncedlyrics') or metadata.get('lyrics')
    ):
        api.logger.debug('LRCLIB Lyrics: skipping %s, lyrics are already present', file.filename)
        return

    queryargs = {
        'track_name': title,
        'artist_name': artist,
    }

    album = getattr(track, 'album', None)
    if album is not None:
        task_id = 'lyrics_%d' % (abs(hash(file.filename)) & 0x7FFFFFFF)
        factory = partial(
            api.web_service.get_url,
            url=LRCLIB_SEARCH_URL,
            handler=partial(_on_response, api, file, album, task_id),
            parse_response_type='json',
            unencoded_queryargs=queryargs,
            priority=True,
        )
        try:
            api.add_album_task(album, task_id, 'LRCLIB: fetching lyrics', request_factory=factory)
            return
        except Exception as exc:  # pragma: no cover - defensive
            api.logger.debug(
                'LRCLIB Lyrics: album task unavailable (%s), falling back to a direct request', exc
            )

    api.web_service.get_url(
        url=LRCLIB_SEARCH_URL,
        handler=partial(_on_response, api, file, None, None),
        parse_response_type='json',
        unencoded_queryargs=queryargs,
        priority=True,
    )


def _on_response(api: PluginApi, file, album, task_id, document, reply, error) -> None:
    """Pick the best search hit and write the tags."""
    try:
        if error:
            api.logger.debug('LRCLIB Lyrics: request failed for %s (%s)', file.filename, error)
            return
        if not isinstance(document, list):
            api.logger.debug('LRCLIB Lyrics: unexpected response for %s', file.filename)
            return
        if not document:
            api.logger.debug('LRCLIB Lyrics: no LRCLIB entry for %s', file.filename)
            return

        metadata = file.metadata
        best = _pick_best(
            document,
            metadata.get('title'),
            metadata.get('artist'),
            _duration_seconds(metadata),
        )
        if best is None:
            api.logger.debug(
                'LRCLIB Lyrics: %s results for %s but none matched title/artist',
                len(document), file.filename,
            )
            return

        if best.get('instrumental'):
            api.logger.info('LRCLIB Lyrics: %s is marked as instrumental', file.filename)
            return

        replace = not _setting(api, 'never_replace')
        synced = (best.get('syncedLyrics') or '').strip()
        plain = (best.get('plainLyrics') or '').strip()

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
                'LRCLIB Lyrics: %s — matched "%s" by %s (%ss, album %s), wrote %s',
                file.filename,
                best.get('trackName'),
                best.get('artistName'),
                best.get('duration'),
                best.get('albumName'),
                ', '.join(written),
            )
        else:
            api.logger.debug('LRCLIB Lyrics: nothing to write for %s', file.filename)
    finally:
        if album is not None and task_id is not None:
            try:
                api.complete_album_task(album, task_id)
            except Exception:  # pragma: no cover - defensive
                pass


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
        api.logger.warning('LRCLIB Lyrics: could not write %s (%s)', path, exc)
    else:
        api.logger.info('LRCLIB Lyrics: wrote %s', path)


# ---------------------------------------------------------------------------
# options page
# ---------------------------------------------------------------------------

class LrclibLyricsOptionsPage(OptionsPage):
    NAME = 'lrclib_lyrics'
    TITLE = 'LRCLIB Lyrics'
    PARENT = 'plugins'

    def __init__(self, parent=None):
        super().__init__(parent)
        self._api = getattr(self, 'api', None)

        self.cb_enabled = QCheckBox(self._tr('option.enabled', 'Fetch lyrics from LRCLIB'))
        self.cb_write_synced = QCheckBox(self._tr(
            'option.write_synced', 'Write synced lyrics (with timestamps) to "syncedlyrics"'
        ))
        self.cb_write_plain = QCheckBox(
            self._tr('option.write_plain', 'Write plain lyrics to "lyrics"')
        )
        self.cb_never_replace = QCheckBox(
            self._tr('option.never_replace', 'Never replace lyrics that are already present')
        )
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
            'Note: the "syncedlyrics" tag is only supported for MP3 and FLAC/OGG. '
            'MP4/M4A files cannot store synced lyrics — enable plain lyrics for those. '
            'LRCLIB matches on artist, title and duration, not on MusicBrainz IDs, '
            'so mismatches are possible — skim the lyrics before saving.',
        ))
        note.setWordWrap(True)

        layout = QVBoxLayout(self)
        layout.addWidget(self.cb_enabled)
        layout.addWidget(self.cb_write_synced)
        layout.addWidget(self.cb_write_plain)
        layout.addWidget(self.cb_never_replace)
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
        self.cb_export_lrc.setChecked(bool(_setting(api, 'export_lrc')))
        self.cb_lrc_never_replace.setChecked(bool(_setting(api, 'lrc_never_replace')))
        self.txt_pattern.setText(_setting(api, 'lrc_filename') or DEFAULT_LRC_PATTERN)

    def save(self) -> None:
        config = self._api.plugin_config
        config['enabled'] = self.cb_enabled.isChecked()
        config['write_synced'] = self.cb_write_synced.isChecked()
        config['write_plain'] = self.cb_write_plain.isChecked()
        config['never_replace'] = self.cb_never_replace.isChecked()
        config['export_lrc'] = self.cb_export_lrc.isChecked()
        config['lrc_never_replace'] = self.cb_lrc_never_replace.isChecked()
        config['lrc_filename'] = self.txt_pattern.text().strip() or DEFAULT_LRC_PATTERN


# ---------------------------------------------------------------------------
# entry points
# ---------------------------------------------------------------------------

def enable(api: PluginApi) -> None:
    """Called when the plugin is enabled."""
    _register_settings(api)
    api.register_file_post_addition_to_track_processor(_on_file_added)
    api.register_file_post_save_processor(_on_file_saved)
    api.register_options_page(LrclibLyricsOptionsPage)
    api.logger.info('LRCLIB Lyrics: enabled')


def disable() -> None:
    """Called when the plugin is disabled."""
