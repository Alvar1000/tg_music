"""Контент заданий рок-дуэли."""
import json
import random
from itertools import combinations
from pathlib import Path
from urllib.parse import quote

import config


def _entries(items):
    artists = {}
    for item in items:
        if not isinstance(item, dict) or not item.get("key") or not item.get("name"):
            continue
        photo = Path(item.get("photo") or "")
        image = None
        if photo.parent == Path("content/tournament_covers") and (config.BASE_DIR / photo).is_file():
            image = "/api/tournament/images/" + quote(photo.name)
        artists[item["key"]] = {"key": item["key"], "name": item["name"],
                               "aliases": item.get("aliases", []), "photo": image}
        if item.get("band"):
            artists[item["key"]]["band"] = item["band"]
    return artists


def _load_tales(artists, people):
    for person in people.values():
        band = artists.get(person.get("band"))
        if band:
            person["band_name"] = band["name"]
            person["photo"] = person["photo"] or band["photo"]
    everyone = artists | people
    content = config.load_content("duel_tales.json", default={})
    cards = {}
    for item in content.get("tales", []) if isinstance(content, dict) else []:
        if not isinstance(item, dict) or item.get("review_hold") or type(item.get("id")) is not int:
            continue
        choices = artists if item.get("band_only") else people if item.get("person_only") else everyone
        key = item.get("answer")
        if key not in choices or not item.get("text") or not item.get("reveal"):
            continue
        group = choices[key].get("band") or key
        accepted = {key}
        if choices is everyone:
            accepted.update([group, *item.get("also", [])])
            accepted.update(p["key"] for p in people.values() if p.get("band") == key)
        cards[str(item["id"])] = {
            "artist": group, "solution": key, "accepted": accepted, "choices": choices,
            "prompt": "Что за группа?" if item.get("ask") == "band" else "Кто это?",
            "text": item["text"], "tags": item.get("tags", []), "reveal": item["reveal"],
        }
    return cards


def load_rounds():
    """Возвращает карточки по ключу и варианты для поиска. Тексты остаются в JSON."""
    directory = config.load_content("duel_dictionary.json", default={})
    if not isinstance(directory, dict):
        directory = {}
    artists = _entries(directory.get("artists", []))
    people = _entries(directory.get("people", []))
    content = config.load_content("duel_detective.json", default={})
    cards = {}
    for item in content.get("dossiers", []) if isinstance(content, dict) else []:
        if not isinstance(item, dict) or item.get("key") not in artists:
            continue
        facts = [f["text"] for f in item.get("facts", []) if isinstance(f, dict) and isinstance(f.get("text"), str) and f["text"].strip()]
        if len(facts) == 5:
            cards[item["key"]] = {"artist": item["key"], "facts": facts, "prompt": "Кто это?" if item.get("ask") == "who" else "Что это за группа?"}
    covers = {}
    for index, cover in enumerate(cover_catalog()):
        path = cover_path(cover)
        if path is None or cover.get("key") not in artists or not isinstance(cover.get("album"), str) or not cover["album"].strip():
            continue
        covers[path.name] = {"artist": cover["key"], "album": cover["album"],
                             "prompt": "Кто записал этот альбом?", "image": f"/duel/covers/{index}"}
    chronology = config.load_content("duel_chronology.json", default={})
    years = {}
    if isinstance(chronology, dict) and isinstance(chronology.get("bands"), list):
        for band in chronology["bands"]:
            key = band.get("key") if isinstance(band, dict) else None
            if isinstance(key, str) and key in artists and type(band.get("year")) is int:
                years[key] = band["year"]
    gap = chronology.get("min_gap", 1) if isinstance(chronology, dict) else 1
    performers = artists.keys() | people.keys()
    intros = {}
    for track in intro_catalog():
        path = intro_path(track)
        if path is None or track.get("artist_key") not in performers or not isinstance(track.get("id"), str):
            continue
        if track.get("artist") and track.get("title"):
            intros[track["id"]] = {"artist": track["artist_key"], "song": f"{track['artist']} — {track['title']}"}
    return {"detective": cards, "cover": covers, "intro": intros, "tale": _load_tales(artists, people),
            "chronology": {"years": years, "min_gap": gap}}, artists


def pick_chronology(pool, used_artists, seen):
    """Подбирает две непересекающиеся четвёрки; порядок показа один на обоих игроков."""
    gap = pool.get("min_gap")
    if type(gap) is not int or gap < 1:
        return []
    years = pool["years"]
    available = sorted(years.keys() - used_artists, key=years.get)

    def fits_four(keys):
        # Самая ранняя подходящая группа оставляет максимум места следующим.
        last = None
        count = 0
        for key in keys:
            if last is None or years[key] - last >= gap:
                last = years[key]
                count += 1
                if count == 4:
                    return True
        return False

    tasks = []
    for remaining in (4, 0):
        if len(available) < 4 + remaining or not fits_four(available):
            return []
        shuffled = random.sample(available, len(available))
        oldest = None
        chosen = None
        for group in combinations(shuffled, 4):
            correct = sorted(group, key=years.get)
            if any(years[b] - years[a] < gap for a, b in zip(correct, correct[1:])):
                continue
            if remaining and not fits_four(k for k in available if k not in group):
                continue
            source = json.dumps(sorted(group))
            stamp = seen.get(("chronology", source))
            if oldest is None or stamp is None or stamp < oldest:
                chosen = (source, list(group))
                oldest = stamp
            if stamp is None:
                break
        if chosen is None:
            return []
        source, order = chosen
        if order == sorted(order, key=years.get):
            order[0], order[1] = order[1], order[0]
        tasks.append((source, order))
        available = [k for k in available if k not in order]
    return tasks


def cover_catalog():
    catalog = config.load_content("duel_covers.json", default={})
    covers = catalog.get("covers") if isinstance(catalog, dict) else None
    return covers if isinstance(covers, list) else []


def intro_catalog():
    catalog = config.load_content("duel_intros.json", default={})
    tracks = catalog.get("tracks") if isinstance(catalog, dict) else None
    return [t for t in tracks if isinstance(t, dict) and t.get("enabled") is True] if isinstance(tracks, list) else []


def intro_path(track):
    name = track.get("file")
    if not isinstance(name, str) or Path(name).name != name or Path(name).suffix.lower() != ".mp3":
        return None
    path = config.CONTENT_DIR / "duel_intros" / name
    return path if path.is_file() and not path.is_symlink() else None


def cover_path(item):
    """Берём только обработанные картинки непосредственно из папки обложек."""
    name = item.get("file") if isinstance(item, dict) else None
    if not isinstance(name, str) or not name or Path(name).name != name:
        return None
    path = config.CONTENT_DIR / "duel_covers" / name
    if path.suffix.lower() not in {".jpg", ".jpeg", ".png"} or path.is_symlink() or not path.is_file():
        return None
    return path
