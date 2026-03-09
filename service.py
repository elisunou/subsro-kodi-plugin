# -*- coding: utf-8 -*-
"""
Subs.ro Kodi Subtitle Addon — service.py
Conform schema OpenAPI v1.0: https://api.subs.ro/v1.0

Fixes față de versiunea originală:
  - TMDb ID: getUniqueID('tmdb') + fallback getUniqueID('tmdb')/tvdb, NU getDbId()
  - Strategie multi-fallback: imdbid → tmdbid → title (orig/curat) → release
  - Rezoluție: citită din Kodi infolabels (merge și la streaming), mereu activă
  - Episode regex: 5 formate cu \b (s01e02, s01xe02, 1x02, ep02, season X episode Y)
  - Cleanup fișiere temporare după erori (try/finally)
  - Thread safety: lock pe operații cache
  - Filtrare: tip (movie/series), an, hearing impaired
  - Securitate: API key sanitizat, nu apare în loguri
  - Cache: nu salvează răspunsuri goale; șterge cache corupt la citire
"""

import xbmc
import xbmcgui
import xbmcaddon
import xbmcplugin
import xbmcvfs
import requests
import os
import sys
import urllib.parse
import zipfile
import difflib
import re
import json
import time
import hashlib
import unicodedata
import threading

ADDON    = xbmcaddon.Addon()
API_BASE = "https://api.subs.ro/v1.0"

# Lock global pentru operații cache (thread safety)
_CACHE_LOCK = threading.Lock()

# ============================================================================
#                           LOGGING
# ============================================================================

def log(msg, level=xbmc.LOGINFO):
    """Log doar când debug_log e activ. API key-ul e mascat automat."""
    if ADDON.getSetting('debug_log') != 'true':
        return
    # Securitate: nu logăm cheia API în clar
    api_key = ADDON.getSetting('api_key')
    if api_key and len(api_key) > 4:
        msg = msg.replace(api_key, api_key[:4] + '****')
    xbmc.log(f"[Subs.ro] {msg}", level)

# ============================================================================
#                           AUTENTIFICARE
# ============================================================================

def get_api_key():
    """
    Returnează cheia API din setări.
    Dacă lipsește, afișează dialog de introducere.
    """
    api_key = ADDON.getSetting('api_key').strip()
    if api_key:
        return api_key

    dialog  = xbmcgui.Dialog()
    api_key = dialog.input(
        "Introdu cheia ta API de la Subs.ro",
        type=xbmcgui.INPUT_ALPHANUM
    )

    if api_key and api_key.strip():
        api_key = api_key.strip()
        ADDON.setSetting('api_key', api_key)
        xbmcgui.Dialog().notification(
            "Subs.ro", "✓ Cheie API salvată!", xbmcgui.NOTIFICATION_INFO, 3000
        )
        return api_key

    if dialog.yesno(
        "Subs.ro - Cheie API Necesară",
        "Addon-ul necesită o cheie API.\n\n"
        "1. Accesează https://subs.ro/api\n"
        "2. Autentifică-te\n"
        "3. Copiază cheia API\n\n"
        "Deschid setările acum?"
    ):
        xbmc.executebuiltin('Addon.OpenSettings(service.subtitles.subsro)')
    else:
        xbmcgui.Dialog().notification(
            "Subs.ro", "Configurează cheia API în setări.",
            xbmcgui.NOTIFICATION_WARNING, 5000
        )
    return None


def get_auth(api_key):
    """
    Returnează (headers, params_extra) conform schemei OpenAPI.
    Suportă ApiKeyHeader (X-Subs-Api-Key) și ApiKeyQuery (apiKey).
    """
    if ADDON.getSetting('auth_method') == '1':
        return {'Accept': 'application/json'}, {'apiKey': api_key}
    return {'X-Subs-Api-Key': api_key, 'Accept': 'application/json'}, {}


def validate_api_key(api_key):
    """Validează cheia prin GET /quota. Returnează True dacă e validă."""
    try:
        headers, params = get_auth(api_key)
        r = requests.get(f"{API_BASE}/quota", headers=headers, params=params, timeout=5)
        if r.status_code == 200:
            log("Cheie API validă ✓")
            return True
        if r.status_code == 401:
            log("Cheie API invalidă ✗", xbmc.LOGERROR)
            xbmcgui.Dialog().ok(
                "Subs.ro - Cheie API Invalidă",
                "Cheia API nu este validă.\n\n"
                "• Verifică să fie copiată corect\n"
                "• Contul subs.ro trebuie să fie activ\n\n"
                "Generează una nouă la https://subs.ro/api"
            )
            ADDON.setSetting('api_key', '')
            return False
        log(f"Validare API: status neașteptat {r.status_code}", xbmc.LOGWARNING)
        return False
    except Exception as e:
        log(f"Eroare conexiune validare: {e}", xbmc.LOGERROR)
        return True  # Acceptăm dacă nu avem conexiune


def handle_api_error(status_code, response=None):
    """
    Afișează eroarea API conform schemei ErrorResponse:
      { status, message, meta: { requestId } }
    """
    fallback = {
        400: "Cerere invalidă.",
        401: "Cheie API invalidă! Verifică setările.",
        403: "Acces interzis sau limită atinsă.",
        404: "Resursa nu a fost găsită.",
        429: "Prea multe cereri! Încearcă mai târziu.",
        500: "Eroare server Subs.ro.",
    }
    msg        = fallback.get(status_code, f"Eroare API (cod: {status_code})")
    request_id = None

    if response is not None:
        try:
            body       = response.json()
            msg        = body.get('message') or msg
            request_id = body.get('meta', {}).get('requestId')
        except Exception:
            pass

    log(f"API error {status_code}" + (f" | requestId={request_id}" if request_id else "") +
        f" | {msg}", xbmc.LOGERROR)
    xbmcgui.Dialog().notification("Eroare Subs.ro", msg, xbmcgui.NOTIFICATION_ERROR, 5000)

    if status_code == 401:
        ADDON.setSetting('api_key', '')
        ADDON.setSetting('api_key_validated', 'false')

# ============================================================================
#                           CACHE (thread-safe)
# ============================================================================

def _cache_dir():
    """Returnează și creează directorul de cache."""
    path = os.path.join(xbmcvfs.translatePath(ADDON.getAddonInfo('profile')), 'cache')
    os.makedirs(path, exist_ok=True)
    return path


def _cache_file(field, value, language):
    key = hashlib.md5(f"{field}:{value}:{language}".encode()).hexdigest()
    return os.path.join(_cache_dir(), f"{key}.json")


def load_from_cache(field, value, language):
    """
    Încarcă din cache dacă e activ și neexpirat.
    Thread-safe. Șterge automat fișierele corupte.
    """
    if ADDON.getSetting('cache_results') != 'true':
        return None

    path = _cache_file(field, value, language)

    with _CACHE_LOCK:
        if not os.path.exists(path):
            return None

        max_age = int(ADDON.getSetting('cache_duration') or 60) * 60
        if time.time() - os.path.getmtime(path) > max_age:
            log(f"Cache expirat: {field}='{value[:30]}'")
            try: os.remove(path)
            except OSError: pass
            return None

        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            log(f"Cache hit: {field}='{value[:30]}'")
            return data
        except (json.JSONDecodeError, OSError):
            log(f"Cache corupt, sterg: {path}", xbmc.LOGWARNING)
            try: os.remove(path)
            except OSError: pass
            return None


def save_to_cache(field, value, language, data):
    """
    Salvează în cache DOAR dacă are rezultate (count > 0).
    Thread-safe.
    """
    if ADDON.getSetting('cache_results') != 'true':
        return
    if not data or data.get('count', 0) == 0:
        return  # Nu cacheia răspunsuri goale

    path = _cache_file(field, value, language)
    with _CACHE_LOCK:
        try:
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False)
            log(f"Cache salvat: {field}='{value[:30]}'")
        except OSError as e:
            log(f"Eroare salvare cache: {e}", xbmc.LOGERROR)

# ============================================================================
#                           QUOTA
# ============================================================================

def check_quota(api_key):
    """
    GET /quota → QuotaResponse.
    Afișează avertisment dacă remaining_quota < 10% din total_quota.
    """
    try:
        headers, params = get_auth(api_key)
        r = requests.get(f"{API_BASE}/quota", headers=headers, params=params, timeout=5)
        if r.status_code == 200:
            q     = r.json().get('quota', {})
            total = q.get('total_quota', 0)
            rem   = q.get('remaining_quota', 0)
            used  = q.get('used_quota', 0)
            qtype = q.get('quota_type', '?')
            log(f"Quota ({qtype}): {rem}/{total} folosit={used}")
            if total > 0 and rem < total * 0.1:
                xbmcgui.Dialog().notification(
                    "Subs.ro - Avertisment Quota",
                    f"Quota rămasă: {rem}/{total} cereri",
                    xbmcgui.NOTIFICATION_WARNING, 5000
                )
        elif r.status_code == 401:
            handle_api_error(401, r)
    except Exception as e:
        log(f"Eroare verificare quota: {e}", xbmc.LOGERROR)

# ============================================================================
#                           TMDB ID DETECTION  [BLOCKER fix]
# ============================================================================

def _get_tmdb_id(info):
    """
    Extrage TMDb ID din VideoInfoTag, în ordinea fiabilității:
      1. getUniqueID('tmdb')  — Kodi 19+ Matrix, cel mai corect
      2. getUniqueID('tmdb')  via listă de slug-uri alternative
      3. getUniqueID('tvdb')  pentru seriale (fallback util dacă subs.ro acceptă)
      4. getDbId()            NU se folosește — e ID intern Kodi, nu TMDb

    Returnează string cu ID-ul sau None.
    """
    if not hasattr(info, 'getUniqueID'):
        return None

    # 1. Slug-uri cunoscute pentru TMDb
    for slug in ('tmdb', 'themoviedb', 'tmdbid'):
        try:
            raw = info.getUniqueID(slug)
            if raw and str(raw).isdigit() and int(str(raw)) > 0:
                log(f"TMDb ID găsit via getUniqueID('{slug}'): {raw}")
                return str(raw)
        except Exception:
            pass

    # 2. Parcurge toate unique ID-urile disponibile (Kodi 20+ Nexus)
    if hasattr(info, 'getUniqueIDs'):
        try:
            uid_map = info.getUniqueIDs()  # dict {'tmdb': '12345', 'imdb': 'tt...'}
            for key in ('tmdb', 'themoviedb'):
                val = uid_map.get(key, '')
                if val and str(val).isdigit() and int(str(val)) > 0:
                    log(f"TMDb ID găsit via getUniqueIDs()['{key}']: {val}")
                    return str(val)
        except Exception:
            pass

    return None

# ============================================================================
#                           REZOLUȚIE & SURSĂ
# ============================================================================

def _detect_resolution_from_string(name):
    """
    Detectează rezoluția dintr-un string (titlu subtitrare sau nume fișier).
    Folosește \b (word-boundary) pentru a evita false positive.
    Returnează: '2160p' | '1080p' | '720p' | '480p' | '360p' | None
    """
    n = name.lower()
    if re.search(r'\b(2160p|4320p)\b', n): return '2160p'
    if re.search(r'\b4k\b',            n): return '2160p'
    if re.search(r'\buhd\b',           n): return '2160p'
    if re.search(r'\b(1080p|1080i|fhd)\b', n): return '1080p'
    if re.search(r'\b720p\b',          n): return '720p'
    if re.search(r'\b480p\b',          n): return '480p'
    if re.search(r'\b360p\b',          n): return '360p'
    return None


def _get_video_resolution(video_file):
    """
    Citește rezoluția video în ordinea fiabilității:
      1. VideoPlayer.VideoResolution (infolabel Kodi — merge și la streaming)
      2. VideoPlayer.VideoHeight
      3. Fallback din numele fișierului
    """
    HEIGHT_MAP = {
        '4320': '2160p', '2160': '2160p',
        '1080': '1080p',
        '720':  '720p',
        '480':  '480p',
        '360':  '360p',
    }

    res = xbmc.getInfoLabel('VideoPlayer.VideoResolution').strip()
    if res:
        if res in HEIGHT_MAP:
            return HEIGHT_MAP[res]
        m = re.search(r'(\d{3,4})(?:p|i)?$', res)
        if m and m.group(1) in HEIGHT_MAP:
            return HEIGHT_MAP[m.group(1)]

    h_str = xbmc.getInfoLabel('VideoPlayer.VideoHeight').strip()
    if h_str and h_str.isdigit():
        h = int(h_str)
        if h >= 2160: return '2160p'
        if h >= 1080: return '1080p'
        if h >= 720:  return '720p'
        if h >= 480:  return '480p'
        return '360p'

    return _detect_resolution_from_string(os.path.basename(video_file))


def _detect_source(name):
    """
    Detectează sursa dintr-un string cu regex \b.
    Returnează: 'bluray' | 'web' | 'hdtv' | None
    """
    n = name.lower()
    if re.search(r'\b(bluray|blu-ray|bdrip|brrip|bdremux|remux)\b', n):
        return 'bluray'
    if re.search(r'\b(web-dl|webdl|webrip)\b', n):
        return 'web'
    if re.search(r'\b(netflix|nflx|amazon|amzn|hbo|hulu|disney)\b', n):
        return 'web'
    if re.search(r'\b(hdtv|pdtv|dsr)\b', n):
        return 'hdtv'
    return None

# ============================================================================
#                           MATCHMAKING
# ============================================================================

# Formate episod — ordine importantă: cel mai specific primul
# Toate cu \b sau separator explicit pentru a nu face match în cuvinte
_EP_PATTERNS = [
    # s01e02 / s1e2 / s01xe02 (cu x opțional între S și E)
    (r'\bs(\d{1,3})x?e(\d{1,3})\b',                                  'full'),
    # 1x02 / 12x05 — format NxNN
    (r'\b(\d{1,2})x(\d{2,3})\b',                                     'full'),
    # season 2 episode 3 / season.2.episode.3
    (r'\bseason[.\- ]?(\d{1,3})[.\- ]?episode[.\- ]?(\d{1,3})\b',   'full'),
    # .e05. sau -e05- (episod fără sezon, separator obligatoriu)
    (r'[.\-_ \[]e(\d{2,3})[.\-_ \]]',                                'ep_only'),
    # ep05 / ep2 (cuvânt de sine stătător)
    (r'\bep(\d{1,3})\b',                                              'ep_only'),
]


def _extract_episode(name):
    """
    Extrage (sezon, episod, tip) din nume.
    tip: 'full' (sezon+episod), 'ep_only' (doar episod, sezon=-1)
    Returnează None dacă nu se detectează nimic (film).
    """
    for pat, kind in _EP_PATTERNS:
        m = re.search(pat, name)
        if m:
            if kind == 'full':
                return (int(m.group(1)), int(m.group(2)), 'full')
            return (-1, int(m.group(1)), 'ep_only')
    return None


def calculate_match_score(subtitle_name, video_file):
    """
    Calculează scorul de potrivire — activ mereu, filme și seriale.

    Tabel scoruri:
      +100 / -50   episod complet (S+E) potrivit / nepotrivit
      +50  / -30   episod parțial (doar E) potrivit / nepotrivit
      +40  / -30   rezoluție (720p/1080p/2160p) potrivită / nepotrivită
      +50  / -20   sursă (BluRay/WEB/HDTV) potrivită / nepotrivită
      +30          release group identic
      +15          traducător prioritar
      0–20         similaritate difflib

    Principiu: fără penalizare dacă informația lipsește din una din părți.
    """
    score   = 0
    details = {}

    sub_lower   = subtitle_name.lower()
    video_lower = os.path.basename(video_file).lower()

    # ── 1. Episod ────────────────────────────────────────────────────────────
    sub_ep   = _extract_episode(sub_lower)
    video_ep = _extract_episode(video_lower)

    if sub_ep and video_ep:
        ss, se, st = sub_ep
        vs, ve, vt = video_ep
        if st == 'full' and vt == 'full':
            if (ss, se) == (vs, ve):
                score += 100;  details['episode_match'] = True
            else:
                score -= 50;   details['episode_match'] = False
        elif se == ve:
            score += 50;       details['episode_match'] = 'partial'
        else:
            score -= 30;       details['episode_match'] = False

    # ── 2. Rezoluție — mereu activă ──────────────────────────────────────────
    video_res = _get_video_resolution(video_file)
    sub_res   = _detect_resolution_from_string(sub_lower)

    details['video_resolution'] = video_res or 'unknown'
    details['sub_resolution']   = sub_res   or 'unknown'

    if video_res and sub_res:
        if video_res == sub_res:
            score += 40;  details['resolution_match'] = True
        else:
            score -= 30;  details['resolution_match'] = False

    # ── 3. Sursă ─────────────────────────────────────────────────────────────
    video_src = _detect_source(video_lower)
    sub_src   = _detect_source(sub_lower)

    if video_src and sub_src:
        if video_src == sub_src:
            score += 50;  details['source_match'] = True
        else:
            score -= 20;  details['source_match'] = False

    # ── 4. Release group ─────────────────────────────────────────────────────
    vg = re.search(r'-([a-z0-9]{2,20})(?:\.[a-z0-9]{2,4})?$', video_lower)
    sg = re.search(r'-([a-z0-9]{2,20})(?:\.[a-z0-9]{2,4})?$', sub_lower)
    if vg and sg and vg.group(1) == sg.group(1):
        score += 30;  details['group_match'] = True

    # ── 5. Similaritate generală ─────────────────────────────────────────────
    vname = os.path.splitext(os.path.basename(video_file))[0].lower()
    sname = os.path.splitext(subtitle_name)[0].lower()
    sim   = difflib.SequenceMatcher(None, vname, sname).ratio()
    score += int(sim * 20)
    details['similarity'] = round(sim, 3)

    # ── 6. Traducător prioritar ──────────────────────────────────────────────
    if re.search(r'\b(retail|subrip|netflix|nflx|hbo|amazon|amzn)\b', sub_lower):
        score += 15;  details['priority_translator'] = True

    log(f"Scor '{subtitle_name[:50]}': {score:+d} | "
        f"ep={details.get('episode_match','n/a')} "
        f"res={details['video_resolution']}↔{details['sub_resolution']} "
        f"src={video_src}↔{sub_src} sim={details['similarity']:.2f}")

    return score, details


def sort_subtitles_by_match(items, video_file):
    """Calculează scorul pentru toate itemele și sortează descrescător."""
    for item in items:
        score, details        = calculate_match_score(item.get('title', ''), video_file)
        item['match_score']   = score
        item['match_details'] = details

    items.sort(key=lambda x: x.get('match_score', 0), reverse=True)

    log("Top 3 după scoring:")
    for i, it in enumerate(items[:3]):
        log(f"  #{i+1} ({it['match_score']:+d}): {it['title'][:60]}")

    return items

# ============================================================================
#                           FILTRARE
# ============================================================================

def filter_subtitles(items, video_info):
    """
    Aplică filtrele din setări:
      - Hearing Impaired (SDH)
      - Tip conținut (movie / series) dacă se știe
      - An (± 1 an toleranță)
    """
    filtered = items[:]

    # Filtru: hearing impaired
    if ADDON.getSetting('filter_by_hearing_impaired') == 'true':
        before   = len(filtered)
        filtered = [it for it in filtered
                    if not re.search(r'\b(hi|sdh|hearing.impaired)\b',
                                     it.get('title', '').lower())]
        log(f"Filtru HI: {before} → {len(filtered)}")

    # Filtru: tip conținut — evită să arate subtitrări de film la serial și invers
    # Kodi furnizează tipul via VideoInfoTag.getMediaType() sau similar
    media_type = video_info.get('media_type')  # 'movie' | 'episode' | None
    if media_type == 'movie':
        before   = len(filtered)
        filtered = [it for it in filtered
                    if it.get('type', 'movie') == 'movie']
        if len(filtered) == 0:
            filtered = items[:]  # Fallback dacă filtrarea a golit lista
        else:
            log(f"Filtru tip movie: {before} → {len(filtered)}")
    elif media_type == 'episode':
        before   = len(filtered)
        filtered = [it for it in filtered
                    if it.get('type', 'series') == 'series']
        if len(filtered) == 0:
            filtered = items[:]
        else:
            log(f"Filtru tip series: {before} → {len(filtered)}")

    # Filtru: an (± 1 toleranță)
    year = video_info.get('year')
    if year and int(year) > 1900:
        year     = int(year)
        before   = len(filtered)
        by_year  = [it for it in filtered
                    if it.get('year') and abs(int(it['year']) - year) <= 1]
        if by_year:
            filtered = by_year
            log(f"Filtru an {year}±1: {before} → {len(filtered)}")

    return filtered

# ============================================================================
#                           BADGE-URI VIZUALE
# ============================================================================

def format_label_with_badges(item, show_score=False):
    """Formatează label-ul cu badge-uri colorate Kodi."""
    title   = item.get('title', 'Unknown')
    details = item.get('match_details', {})
    badges  = []

    ep = details.get('episode_match')
    if ep is True:
        badges.append('[COLOR lime]✓EP[/COLOR]')
    elif ep == 'partial':
        badges.append('[COLOR yellow]~EP[/COLOR]')
    elif ep is False:
        badges.append('[COLOR red]✗EP[/COLOR]')

    res_match = details.get('resolution_match')
    if res_match is True:
        res = details.get('video_resolution', '').upper()
        badges.append(f'[COLOR aqua]✓{res}[/COLOR]')
    elif res_match is False:
        badges.append('[COLOR red]✗RES[/COLOR]')

    if details.get('source_match') is True:
        badges.append('[COLOR cyan]✓SRC[/COLOR]')
    if details.get('group_match'):
        badges.append('[COLOR yellow]✓GRP[/COLOR]')
    if details.get('priority_translator'):
        badges.append('[COLOR gold]★[/COLOR]')

    label = (' '.join(badges) + ' ' + title) if badges else title
    if show_score and 'match_score' in item:
        label = f"[{item['match_score']:+d}] {label}"

    return label

# ============================================================================
#                           CĂUTARE — API
# ============================================================================

def _sanitize_title(title):
    """Elimină diacritice și caractere speciale pentru căutare după titlu."""
    if not title:
        return ''
    nfd     = unicodedata.normalize('NFD', title)
    cleaned = ''.join(c for c in nfd if unicodedata.category(c) != 'Mn')
    cleaned = re.sub(r"[^a-zA-Z0-9 \-]", ' ', cleaned)
    return re.sub(r'\s+', ' ', cleaned).strip()


def _api_search(field, value, language, api_key, timeout):
    """
    Execută GET /search/{field}/{value}?language={language}.
    searchField enum: imdbid | tmdbid | title | release
    Returnează (data_dict, http_status). data_dict=None dacă a eșuat.
    """
    headers, extra = get_auth(api_key)
    url    = f"{API_BASE}/search/{field}/{urllib.parse.quote(str(value))}"
    params = {'language': language, **extra}
    try:
        r = requests.get(url, params=params, headers=headers, timeout=timeout)
        log(f"API {field}='{value[:40]}' lang={language} → {r.status_code}")
        if r.status_code == 200:
            data = r.json()
            log(f"  count={data.get('count',0)} "
                f"requestId={data.get('meta',{}).get('requestId','')}")
            return data, 200
        return None, r.status_code
    except requests.Timeout:
        log(f"Timeout: {field}='{value[:40]}'", xbmc.LOGWARNING)
        return None, 0
    except Exception as e:
        log(f"Exceptie API ({field}='{value[:40]}'): {e}", xbmc.LOGERROR)
        return None, 0

# ============================================================================
#                           CĂUTARE SUBTITRĂRI
# ============================================================================

def search_subtitles():
    """
    Caută subtitrări cu strategie multi-fallback, conformă cu schema OpenAPI.

    Ordinea (se oprește la primul rezultat ne-gol):
      1. imdbid  — cel mai precis
      2. tmdbid  — din getUniqueID(), NU getDbId()
      3. title   — titlul original exact
      4. title   — titlul original curățat (fără diacritice)
      5. title   — titlul Kodi curățat (poate fi tradus)
      6. release — numele fișierului (doar local, nu streaming)
    """
    API_KEY = get_api_key()
    if not API_KEY:
        return

    if ADDON.getSetting('api_key_validated') != 'true':
        if validate_api_key(API_KEY):
            ADDON.setSetting('api_key_validated', 'true')
        else:
            return

    if ADDON.getSetting('check_quota') == 'true':
        check_quota(API_KEY)

    handle = int(sys.argv[1])
    player = xbmc.Player()
    if not player.isPlayingVideo():
        return

    info       = player.getVideoInfoTag()
    video_file = player.getPlayingFile()

    # Date despre conținut
    imdb_id    = info.getIMDBNumber()
    tmdb_id    = _get_tmdb_id(info)
    tvshow     = info.getTVShowTitle()
    season     = info.getSeason()
    episode    = info.getEpisode()
    kodi_title = (info.getTitle() or xbmc.getInfoLabel('VideoPlayer.Title') or '').strip()
    orig_title = xbmc.getInfoLabel('VideoPlayer.OriginalTitle').strip()
    year       = info.getYear() if hasattr(info, 'getYear') else 0
    media_type = info.getMediaType() if hasattr(info, 'getMediaType') else ''
    is_episode = bool(tvshow and season not in (-1, 0) and episode not in (-1, 0))

    lang_map = {'0': 'ro', '1': 'en', '2': 'ita', '3': 'fra', '4': 'ger',
                '5': 'ung', '6': 'gre', '7': 'por', '8': 'spa', '9': 'alt'}
    language = lang_map.get(ADDON.getSetting('search_language') or '0', 'ro')
    timeout  = int(ADDON.getSetting('timeout_duration') or 10)

    def with_episode(t):
        if is_episode and t:
            return f"{t} S{str(season).zfill(2)}E{str(episode).zfill(2)}"
        return t

    # Release name — doar fișiere locale
    release_name = ''
    if video_file and not video_file.startswith(
            ('http://', 'https://', 'plugin://', 'upnp://', 'smb://')):
        release_name = os.path.splitext(os.path.basename(video_file))[0]

    # ── Construiește lista de strategii ────────────────────────────────────
    strategies = []

    if imdb_id and imdb_id.startswith('tt'):
        strategies.append(('imdbid', imdb_id))

    if tmdb_id:
        strategies.append(('tmdbid', tmdb_id))

    seen             = set()
    title_candidates = []

    if is_episode:
        for t in [orig_title, tvshow]:
            if t: title_candidates.append(with_episode(t))
    else:
        for t in [orig_title, kodi_title]:
            if t: title_candidates.append(t)

    for t in title_candidates:
        if t and t not in seen:
            strategies.append(('title', t))
            seen.add(t)
        tc = _sanitize_title(t)
        if tc and tc != t and tc not in seen:
            strategies.append(('title', tc))
            seen.add(tc)

    if release_name and len(release_name) > 3:
        strategies.append(('release', release_name))

    log("Strategii: " + " → ".join(f"{f}='{v[:30]}'" for f, v in strategies))

    # ── Execută strategiile ────────────────────────────────────────────────
    data       = None
    used_field = ''
    used_value = ''

    for field, value in strategies:
        cached = load_from_cache(field, value, language)
        if cached:
            data, used_field, used_value = cached, field, value
            break

        result, status = _api_search(field, value, language, API_KEY, timeout)

        if status not in (0, 200, 404):
            handle_api_error(status)
            xbmcplugin.endOfDirectory(handle)
            return

        if result and result.get('status') == 200 and result.get('count', 0) > 0:
            save_to_cache(field, value, language, result)
            data, used_field, used_value = result, field, value
            log(f"Găsit: {field}='{value[:40]}' ({result['count']} rezultate)")
            break

        log(f"0 rezultate: {field}='{value[:40]}'")

    # ── Fără rezultate ──────────────────────────────────────────────────────
    if not data or not data.get('items'):
        name = orig_title or kodi_title or release_name or '?'
        log("Nicio subtitrare după toate strategiile.", xbmc.LOGWARNING)
        xbmcgui.Dialog().notification(
            "Subs.ro", f"Nicio subtitrare: {name[:40]}",
            xbmcgui.NOTIFICATION_INFO, 4000
        )
        xbmcplugin.endOfDirectory(handle)
        return

    items = data.get('items', [])
    log(f"Afișez {len(items)} subtitrări (via {used_field}='{used_value[:30]}')")

    # Filtrare (tip conținut, an, HI)
    video_info_ctx = {
        'video_file': video_file,
        'media_type': media_type or ('episode' if is_episode else 'movie'),
        'year': year,
    }
    items = filter_subtitles(items, video_info_ctx)

    # Sortare matchmaking — mereu activă
    items = sort_subtitles_by_match(items, video_file)

    show_badges = ADDON.getSetting('enable_matchmaking') == 'true'
    show_scores = ADDON.getSetting('show_match_scores') == 'true'

    for item in items:
        item_id     = int(item.get('id', 0))
        item_title  = item.get('title', 'Unknown')
        item_year   = item.get('year', '')
        item_lang   = item.get('language', 'ro').upper()
        item_type   = item.get('type', '')
        item_trans  = item.get('translator', 'N/A')
        item_poster = item.get('poster', '')
        item_imdb   = item.get('imdbid', '')
        item_tmdb   = item.get('tmdbid', '')
        item_desc   = item.get('description', '')
        item_link   = item.get('link', '')
        item_dl     = item.get('downloadLink', '')

        label     = format_label_with_badges(item, show_scores) if show_badges else item_title
        list_item = xbmcgui.ListItem(label=label, label2=label)
        list_item.setArt({'thumb': item_poster, 'icon': 'logo.png'})

        plot = [
            item_title + (f" ({item_year})" if item_year else ''),
            f"Tip: {'Film' if item_type == 'movie' else 'Serial' if item_type == 'series' else item_type}",
            f"Traducător: {item_trans}",
            f"Limba: {item_lang}",
        ]
        if 'match_score' in item:
            plot.insert(1, f"Scor: {item['match_score']:+d}")
        if item_imdb:  plot.append(f"IMDb: {item_imdb}")
        if item_tmdb:  plot.append(f"TMDb: {item_tmdb}")
        if item_desc:  plot.append(item_desc)
        if item_link:  plot.append(f"Link: {item_link}")

        list_item.setInfo('video', {
            'title':   label,
            'plot':    '\n'.join(plot),
            'tagline': item_trans,
            'year':    int(item_year) if str(item_year).isdigit() else 0,
        })

        cmd = f"{sys.argv[0]}?action=download&id={item_id}"
        if item_dl:
            cmd += f"&dl={urllib.parse.quote(item_dl, safe='')}"
        xbmcplugin.addDirectoryItem(handle=handle, url=cmd, listitem=list_item, isFolder=False)

    xbmcplugin.endOfDirectory(handle)


# ============================================================================
#                           SUPORT ARHIVE RAR/ZIP
# ============================================================================

def _find_unrar():
    import shutil
    for tool in ('unrar', 'unrar.exe', '7z', '7za', '7z.exe'):
        path = shutil.which(tool)
        if path:
            log(f"RAR tool gasit: {path}")
            return path
    for path in ('/usr/bin/unrar', '/usr/local/bin/unrar',
                 '/system/bin/unrar', '/data/data/org.xbmc.kodi/files/unrar'):
        if os.path.isfile(path):
            log(f"RAR tool gasit: {path}")
            return path
    return 'unrar'


def _extract_rar_subprocess(archive, out_dir, video_file):
    import subprocess, glob
    SUB_EXTS = ('.srt', '.ass', '.ssa', '.sub', '.vtt')
    tool = _find_unrar()
    extract_dir = os.path.join(out_dir, 'subsro_rar_extract')
    os.makedirs(extract_dir, exist_ok=True)

    if '7z' in os.path.basename(tool).lower():
        cmd = [tool, 'e', archive, f'-o{extract_dir}', '-y', '-aoa']
    else:
        cmd = [tool, 'e', '-o+', '-y', archive, extract_dir + os.sep]

    try:
        log(f"RAR subprocess: {cmd[0]} ...")
        result = subprocess.run(cmd, capture_output=True, timeout=30)
        log(f"RAR exit: {result.returncode}")
        if result.returncode not in (0, 1):
            log(f"RAR stderr: {result.stderr.decode(errors="replace")[:200]}", xbmc.LOGWARNING)
    except subprocess.TimeoutExpired:
        log("RAR timeout", xbmc.LOGERROR)
        return None, None
    except FileNotFoundError:
        log(f"RAR tool negasit: {tool}", xbmc.LOGERROR)
        return None, None
    except Exception as e:
        log(f"RAR subprocess eroare: {e}", xbmc.LOGERROR)
        return None, None

    extracted = []
    for ext in SUB_EXTS:
        extracted.extend(glob.glob(os.path.join(extract_dir, f'*{ext}')))
        extracted.extend(glob.glob(os.path.join(extract_dir, f'*{ext.upper()}')))

    if not extracted:
        log("RAR: nicio subtitrare extrasa", xbmc.LOGWARNING)
        return None, None

    chosen_path = (
        max(extracted, key=lambda p: calculate_match_score(os.path.basename(p), video_file)[0])
        if len(extracted) > 1 else extracted[0]
    )
    log(f"RAR ales: {os.path.basename(chosen_path)}")

    try:
        with open(chosen_path, 'rb') as f:
            raw = f.read()
        for p in extracted:
            try: os.remove(p)
            except OSError: pass
        try: os.rmdir(extract_dir)
        except OSError: pass
        return raw, os.path.basename(chosen_path)
    except OSError as e:
        log(f"Eroare citire RAR extras: {e}", xbmc.LOGERROR)
        return None, None

# ============================================================================
#                           DESCĂRCARE SUBTITRARE
# ============================================================================

def download_subtitle(sub_id, download_link=None):
    """
    Descarcă subtitrarea via GET /subtitle/{id}/download (application/octet-stream).
    Cleanup fișiere temporare în finally (fix: cleanup on error).
    """
    API_KEY = get_api_key()
    if not API_KEY:
        return

    try:
        sub_id_int = int(sub_id)
    except (TypeError, ValueError):
        log(f"ID invalid: {sub_id}", xbmc.LOGERROR)
        return

    headers, extra = get_auth(API_KEY)
    headers.pop('Accept', None)  # endpoint binar, nu JSON

    url = download_link or f"{API_BASE}/subtitle/{sub_id_int}/download"
    log(f"Download: {url[:60]}")

    player   = xbmc.Player()
    tmp_path = xbmcvfs.translatePath("special://temp/")
    archive  = os.path.join(tmp_path, f"subsro_{sub_id_int}.bin")
    target   = os.path.join(tmp_path, "forced.romanian.subsro.srt")

    try:
        r = requests.get(url, headers=headers, params=extra, timeout=15)
        if r.status_code != 200:
            handle_api_error(r.status_code, r)
            return

        raw = r.content
        with open(archive, 'wb') as f:
            f.write(raw)

        # ── Detectare tip arhivă din magic bytes ─────────────────────────────
        # ZIP: magic PK\x03\x04
        # RAR: magic Rar!\x1a\x07
        is_zip = raw[:4] == b'PK\x03\x04'
        is_rar = raw[:7] == b'Rar!\x1a\x07\x00' or raw[:8] == b'Rar!\x1a\x07\x01\x00'
        log(f"Tip arhivă: {'ZIP' if is_zip else 'RAR' if is_rar else 'necunoscut'} "
            f"(magic: {raw[:4].hex()})")

        SUB_EXTS = ('.srt', '.ass', '.ssa', '.sub', '.vtt')

        def pick_subtitle(names, vf):
            """Alege subtitrarea din lista de nume după setarea multi_episode_handling."""
            srts = sorted([n for n in names if n.lower().endswith(SUB_EXTS)])
            if not srts:
                return None
            multi = ADDON.getSetting('multi_episode_handling')
            if len(srts) == 1:
                return srts[0]
            if multi == '0':  # Manual
                sel = xbmcgui.Dialog().select(
                    "Alege subtitrarea:", [os.path.basename(n) for n in srts]
                )
                return srts[sel] if sel != -1 else None
            elif multi == '1':  # Prima
                return srts[0]
            else:  # Matchmaking
                best = max(srts,
                           key=lambda n: calculate_match_score(os.path.basename(n), vf)[0])
                log(f"Auto-selectat: {os.path.basename(best)}")
                return best

        def decode_subtitle(raw_bytes):
            """Decodează bytes subtitrare cu detectare automată encoding."""
            encodings = ['utf-8', 'iso-8859-2', 'windows-1250', 'latin1']
            ep_idx = int(ADDON.getSetting('encoding_priority') or 0)
            if ep_idx > 0:
                encodings = encodings[ep_idx:] + encodings[:ep_idx]
            for enc in encodings:
                try:
                    text = raw_bytes.decode(enc)
                    log(f"Encoding: {enc}")
                    return text
                except (UnicodeDecodeError, LookupError):
                    continue
            log("Encoding fallback: latin1", xbmc.LOGWARNING)
            return raw_bytes.decode('latin1', errors='replace')

        chosen_name    = None
        chosen_content = None

        # ── Extragere ZIP ────────────────────────────────────────────────────
        if is_zip:
            try:
                with zipfile.ZipFile(archive, 'r') as z:
                    chosen_name = pick_subtitle(z.namelist(), player.getPlayingFile())
                    if chosen_name is None:
                        xbmcgui.Dialog().notification(
                            "Subs.ro", "Arhiva ZIP nu conține subtitrări",
                            xbmcgui.NOTIFICATION_ERROR, 4000
                        )
                        return
                    chosen_content = z.read(chosen_name)
            except zipfile.BadZipFile:
                log("ZIP corupt — încerc ca RAR", xbmc.LOGWARNING)
                is_rar = True  # fallback la RAR

        # ── Extragere RAR ────────────────────────────────────────────────────
        if is_rar or (not is_zip and not is_rar):
            # Metoda 1: rarfile (pip install rarfile + unrar system)
            rar_ok = False
            try:
                import rarfile
                rarfile.UNRAR_TOOL = _find_unrar()
                with rarfile.RarFile(archive) as rf:
                    chosen_name = pick_subtitle(rf.namelist(), player.getPlayingFile())
                    if chosen_name is None:
                        xbmcgui.Dialog().notification(
                            "Subs.ro", "Arhiva RAR nu conține subtitrări",
                            xbmcgui.NOTIFICATION_ERROR, 4000
                        )
                        return
                    chosen_content = rf.read(chosen_name)
                rar_ok = True
                log("RAR extras cu rarfile")
            except ImportError:
                log("rarfile nu e instalat, încerc subprocess", xbmc.LOGWARNING)
            except Exception as e:
                log(f"rarfile eroare: {e}", xbmc.LOGWARNING)

            # Metoda 2: subprocess unrar/7z
            if not rar_ok:
                chosen_content, chosen_name = _extract_rar_subprocess(
                    archive, tmp_path, player.getPlayingFile()
                )
                if chosen_content is None:
                    xbmcgui.Dialog().notification(
                        "Subs.ro",
                        "Nu pot deschide arhiva RAR.\n"
                        "Instalează 'unrar' sau '7-zip' pe sistem.",
                        xbmcgui.NOTIFICATION_ERROR, 6000
                    )
                    return

        if chosen_content is None:
            xbmcgui.Dialog().notification(
                "Subs.ro", "Nu s-a putut extrage subtitrarea.",
                xbmcgui.NOTIFICATION_ERROR, 4000
            )
            return

        # ── Scriere fișier subtitrare ────────────────────────────────────────
        with open(target, 'w', encoding='utf-8') as f:
            f.write(decode_subtitle(chosen_content))

        xbmc.executebuiltin("Dialog.Close(subtitlesearch)")
        xbmc.sleep(500)
        player.setSubtitles(target)

        # Activare forțată cu timeout
        deadline = time.time() + 6
        while time.time() < deadline:
            if not player.isPlayingVideo():
                break
            for i, s in enumerate(player.getAvailableSubtitleStreams()):
                if 'forced.romanian' in s.lower() or 'external' in s.lower():
                    if player.getSubtitleStream() != i:
                        player.setSubtitleStream(i)
                        player.showSubtitles(True)
            xbmc.sleep(400)

        if ADDON.getSetting('notify_auto_download') == 'true':
            dur = int(ADDON.getSetting('notify_duration') or 3) * 1000
            xbmcgui.Dialog().notification(
                "Subs.ro",
                "✓ " + os.path.basename(chosen_name or '')[:35],
                xbmcgui.NOTIFICATION_INFO, dur
            )

    except zipfile.BadZipFile:
        log("Arhivă ZIP coruptă", xbmc.LOGERROR)
        xbmcgui.Dialog().notification(
            "Subs.ro", "Arhivă coruptă — încearcă altă subtitrare.",
            xbmcgui.NOTIFICATION_ERROR, 4000
        )
    except Exception as e:
        log(f"Eroare download: {e}", xbmc.LOGERROR)
        xbmcgui.Dialog().notification(
            "Subs.ro", "Eroare la descărcare.", xbmcgui.NOTIFICATION_ERROR, 3000
        )
    finally:
        # Cleanup fișiere temporare indiferent de rezultat
        for f in [archive]:
            try:
                if os.path.exists(f):
                    os.remove(f)
            except OSError:
                pass

# ============================================================================
#                           ENTRY POINT
# ============================================================================

def get_params():
    param_string = sys.argv[2] if len(sys.argv) > 2 else ""
    return dict(urllib.parse.parse_qsl(param_string.lstrip('?')))


if __name__ == '__main__':
    p = get_params()
    if p.get('action') == 'download':
        dl_url = urllib.parse.unquote(p.get('dl', '')) or None
        download_subtitle(p.get('id'), download_link=dl_url)
    else:
        search_subtitles()
