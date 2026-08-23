#!/usr/bin/env python3
"""Switch off the workspace rules that name columns the database does not have.

    python tools/retire_wrong_rules.py --password ... --dry-run
    python tools/retire_wrong_rules.py --password ...
    python tools/retire_wrong_rules.py --password ... --enable   # put them back

Eight rules shipped in `backend/domains/domains.yml` naming columns that do not
exist. Each was checked against `information_schema` on the running warehouse before
being listed here -- not against the repository's seed SQL, which disagrees with what
is actually loaded in at least four places.

    world       city.country_code           -> countrycode
    world       countrylanguage.is_official -> isofficial (a 'T'/'F' character)
    northwind   unit_price, quantity        -> unitprice, qty
    northwind   shipped_date, order_date    -> shippeddate, orderdate
    northwind   employee.reports_to         -> employee.mgrid
    booking     check_out, check_in         -> checkoutdate, checkindate
    booking     "filter on status"          -> reservation has no status column
    ecommerce   "refunded" order status     -> no such status exists anywhere

These are not cosmetic. Every one is prepended to a prompt, so the model is told a
column name that will not resolve, and the failure surfaces as a policy rejection or
a query the warehouse refuses.

**Disabled rather than deleted, deliberately.** The provisioner dedupes on exact
text, so a deleted row is re-added by the next `provision` run straight from the file
-- the file is the source of truth and the file still contains the rule, commented
out for future deployments but present in every database already provisioned.
Disabling leaves the row in place, so the dedupe still recognises it and never writes
it again, and the console shows a rule somebody switched off rather than one that
silently vanished.

Matched on exact whitespace-normalised text, and only rules whose text is in the list
below are touched. `--enable` reverses the whole thing.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar
from typing import Any, Dict, List, Tuple

#: workspace -> the exact rule texts to switch off. Copied verbatim from the blocks
#: now commented out in domains.yml; if you reword one there, this stops matching,
#: which is the intended failure mode -- it means the file and the stack disagree.
WRONG: Dict[str, List[str]] = {
    "world": [
        "world.country.code is a three-letter ISO code and is what city.country_code "
        "and countrylanguage.country_code point at. world.country.code2 is the "
        "two-letter code and joins to nothing here.",
        "world.countrylanguage.is_official is the flag that distinguishes an official "
        'language from one that is merely spoken. "What language do they speak" '
        "usually means the largest by percentage, not the official one.",
    ],
    "northwind": [
        "Line revenue is unit_price * quantity * (1 - discount). The discount column "
        "is a fraction between 0 and 1, not a percentage, and ignoring it overstates "
        "revenue on roughly a fifth of the lines.",
        "Shipping delay is shipped_date - order_date on salesorder. A null "
        "shipped_date means not yet shipped, which is different from a delay of zero "
        "and must not be averaged in as one.",
        "Employees report to other employees through northwind.employee.reports_to. "
        '"Whose team" questions need that self-join.',
    ],
    "booking": [
        "A reservation covers a date range. Nights are check_out - check_in, and a "
        "same-day booking is zero nights, not one. Revenue for a stay is nights "
        "multiplied by the rate.",
        "Cancelled reservations keep their row. Filter on status before reporting "
        "occupancy or revenue.",
    ],
    "ecommerce": [
        "Exclude cancelled and refunded orders from revenue. The current state is on "
        "ecommerce.orders.status; ecommerce.order_status_history holds how it got "
        "there and will double-count if joined carelessly.",
    ],
}


def _norm(text: str) -> str:
    return " ".join(str(text).split())


class Client:
    def __init__(self, base: str, tenant: str) -> None:
        self.base = base.rstrip("/")
        self.tenant = tenant
        self.jar = CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar)
        )

    def _csrf(self) -> str:
        for cookie in self.jar:
            if cookie.name == "vanna_csrf":
                return urllib.parse.unquote(cookie.value or "")
        return ""

    def call(self, method: str, path: str, body: Any = None) -> Tuple[int, Any]:
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Accept": "application/json", "X-Tenant-Id": self.tenant}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if token := self._csrf():
            headers["X-CSRF-Token"] = token

        request = urllib.request.Request(
            f"{self.base}{path}", data=data, headers=headers, method=method
        )
        try:
            with self.opener.open(request, timeout=120) as response:
                raw = response.read().decode(errors="replace")
                try:
                    return response.status, json.loads(raw or "{}")
                except json.JSONDecodeError:
                    return response.status, {"detail": raw[:200]}
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode(errors="replace")
            try:
                return exc.code, json.loads(raw or "{}")
            except json.JSONDecodeError:
                return exc.code, {"detail": raw[:300]}

    def sign_in(self, email: str, password: str) -> None:
        self.call("GET", "/")
        status, payload = self.call(
            "POST",
            "/api/vanna/v2/auth/login",
            {"email": email, "password": password, "tenant": self.tenant},
        )
        if status != 200:
            raise SystemExit(f"sign-in failed for {self.tenant} ({status}): {payload}")


def apply_to(client: Client, texts: List[str], *, enable: bool, dry_run: bool) -> Dict[str, int]:
    base = f"/api/vanna/v2/admin/tenants/{urllib.parse.quote(client.tenant)}/instructions"
    status, payload = client.call("GET", base)
    if status != 200:
        raise SystemExit(f"cannot read {client.tenant} instructions ({status}): {payload}")

    rows = payload.get("instructions") or payload.get("rules") or []
    wanted = {_norm(t) for t in texts}
    counts = {"changed": 0, "already": 0, "missing": 0}
    seen = set()

    for row in rows:
        text = _norm(row.get("text") or "")
        if text not in wanted:
            continue
        seen.add(text)

        if bool(row.get("enabled", True)) == enable:
            counts["already"] += 1
            print(f"    {'on' if enable else 'off'} already  {text[:64]}")
            continue
        if dry_run:
            counts["changed"] += 1
            print(f"    would turn {'on ' if enable else 'off'} {text[:64]}")
            continue

        code, result = client.call(
            "POST",
            f"{base}/{urllib.parse.quote(str(row['id']))}/enabled",
            {"enabled": enable},
        )
        if code == 200:
            counts["changed"] += 1
            print(f"    turned {'on ' if enable else 'off'} {text[:64]}")
        else:
            print(f"    FAILED {code} on {text[:50]}: {result}")

    for text in sorted(wanted - seen):
        counts["missing"] += 1
        print(f"    not present  {text[:64]}")

    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://localhost:3000")
    parser.add_argument("--email", default="demo@example.com")
    parser.add_argument("--password", required=True)
    parser.add_argument("--enable", action="store_true", help="switch them back on")
    parser.add_argument("--dry-run", action="store_true", help="change nothing")
    args = parser.parse_args()

    verb = "re-enabling" if args.enable else "retiring"
    print(f"{verb} rules that name columns the database does not have")
    if args.dry_run:
        print("(dry run -- nothing will change)")

    total = {"changed": 0, "already": 0, "missing": 0}
    for tenant, texts in WRONG.items():
        print(f"\n  {tenant}")
        client = Client(args.url, tenant)
        client.sign_in(args.email, args.password)
        counts = apply_to(client, texts, enable=args.enable, dry_run=args.dry_run)
        for key in total:
            total[key] += counts[key]

    print("\n--- result ---")
    print(f"  changed     : {total['changed']}")
    print(f"  already set : {total['already']}")
    if total["missing"]:
        print(
            f"  not present : {total['missing']} -- either never provisioned here, or "
            "the text in domains.yml was reworded and no longer matches"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
