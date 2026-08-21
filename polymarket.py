"""Polymarket: найти наш матч, снять стакан, сказать — выгоднее ли там.

Правило Владислава от 20.08.2026, дословно: «сравнивать будем с лучшей ценой
входа на БК, делаем таким образом чтобы цена была лучше минимум на 5%».

То есть база сравнения — НЕ справедливая цена и не витрина Polymarket, а то,
что мы реально можем взять у конторы прямо сейчас: `entry_price` нашего
сигнала. Порог — плюс пять процентов сверху. Всё остальное в этом модуле
обслуживает этот один вопрос.

ПОЧЕМУ ЭТО НЕ РАЗОВАЯ ПРОВЕРКА, А СЛЕЖЕНИЕ. Замер 20.08: все 1200 открытых
футбольных событий Polymarket укладываются в трое суток, а наши сигналы
срабатывают за 26–44 часа до старта. В момент сигнала рынка там часто ещё
физически нет. Владислав на это ответил: «то что матчи не выставляют
заблаговременно — похуй, мы будем делать свою работу и искать эту линию». Так
и сделано: сигнал остаётся в очереди до самого стартового свистка, и каждый
раз, когда мы к нему возвращаемся, линия могла появиться или сдвинуться в нашу
сторону. Мы не спрашиваем один раз — мы ждём свою цену.

ЧАСТОТА ПОДСТРАИВАЕТСЯ САМА, см. `due_in_minutes`. Дёргать бесплатный API раз
в пять минут за сутки до матча, которого там ещё нет, — это шум; дёргать раз в
два часа за двадцать минут до старта — это проспать зазор. Лестница по времени
до старта решает обе задачи.

ДВА ПУБЛИЧНЫХ БЕСПЛАТНЫХ ЭНДПОИНТА, квоту The Odds API не трогают вообще:
  Gamma https://gamma-api.polymarket.com/events   — индекс событий и токены
  CLOB  https://clob.polymarket.com/book          — настоящая глубина

Витрине (`outcomePrices`) не верим ни при каких обстоятельствах. Замер на живом
рынке Швёнтек–Рыбакина 20.08: витрина 0.415 (коэф 2.410), лучший ask 0.42
(коэф 2.381). Витрина оптимистичнее стакана, и ставить по ней — значит считать
зазор, которого нет.

Модуль НИКОГДА не бросает исключение наружу из `check()`: сбой Polymarket не
имеет права уронить опрос котировок, ради которого всё построено.
"""
from __future__ import annotations

import json
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

import requests

from config import (
    POLYMARKET_ENABLED,
    POLYMARKET_MIN_EDGE_PCT,
    POLYMARKET_TARGET_STAKE,
    POLYMARKET_COST_FRAC,
    POLYMARKET_TAGS,
    POLYMARKET_INDEX_TTL_MINUTES,
    POLYMARKET_DATE_WINDOW_HOURS,
    PM_LAG_3_STARS,
    PM_LAG_4_STARS,
    MOVED_FOR_3_STARS,
    MOVED_FOR_4_STARS,
)

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"

HTTP_TIMEOUT = 12
HTTP_RETRIES = 2
RETRY_BACKOFF = 0.7
INDEX_PAGE = 100
# 21.08: подняли с 20. По замеру в теге soccer на тот момент лежало 2100
# открытых событий, то есть при двадцати страницах хвост в сотню событий
# просто не доезжал -- и Genoa CFC vs. SSC Napoli, наш живой сигнал, лежал
# ровно в этом хвосте, на двадцать второй странице.
INDEX_MAX_PAGES = 45

LAST_DIAG: dict[str, Any] = {}

# Suffixes Polymarket hangs off one fixture. A single match becomes half a
# dozen events -- "- Player Props", "- Exact Score", "- Total Corners" -- and
# only the bare one carries the moneyline. Matching without stripping these
# means eventually pricing corners as if they were the match result.
_SUFFIXES = (
    " - player props", " - exact score", " - total corners",
    " - first team to score", " - more markets", " - both teams to score",
    " - halftime", " - correct score", " - goalscorer",
)

# Cut from names before comparing. Club affixes carry no identity: "CF
# Montréal" and "CF Montreal" and "Montreal" are one team, and the accent is
# stripped separately.
_AFFIXES = {
    "fc", "cf", "sc", "ca", "cd", "se", "ac", "sk", "if", "bk", "pe", "afc",
    "cfc", "club", "de", "the", "vs", "v", "and", "united", "city", "town",
    "calcio", "sportif", "clube", "atletico", "athletic", "real", "deportivo",
}

# Устойчивые расхождения, которые нормализация не чинит: разные слова, а не
# разное написание. Копится по мере встреч -- это и есть наше покрытие.
# Ключ и значение нормализуются одинаково, так что писать можно по-человечески.
TEAM_ALIASES: dict[str, str] = {
    "la galaxy": "los angeles galaxy",
    "ny red bulls": "new york red bulls",
    "ny city": "new york city",
    "shanghai sipg": "shanghai port",
    "flamengo rj": "flamengo",
    "sport recife": "sport club recife",
    "vancouver whitecaps": "vancouver whitecaps",
}

_SESSION: Optional[requests.Session] = None
_INDEX: dict[str, Any] = {"at": None, "events": []}


def _session() -> requests.Session:
    global _SESSION
    if _SESSION is None:
        s = requests.Session()
        s.headers.update({"User-Agent": "odds-tracker/steamline (+github kev1nst21)"})
        _SESSION = s
    return _SESSION


def _get(url: str, params: Optional[dict] = None) -> Optional[Any]:
    err = None
    for attempt in range(HTTP_RETRIES + 1):
        try:
            r = _session().get(url, params=params, timeout=HTTP_TIMEOUT)
            if r.status_code >= 500:
                err = f"HTTP {r.status_code}"
                time.sleep(RETRY_BACKOFF * (attempt + 1))
                continue
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError) as e:
            err = str(e)
            time.sleep(RETRY_BACKOFF * (attempt + 1))
    LAST_DIAG["http_error"] = f"{url}: {err}"
    return None


# --------------------------------------------------------------------------
# Имена
# --------------------------------------------------------------------------

def _strip_suffix(title: str) -> str:
    t = (title or "").strip()
    low = t.lower()
    for suf in _SUFFIXES:
        if low.endswith(suf):
            return t[: -len(suf)].strip()
    return t


def _words(name: str) -> list[str]:
    """Нижний регистр, без диакритики и пунктуации, без клубных аффиксов."""
    if not name:
        return []
    s = unicodedata.normalize("NFKD", str(name))
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.lower()
    for ch in "._-/&'’`,:()":
        s = s.replace(ch, " ")
    return [w for w in s.split() if w and w not in _AFFIXES]


def _norm(name: str) -> str:
    core = " ".join(_words(name))
    return TEAM_ALIASES.get(core, core)


def team_in(team: str, blob: str) -> bool:
    """Есть ли команда в строке.

    Совпадения по одному длинному токену достаточно: "Columbus Crew SC" против
    "Columbus Crew" разойдутся по числу слов, но сойдутся по "columbus". Для
    коротких имён (двух-трёхбуквенных) требуем все токены -- иначе "Lyon"
    поймает "Lyon-Duchere" и любой другой шум.
    """
    want = _words(_norm(team))
    if not want:
        return False
    have = set(_words(_norm(blob)))
    strong = [w for w in want if len(w) >= 4]
    if strong:
        return any(w in have for w in strong)
    return all(w in have for w in want)


# --------------------------------------------------------------------------
# Время
# --------------------------------------------------------------------------

def _dt(value: Any) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    s = str(value).replace("Z", "+00:00")
    try:
        d = datetime.fromisoformat(s)
    except ValueError:
        return None
    return d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d.astimezone(timezone.utc)


def _event_start(ev: dict) -> Optional[datetime]:
    """Когда НАЧИНАЕТСЯ матч. Не когда рынок выставили.

    21.08.2026 — здесь был баг, который в одиночку убивал весь футбол.

    У событий Polymarket три даты, и они значат совсем разное:
        startDate      — когда рынок ВЫСТАВИЛИ на площадку;
        gameStartTime  — настоящее начало матча, но заполнено далеко не всегда
                         (в теннисе есть, в футболе на замере было пусто);
        endDate        — крайний срок расчёта, и для матчевых рынков это
                         фактически и есть начало матча.

    Прежний код читал startDate как начало матча. Для «Genoa CFC vs. SSC
    Napoli» это давало 17.08 19:35 — момент листинга, — при том что матч
    начинается 22.08 18:45. Проверка окна дат сравнивала наш реальный старт с
    датой листинга, расходилась на четверо суток и отбрасывала событие. Так
    отбрасывался КАЖДЫЙ футбольный матч: покрытие по футболу было 0 из 14, и
    выглядело это как «Polymarket не котирует наши лиги», хотя котирует.

    Порядок теперь такой: точное время, если оно есть; иначе срок расчёта,
    который для матчевых рынков совпадает со стартом; startDate не
    рассматривается как начало матча никогда.
    """
    for k in ("gameStartTime", "startTime"):
        d = _dt(ev.get(k))
        if d:
            return d
    d = _dt(ev.get("endDate"))
    if d:
        return d
    for m in ev.get("markets") or []:
        d = _dt(m.get("gameStartTime"))
        if d:
            return d
    return None


def due_in_minutes(lead_hours: float, matched: bool) -> int:
    """Через сколько минут имеет смысл вернуться к этому сигналу.

    Самонастройка, которую просил Владислав: «ты должен в процессе сам
    подстроиться в плане как часто его обновлять чтобы результат лучше был».

    Логика лестницы -- из замера, а не из головы. Polymarket выставляет матчи
    примерно за трое суток, поэтому за 36 часов до старта спрашивать чаще, чем
    раз в несколько часов, буквально не о чем: рынка нет. А в последние часы
    линия живёт и ходит, и там каждый пропущенный цикл -- это, возможно, тот
    самый зазор, ради которого всё делается.

    Найденный матч ужесточает шаг: если рынок уже есть, движение цены важнее
    самого факта появления.
    """
    # Ужесточено 20.08 вечером: «полимаркет обновляй чаще остальных». Опрос
    # котировок жёстко привязан к кредитам и потому редок; здесь оба эндпоинта
    # бесплатные, и единственная цена частоты -- время цикла. Поэтому уже
    # найденный рынок в пределах суток смотрится КАЖДЫЙ цикл: там есть цена, и
    # она ходит, а пропущенный цикл -- это, возможно, тот самый зазор.
    #
    # Ненайденный смотрится реже и это не лень: их список открытых матчей
    # укладывается в трое суток, так что за полтора дня до старта рынка там
    # чаще всего просто нет, и сотый запрос ответит то же, что первый.
    if lead_hours > 36:
        return 30 if matched else 120
    if lead_hours > 12:
        return 10 if matched else 45
    if lead_hours > 3:
        return 0 if matched else 15
    return 0  # последние часы -- каждый цикл, независимо от всего


# --------------------------------------------------------------------------
# Индекс событий
# --------------------------------------------------------------------------

def build_index(force: bool = False) -> list[dict]:
    """Все открытые события по нашим тегам, одним куском.

    Матчинг поиском (`/public-search?q=...`) провалился на замере 20.08: он
    нашёл 13 теннисных событий из 13 и НОЛЬ футбольных из 11, и вывод
    "Polymarket не котирует футбол" был бы полностью ложным -- в индексе тогда
    же лежало 1200 открытых футбольных событий. Поиск ищет по нашей строке, а
    наша строка не их строка: "CF Montreal" против "CF Montréal",
    "LA Galaxy" против "Los Angeles Galaxy".

    Индекс снимает вопрос: мы забираем всё, что открыто, и сравниваем сами, по
    своим правилам, с алиасами и окном даты. Запросы бесплатные, поэтому цена
    решения -- только время.
    """
    now = datetime.now(timezone.utc)
    age_ok = (_INDEX["at"] and not force
              and (now - _INDEX["at"]).total_seconds() < POLYMARKET_INDEX_TTL_MINUTES * 60)
    if age_ok:
        return _INDEX["events"]

    events: list[dict] = []
    seen: set[str] = set()
    for tag in [t.strip() for t in (POLYMARKET_TAGS or "").split(",") if t.strip()]:
        for page in range(INDEX_MAX_PAGES):
            data = _get(f"{GAMMA}/events", params={
                "tag_slug": tag, "closed": "false", "limit": INDEX_PAGE,
                "offset": page * INDEX_PAGE, "order": "startDate", "ascending": "false",
            })
            if not isinstance(data, list) or not data:
                break
            for ev in data:
                eid = str(ev.get("id") or ev.get("slug") or "")
                if eid and eid not in seen:
                    seen.add(eid)
                    events.append(ev)
            if len(data) < INDEX_PAGE:
                break
    if events:
        _INDEX["at"] = now
        _INDEX["events"] = events
    LAST_DIAG["index_size"] = len(events)
    return _INDEX["events"]


def merge_siblings(events: Iterable[dict]) -> list[dict]:
    """Склеить события одной фикстуры в одно, со всеми рынками сразу.

    Polymarket разносит один матч по нескольким событиям: голое «Genoa CFC vs.
    SSC Napoli» несёт победу, «... - Player Props» несёт бомбардиров, «... -
    Total Corners» угловые. Матчер брал ПЕРВОЕ подошедшее по названию — и если
    первым оказывался props, мы уходили с событием, в котором нет ни одного
    рынка на победу, и записывали «нет токена под наш исход». По журналу это
    неотличимо от «рынка нет вовсе», хотя причины противоположные.

    После склейки решение принимается по всем рынкам фикстуры сразу, а не по
    тому, какой кусок попался первым.
    """
    by_base: dict[tuple, dict] = {}
    for ev in events:
        base = _strip_suffix(ev.get("title") or "")
        start = _event_start(ev)
        key = (base.lower(), start.date().isoformat() if start else "")
        cur = by_base.get(key)
        if cur is None:
            merged = dict(ev)
            merged["title"] = base
            merged["markets"] = list(ev.get("markets") or [])
            merged["_sources"] = 1
            by_base[key] = merged
            continue
        cur["markets"].extend(ev.get("markets") or [])
        cur["_sources"] += 1
        # Голое событие -- носитель основного рынка, его slug и id
        # предпочтительнее для ссылки.
        if (ev.get("title") or "").strip() == base:
            cur["slug"] = ev.get("slug") or cur.get("slug")
            cur["id"] = ev.get("id") or cur.get("id")
    return list(by_base.values())


def match_event(events: Iterable[dict], home: str, away: str,
                start_iso: str) -> Optional[dict]:
    """Наше событие в их индексе, или None. Гадать не имеем права."""
    target = _dt(start_iso)
    best, best_score = None, -1.0
    for ev in events:
        title = _strip_suffix(ev.get("title") or "")
        if not (team_in(home, title) and team_in(away, title)):
            continue
        start = _event_start(ev)
        if target and start:
            gap = abs((start - target).total_seconds()) / 3600.0
            if gap > POLYMARKET_DATE_WINDOW_HOURS:
                continue
            score = 2.0 - gap / POLYMARKET_DATE_WINDOW_HOURS
        else:
            # Без даты не отбрасываем, но и не предпочитаем: у части событий
            # Polymarket проставляет только время листинга.
            score = 0.5
        # Голое событие фикстуры несёт основной рынок; "- Exact Score" и
        # прочие суффиксы несут что угодно, только не победу в матче.
        if (ev.get("title") or "").strip() == title:
            score += 1.0
        if score > best_score:
            best, best_score = ev, score
    LAST_DIAG["match_score"] = round(best_score, 3) if best else None
    return best


# --------------------------------------------------------------------------
# Токен нашего исхода
# --------------------------------------------------------------------------

def _loads(v: Any) -> Any:
    if isinstance(v, str):
        try:
            return json.loads(v)
        except (ValueError, TypeError):
            return v
    return v


def pick_token(event: dict, pick_name: str, home: str, away: str) -> Optional[dict]:
    """token_id того исхода, на который мы ставим.

    Polymarket встречается в двух формах, и обе живые:
      1) один рынок с двумя ИМЕНОВАННЫМИ исходами -- ["Iga Swiatek",
         "Elena Rybakina"]. Так устроен теннис и часть футбола;
      2) рынок-вопрос с Yes/No -- "Will X win?". Так устроены старые события и
         аутрайты.
    Разбираем обе, начиная с первой: она точнее, потому что имя исхода прямо
    называет игрока или команду.
    """
    for m in event.get("markets") or []:
        outcomes = _loads(m.get("outcomes")) or []
        tokens = _loads(m.get("clobTokenIds")) or []
        if not (isinstance(outcomes, list) and isinstance(tokens, list)):
            continue
        if len(outcomes) != len(tokens) or not outcomes:
            continue
        q = (m.get("question") or "")
        # Форма 1: имя исхода совпадает с нашим выбором.
        for oc, tid in zip(outcomes, tokens):
            if str(oc).strip().lower() in ("yes", "no"):
                continue
            if team_in(pick_name, str(oc)):
                return {"token_id": str(tid), "outcome": str(oc),
                        "question": q, "shape": "named"}
        # Форма 2: "Will <pick> win?" -> берём Yes.
        if team_in(pick_name, q) and not _is_prop_question(q):
            other = away if team_in(home, pick_name) else home
            if team_in(other, q):
                continue  # вопрос про обе команды -- не про одну победу
            for oc, tid in zip(outcomes, tokens):
                if str(oc).strip().lower() == "yes":
                    return {"token_id": str(tid), "outcome": "Yes",
                            "question": q, "shape": "yes_no"}
    LAST_DIAG["token_error"] = f"нет токена под «{pick_name}»"
    return None


def _is_prop_question(q: str) -> bool:
    low = (q or "").lower()
    return any(w in low for w in (
        "corner", "score exactly", "exact score", "both teams", "total",
        "over", "under", "first team", "handicap", "props", "assists",
        "cards", "booking", "sets", "games"))


def fetch_book(token_id: str) -> Optional[dict]:
    return _get(f"{CLOB}/book", params={"token_id": token_id})


def asks_from(book: dict) -> list[tuple[float, float]]:
    out = []
    for lvl in (book or {}).get("asks") or []:
        try:
            p, s = float(lvl["price"]), float(lvl["size"])
        except (KeyError, TypeError, ValueError):
            continue
        if 0.0 < p < 1.0 and s > 0:
            out.append((p, s))
    return sorted(out, key=lambda x: x[0])   # дешевле = выше коэффициент


# --------------------------------------------------------------------------
# Математика. Чистая, тестируется без сети.
# --------------------------------------------------------------------------

def plan_entry(asks: list[tuple[float, float]],
               entry_price: float,
               target_stake: float = None,
               min_edge_pct: float = None,
               cost_frac: float = None) -> dict:
    """Сколько можно взять на Polymarket по цене лучше нашей БК на min_edge_pct.

    Правило Владислава целиком: база -- `entry_price`, то есть лучшая цена,
    которую мы реально можем взять у конторы по этому же сигналу. Порог --
    entry_price * (1 + min_edge_pct/100). Берём уровни стакана от самого
    дешёвого (самый высокий коэффициент), пока коэффициент уровня держит порог
    И пока не набрали target_stake.

    Отдельно про `exec_stake_usd` -- это ответ на «залезем ли мы сделкой на
    $200». Если стакан по нужной цене держит меньше, чем мы хотим поставить,
    возвращается ровно та сумма, которая влезает, а не отказ: Владислав просил
    «если не залезаем, то писать ту сумму по которой залезли».

    Комиссии. `cost_frac` по умолчанию 0: пять процентов порога И ЕСТЬ запас,
    и накидывать сверху ещё один слой значило бы считать его дважды. Ручка
    оставлена, потому что фактические издержки Polygon/USDC ещё не померены на
    реальной сделке -- когда померим, поставим сюда число, а не догадку.
    """
    target = POLYMARKET_TARGET_STAKE if target_stake is None else target_stake
    edge = POLYMARKET_MIN_EDGE_PCT if min_edge_pct is None else min_edge_pct
    cost = POLYMARKET_COST_FRAC if cost_frac is None else cost_frac

    if not entry_price or entry_price <= 1.0:
        return {"ok": False, "reason": "нет цены входа в БК"}

    need_coef = entry_price * (1.0 + edge / 100.0) * (1.0 + cost)
    best_coef = round(1.0 / asks[0][0], 4) if asks else None

    stake = shares = 0.0
    levels: list[list[float]] = []
    for price, size in asks:
        coef = 1.0 / price
        if coef < need_coef:
            break                      # дальше только хуже: цены растут
        room = target - stake
        if room <= 0:
            break
        take_usd = min(room, price * size)
        stake += take_usd
        shares += take_usd / price
        levels.append([round(coef, 4), round(take_usd, 2)])

    avg = round(shares / stake, 4) if stake > 0 else None
    return {
        "ok": True,
        "entry_price": round(float(entry_price), 4),
        "need_coef": round(need_coef, 4),
        "best_coef": best_coef,
        "avg_coef": avg,
        "exec_stake_usd": round(stake, 2),
        "target_stake_usd": round(target, 2),
        "fits_target": bool(stake >= target - 0.01),
        "edge_pct": round((avg / entry_price - 1.0) * 100.0, 2) if avg else None,
        "take": bool(stake > 0),
        "levels": levels,
        "min_edge_pct": edge,
        "cost_frac": cost,
    }


# --------------------------------------------------------------------------
# Одна проверка одного сигнала
# --------------------------------------------------------------------------

def check(home: str, away: str, start_iso: str, pick_name: str,
          entry_price: float, opt_price: float = None,
          events: list[dict] = None, old_price: float = None,
          new_price: float = None, down_count: int = 0,
          books_count: int = 0) -> list[dict]:
    """Одна проверка одного события — но по ОБЕИМ ногам.

    Возвращает список: по строке на каждую найденную ногу (агрессивную и
    оптимальную), либо одну строку с matched=False и причиной. Никогда не
    бросает исключение: сбой Polymarket не имеет права уронить опрос.

    Оптимальная нога считается только если у нас ЕСТЬ с чем сравнивать —
    цена двойного шанса у конторы. Без базы правило «лучше на 5%» бессмысленно,
    и выдумывать базу мы не будем: строка просто не появится.
    """
    stamp = datetime.now(timezone.utc).isoformat()
    base = {"checked_at": stamp, "matched": False, "leg": "aggressive"}
    if not POLYMARKET_ENABLED:
        return [{**base, "reason": "выключено"}]
    try:
        idx = build_index() if events is None else events
        if not idx:
            return [{**base, "reason": "индекс пуст"}]
        idx = merge_siblings(idx)
        ev = match_event(idx, home, away, start_iso)
        if not ev:
            return [{**base, "reason": "события нет на Polymarket",
                     "index_size": len(idx)}]
        meta = {"event_title": ev.get("title"), "event_slug": ev.get("slug"),
                "markets_total": len(ev.get("markets") or [])}
        legs = find_legs(ev, pick_name, home, away)
        if not legs:
            return [{**base, "reason": "нет подходящего рынка", **meta}]

        bench = {"aggressive": entry_price, "optimal": opt_price}
        out = []
        # Отставание -- свойство СОБЫТИЯ, а не отдельной ноги: оно отвечает на
        # вопрос "насколько эта площадка вообще заметила движение". Считаем его
        # один раз по основному рынку и вешаем на обе ноги, потому что двойной
        # шанс переставляется вслед за победой, а не сам по себе.
        lag = None
        for name in ("aggressive", "optimal"):
            leg = legs.get(name)
            if leg is None:
                continue
            price = bench.get(name)
            if not price:
                # Нет цены у конторы -- сравнивать не с чем. Пишем строкой, а не
                # молчим: отсутствие базы это факт о нашей стороне, и он тоже
                # данные (именно так теннис годами не давал безопасной цены).
                out.append({**base, "leg": name, "matched": True, **meta,
                            "token_id": leg["token_id"], "outcome": leg["outcome"],
                            "question": leg.get("question"),
                            "reason": "нет цены у конторы для сравнения",
                            "take": False, "exec_stake_usd": 0.0})
                continue
            book = fetch_book(leg["token_id"])
            asks = asks_from(book or {})
            if not asks:
                out.append({**base, "leg": name, "matched": True, **meta,
                            "token_id": leg["token_id"], "reason": "пустой стакан",
                            "take": False, "exec_stake_usd": 0.0})
                continue
            plan = plan_entry(asks, price)
            if name == "aggressive":
                lag = pm_lag(plan.get("best_coef"), old_price, new_price)
            out.append({
                **base, "leg": name, "matched": True, **meta,
                "pm_lag": lag, "down_count": down_count, "books_count": books_count,
                "token_id": leg["token_id"], "outcome": leg["outcome"],
                "shape": leg["shape"], "question": leg.get("question"),
                "means": leg.get("means"),
                **plan,
            })
        return out
    except Exception as e:                                   # noqa: BLE001
        LAST_DIAG["exception"] = repr(e)
        return [{**base, "reason": "ошибка", "detail": repr(e)}]


# --------------------------------------------------------------------------
# Звёзды Polymarket
# --------------------------------------------------------------------------

def pm_lag(pm_coef: float, old_price: float, new_price: float):
    """Какую долю движения Polymarket ЕЩЁ НЕ отыграл. Это и есть наш эдж.

    Мысль Владислава, 20.08: «звёзды от полимаркета должны исходить из
    вероятности того что ставить надо, а не процентов и повышения. Если мы
    видим что на большом количестве просело БК, а на полике нет — то это
    охуенно».

    Он прав, и прежняя шкала по размеру зазора мерила не то. Зазор в процентах
    отвечает на вопрос «насколько тут дешевле», а деньги приносит ответ на
    другой: «насколько этот рынок ещё не понял того, что уже поняли все
    остальные». Это разные величины, и совпадают они только случайно.

    Считается так. Движение у контор идёт от old_price к new_price — это
    расстояние, которое рынок уже прошёл на деньгах. Смотрим, где на этом
    отрезке стоит Polymarket:

        lag = (pm_coef - new_price) / (old_price - new_price)

        1.0  Polymarket стоит на ДОвиженческой цене. Не шелохнулся. Максимум:
             мы берём вчерашнюю цену на рынке, который вот-вот переставят.
        0.5  отыграл половину движения.
        0.0  отыграл полностью, стоит там же, где конторы после движения.
             Зазора по смыслу нет, даже если арифметически он есть.
       <0.0  Polymarket УШЁЛ ДАЛЬШЕ контор. Там уже знают больше нашего, и
             наше «движение» для них старая новость.
       >1.0  стоит выше, чем контора ДО движения. Лучше не бывает.

    None, если движения не было (old == new) — тогда мерить нечего, и врать
    числом нельзя.
    """
    try:
        span = float(old_price) - float(new_price)
        if span <= 0:
            return None
        return round((float(pm_coef) - float(new_price)) / span, 4)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def pm_stars(lag=None, down_count: int = 0, books_count: int = 0,
             exec_stake: float = 0.0, target: float = None,
             edge_pct=None) -> int:
    """Насколько это хорошая сделка НА POLYMARKET. Две величины, обе нужны.

    ШИРИНА — у скольких контор поехала линия. Это вероятность того, что
    движение вообще настоящее, а не чья-то разовая ставка. Одна контора может
    ошибиться, сорок независимых — нет.

    ОТСТАВАНИЕ — насколько Polymarket этого ещё не заметил. Это то, сколько
    нам достанется.

    Ни одна из двух в одиночку ничего не стоит, и в этом весь смысл. Сорок
    контор поехали, а Polymarket уже переставился — движение настоящее, но
    денег в нём для нас нет. Polymarket стоит колом, а поехали две конторы —
    он, скорее всего, просто прав, и это мы шумим, а не он. Деньги живут
    ровно на пересечении: РЫНОК УВЕРЕН, А ЭТА ПЛОЩАДКА ЕЩЁ НЕ ЗНАЕТ.

        ★★★★  ширина от MOVED_FOR_4_STARS контор И отставание от 0.8
              И влезает полный размер
        ★★★   ширина от MOVED_FOR_3_STARS контор И отставание от 0.5
        ★★    прошло правило входа (цена не хуже конторы), но одно из двух
              условий не выполнено

    Ноль означает «сделки нет», а не «плохая сделка»: ниже порога входа мы не
    ставим вовсе, и смешивать эти два состояния нельзя.

    edge_pct принимается только ради совместимости со старыми записями в
    журнале, где ширины и отставания ещё не сохранялось. Новые оценки на него
    не опираются.
    """
    tgt = POLYMARKET_TARGET_STAKE if target is None else target
    if exec_stake <= 0:
        return 0
    if lag is None:
        # Старая запись без отставания: честно отдаём нижнюю ступень, а не
        # выдумываем оценку из процента, который её не измеряет.
        return 2 if (edge_pct is not None and edge_pct >= POLYMARKET_MIN_EDGE_PCT) else 0
    full = exec_stake >= tgt - 0.01
    if (down_count >= MOVED_FOR_4_STARS and lag >= PM_LAG_4_STARS and full):
        return 4
    if down_count >= MOVED_FOR_3_STARS and lag >= PM_LAG_3_STARS:
        return 3
    return 2


# --------------------------------------------------------------------------
# Всё событие целиком, а не одна победа
# --------------------------------------------------------------------------

# Что за рынок перед нами. Классификация грубая намеренно: нам не нужно понять
# каждый экзотический рынок Polymarket, нам нужно не перепутать победу в матче
# с числом угловых.
KIND_MONEYLINE = "moneyline"
KIND_DOUBLE_CHANCE = "double_chance"
KIND_TOTAL = "total"
KIND_SPREAD = "spread"
KIND_OTHER = "other"


def classify_market(question: str, outcomes: list) -> str:
    q = (question or "").lower()
    names = [str(o).strip().lower() for o in (outcomes or [])]
    if any(n.startswith(("over", "under")) for n in names) or " o/u " in q or "total" in q:
        return KIND_TOTAL
    if "spread" in q or "handicap" in q or any("+" in n or "−" in n for n in names):
        return KIND_SPREAD
    if any(w in q for w in ("corner", "exact score", "first team", "both teams",
                            "goalscorer", "card", "assist", "completed match")):
        return KIND_OTHER
    if set(names) == {"yes", "no"}:
        return KIND_MONEYLINE
    if len(names) in (2, 3):
        return KIND_MONEYLINE
    return KIND_OTHER


def explode(event: dict) -> list[dict]:
    """Каждый рынок события с токенами и витринными ценами.

    Просьба 20.08: «на полики раскрывай полное событие и ищи такой же вариант
    как поставить в оптимальной стратегии». Одно теннисное событие Polymarket
    несёт полтора десятка рынков — победа, тотал сетов, тотал геймов, фора.
    Раньше мы смотрели только на первый и не знали, что теряем.

    Цены здесь ВИТРИННЫЕ и годятся только на то, чтобы решить, за каким
    стаканом идти. Ставить по ним нельзя: замер 20.08 показал витрину 0.415
    против лучшего аска 0.42 на том же рынке.
    """
    out = []
    for m in event.get("markets") or []:
        outcomes = _loads(m.get("outcomes")) or []
        tokens = _loads(m.get("clobTokenIds")) or []
        prices = _loads(m.get("outcomePrices")) or []
        if not (isinstance(outcomes, list) and isinstance(tokens, list)):
            continue
        if len(outcomes) != len(tokens) or not outcomes:
            continue
        q = m.get("question") or ""
        legs = []
        for i, (oc, tid) in enumerate(zip(outcomes, tokens)):
            try:
                shop = float(prices[i]) if i < len(prices) else None
            except (TypeError, ValueError):
                shop = None
            legs.append({"outcome": str(oc), "token_id": str(tid),
                         "showcase_price": shop,
                         "showcase_coef": round(1.0 / shop, 4) if shop else None})
        out.append({"question": q, "kind": classify_market(q, outcomes),
                    "closed": bool(m.get("closed")), "outcomes": legs})
    return out


def find_legs(event: dict, pick_name: str, home: str, away: str,
              opponent: str = None) -> dict:
    """Две ноги одной сделки: агрессивная и оптимальная.

    АГРЕССИВНАЯ — прямая победа нашего выбора. Это то же, что мы ставим у
    конторы по entry_price, и сравнивается с ней напрямую.

    ОПТИМАЛЬНАЯ — аналог нашего «безопасного варианта». У конторы это двойной
    шанс: «наш или ничья». На Polymarket его не продают отдельной строкой, но
    он там есть: «наш ИЛИ ничья» — это ровно «соперник НЕ победит», то есть
    токен No на рынке победы соперника. Ставка та же, вход другой.

    Возвращаются обе, если обе нашлись; бот сам решит, брать одну или две —
    «бот в таком случае будет делать две ставки с разным кофом, если такое
    возможно».
    """
    opp = opponent or (away if team_in(home, pick_name) else home)
    legs: dict[str, dict] = {}
    for m in event.get("markets") or []:
        outcomes = _loads(m.get("outcomes")) or []
        tokens = _loads(m.get("clobTokenIds")) or []
        if not (isinstance(outcomes, list) and isinstance(tokens, list)):
            continue
        if len(outcomes) != len(tokens) or not outcomes:
            continue
        q = m.get("question") or ""
        if classify_market(q, outcomes) == KIND_OTHER or _is_prop_question(q):
            continue
        names = [str(o) for o in outcomes]
        low = [n.strip().lower() for n in names]

        if "aggressive" not in legs:
            for n, tid in zip(names, tokens):
                if n.strip().lower() in ("yes", "no"):
                    continue
                if team_in(pick_name, n):
                    legs["aggressive"] = {"token_id": str(tid), "outcome": n,
                                          "question": q, "shape": "named"}
                    break
        if "aggressive" not in legs and set(low) == {"yes", "no"}:
            if team_in(pick_name, q) and not team_in(opp, q):
                for n, tid in zip(names, tokens):
                    if n.strip().lower() == "yes":
                        legs["aggressive"] = {"token_id": str(tid), "outcome": "Yes",
                                              "question": q, "shape": "yes_no"}
                        break
        # Оптимальная: "Will <соперник> win?" -> No. Это и есть двойной шанс.
        if "optimal" not in legs and set(low) == {"yes", "no"}:
            if team_in(opp, q) and not team_in(pick_name, q):
                for n, tid in zip(names, tokens):
                    if n.strip().lower() == "no":
                        legs["optimal"] = {
                            "token_id": str(tid), "outcome": "No",
                            "question": q, "shape": "yes_no_inverted",
                            "means": f"{pick_name} или ничья",
                        }
                        break
    LAST_DIAG["legs"] = list(legs)
    return legs
