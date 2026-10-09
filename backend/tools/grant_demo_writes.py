#!/usr/bin/env python3
"""Grant write access on a few small tables, so the write flow can be exercised.

    python tools/grant_demo_writes.py --tenant chinook --password ...
    python tools/grant_demo_writes.py --tenant chinook --password ... --revoke

This exists because the write flow cannot be demonstrated -- or tested end to end,
or captured in a Q&A corpus -- without something it is allowed to write to. It sets
per-table grants through the admin API, which is the same path the operator console
uses, so nothing here reaches past the application's own authorisation.

Three deliberate limits on what it grants:

* **Small, self-contained tables.** ``genre`` (25 rows) and ``playlist`` (18) are
  leaves: nothing points at them that a demo would notice. They get the full set.
* **``customer`` gets UPDATE only.** No INSERT, no DELETE. A deleted customer
  orphans 400 invoices, which is not a demo, it is a mess to clean up.
* **Nothing else at all.** ``invoice``, ``invoice_line`` and ``track`` stay
  read-only. A corpus of refusals is more useful than a corpus of regrets, and
  "the agent may not touch the ledger" is the more interesting property to keep.

``--revoke`` puts every one of them back to read-only, which is how you undo this.

Note that the grants this replaces named tables that do not exist -- ``albums``,
``customers``, ``invoices``, plural -- while the warehouse has ``album``,
``customer``, ``invoice``. They were inherited from an older catalogue, and they
resolved for nothing: a write against the real table found no grant and was refused
as though by policy rather than by typo. Granting on the real names is most of what
this script is for.
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

#: (table, can_insert, can_update, can_delete). Read is implied -- the grant model
#: refuses a write grant without it, which is the right invariant: you cannot
#: safely update rows you are not allowed to see.
TABLES: List[Tuple[str, bool, bool, bool]] = [
    ("genre", True, True, True),
    ("playlist", True, True, True),
    ("customer", False, True, False),
]

#: Both roles that may write at all. Viewers cannot, by design, and granting to a
#: role nobody holds is the mistake the API's own role check exists to catch.
ROLES = ("admin", "analyst")


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
        headers = {"Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if token := self._csrf():
            headers["X-CSRF-Token"] = token
        headers["X-Tenant-Id"] = self.tenant

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
            raise SystemExit(f"sign-in failed ({status}): {payload}")


def apply_grants(client: Client, *, revoke: bool) -> Dict[str, int]:
    """Set every grant, under both the bare and schema-qualified table name.

    Both spellings because the grant store keys on the name as written and the
    resolver's choice depends on how the catalogue recorded the table. Setting one
    and hoping is how you end up debugging a refusal that is really a naming
    mismatch -- which is exactly the state this script found things in.
    """
    counts = {"ok": 0, "failed": 0}
    for role in ROLES:
        for table, can_insert, can_update, can_delete in TABLES:
            for name in (table, f"chinook.{table}"):
                payload = {
                    "role": role,
                    "table": name,
                    "can_read": True,
                    "can_insert": False if revoke else can_insert,
                    "can_update": False if revoke else can_update,
                    "can_delete": False if revoke else can_delete,
                    "autofill_columns": True,
                }
                status, result = client.call(
                    "PUT",
                    f"/api/vanna/v2/admin/tenants/{urllib.parse.quote(client.tenant)}"
                    "/grants/table",
                    payload,
                )
                verb = "read-only" if revoke else "".join(
                    letter
                    for letter, on in (
                        ("I", can_insert), ("U", can_update), ("D", can_delete)
                    )
                    if on
                )
                if status == 200:
                    counts["ok"] += 1
                    print(f"  {role:<8} {name:<20} {verb}")
                else:
                    counts["failed"] += 1
                    detail = result.get("detail") if isinstance(result, dict) else result
                    print(f"  {role:<8} {name:<20} FAILED {status}: {detail}")
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://localhost:3000")
    parser.add_argument("--email", default="demo@example.com")
    parser.add_argument("--password", required=True)
    parser.add_argument("--tenant", default="chinook")
    parser.add_argument(
        "--revoke",
        action="store_true",
        help="put every table in the list back to read-only",
    )
    args = parser.parse_args()

    client = Client(args.url, args.tenant)
    client.sign_in(args.email, args.password)

    print(
        f"{'revoking' if args.revoke else 'granting'} write access on "
        f"{len(TABLES)} tables in {args.tenant}"
    )
    counts = apply_grants(client, revoke=args.revoke)

    status, payload = client.call(
        "GET",
        f"/api/vanna/v2/admin/tenants/{urllib.parse.quote(args.tenant)}/grants",
    )
    version = payload.get("version") if isinstance(payload, dict) else "?"
    print(f"\n  applied {counts['ok']}, failed {counts['failed']}")
    print(f"  grants version now: {version}")
    if counts["failed"]:
        print("\n  a failure here usually means the role or table name was rejected")
    return 1 if counts["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
