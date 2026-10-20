#!/usr/bin/env python3
"""
Панель агентства для Авито: все клиенты на одной странице.
Демо-режим + форма для добавления своего аккаунта (ключи не сохраняются).

Запуск: python panel_demo.py  → http://127.0.0.1:8765
Нужен только Python 3.8+, сторонние библиотеки не требуются.
"""

import json
import os
import random
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

API = "https://api.avito.ru"
HOST = "0.0.0.0"
PORT = int(os.environ.get("PORT", 8765))
HERE = os.path.dirname(os.path.abspath(__file__))
SOURCE = "agency-panel"
DAYS = 61
CACHE_SEC = 65
MSK = timezone(timedelta(hours=3))

_tokens = {}
_cache = {}
_lock = threading.Lock()

# ── Временные аккаунты (в памяти, не сохраняются на диск) ────────────────────
_temp_clients = []
_temp_lock = threading.Lock()


# ── Демо-данные ───────────────────────────────────────────────────────────────

def _fake_days(seed, base_c, base_spend, trend=0.0):
    rng = random.Random(seed)
    today = date.today()
    out = []
    for i in range(DAYS):
        d = (today - timedelta(days=DAYS - 1 - i)).isoformat()
        factor = 1 + trend * (i - 30) / 30
        c = max(0, int(base_c * factor * rng.uniform(0.6, 1.4)))
        v = c * rng.randint(12, 30) if c else rng.randint(20, 80)
        sp = c * base_spend * rng.uniform(0.85, 1.15) if c else 0
        out.append({
            "date": d, "views": v, "contacts": c,
            "spend": round(sp, 2), "cs": round(sp, 2),
            "phone": int(c * 0.55), "chat": int(c * 0.45),
        })
    return out


def make_demo_data():
    today = date.today()

    def day_entry(days_list, offset):
        L = len(days_list)
        idx = L - 1 - offset
        return days_list[idx] if 0 <= idx < L else {"views": 0, "contacts": 0, "spend": 0, "cs": 0}

    clients = [
        {
            "name": "Автосервис Центр",
            "profile_url": "",
            "wallet": {"real": 8400, "bonus": 320},
            "advance": {"value": 2100},
            "reviews": {
                "rating": {"score": 4.8, "count": 134},
                "new": 3, "noans": [
                    {"id": 1, "ts": int(time.time()) - 7200, "score": 4,
                     "who": "Александр К.", "text": "Хорошо поменяли масло, только ждать пришлось долго.",
                     "item": "Замена масла и фильтра", "rejected": False},
                ]
            },
            "noreply": {"list": []},
            "days": {"list": _fake_days(1, 4, 380, trend=0.05)},
            "target_cpl": 500,
            "cached_at": time.time(),
        },
        {
            "name": "Вывоз мусора",
            "profile_url": "",
            "wallet": {"real": 15200, "bonus": 0},
            "advance": {"value": 4300},
            "reviews": {
                "rating": {"score": 4.9, "count": 287},
                "new": 1, "noans": []
            },
            "noreply": {"list": [
                {"kind": "msg", "id": "c1", "ts": int(time.time()) - 3600,
                 "who": "Дмитрий", "item": "Вывоз строительного мусора", "text": "Можете сегодня?"},
                {"kind": "call", "id": "c2", "ts": int(time.time()) - 5400,
                 "who": "Покупатель", "item": "Вывоз мусора после ремонта", "text": ""},
            ]},
            "days": {"list": _fake_days(2, 7, 280, trend=0.15)},
            "target_cpl": 350,
            "cached_at": time.time(),
        },
        {
            "name": "Кузовной ремонт",
            "profile_url": "",
            "wallet": {"real": 900, "bonus": 0},
            "advance": {"value": 0},
            "reviews": {
                "rating": {"score": 4.6, "count": 52},
                "new": 2, "noans": [
                    {"id": 2, "ts": int(time.time()) - 86400, "score": 3,
                     "who": "Иван М.", "text": "Сделали нормально но дольше срока.",
                     "item": "Ремонт бампера", "rejected": False},
                    {"id": 3, "ts": int(time.time()) - 172800, "score": 5,
                     "who": "Мария С.", "text": "Отличная работа! Рекомендую.",
                     "item": "Покраска двери", "rejected": False},
                ]
            },
            "noreply": {"list": []},
            "days": {"list": _fake_days(3, 3, 650, trend=-0.1)},
            "target_cpl": 700,
            "cached_at": time.time(),
        },
        {
            "name": "Детейлинг Авто",
            "profile_url": "",
            "wallet": {"real": 5600, "bonus": 800},
            "advance": {"value": 1200},
            "reviews": {
                "rating": {"score": 5.0, "count": 41},
                "new": 0, "noans": []
            },
            "noreply": {"list": []},
            "days": {"list": _fake_days(4, 2, 920, trend=0.2)},
            "target_cpl": None,
            "cached_at": time.time(),
        },
    ]
    return {"clients": clients, "order": [c["name"] for c in clients], "demo": True}


# ── API ───────────────────────────────────────────────────────────────────────

class ApiError(Exception):
    pass


def _request(method, path, token=None, params=None, body=None, form=None, headers=None):
    url = API + path
    if params:
        url += "?" + urllib.parse.urlencode(params, doseq=True)
    h = {"Accept": "application/json"}
    h.update(headers or {})
    data = None
    if token:
        h["Authorization"] = "Bearer " + token
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        h["Content-Type"] = "application/x-www-form-urlencoded"
    elif body is not None:
        data = json.dumps(body).encode()
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        msg = {401: "ключи не подошли", 403: "нет доступа",
               429: "Авито просит подождать минуту"}.get(e.code, "")
        raise ApiError(f"{e.code} {msg}".strip())
    except urllib.error.URLError as e:
        raise ApiError(f"нет связи с Авито ({e.reason})")


def get_token(client):
    cid = client["client_id"]
    with _lock:
        tok = _tokens.get(cid)
        if tok and tok[1] > time.time() + 60:
            return tok[0]
    r = _request("POST", "/token", form={
        "grant_type": "client_credentials",
        "client_id": cid,
        "client_secret": client["client_secret"],
    })
    if "access_token" not in r:
        raise ApiError("ключи не подошли")
    with _lock:
        _tokens[cid] = (r["access_token"], time.time() + int(r.get("expires_in", 3600)))
    return r["access_token"]


def safe(fn):
    try:
        return fn()
    except ApiError as e:
        return {"error": str(e)}
    except Exception as e:
        return {"error": f"ошибка: {e}"}


def fetch_client(client):
    row = {"name": client.get("name") or client["client_id"][:8]}
    try:
        tok = get_token(client)
        me = _request("GET", "/core/v1/accounts/self", tok)
    except ApiError as e:
        row["fatal"] = str(e)
        return row
    uid = me["id"]
    row["profile_url"] = me.get("profile_url", "")
    week_ago = time.time() - 7 * 86400

    def noreply():
        chats = []
        for off in (0, 100, 200):
            try:
                r = _request("GET", f"/messenger/v2/accounts/{uid}/chats", tok,
                             params={"limit": 100, "offset": off, "chat_types": ["u2i", "u2u"]})
            except ApiError as e:
                if str(e).startswith("403"):
                    raise ApiError("мессенджер по API недоступен на тарифе клиента")
                raise
            part = r.get("chats", [])
            chats += part
            last = min([c.get("updated") or c.get("created") or 0 for c in part] or [0])
            if len(part) < 100 or last < week_ago:
                break
        items = []
        for c in chats:
            m = c.get("last_message") or {}
            ts = m.get("created") or c.get("updated") or 0
            if ts < week_ago:
                continue
            content = m.get("content") or {}
            text = (content.get("text") or "").strip()
            call = content.get("call") or {}
            if m.get("type") == "call":
                if call.get("status") != "missed" and m.get("direction") == "out":
                    continue
                kind = "call"
            elif "ознакомился с вашим предложением" in text.lower():
                kind = "offer"
            elif m.get("type") == "system":
                kind = "system"
            elif m.get("direction") == "in":
                kind = "msg"
            else:
                continue
            others = [u for u in c.get("users", []) if u.get("id") != uid]
            ctx = (c.get("context") or {}).get("value") or {}
            items.append({
                "kind": kind, "id": c.get("id"), "ts": ts,
                "who": (others[0].get("name") if others else "") or "Покупатель",
                "item": ctx.get("title") or "",
                "text": text[:1000] if kind in ("msg", "system") else "",
                "mtype": m.get("type"),
            })
        items.sort(key=lambda x: -x["ts"])
        return {"list": items}

    def reviews():
        new, noans = 0, []
        rating = None
        try:
            info = _request("GET", "/ratings/v1/info", tok)
            if info.get("isEnabled") and info.get("rating"):
                rating = {"score": info["rating"].get("score"),
                          "count": info["rating"].get("reviewsCount")}
        except ApiError:
            pass
        for off in range(0, 500, 50):
            try:
                r = _request("GET", "/ratings/v1/reviews", tok,
                             params={"offset": off, "limit": 50})
            except ApiError as e:
                if str(e).startswith("403"):
                    raise ApiError("нет доступа к отзывам по API")
                raise
            part = r.get("reviews", [])
            for rv in part:
                ts = rv.get("createdAt") or 0
                if ts < week_ago:
                    continue
                new += 1
                ans = rv.get("answer")
                answered = ans and ans.get("status") in ("published", "moderation")
                if not answered and rv.get("canAnswer", True):
                    noans.append({
                        "id": rv.get("id"), "ts": ts, "score": rv.get("score"),
                        "who": (rv.get("sender") or {}).get("name") or "",
                        "text": (rv.get("text") or "")[:200],
                        "item": (rv.get("item") or {}).get("title") or "",
                        "rejected": bool(ans and ans.get("status") == "rejected"),
                    })
            oldest = min([x.get("createdAt") or 0 for x in part] or [0])
            if len(part) < 50 or oldest < week_ago:
                break
        noans.sort(key=lambda x: -x["ts"])
        return {"new": new, "noans": noans, "rating": rating}

    def wallet():
        r = _request("GET", f"/core/v1/accounts/{uid}/balance/", tok)
        return {"real": r.get("real", 0) or 0, "bonus": r.get("bonus", 0) or 0}

    def advance():
        r = _request("POST", "/cpa/v3/balanceInfo", tok, body={},
                     headers={"X-Source": SOURCE})
        if "balance" not in r:
            raise ApiError("нет данных")
        return {"value": (r["balance"] or 0) / 100}

    def days():
        today = datetime.now(MSK).date()
        start = today - timedelta(days=DAYS - 1)
        r = _request("POST", f"/stats/v2/accounts/{uid}/items", tok, body={
            "dateFrom": start.isoformat(),
            "dateTo": today.isoformat(),
            "grouping": "day",
            "metrics": ["views", "contacts", "contactsShowPhone", "contactsMessenger",
                        "allSpending", "spending", "spendingBonus",
                        "presenceSpending", "promoSpending",
                        "averageContactCost", "averageViewCost"],
            "limit": 1000, "offset": 0,
        })
        by_date = {}
        for g in r.get("result", {}).get("groupings", []):
            gid = g.get("id")
            if not isinstance(gid, (int, float)):
                continue
            d = datetime.fromtimestamp(gid + 3 * 3600, timezone.utc).date().isoformat()
            m = {x["slug"]: x.get("value") or 0 for x in g.get("metrics", [])}
            spend = (m.get("allSpending") or (m.get("spending", 0) + m.get("spendingBonus", 0))) / 100
            by_date[d] = {
                "cs": avito_cost_base(m, spend),
                "avito_avg": bool(m.get("averageContactCost") and m.get("contacts")),
                "views": m.get("views", 0),
                "contacts": m.get("contacts", 0),
                "spend": (m.get("allSpending") or (m.get("spending", 0) + m.get("spendingBonus", 0))) / 100,
                "phone": m.get("contactsShowPhone"),
                "chat": m.get("contactsMessenger"),
                "promo": (m["promoSpending"] / 100) if "promoSpending" in m else None,
                "presence": (m["presenceSpending"] / 100) if "presenceSpending" in m else None,
                "bonus": (m["spendingBonus"] / 100) if "spendingBonus" in m else None,
            }
        prof = profile_spendings(uid, tok, start, today)
        scale = 1.0
        ratios = []
        for d, sp in prof.items():
            items_rub = by_date.get(d, {}).get("spend", 0)
            parts = (sp.get("promo") or 0) + (sp.get("presence") or 0)
            if items_rub > 0 and parts > 0:
                ratios.append(parts / items_rub)
        if ratios and sorted(ratios)[len(ratios) // 2] > 20:
            scale = 0.01
        for d, sp in prof.items():
            vals = {k: (None if x is None else x * scale) for k, x in sp.items()}
            parts = [vals[k] for k in ("promo", "presence", "commission", "rest") if vals[k] is not None]
            total = vals["all"] if vals["all"] is not None else (sum(parts) if parts else None)
            if total is None:
                continue
            v = by_date.setdefault(d, {"views": 0, "contacts": 0, "spend": 0, "cs": 0})
            v["spend"] = total
            v["cs"] = total
            for k in ("promo", "presence", "rest"):
                if vals[k] is not None:
                    v[k] = vals[k]
        out = []
        for i in range(DAYS):
            d = (start + timedelta(days=i)).isoformat()
            v = by_date.get(d, {"views": 0, "contacts": 0, "spend": 0, "cs": 0})
            out.append(dict(date=d, **v))
        return {"list": out}

    with ThreadPoolExecutor(max_workers=5) as ex:
        futs = {k: ex.submit(safe, fn) for k, fn in [
            ("reviews", reviews), ("noreply", noreply),
            ("wallet", wallet), ("advance", advance), ("days", days),
        ]}
        for k, f in futs.items():
            row[k] = f.result()
    return row


def _day_of(raw):
    s = str(raw)
    if s.isdigit():
        return datetime.fromtimestamp(int(s) + 3 * 3600, timezone.utc).date().isoformat()
    return s[:10]


def profile_spendings(uid, tok, start, end):
    try:
        r = _request("POST", f"/stats/v2/accounts/{uid}/spendings", tok, body={
            "dateFrom": start.isoformat(), "dateTo": end.isoformat(), "grouping": "day",
            "spendingTypes": ["all", "presence", "promotion", "commission", "rest"]})
    except ApiError:
        return {}
    res = {}
    for g in r.get("result", {}).get("groupings", []):
        vals = {s.get("slug"): s.get("value") for s in g.get("spendings", [])}
        res[_day_of(g.get("date"))] = {
            "all": vals.get("all"), "promo": vals.get("promotion"),
            "presence": vals.get("presence"), "commission": vals.get("commission"),
            "rest": vals.get("rest")}
    return res


def avito_cost_base(m, spend):
    c, v = m.get("contacts", 0) or 0, m.get("views", 0) or 0
    for avg, cnt in ((m.get("averageContactCost"), c), (m.get("averageViewCost"), v)):
        if avg and cnt:
            base = avg * cnt
            if spend > 0:
                return base / 100 if base / spend > 20 else base
            return base / 100
    return spend


def fetch_cached(client):
    cid = client["client_id"]
    with _lock:
        hit = _cache.get(cid)
    extra = {"target_cpl": _num(client.get("target_cpl"))}
    if hit and time.time() - hit[0] < CACHE_SEC:
        return dict(hit[1], cached_at=hit[0], **extra)
    row = fetch_client(client)
    if hit:
        for k in ("days", "advance"):
            part = row.get(k)
            if isinstance(part, dict) and str(part.get("error", "")).startswith("429") and k in hit[1]:
                row[k] = hit[1][k]
    now = time.time()
    if "fatal" not in row:
        with _lock:
            _cache[cid] = (now, row)
    return dict(row, cached_at=now, **extra)


def _num(v):
    try:
        v = float(str(v).replace(" ", "").replace(",", "."))
        return v if v > 0 else None
    except (TypeError, ValueError):
        return None


ORDER_FILE = os.path.join(HERE, "order.json")


def load_order():
    try:
        with open(ORDER_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return [x for x in data if isinstance(x, str)]
    except Exception:
        return []


def clients_path():
    for name in ("clients.json", "clients.json.json", "clients.json.txt"):
        if os.path.exists(os.path.join(HERE, name)):
            return os.path.join(HERE, name)
    return None


_file_lock = threading.Lock()

TEMPLATES_FILE = os.path.join(HERE, "templates.json")
DEFAULT_TEMPLATES = [
    {"title": "Приветствие", "text": "Здравствуйте, {имя}! Спасибо за обращение. Подскажите, пожалуйста, адрес и удобное время?"},
    {"title": "Цена", "text": "{имя}, стоимость зависит от объёма работ. Напишите, пожалуйста, подробности — сразу посчитаем."},
    {"title": "Перезвоню", "text": "{имя}, здравствуйте! Оставьте, пожалуйста, номер телефона — перезвоним в течение 10 минут."},
]


def load_templates():
    try:
        with open(TEMPLATES_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return [x for x in data if isinstance(x, dict) and x.get("text")]
    except Exception:
        return DEFAULT_TEMPLATES


def load_clients():
    path = clients_path()
    if not path:
        return [], None
    try:
        with open(path, encoding="utf-8-sig") as f:
            data = json.load(f)
    except Exception as e:
        return [], f"Не получилось прочитать clients.json: {e}"
    clients = [c for c in data if c.get("client_id") and c.get("client_secret")
               and "ВСТАВЬТЕ" not in c["client_id"]]
    return clients, None


def answer_review(name, review_id, message):
    clients, _ = load_clients()
    with _temp_lock:
        all_clients = clients + list(_temp_clients)
    cl = next((c for c in all_clients if c.get("name") == name), None)
    if not cl:
        raise ValueError("клиент не найден")
    message = (message or "").strip()
    if not message:
        raise ValueError("пустой ответ")
    tok = get_token(cl)
    try:
        r = _request("POST", "/ratings/v1/answers", tok,
                     body={"reviewId": int(review_id), "message": message})
    except ApiError as e:
        s = str(e)
        if s.startswith("403"):
            raise ValueError("Авито не даёт отвечать на отзывы через API для этого аккаунта")
        if s.startswith("404"):
            raise ValueError("отзыв не найден")
        if s.startswith("400"):
            raise ValueError("Авито не принял ответ")
        raise ValueError(s)
    with _lock:
        hit = _cache.get(cl["client_id"])
        if hit and isinstance(hit[1].get("reviews"), dict) and "noans" in hit[1]["reviews"]:
            hit[1]["reviews"]["noans"] = [x for x in hit[1]["reviews"]["noans"]
                                          if x.get("id") != int(review_id)]
    return r


# ── Страница ──────────────────────────────────────────────────────────────────

PAGE = r"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Клиенты Авито</title>
<style>
:root{--bg:#f6f5f2;--card:#fff;--ink:#1d1d1b;--mut:#77756f;--line:#e6e3dc;--acc:#0a7cff;--up:#2b8a3e;--down:#d9480f;--soft:#f0eee9;
--crit:#fff0ee;--critb:#d9480f;--warn:#fff6e6;--warnb:#e8a33d;--good:#ecf8ef;--goodb:#2b8a3e;--bar:#c9d9ee}
@media (prefers-color-scheme:dark){:root{--bg:#161615;--card:#1f1f1d;--ink:#eceae4;--mut:#9a978f;--line:#33322e;--acc:#4ea1ff;--up:#69db7c;--down:#ff8a5c;--soft:#2a2a27;
--crit:#33201b;--critb:#ff8a5c;--warn:#2e2618;--warnb:#e8a33d;--good:#1b2b1f;--goodb:#69db7c;--bar:#2f4058}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
.wrap{max-width:1800px;margin:0 auto;padding:22px 16px 48px}
header{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;margin-bottom:16px}
h1{font-size:22px;margin:0}.sub{color:var(--mut);font-size:13px}
.go{background:var(--acc);color:#fff;border:0;border-radius:10px;padding:9px 16px;font-size:14px;cursor:pointer}
.go:disabled{opacity:.5;cursor:wait}
.att{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:12px;margin-bottom:16px}
.col{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px 14px;border-top:3px solid var(--line)}
.col.crit{border-top-color:var(--critb)}.col.warn{border-top-color:var(--warnb)}.col.good{border-top-color:var(--goodb)}
.col h2{font-size:14px;margin:0 0 8px;display:flex;justify-content:space-between}.col h2 span{color:var(--mut);font-weight:400}
.col ul{list-style:none;margin:0;padding:0;display:grid;gap:6px}
.col li{padding:7px 9px;border-radius:8px;font-size:13px}
.crit li{background:var(--crit)}.warn li{background:var(--warn)}.good li{background:var(--good)}
.col li b{font-weight:600}.col .empty{color:var(--mut);font-size:13px}
.tbl{overflow:visible;background:var(--card);border:1px solid var(--line);border-radius:12px}
table{border-collapse:collapse;width:100%}
th,td{padding:8px 7px;border-bottom:1px solid var(--line);text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums;vertical-align:top}
th{font-size:11px;color:var(--mut);font-weight:600;text-transform:uppercase;letter-spacing:.03em;vertical-align:bottom}
th.grp{text-align:center;color:var(--ink)}
th:first-child,td:first-child{text-align:left}
.bl{border-left:1px solid var(--line)}
tr.cl{cursor:pointer}tr.cl:hover>td{background:var(--soft)}
.big{font-size:15px;font-weight:600}
.d{font-size:12px;margin-left:4px}.up{color:var(--up)}.down{color:var(--down)}.mut{color:var(--mut)}
.s{display:block;font-size:12px;color:var(--mut)}
.hot{color:var(--down);font-weight:700}.err{color:var(--down);font-size:12px;white-space:normal}
.tag{display:inline-block;font-size:12px;padding:2px 8px;border-radius:20px;font-weight:600}
.tag.crit{background:var(--crit);color:var(--critb)}.tag.warn{background:var(--warn);color:var(--warnb)}.tag.good{background:var(--good);color:var(--goodb)}.tag.ok{background:var(--soft);color:var(--mut)}
a{color:var(--acc);text-decoration:none}.arr{color:var(--mut);display:inline-block;width:14px}
.det>td{background:var(--soft);padding:0}
.inner{padding:10px 12px 14px 36px;display:grid;gap:10px}
.inner table{min-width:0}.inner th,.inner td{background:transparent;padding:5px 10px;font-size:13px;vertical-align:middle}
.chart{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:8px 10px}
.chart .lg{font-size:12px;color:var(--mut);display:flex;gap:14px;margin-bottom:4px;flex-wrap:wrap}
.chart .lg i{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:4px;vertical-align:-1px}
.cwrap{position:relative}
.dbar{cursor:pointer}.dbar:hover .bg,.dbar.on .bg{fill:var(--soft)}.dbar.on rect:nth-child(3){fill:var(--acc);opacity:.55}
.tip{position:fixed;z-index:5;min-width:250px;max-width:300px;background:var(--card);border:1px solid var(--line);border-radius:14px;padding:11px 14px;box-shadow:0 8px 28px rgba(0,0,0,.18);font-size:13px;pointer-events:auto}
.tip .th{font-weight:700;margin-bottom:6px}.tip .tl{display:flex;align-items:baseline;gap:6px;margin:3px 0}
.tip .tl i{flex:1;border-bottom:1px dotted var(--line);transform:translateY(-3px)}.tip .tl.sub{color:var(--mut);font-size:12px;padding-left:10px;margin:1px 0}
.tip .tl.tot{border-top:1px solid var(--line);padding-top:6px;margin-top:6px}.tip .tl.tot b{font-size:15px}
.tip .tn{color:var(--mut);font-size:11px;margin-top:6px}
.nm{font-weight:600}.hint{display:block;font-size:11.5px;color:var(--acc);margin-left:14px}
.pl{display:block;font-size:11.5px;margin-left:14px;color:var(--mut)}
tr.cl.on>td{background:var(--soft)}td[data-p]:hover{box-shadow:inset 0 0 0 2px var(--acc);border-radius:4px}
.col li[data-n]{cursor:pointer}.col li[data-n]:hover{outline:2px solid var(--line)}
.dh{display:flex;gap:12px;align-items:center;flex-wrap:wrap}
.ptabs{display:flex;background:var(--card);border:1px solid var(--line);border-radius:9px;padding:3px}
.ptabs button{background:none;border:0;color:var(--ink);padding:6px 11px;border-radius:6px;cursor:pointer;font-size:13px}
.ptabs button.on{background:var(--acc);color:#fff}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:8px}
.kpi{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:9px 12px}
.kpi b{font-size:19px;font-variant-numeric:tabular-nums}.kl{display:block;font-size:12px;color:var(--mut)}.kp{display:block;font-size:12px;color:var(--mut)}
tr.inp>td{background:var(--card)!important}
.cwrap{position:relative}
.dbar{cursor:pointer}.dbar:hover .bg,.dbar.on .bg{fill:var(--soft)}.dbar.on rect:nth-child(3){fill:var(--acc);opacity:.55}
.tip{position:fixed;z-index:5;min-width:250px;max-width:300px;background:var(--card);border:1px solid var(--line);border-radius:14px;padding:11px 14px;box-shadow:0 8px 28px rgba(0,0,0,.18);font-size:13px;pointer-events:auto}
.tip .th{font-weight:700;margin-bottom:6px}.tip .tl{display:flex;align-items:baseline;gap:6px;margin:3px 0}
.tip .tl i{flex:1;border-bottom:1px dotted var(--line);transform:translateY(-3px)}.tip .tl.sub{color:var(--mut);font-size:12px;padding-left:10px;margin:1px 0}
.tip .tl.tot{border-top:1px solid var(--line);padding-top:6px;margin-top:6px}.tip .tl.tot b{font-size:15px}
.tip .tn{color:var(--mut);font-size:11px;margin-top:6px}
.ctrl{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.seg{display:flex;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:3px}
.seg button{background:none;border:0;color:var(--ink);padding:7px 12px;border-radius:7px;cursor:pointer;font-size:13px}
.seg button.on{background:var(--acc);color:#fff}
.grip{cursor:grab;color:var(--mut);font-size:16px;padding:0 6px 0 0;user-select:none;display:inline-block}
.grip:hover{color:var(--acc)}tr.dragging{opacity:.4}body.dragmode tr.det{display:none}
tr.dropB>td{box-shadow:inset 0 3px 0 var(--acc)}tr.dropA>td{box-shadow:inset 0 -3px 0 var(--acc)}
.hint,.pl{margin-left:36px!important}
.del{display:block;margin:2px 0 0 36px;background:none;border:0;padding:0;font-size:11.5px;color:var(--mut);cursor:pointer;opacity:0;transition:opacity .15s}
tr.cl:hover .del,.del:focus{opacity:1}.del:hover{color:var(--down);text-decoration:underline}
@media (hover:none){.del{opacity:1}}
.cnt{display:block;margin-top:6px;font-size:15px;font-weight:600;color:var(--ink)}
.cnt .u{font-size:12px;font-weight:400;color:var(--mut)}
.nrp{white-space:nowrap;font-size:12.5px}
.rl{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:8px 10px;display:grid;gap:4px}
.rlh{font-weight:600;font-size:13px;margin-bottom:2px}
.ri{display:flex;gap:10px;align-items:flex-start;padding:6px 8px;border-radius:8px;color:var(--ink);text-decoration:none}
a.ri:hover{background:var(--soft)}.rk{width:68px;flex:none;font-size:14px}.rb{flex:1;min-width:0;white-space:normal;font-size:13px}
.rt{display:block;margin-top:2px}.ri2{display:block;font-size:12px}.go2{color:var(--acc);font-size:12px;flex:none}
.stars{color:#f5a524;letter-spacing:1px;font-size:12px}.stars i{color:var(--line);font-style:normal}
@media (max-width:1400px){body{font-size:13px}.big{font-size:14px}.cnt{font-size:14px}th,td{padding:7px 5px}th{font-size:10.5px}}
td:first-child{white-space:normal;min-width:110px}
th{white-space:normal}td{white-space:normal}.big,.cnt,.d{white-space:nowrap}.s{white-space:normal}
@media (max-width:1300px){.wrap{padding:16px 8px 40px}th,td{padding:6px 4px}.d{margin-left:2px;font-size:11px}.s{font-size:11px}}
.ans{display:flex;gap:8px;align-items:flex-start;margin-top:6px;flex-wrap:wrap}
.ans textarea{flex:1;min-width:220px;font:inherit;font-size:13px;color:var(--ink);background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:6px 8px;resize:vertical}
.go.sm{padding:6px 12px;font-size:13px}.ansmsg{font-size:12px;color:var(--down);align-self:center}
.nrb{font-size:12px;font-weight:600;color:var(--down);white-space:nowrap}
.abtn{display:flex;gap:6px;align-items:flex-start}.qr{position:relative}
.ghost{background:var(--card);color:var(--ink);border:1px solid var(--line);border-radius:10px;cursor:pointer}
.ghost.sm{padding:6px 12px;font-size:13px}.ghost:hover{border-color:var(--acc)}
.pl2{font-size:12px;color:var(--mut);align-self:center}
.qrm{position:absolute;z-index:20;right:0;top:calc(100% + 4px);width:340px;max-height:320px;overflow:auto;background:var(--card);border:1px solid var(--line);border-radius:12px;box-shadow:0 10px 30px rgba(0,0,0,.2);padding:6px}
.qrm button{display:block;width:100%;text-align:left;background:none;border:0;color:var(--ink);padding:7px 9px;border-radius:8px;cursor:pointer;font:inherit;font-size:13px}
.qrm button:hover{background:var(--soft)}
.modal{position:fixed;inset:0;background:rgba(0,0,0,.45);z-index:50;display:flex;align-items:center;justify-content:center;padding:16px}
.mbox{background:var(--card);color:var(--ink);border-radius:14px;padding:20px;width:min(480px,100%);max-height:90vh;overflow:auto}
.mbox h2{margin:0 0 6px;font-size:17px}.mbox p{font-size:13px;color:var(--mut);margin:0 0 16px}
.tl2{display:grid;gap:8px}
.fi{display:grid;gap:6px;margin-bottom:12px}
.fi label{font-size:13px;color:var(--mut)}
.fi input{font:inherit;font-size:14px;color:var(--ink);background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:9px 12px;outline:none;transition:border-color .15s}
.fi input:focus{border-color:var(--acc)}
.mact{display:flex;gap:8px;align-items:center;margin-top:4px}
.msg{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px}
.note{color:var(--mut);font-size:12px;margin-top:12px;line-height:1.55}
/* демо-баннер */
.demo-bar{background:linear-gradient(90deg,#fff8e6,#fff3d6);border-bottom:1px solid #f0c040;padding:10px 20px;display:flex;align-items:center;gap:12px;font-size:13px}
@media (prefers-color-scheme:dark){.demo-bar{background:linear-gradient(90deg,#2e2410,#3a2d10);border-color:#5c4a10}}
.demo-bar b{font-weight:600}.demo-bar .dbtry{margin-left:auto;background:var(--acc);color:#fff;border:0;border-radius:8px;padding:7px 14px;font-size:13px;cursor:pointer}
.errmsg{color:var(--down);font-size:13px;margin-top:8px}
</style></head><body>
<div id="demoBar" class="demo-bar" style="display:none">
  <span>👋 <b>Демо-режим.</b> Данные ненастоящие — для знакомства с панелью.</span>
  <button class="dbtry" onclick="openTryModal()">Проверить свой аккаунт →</button>
</div>
<div class="wrap">
<header><div><h1>Клиенты Авито</h1><div class="sub" id="upd">Загружаю…</div></div>
<div class="ctrl"><div class="seg" id="sortsw"></div>
<button class="ghost" style="padding:9px 14px;font-size:14px" onclick="openTryModal()">+ Свой аккаунт</button>
<button class="go" id="go" onclick="load()">Обновить всё</button></div></header>
<div id="out"></div>
<div class="note">
<b>CPL</b> — цена контакта: расход ÷ контакты. <b>Конверсия</b> — контакты ÷ просмотры. ▲▼ — к прошлому такому же периоду.<br>
Сигналы строятся по <b>7 полным дням против предыдущих 7</b>. Клик по строке клиента — подробная статистика и график за 30 дней.
</div></div>

<!-- Модальное окно: добавить свой аккаунт -->
<div id="tryModal" class="modal" style="display:none" onclick="if(event.target===this)closeTryModal()">
<div class="mbox">
  <h2>Проверить свой аккаунт</h2>
  <p>Введите ключи из кабинета Авито Pro → Интеграции и API.<br>Ключи не сохраняются — пропадут при перезапуске панели.</p>
  <div class="fi"><label>Имя (для отображения)</label><input id="tryName" placeholder="Например: Мой магазин" /></div>
  <div class="fi"><label>client_id</label><input id="tryCid" placeholder="client_id" autocomplete="off" /></div>
  <div class="fi"><label>client_secret</label><input id="trySec" placeholder="client_secret" type="password" autocomplete="off" /></div>
  <div id="tryErr" class="errmsg" style="display:none"></div>
  <div class="mact">
    <button class="go" id="tryBtn" onclick="addTempAccount()">Добавить</button>
    <button class="ghost" style="padding:9px 14px;font-size:14px" onclick="closeTryModal()">Отмена</button>
  </div>
</div></div>

<script>
const T={lowDays:3,zeroSpend:300,trafficDrop:40,staleDays:2,cplUp:20,convDrop:20,contDrop:20,overTarget:10,cplDown:15,contUp:20,minBase:5};
const nf=new Intl.NumberFormat('ru-RU',{maximumFractionDigits:0});
const n=v=>nf.format(Math.round(v||0)), rub=v=>v==null?'—':nf.format(Math.round(v))+' ₽';
const nf1=new Intl.NumberFormat('ru-RU',{minimumFractionDigits:1,maximumFractionDigits:1});
const rub1=v=>v==null?'—':(v<1000?nf1.format(v):nf.format(Math.round(v)))+' ₽';
const pctf=v=>v==null?'—':(v<10?v.toFixed(2):v.toFixed(1)).replace('.',',')+' %';
const esc=s=>String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
let DATA=null, OPEN=new Map(), SORT='signal', PIN=null;

// Модалка
function openTryModal(){document.getElementById('tryModal').style.display='flex';document.getElementById('tryCid').focus();}
function closeTryModal(){document.getElementById('tryModal').style.display='none';document.getElementById('tryErr').style.display='none';}
async function addTempAccount(){
  const name=document.getElementById('tryName').value.trim()||'Мой аккаунт';
  const cid=document.getElementById('tryCid').value.trim();
  const sec=document.getElementById('trySec').value.trim();
  const err=document.getElementById('tryErr');
  if(!cid||!sec){err.textContent='Заполните client_id и client_secret';err.style.display='block';return;}
  const btn=document.getElementById('tryBtn');btn.disabled=true;btn.textContent='Проверяю…';err.style.display='none';
  try{
    const r=await fetch('/add-temp',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({name,client_id:cid,client_secret:sec})});
    const j=await r.json();
    if(!j.ok)throw new Error(j.error||'ошибка');
    closeTryModal();
    document.getElementById('tryName').value='';
    document.getElementById('tryCid').value='';
    document.getElementById('trySec').value='';
    load();
  }catch(e){err.textContent=e.message;err.style.display='block';}
  finally{btn.disabled=false;btn.textContent='Добавить';}
}

const cs=d=>d.cs!=null?d.cs:d.spend;
function sum(list,a,b){const L=list.length;const r={views:0,contacts:0,spend:0,cs:0};
 for(let o=a;o<=b;o++){const d=list[L-1-o];if(d){r.views+=d.views;r.contacts+=d.contacts;r.spend+=d.spend;r.cs+=cs(d);}}
 r.cpl=r.contacts&&r.cs?r.cs/r.contacts:null; r.conv=r.views?r.contacts/r.views*100:null; return r;}
function ch(cur,prev){if(cur==null||prev==null||prev===0)return null;return (cur-prev)/prev*100;}
function delta(cur,prev,goodUp){const p=ch(cur,prev);
 if(p==null||Math.abs(p)<0.05)return '';
 const g=goodUp===null?null:(p>0)===goodUp;const cls=g===null?'mut':(g?'up':'down');
 return `<span class="d ${cls}">${p>0?'▲':'▼'}${Math.abs(p).toFixed(1).replace('.',',')}%</span>`;}
const signed=p=>(p>0?'+':'−')+Math.abs(p).toFixed(0)+'%';

function money(r){let m=0,ok=false;
 if(r.wallet&&!r.wallet.error){m+=r.wallet.real;ok=true;}
 if(r.advance&&!r.advance.error){m+=r.advance.value;ok=true;}
 if(!ok||!r.days||r.days.error)return null;
 const s=sum(r.days.list,1,7).spend/7;return {m,perDay:s,left:s>0?m/s:null};}

function diagnose(r){
 const S={crit:[],warn:[],good:[],tag:null};
 const add=(lvl,tag,text)=>{S[lvl].push(text);if(!S.tag||rank(lvl)<rank(S.tag.lvl))S.tag={lvl,t:tag};};
 if(r.fatal){add('crit','Нет данных',`${esc(r.fatal)}, данные не загружены`);return S;}
 const rv=r.reviews;
 if(rv&&!rv.error&&rv.noans&&rv.noans.length)add('warn','Отзывы',`отзывов без ответа за 7 дн.: ${rv.noans.length}`);
 const mo=money(r);
 if(mo&&mo.left!=null&&mo.left<T.lowDays)
  add('crit','Бюджет',`денег ${rub(mo.m)} — хватит ≈ ${mo.left<1?'меньше дня':Math.floor(mo.left)+' дн.'} при расходе ${rub(mo.perDay)}/день`);
 if(!r.days||r.days.error){add('crit','Нет данных',`статистика не загрузилась`);return S;}
 const L=r.days.list;
 let gap=0;for(let o=1;o<L.length;o++){const d=L[L.length-1-o];if(d.views||d.contacts||d.spend)break;gap++;}
 const hadBefore=sum(L,gap+1,Math.min(gap+30,L.length-1)).views>0;
 if(gap>=T.staleDays&&hadBefore){add('crit','Нет данных',`статистика пустая ${gap} дн. подряд`);return S;}
 const y=sum(L,1,1);
 if(y.contacts===0&&y.spend>=T.zeroSpend)add('crit','Нет контактов',`вчера расход ${rub(y.spend)} и 0 контактов`);
 const w=sum(L,1,7),p=sum(L,8,14);
 const cV=ch(w.views,p.views),cC=ch(w.contacts,p.contacts),cS=ch(w.spend,p.spend),cL=ch(w.cpl,p.cpl),cR=ch(w.conv,p.conv);
 const enough=p.contacts>=T.minBase;
 const trafficCrit=enough&&cV!=null&&cV<=-T.trafficDrop;
 if(trafficCrit)add('crit','Трафик',`просмотры за 7 дн. упали на ${Math.abs(cV).toFixed(0)}%`);
 else if(enough&&cL!=null&&cL>=T.cplUp)add('warn','CPL',`CPL ${rub1(w.cpl)} — вырос на ${cL.toFixed(0)}%`);
 else if(enough&&cR!=null&&cR<=-T.convDrop)add('warn','Конверсия',`конверсия ${pctf(w.conv)} против ${pctf(p.conv)}`);
 else if(enough&&cC!=null&&cC<=-T.contDrop)add('warn','Контакты',`контактов ${n(w.contacts)} — на ${Math.abs(cC).toFixed(0)}% меньше`);
 const tgt=r.target_cpl;
 if(tgt&&w.cpl!=null){const o=(w.cpl-tgt)/tgt*100;if(o>=T.overTarget)add('warn','Выше цели',`CPL ${rub1(w.cpl)} выше цели ${rub(tgt)} на ${o.toFixed(0)}%`);}
 if(enough&&cL!=null&&cL<=-T.cplDown&&(cC==null||cC>-10))add('good','CPL ↓',`CPL ${rub1(w.cpl)} снизился на ${Math.abs(cL).toFixed(0)}%`);
 else if(enough&&cC!=null&&cC>=T.contUp&&(cL==null||cL<10))add('good','Рост',`контактов ${n(w.contacts)} — на ${cC.toFixed(0)}% больше`);
 return S;}
const rank=l=>({crit:0,warn:1,good:2}[l]);
function fmtDate(s){if(!s)return '—';return new Date(s+'T12:00:00').toLocaleDateString('ru-RU',{day:'numeric',month:'short'});}

function attention(diags){
 const box=(lvl,title,emptyText)=>{const items=[];
  for(const [r,S] of diags)for(const t of S[lvl])items.push(`<li data-n="${esc(r.name)}" onclick="openClient(this.dataset.n)"><b>${esc(r.name)}</b>: ${t}</li>`);
  return `<div class="col ${lvl}"><h2>${title}<span>${items.length||''}</span></h2>${items.length?`<ul>${items.join('')}</ul>`:`<div class="empty">${emptyText}</div>`}</div>`;};
 return `<div class="att">${box('crit','Критично','Критичных проблем нет')}${box('warn','Ухудшение','Устойчивых ухудшений нет')}${box('good','Положительная динамика','Пока без заметных улучшений')}</div>`;}

function cplCell(list,a,b,pa,pb,cls,partial,per){const c=sum(list,a,b),p=sum(list,pa,pb);
 if(c.contacts&&!c.cs)c.cpl=null;
 return `<td class="${cls||''}" data-p="${per}"><span class="big">${rub1(c.cpl)}</span>${delta(c.cpl,p.cpl,false)}<span class="cnt">${n(c.contacts)} <span class="u">конт.</span>${delta(c.contacts,p.contacts,true)}</span></td>`;}

let SORT_INIT=false;
function sortSwitch(){
 const sw=document.getElementById('sortsw');
 if(SORT_INIT)return;SORT_INIT=true;
 sw.innerHTML=['signal','mine'].map(v=>`<button class="${SORT===v?'on':''}" onclick="setSrt('${v}')">${v==='signal'?'Сначала проблемные':'Мой порядок'}</button>`).join('');
}
function setSrt(v){SORT=v;SORT_INIT=false;safeRender();}

function render(){
 PIN=null;const out=document.getElementById('out');if(!DATA)return;
 if(DATA.demo)document.getElementById('demoBar').style.display='flex';
 if(DATA.error){out.innerHTML=`<div class="msg">${esc(DATA.error)}</div>`;return;}
 const diags=DATA.clients.map(r=>[r,diagnose(r)]);
 sortSwitch();
 if(SORT==='mine'){const pos=new Map((DATA.order||[]).map((x,i)=>[x,i]));
  const base=new Map(DATA.clients.map((c,i)=>[c.name,i]));
  diags.sort((a,b)=>(pos.has(a[0].name)?pos.get(a[0].name):1e6+base.get(a[0].name))-(pos.has(b[0].name)?pos.get(b[0].name):1e6+base.get(b[0].name)));}
 else diags.sort((a,b)=>(a[1].tag?rank(a[1].tag.lvl):3)-(b[1].tag?rank(b[1].tag.lvl):3));
 let rows='';
 for(const [r,S] of diags){
  const nrc=r.noreply&&!r.noreply.error?r.noreply.list.length:0;
  const name=`<b class="nm">${esc(r.name)}</b>${nrc?` <span class="nrb" title="Чаты без ответа">💬 ${nrc}</span>`:''}`;
  const tag=S.tag?`<span class="tag ${S.tag.lvl}">${S.tag.t}</span>`:'<span class="tag ok">Норма</span>';
  if(r.fatal){rows+=`<tr class="cl" data-n="${esc(r.name)}"><td><span class="arr"></span>${name}</td><td colspan="9"><span class="err">${esc(r.fatal)}</span></td><td class="bl">${tag}</td></tr>`;continue;}
  const ds=r.days,mo=money(r);
  const tdU=reviewsCell(r.reviews);
  const parts=[];if(r.wallet&&!r.wallet.error)parts.push(`кошелёк ${rub(r.wallet.real)}`);if(r.advance&&!r.advance.error)parts.push(`аванс ${rub(r.advance.value)}`);
  const tdM=`<td class="bl">${mo?`<span class="big">${rub(mo.m)}</span>`:'—'}<span class="s">${parts.join(' · ')||''}</span>${mo&&mo.left!=null?`<span class="s ${mo.left<T.lowDays?'hot':''}">хватит ≈ ${mo.left>=99?'99+':Math.floor(mo.left)} дн.</span>`:''}</td>`;
  let tdP;
  if(!ds||ds.error)tdP=`<td colspan="8" class="bl"><span class="err">статистика: ${esc(ds?ds.error:'нет данных')}</span></td>`;
  else{const L=ds.list,w=sum(L,1,7),p=sum(L,8,14),tgt=r.target_cpl;
   const tg=tgt&&w.cpl!=null?`<span class="s ${w.cpl>tgt*(1+T.overTarget/100)?'hot':''}">цель ${rub(tgt)}</span>`:'';
   tdP=cplCell(L,0,0,1,1,'bl',true,'today')+cplCell(L,1,1,2,2,'',false,'yday')
    +`<td class="bl" data-p="w"><span class="big">${rub1(w.cpl)}</span>${delta(w.cpl,p.cpl,false)}${tg}</td>`
    +cplCell(L,1,30,31,60,'',false,'m30')
    +`<td class="bl" data-p="w">${n(w.contacts)}${delta(w.contacts,p.contacts,true)}</td>`
    +`<td data-p="w">${pctf(w.conv)}${delta(w.conv,p.conv,true)}<span class="s">${n(w.views)} просм.</span></td>`
    +`<td data-p="w">${rub(w.spend)}${delta(w.spend,p.spend,null)}</td>`;}}
  const isOpen=OPEN.has(r.name);
  rows+=`<tr class="cl${isOpen?' on':''}" data-n="${esc(r.name)}" onclick="rowClick(event,this)"><td><span class="arr">${isOpen?'▾':'▸'}</span>${name}</td>${tdU}${tdM}${tdP}<td class="bl">${tag}</td></tr>`;
  if(isOpen){const per=OPEN.get(r.name)||'w';rows+=detailRow(r,per);}
 }
 out.innerHTML=attention(diags)+`<div class="tbl"><table><thead>
 <tr><th rowspan="2">Клиент</th><th rowspan="2">Отзывы<br><span class="part">рейтинг · за 7 дн.</span></th><th rowspan="2" class="bl">Деньги</th>
 <th colspan="4" class="grp bl">CPL · цена контакта</th><th colspan="3" class="grp bl">Последние 7 дней</th><th rowspan="2" class="bl">Сигнал</th></tr>
 <tr><th class="bl">Сегодня <span class="part">неполный</span></th><th>Вчера</th><th class="bl">7 дней</th><th>30 дней</th>
 <th class="bl">Контакты</th><th>Конверсия</th><th>Расход</th></tr></thead><tbody>${rows}</tbody></table></div>`;}

function reviewsCell(rv){
 if(!rv)return '<td class="mut">—</td>';
 if(rv.error)return `<td><span class="mut" title="${esc(rv.error)}">нет доступа</span></td>`;
 const na=(rv.noans||[]).length,rt=rv.rating;
 const rat=rt&&rt.score!=null?`<span class="s"><span class="stars">★</span> ${String(rt.score).replace('.',',')} · ${n(rt.count)} отз.</span>`:'';
 return `<td>${rat}${na?`<span class="big hot">${na}</span><span class="s">без ответа</span>`:'<span class="mut">все отвечены</span>'}<span class="s">новых: ${rv.new||0}</span></td>`;}
function stars(s){s=Math.max(0,Math.min(5,s||0));return '<span class="stars">'+'★'.repeat(s)+'<i>'+'★'.repeat(5-s)+'</i></span>';}
function ago(ts){const m=Math.round((Date.now()/1000-ts)/60);if(m<60)return m+' мин назад';const h=Math.round(m/60);if(h<24)return h+' ч назад';return Math.round(h/24)+' дн назад';}

const PER={today:{t:'Сегодня',a:0,b:0,pa:1,pb:1,cmp:'вчера',rows:[0,13]},
 yday:{t:'Вчера',a:1,b:1,pa:2,pb:2,cmp:'позавчера',rows:[0,13]},
 w:{t:'7 дней',a:1,b:7,pa:8,pb:14,cmp:'прошлые 7 дней',rows:[0,14]},
 m30:{t:'30 дней',a:1,b:30,pa:31,pb:60,cmp:'прошлые 30 дней',rows:[0,30]}};
function kpi(label,cur,prev,fmt,goodUp,partial){
 return `<div class="kpi"><span class="kl">${label}</span><b>${fmt(cur)}</b>${delta(cur,prev,goodUp)}<span class="kp">было ${fmt(prev)}</span></div>`;}
function detailRow(r,per){const list=(r.days&&r.days.list)||[],tgt=r.target_cpl,P=PER[per]||PER.w,L=list.length;
 const c=sum(list,P.a,P.b),p=sum(list,P.pa,P.pb),part=per==='today';
 const tabs=Object.entries(PER).map(([k,v])=>`<button class="${k===per?'on':''}" data-n="${esc(r.name)}" data-p="${k}" onclick="setPer(event,this)">${v.t}</button>`).join('');
 const dates=L>0?(P.a===P.b?fmtDate(list[L-1-P.a]?.date||''):`${fmtDate(list[Math.max(0,L-1-P.b)]?.date||'')} — ${fmtDate(list[Math.max(0,L-1-P.a)]?.date||'')}`):'';
 const kp=`<div class="kpis">
  ${kpi('Цена контакта',c.cpl,p.cpl,rub1,false)}
  ${kpi('Контакты',c.contacts,p.contacts,n,true,part)}
  ${kpi('Просмотры',c.views,p.views,n,true,part)}
  ${kpi('Конверсия',c.conv,p.conv,pctf,true)}
  ${kpi('Расход',c.spend,p.spend,rub,null,part)}
  ${tgt?`<div class="kpi"><span class="kl">Цель CPL</span><b>${rub(tgt)}</b>${c.cpl!=null?`<span class="d ${c.cpl>tgt?'down':'up'}">${c.cpl>tgt?'выше на '+((c.cpl-tgt)/tgt*100).toFixed(0)+'%':'в пределах цели'}</span>`:''}</div>`:''}</div>`;
 let tr='';
 for(let o=P.rows[0];o<=Math.min(P.rows[1],L-2);o++){const d=list[L-1-o],q=list[L-2-o],inP=o>=P.a&&o<=P.b;
  const dc=sum(list,o,o),qc=sum(list,o+1,o+1);
  const lbl=o===0?'сегодня <span class="part">неполный</span>':o===1?'вчера':new Date(d.date+'T12:00:00').toLocaleDateString('ru-RU',{day:'numeric',month:'short',weekday:'short'});
  tr+=`<tr class="${inP?'inp':''}"><td>${lbl}</td><td><span class="big">${rub1(dc.cpl)}</span>${delta(dc.cpl,qc.cpl,false)}</td><td>${n(d.contacts)}${delta(d.contacts,q.contacts,true)}</td><td>${pctf(dc.conv)}${delta(dc.conv,qc.conv,true)}</td><td>${n(d.views)}${delta(d.views,q.views,true)}</td><td>${rub(d.spend)}${delta(d.spend,q.spend,null)}</td></tr>`;}
 return `<tr class="det"><td colspan="12"><div class="inner">
  <div class="dh"><div class="ptabs">${tabs}</div><div class="mut">${esc(r.name)} · ${P.t.toLowerCase()} (${dates}) против: ${P.cmp}${part?' · день неполный, сравнение со всем вчерашним днём, как в Авито':''}</div></div>
  ${kp}${replyLists(r)}${chart(r)}
  <table><thead><tr><th>День</th><th>CPL</th><th>Контакты</th><th>Конверсия</th><th>Просмотры</th><th>Расход</th></tr></thead><tbody>${tr}</tbody></table>
  <div class="mut" style="font-size:12px">Подсвечены дни выбранного периода. ▲▼ в таблице — к предыдущему дню.</div></div></td></tr>`;}
const CH={};
function chart(r){const list=r.days.list,tgt=r.target_cpl,L=list.length,days=[];
 for(let o=30;o>=0;o--)days.push(list[L-1-o]);
 const key=r.name;CH[key]={days,tgt};
 const W=1100,H=190,pl=30,pr=52,pt=10,pb=40,iw=W-pl-pr,ih=H-pt-pb,bw=iw/days.length;
 const mc=Math.max(1,...days.map(d=>d.contacts));
 const cpls=days.map(d=>d.contacts?cs(d)/d.contacts:null),ml=Math.max(1,...cpls.filter(v=>v!=null),tgt||0);
 let g='',pts=[];
 days.forEach((d,i)=>{const x=pl+i*bw,h=d.contacts/mc*ih,today=i===days.length-1;
  const dt=new Date(d.date+'T12:00:00'),wd=dt.getDay(),we=wd===0||wd===6;
  g+=`<g class="dbar" data-k="${esc(key)}" data-i="${i}">
   <rect class="hit" x="${(x).toFixed(1)}" y="${pt}" width="${bw.toFixed(1)}" height="${ih+pb}" fill="transparent"/>
   <rect class="bg" x="${(x+1).toFixed(1)}" y="${pt}" width="${(bw-2).toFixed(1)}" height="${ih}" rx="3" fill="${today?'var(--soft)':'transparent'}"/>
   <rect x="${(x+1.5).toFixed(1)}" y="${(pt+ih-h).toFixed(1)}" width="${(bw-3).toFixed(1)}" height="${Math.max(h,d.contacts?2:0).toFixed(1)}" rx="3" fill="var(--bar)"/>
   <text x="${(x+bw/2).toFixed(1)}" y="${pt+ih+14}" text-anchor="middle" font-size="10.5" fill="${we?'var(--down)':'var(--ink)'}">${dt.getDate()}</text>
   <text x="${(x+bw/2).toFixed(1)}" y="${pt+ih+27}" text-anchor="middle" font-size="9.5" fill="var(--mut)">${['вс','пн','вт','ср','чт','пт','сб'][wd]}</text></g>`;
  if(cpls[i]!=null&&!today)pts.push(`${(x+bw/2).toFixed(1)},${(pt+ih-cpls[i]/ml*ih).toFixed(1)}`);});
 const ty=tgt?(pt+ih-tgt/ml*ih).toFixed(1):0;
 const tl=tgt?`<line x1="${pl}" x2="${W-pr}" y1="${ty}" y2="${ty}" stroke="var(--warnb)" stroke-dasharray="4 3" pointer-events="none"/><text x="${W-pr+4}" y="${+ty+4}" font-size="10" fill="var(--warnb)">цель</text>`:'';
 const ax=`<text x="${pl-5}" y="${pt+9}" text-anchor="end" font-size="10" fill="var(--mut)">${mc}</text><text x="${pl-5}" y="${pt+ih}" text-anchor="end" font-size="10" fill="var(--mut)">0</text>
  <text x="${W-pr+4}" y="${pt+9}" font-size="10" fill="var(--acc)">${n(ml)} ₽</text>`;
 return `<div class="chart" data-k="${esc(key)}"><div class="lg"><span><i style="background:var(--bar)"></i>контакты</span><span><i style="background:var(--acc)"></i>цена контакта</span>${tgt?'<span><i style="background:var(--warnb)"></i>цель</span>':''}<span>наведите или нажмите на день — покажу всё за этот день</span></div>
 <div class="cwrap"><svg viewBox="0 0 ${W} ${H}" width="100%" style="display:block" role="img" aria-label="Контакты и цена контакта по дням">${g}${tl}<polyline points="${pts.join(' ')}" fill="none" stroke="var(--acc)" stroke-width="2" stroke-linejoin="round" pointer-events="none"/>${ax}</svg><div class="tip" hidden></div></div></div>`;}
function tipHtml(d,prev,tgt){
 const dt=new Date(d.date+'T12:00:00'),today=d.date===DATA_TODAY();
 const cpl=d.contacts?cs(d)/d.contacts:null,pc=prev&&prev.contacts?cs(prev)/prev.contacts:null;
 const conv=d.views?d.contacts/d.views*100:null;
 const line=(label,val,extra)=>`<div class="tl"><span>${label}</span><i></i><b>${val}</b>${extra||''}</div>`;
 const sub=(label,val)=>`<div class="tl sub"><span>${label}</span><i></i><span>${val}</span></div>`;
 let h=`<div class="th">${dt.toLocaleDateString('ru-RU',{day:'numeric',month:'long',weekday:'short'})}${today?' <span class="part">неполный день</span>':''}</div>`;
 h+=line('Контакты',n(d.contacts),prev?delta(d.contacts,prev.contacts,true):'');
 if(d.phone!=null)h+=sub('посмотрели телефон',n(d.phone));
 if(d.chat!=null)h+=sub('написали в чат',n(d.chat));
 h+=line('Просмотры',n(d.views),prev?delta(d.views,prev.views,true):'');
 h+=line('Конверсия',pctf(conv));
 h+=line('Расход',rub(d.spend),prev?delta(d.spend,prev.spend,null):'');
 if(d.promo!=null)h+=sub('на продвижение',rub(d.promo));
 if(d.presence!=null)h+=sub('размещение и целевые',rub(d.presence));
 if(d.rest)h+=sub('прочее (тариф и др.)',rub(d.rest));
 if(d.bonus)h+=sub('из них бонусами',rub(d.bonus));
 h+=`<div class="tl tot"><span>Цена контакта</span><i></i><b>${rub1(cpl)}</b>${prev?delta(cpl,pc,false):''}</div>`;
 if(tgt&&cpl!=null)h+=`<div class="tl sub"><span>цель ${rub(tgt)}</span><i></i><span class="${cpl>tgt?'down':'up'}">${cpl>tgt?'выше на '+((cpl-tgt)/tgt*100).toFixed(0)+'%':'в пределах'}</span></div>`;
 return h+(prev?'<div class="tn">▲▼ — к предыдущему дню</div>':'');}
function DATA_TODAY(){const c=DATA&&DATA.clients.find(x=>x.days&&x.days.list);return c?c.days.list[c.days.list.length-1].date:'';}
function showTip(gEl,pin){const k=gEl.dataset.k,i=+gEl.dataset.i,C=CH[k];if(!C)return;
 const box=gEl.closest('.cwrap'),tip=box.querySelector('.tip');
 box.querySelectorAll('.dbar.on').forEach(x=>x.classList.remove('on'));gEl.classList.add('on');
 tip.innerHTML=tipHtml(C.days[i],C.days[i-1],C.tgt);tip.hidden=false;
 placeTip(gEl,tip);if(pin)PIN=gEl;}
function placeTip(gEl,tip){
 const gr=gEl.getBoundingClientRect(),w=tip.offsetWidth,h=tip.offsetHeight;
 let x=gr.right+8;if(x+w>innerWidth-8)x=gr.left-w-8;
 let y=gr.top+gr.height/2-h/2;y=Math.min(Math.max(8,y),innerHeight-h-8);
 tip.style.left=Math.max(8,x)+'px';tip.style.top=y+'px';}
addEventListener('scroll',()=>{if(PIN&&document.contains(PIN))placeTip(PIN,PIN.closest('.cwrap').querySelector('.tip'));
 else document.querySelectorAll('.cwrap').forEach(hideTip);},true);
function hideTip(box){if(PIN&&box.contains(PIN))return;box.querySelector('.tip').hidden=true;box.querySelectorAll('.dbar.on').forEach(x=>x.classList.remove('on'));}
document.addEventListener('mouseover',e=>{const g=e.target.closest&&e.target.closest('.dbar');if(g&&!PIN)showTip(g,false);});
document.addEventListener('mouseout',e=>{const b=e.target.closest&&e.target.closest('.cwrap');if(b&&!b.contains(e.relatedTarget))hideTip(b);});
document.addEventListener('click',e=>{const g=e.target.closest&&e.target.closest('.dbar');
 if(g){e.stopPropagation();if(PIN===g){PIN=null;}else{PIN=null;showTip(g,true);}return;}
 if(PIN&&!(e.target.closest&&e.target.closest('.tip'))){const b=PIN.closest('.cwrap');PIN=null;if(b)hideTip(b);}},true);
const DRAFT={};
document.addEventListener('input',e=>{const ta=e.target;if(ta.tagName==='TEXTAREA'&&ta.dataset.k)DRAFT[ta.dataset.k]=ta.value;});
async function sendAnswer(btn){const name=btn.dataset.n,id=+btn.dataset.id,box=btn.closest('.ans'),ta=box.querySelector('textarea'),msg=box.querySelector('.ansmsg');
 const text=ta.value.trim();if(!text){msg.textContent='Напишите ответ';return;}
 if(!confirm(`Опубликовать ответ на отзыв от имени «${name}»?\n\n«${text}»`))return;
 btn.disabled=true;msg.textContent='Отправляю…';
 try{const r=await fetch('/answer',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name,reviewId:id,message:text})});
  const j=await r.json();if(!j.ok)throw new Error(j.error||'ошибка');
  delete DRAFT[name+'|'+id];
  const c=DATA.clients.find(x=>x.name===name);if(c&&c.reviews&&c.reviews.noans)c.reviews.noans=c.reviews.noans.filter(x=>x.id!==id);
  safeRender();}
 catch(e){btn.disabled=false;msg.textContent='Не отправлено: '+e.message;}}
function replyLists(r){let h='';
 const rv=r.reviews;
 if(rv&&!rv.error&&rv.noans&&rv.noans.length){
  h+=`<div class="rl"><div class="rlh">Отзывы без ответа · ${rv.noans.length}</div>`+rv.noans.map(x=>
   `<div class="ri"><span class="rk">${stars(x.score)}</span><span class="rb"><b>${esc(x.who||'Покупатель')}</b> <span class="mut">· ${ago(x.ts)}${x.rejected?' · прошлый ответ отклонён':''}</span>
    ${x.text?`<span class="rt">«${esc(x.text)}»</span>`:''}${x.item?`<span class="mut ri2">${esc(x.item)}</span>`:''}
    <span class="ans" onclick="event.stopPropagation()"><textarea rows="2" placeholder="Ваш ответ…" data-k="${esc(r.name)}|${x.id}">${esc(DRAFT[r.name+'|'+x.id]||'')}</textarea>
    <button class="go sm" data-n="${esc(r.name)}" data-id="${x.id}" onclick="sendAnswer(this)">Ответить</button><span class="ansmsg"></span></span></span></div>`).join('')+`</div>`;}
 return h;}
function safeRender(){try{render();}catch(e){document.getElementById('out').insertAdjacentHTML('afterbegin','<div class="msg">Ошибка: '+esc(e.message)+'</div>');console.error(e);}}
function rowClick(ev,tr){const name=tr.dataset.n,td=ev.target.closest('td'),per=td&&td.dataset.p;
 if(per){OPEN.get(name)===per?OPEN.delete(name):OPEN.set(name,per);}
 else{OPEN.has(name)?OPEN.delete(name):OPEN.set(name,'w');}
 safeRender();}
function setPer(ev,btn){ev.stopPropagation();OPEN.set(btn.dataset.n,btn.dataset.p);safeRender();}
function openClient(name){if(!OPEN.has(name))OPEN.set(name,'w');safeRender();}
async function load(){
 const b=document.getElementById('go');b.disabled=true;b.textContent='Загружаю…';
 let r;try{r=await fetch('/data');DATA=await r.json();}
 catch(e){document.getElementById('out').innerHTML='<div class="msg">Панель не отвечает.</div>';b.disabled=false;b.textContent='Обновить всё';return;}
 try{render();
  const t=DATA.clients&&DATA.clients.length?Math.min(...DATA.clients.map(c=>c.cached_at||Date.now()/1000)):Date.now()/1000;
  document.getElementById('upd').textContent=DATA.demo?'Демо-данные · '+new Date().toLocaleTimeString('ru-RU'):'Данные Авито на '+new Date(t*1000).toLocaleTimeString('ru-RU');}
 catch(e){document.getElementById('out').innerHTML='<div class="msg">Ошибка отображения: '+esc(e.message)+'</div>';console.error(e);}
 b.disabled=false;b.textContent='Обновить всё';}
load();
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype):
        b = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path == "/":
            self._send(200, PAGE, "text/html")
        elif self.path == "/data":
            real_clients, err = load_clients()
            with _temp_lock:
                temp = list(_temp_clients)
            all_clients = real_clients + temp
            if not all_clients:
                # Нет реальных клиентов — возвращаем демо
                self._send(200, json.dumps(make_demo_data(), ensure_ascii=False), "application/json")
                return
            with ThreadPoolExecutor(max_workers=8) as ex:
                rows = list(ex.map(fetch_cached, all_clients))
            demo_flag = len(real_clients) == 0
            self._send(200, json.dumps(
                {"clients": rows, "order": load_order(), "demo": demo_flag},
                ensure_ascii=False), "application/json")
        else:
            self._send(404, "not found", "text/plain")

    def do_POST(self):
        if self.path == "/add-temp":
            try:
                n = int(self.headers.get("Content-Length", 0))
                d = json.loads(self.rfile.read(n).decode("utf-8"))
                cid = (d.get("client_id") or "").strip()
                sec = (d.get("client_secret") or "").strip()
                name = (d.get("name") or cid[:8] or "Мой аккаунт").strip()
                if not cid or not sec:
                    raise ValueError("Укажите client_id и client_secret")
                # Проверяем ключи
                client = {"client_id": cid, "client_secret": sec, "name": name}
                tok = get_token(client)  # бросит ApiError если неверно
                with _temp_lock:
                    _temp_clients[:] = [c for c in _temp_clients if c["client_id"] != cid]
                    _temp_clients.append(client)
                self._send(200, '{"ok":true}', "application/json")
            except (ApiError, ValueError) as e:
                self._send(400, json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False), "application/json")
            except Exception as e:
                self._send(400, json.dumps({"ok": False, "error": f"ошибка: {e}"}, ensure_ascii=False), "application/json")
            return
        if self.path == "/answer":
            try:
                n = int(self.headers.get("Content-Length", 0))
                d = json.loads(self.rfile.read(n).decode("utf-8"))
                answer_review(d.get("name"), d.get("reviewId"), d.get("message"))
                self._send(200, '{"ok":true}', "application/json")
            except Exception as e:
                self._send(400, json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False), "application/json")
            return
        self._send(404, "not found", "text/plain")

    def log_message(self, *a):
        pass


def main():
    try:
        srv = ThreadingHTTPServer((HOST, PORT), Handler)
    except OSError:
        print(f"Порт {PORT} занят. Попробуйте закрыть другое окно панели.")
        input("Нажмите Enter…")
        return
    local_url = f"http://127.0.0.1:{PORT}"
    print(f"Панель запущена: {local_url}")
    print("Не закрывайте это окно. Остановить: Ctrl+C")
    print()
    print("Чтобы поделиться ссылкой с другими — запустите ngrok:")
    print(f"  ngrok http {PORT}")
    threading.Timer(1.0, lambda: webbrowser.open(local_url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nОстановлено.")
        sys.exit(0)


if __name__ == "__main__":
    main()
