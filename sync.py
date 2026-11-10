#!/usr/bin/env python3
"""
base-vat-sync
-------------
Przepisuje numer VAT UE zweryfikowany w Shopify (Shopify Tax / VIES) do zamowienia w Base
i ustawia stawke 0% (odwrotne obciazenie), tak jak robi to regula WDT w Base.

Druga sciezka (komentarz klienta): jesli zamowienie jedzie do kraju UE (nie PL), pole NIP
w Base jest puste, a w komentarzu klienta (aplikacja B2B) jest numer w linii
"EU VAT Validation: ...", skrypt sam sprawdza numer w VIES (nie ufa statusowi z aplikacji)
i dopiero wtedy wpisuje go do pola NIP oraz ustawia VAT 0% na pozycjach (regula WDT w Base
uruchamia sie tylko przy pobraniu zamowienia, wiec pozniejszego wpisu NIP sama nie obsluzy).
Przy numerze nieprawidlowym albo gdy VIES nie odpowiada - nic nie zmienia, tylko loguje.

Zasady bezpieczenstwa:
  * Repozytorium jest PUBLICZNE, wiec w logach NIE wypisujemy zadnych danych klientow
    (imion, adresow, e-maili, numerow VAT). Tylko numery zamowien i liczniki.
  * Tokeny sa wylacznie w GitHub Secrets (zmienne srodowiskowe), nigdy w kodzie.
  * Domyslnie DRY_RUN=1: skrypt niczego nie zapisuje w Base, tylko pokazuje co by zrobil.
  * Zamowienia, ktore maja juz NIP albo znacznik [VAT-AUTO], sa pomijane (ponowne
    uruchomienie jest bezpieczne).
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

MARKER = "[VAT-AUTO]"
SHOPS = {
    "ehair": {
        "domain": "c2cae8-b1.myshopify.com",
        "id_env": "EHAIR_CLIENT_ID",
        "secret_env": "EHAIR_CLIENT_SECRET",
        "base_name": os.environ.get("BASE_NAME_EHAIR", "e-hairshop.com"),
    },
    "herflow": {
        "domain": "0nsjvw-3c.myshopify.com",
        "id_env": "HERFLOW_CLIENT_ID",
        "secret_env": "HERFLOW_CLIENT_SECRET",
        "base_name": os.environ.get("BASE_NAME_HERFLOW", "her flow"),
    },
}

DRY_RUN = os.environ.get("DRY_RUN", "1") != "0"
LOOKBACK_HOURS = int(os.environ.get("LOOKBACK_HOURS", "6"))
WAIT_SECONDS = int(os.environ.get("WAIT_SECONDS", "300"))
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "10"))


def log(msg):
    print(msg, flush=True)


# ----------------------------------------------------------------------------
# Rozpoznawanie wpisu o VAT na osi czasu zamowienia w Shopify
# ----------------------------------------------------------------------------
VAT_RE = re.compile(r"(?:numer(?:u)? VAT|VAT number|VAT ID)[^:]*:\s*([A-Za-z]{2}[ .\-]?[A-Za-z0-9]{2,16})(?![A-Za-z0-9])",
                    re.IGNORECASE)
INVALID_RE = re.compile(r"nieprawid|niewa[zż]n|invalid|not valid", re.IGNORECASE)
VALID_RE = re.compile(r"prawid[lł]owy|wa[zż]ny|\bvalid\b", re.IGNORECASE)
REVERSE_RE = re.compile(r"odwrotne obci[aą][zż]enie|reverse charge", re.IGNORECASE)


def parse_vat_event(message):
    """Zwraca (numer_vat, odwrotne_obciazenie) albo None, jesli to nie jest wpis o waznym numerze VAT."""
    if not message:
        return None
    text = re.sub(r"<[^>]+>", "", message).strip()
    if INVALID_RE.search(text):
        return None
    if not VALID_RE.search(text):
        return None
    m = VAT_RE.search(text)
    if not m:
        return None
    vat = re.sub(r"[^A-Za-z0-9]", "", m.group(1)).upper()
    if len(vat) < 4:
        return None
    return vat, bool(REVERSE_RE.search(text))


def extract_vat(events):
    """Z listy komunikatow oredzia wybiera pierwszy wpis o waznym VAT."""
    for msg in events:
        parsed = parse_vat_event(msg)
        if parsed:
            return parsed
    return None


# ----------------------------------------------------------------------------
# Shopify
# ----------------------------------------------------------------------------
def http_json(url, data=None, headers=None, form=False):
    headers = dict(headers or {})
    body = None
    if data is not None:
        if form:
            body = urllib.parse.urlencode(data).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            body = json.dumps(data).encode()
            headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers, method="POST" if body else "GET")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        # Nie wypisujemy tresci odpowiedzi (moze zawierac dane), tylko kod.
        raise RuntimeError(f"HTTP {e.code} z {urllib.parse.urlparse(url).netloc}") from None


def shopify_token(shop):
    cid = os.environ.get(shop["id_env"])
    secret = os.environ.get(shop["secret_env"])
    if not cid or not secret:
        raise RuntimeError(f"Brak sekretow {shop['id_env']} / {shop['secret_env']}")
    res = http_json(
        f"https://{shop['domain']}/admin/oauth/access_token",
        {"client_id": cid, "client_secret": secret, "grant_type": "client_credentials"},
    )
    token = res.get("access_token")
    if not token:
        raise RuntimeError("Shopify nie zwrocil access_token")
    return token


def shopify_gql(shop, token, query, variables=None):
    res = http_json(
        f"https://{shop['domain']}/admin/api/2026-01/graphql.json",
        {"query": query, "variables": variables or {}},
        {"X-Shopify-Access-Token": token},
    )
    if res.get("errors"):
        raise RuntimeError("Shopify GraphQL: " + json.dumps(res["errors"])[:300])
    return res["data"]


ORDERS_Q = """
query($q: String!, $after: String) {
  orders(first: 50, query: $q, after: $after, sortKey: CREATED_AT, reverse: true) {
    pageInfo { hasNextPage endCursor }
    nodes {
      legacyResourceId
      name
      createdAt
      events(first: 40) { nodes { message } }
    }
  }
}
"""


def shopify_orders(shop, token, query):
    out, after = [], None
    for _ in range(10):  # maks. 500 zamowien na przebieg
        data = shopify_gql(shop, token, ORDERS_Q, {"q": query, "after": after})["orders"]
        for n in data["nodes"]:
            msgs = [e.get("message") or "" for e in n["events"]["nodes"]]
            out.append({
                "legacy_id": str(n["legacyResourceId"]),
                "name": n["name"],
                "created_at": n["createdAt"],
                "vat": extract_vat(msgs),
                "events": len(msgs),
            })
        if not data["pageInfo"]["hasNextPage"]:
            break
        after = data["pageInfo"]["endCursor"]
    return out


# ----------------------------------------------------------------------------
# Base (BaseLinker)
# ----------------------------------------------------------------------------
def base_call(method, params):
    token = os.environ.get("BASELINKER_TOKEN")
    if not token:
        raise RuntimeError("Brak sekretu BASELINKER_TOKEN")
    res = http_json(
        "https://api.baselinker.com/connector.php",
        {"method": method, "parameters": json.dumps(params)},
        {"X-BLToken": token},
        form=True,
    )
    if res.get("status") != "SUCCESS":
        raise RuntimeError(f"Base {method}: {res.get('error_code')} {str(res.get('error_message'))[:120]}")
    return res


def base_shop_id(shop_name):
    sources = base_call("getOrderSources", {}).get("sources", {}).get("shop", {})
    for sid, name in sources.items():
        if name.strip().lower() == shop_name.strip().lower():
            return int(sid)
    raise RuntimeError(f"Nie znaleziono w Base sklepu o nazwie '{shop_name}' (dostepne ID: {sorted(sources)})")


def base_orders(source_id, since_ts):
    out, ts = [], since_ts
    for _ in range(20):
        res = base_call("getOrders", {
            "date_confirmed_from": ts,
            "get_unconfirmed_orders": False,
            "filter_order_source": "shop",
            "filter_order_source_id": source_id,
        })
        batch = res.get("orders", [])
        out.extend(batch)
        if len(batch) < 100:
            break
        ts = max(int(o["date_confirmed"]) for o in batch) + 1
    return out


def match_keys(order):
    digits = re.sub(r"\D", "", order["name"])
    keys = {order["legacy_id"], digits}
    if digits and int(digits) > 10000:
        keys.add(str(int(digits) - 10000))  # w Base numer w sklepie bywa pomniejszony o 10000
    return keys


def find_base_order(order, base_list):
    keys = match_keys(order)
    for b in base_list:
        for field in ("external_order_id", "shop_order_id"):
            if str(b.get(field, "")) in keys:
                return b, field
    return None, None


def process(order, base_order, matched_by):
    """Zwraca krotki opis wykonanej (lub planowanej) akcji. Bez danych klienta."""
    name = order["name"]
    vat, reverse = order["vat"]
    comments = base_order.get("admin_comments") or ""
    if MARKER in comments:
        return f"{name}: pomijam (juz obsluzone)"
    if (base_order.get("invoice_nip") or "").strip():
        return f"{name}: pomijam (w Base jest juz NIP)"

    products = base_order.get("products", [])
    to_fix = [p for p in products if reverse and float(p.get("tax_rate", 0)) != 0.0]
    note = f"{MARKER} NIP z Shopify (VIES OK)" + (", VAT 0% (odwrotne obciazenie)" if reverse else "")
    new_comments = (comments + "\n" if comments else "") + note

    plan = f"{name}: NIP{' + VAT 0% na ' + str(len(to_fix)) + ' poz.' if to_fix else ''} (dopasowanie po {matched_by})"
    if DRY_RUN:
        return "[PODGLAD] " + plan

    base_call("setOrderFields", {
        "order_id": base_order["order_id"],
        "invoice_nip": vat,
        "admin_comments": new_comments,
    })
    for p in to_fix:
        base_call("setOrderProductFields", {
            "order_id": base_order["order_id"],
            "order_product_id": p["order_product_id"],
            "tax_rate": 0,
        })
    return "[ZAPISANO] " + plan


# ----------------------------------------------------------------------------
# NIP tylko w komentarzu klienta + sprawdzenie w VIES
# ----------------------------------------------------------------------------
# Kraje UE bez Polski (kody jak w Base). Dla tych krajow regula WDT w Base ma sens.
EU_COUNTRIES = {
    "AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR", "HU",
    "IE", "IT", "LV", "LT", "LU", "MT", "NL", "PT", "RO", "SK", "SI", "ES", "SE",
}
# Prefiksy numerow VAT UE (Grecja ma w numerze VAT "EL", a nie "GR"). Bez "PL".
VAT_PREFIXES = {"EL" if c == "GR" else c for c in EU_COUNTRIES}

COMMENT_VAT_RE = re.compile(r"EU VAT Validation:\s*([A-Za-z]{2}[ .\-]?[A-Za-z0-9]{2,12})(?![A-Za-z0-9])",
                            re.IGNORECASE)
VIES_URL = "https://ec.europa.eu/taxation_customs/vies/rest-api/check-vat-number"


def extract_comment_vat(user_comments):
    """Z komentarza klienta wyciaga numer z linii 'EU VAT Validation: ...' (np. ESB12345678) albo None."""
    if not user_comments:
        return None
    m = COMMENT_VAT_RE.search(user_comments)
    if not m:
        return None
    vat = re.sub(r"[^A-Za-z0-9]", "", m.group(1)).upper()
    if len(vat) < 4 or not any(ch.isdigit() for ch in vat[2:]):
        return None  # np. "EU VAT Validation: Pending" - to nie jest numer
    return vat


def vies_check(vat):
    """Pyta publiczne API VIES o numer (z prefiksem kraju). Zwraca jedno z:
    'aktywny', 'nieaktywny', 'niedostepny' (blad albo brak odpowiedzi VIES).
    Zapis do Base nastepuje wylacznie przy 'aktywny'. Odpowiedzi VIES nie logujemy."""
    prefix, number = vat[:2], vat[2:]
    for attempt in range(3):
        res = None
        try:
            res = http_json(VIES_URL, {"countryCode": prefix, "vatNumber": number})
        except Exception:  # blad sieci/serwera VIES - sprobuj jeszcze raz
            res = None
        if res is not None:
            err = res.get("userError")
            if res.get("actionSucceed") is False or (err and err not in ("VALID", "INVALID", "INVALID_INPUT")):
                res = None  # VIES zglasza problem po swojej stronie (np. serwer kraju nie odpowiada)
            elif res.get("valid") is True:
                return "aktywny"
            elif res.get("valid") is False:
                return "nieaktywny"
        if attempt < 2:
            time.sleep(3)
    return "niedostepny"


def process_comment(order, base_order, matched_by):
    """Zamowienie bez NIP w Base, ale z numerem VAT UE w komentarzu klienta.
    Zwraca opis akcji albo None, gdy zamowienie nie spelnia warunkow (wtedy nic nie logujemy).
    W logach (repo jest publiczne) nie ma numeru VAT ani danych klienta."""
    name = order["name"]
    comments = base_order.get("admin_comments") or ""
    if MARKER in comments:
        return None
    country = (base_order.get("delivery_country_code") or "").upper()
    if country not in EU_COUNTRIES:
        return None
    if (base_order.get("invoice_nip") or "").strip():
        return None
    vat = extract_comment_vat(base_order.get("user_comments"))
    if not vat:
        return None

    if vat[:2] not in VAT_PREFIXES:
        return f"{name}: numer z komentarza ma prefiks spoza UE-bez-PL ({vat[:2]}) - nic nie zmieniam, do sprawdzenia recznie"
    status = vies_check(vat)
    if status == "niedostepny":
        return f"{name}: VIES nie odpowiada - nic nie zmieniam, sprobuje przy nastepnym przebiegu"
    if status == "nieaktywny":
        return f"{name}: numer z komentarza jest NIEPRAWIDLOWY w VIES - nic nie zmieniam, do sprawdzenia recznie"

    # Regula WDT w Base uruchamia sie tylko przy pobraniu zamowienia (wyzwalacz "Pobrano zamowienie"),
    # wiec po pozniejszym wpisie NIP stawka sama sie nie zmieni - ustawiamy 0% sami,
    # tak jak w sciezce z Shopify.
    to_fix = [p for p in base_order.get("products", []) if float(p.get("tax_rate", 0)) != 0.0]
    plan = (f"{name}: NIP z komentarza klienta -> pole NIP w Base + VAT 0% na {len(to_fix)} poz. "
            f"(kraj dostawy {country}, prefiks {vat[:2]}, {len(vat) - 2} znakow, VIES: aktywny) "
            f"(dopasowanie po {matched_by})")
    if DRY_RUN:
        return "[PODGLAD] " + plan

    # Najpierw stawki, na koncu NIP: gdyby cos po drodze zawiodlo, zamowienie dalej nie ma NIP
    # i przy nastepnym przebiegu skrypt sprobuje jeszcze raz.
    for p in to_fix:
        base_call("setOrderProductFields", {
            "order_id": base_order["order_id"],
            "order_product_id": p["order_product_id"],
            "tax_rate": 0,
        })
    note = f"{MARKER} NIP z komentarza klienta (VIES OK), VAT 0%"
    new_comments = (comments + "\n" if comments else "") + note
    fields = {"order_id": base_order["order_id"], "invoice_nip": vat}
    if len(new_comments) <= 200:  # limit pola admin_comments w API Base
        fields["admin_comments"] = new_comments
    base_call("setOrderFields", fields)
    return "[ZAPISANO] " + plan


def run_shop(key, only_order_name=None):
    shop = SHOPS[key]
    log(f"== {key} ({'PODGLAD' if DRY_RUN else 'ZAPIS'}) ==")
    token = shopify_token(shop)
    if only_order_name:
        query = f"name:{only_order_name}"
    else:
        since = (datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)).strftime("%Y-%m-%dT%H:%M:%SZ")
        query = f"created_at:>={since}"
    found = shopify_orders(shop, token, query)
    log(f"Zamowien znalezionych w Shopify: {len(found)} (wpisow na osi czasu: {sum(o['events'] for o in found)})")
    orders = [o for o in found if o["vat"]]
    log(f"Zamowien z waznym numerem VAT w Shopify: {len(orders)}")
    if not found:
        return 0

    # Zamowienia bez wpisu o VAT w Shopify tez trzeba obejrzec w Base: numer VAT UE
    # moze byc tylko w komentarzu klienta (sciezka process_comment).
    source_id = base_shop_id(shop["base_name"])
    deadline = time.time() + (WAIT_SECONDS if only_order_name else 0)
    pending = list(found)
    while True:
        oldest = min(datetime.fromisoformat(o["created_at"].replace("Z", "+00:00")) for o in pending)
        base_list = base_orders(source_id, int(oldest.timestamp()) - 3600)
        still = []
        for o in pending:
            b, field = find_base_order(o, base_list)
            if b is None:
                still.append(o)
                continue
            msg = process(o, b, field) if o["vat"] else process_comment(o, b, field)
            if msg:
                log(msg)
        pending = still
        if not pending or time.time() >= deadline:
            break
        time.sleep(POLL_SECONDS)
    for o in pending:
        if o["vat"]:
            log(f"{o['name']}: brak w Base (jeszcze nie pobrane albo nie dopasowano) - sprobuje przy nastepnym przebiegu")
    return 0


def main():
    only_shop = os.environ.get("ONLY_SHOP", "").strip()
    only_order = os.environ.get("ONLY_ORDER", "").strip()
    keys = [only_shop] if only_shop else list(SHOPS)
    failed = False
    for key in keys:
        if key not in SHOPS:
            log(f"Nieznany sklep: {key}")
            failed = True
            continue
        try:
            run_shop(key, only_order or None)
        except Exception as e:  # jeden sklep nie moze zablokowac drugiego
            log(f"BLAD ({key}): {e}")
            failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
