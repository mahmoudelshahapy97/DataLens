#!/usr/bin/env python3
"""Remove the ad-hoc instructions and starter questions pushed into a workspace.

    python tools/prune_chinook_seed.py --password ... --dry-run
    python tools/prune_chinook_seed.py --password ...

``seed_demo_data.py`` used to write workspace instructions and starter questions over
the admin API. That was the wrong home for them: `backend/domains/domains.yml` is the
source of truth, it is in version control, and it is what a fresh deployment
provisions. Content pushed over the API exists only in one database, drifts from the
file, and stacks up on top of what the provisioner already wrote.

On chinook the drift got actively harmful. The seeded rule

    "The invoice table is the source of truth for revenue. Do not sum invoice_line
     unit prices to get revenue -- use invoice.total."

**contradicts** the domain rule the provisioner wrote, which says revenue means
``sum(invoice_line.unit_price * invoice_line.quantity)``. Two rules in one prompt
telling the model opposite things about the same measure is worse than either alone.
The starters had the same problem in miniature: ten pushed on top of three left
thirteen buttons on the chat screen, in an order nobody chose.

So this deletes them, and only them. Three predicates have to hold on a row before it
is touched:

* ``origin == "tenant"``     -- written through the API, not by the provisioner
* ``created_by == <email>``  -- written by this account
* the text is one this script knows ``seed_demo_data`` wrote

Imported from ``seed_demo_data`` rather than copied, so the match set cannot drift
from what was actually written. A provisioned rule is ``origin="library"`` with
``source_pack="domain:<id>"``, so it fails the first predicate and cannot be caught by
accident even if somebody later pastes the same sentence into the file.

``--dry-run`` prints what would go without deleting anything, which is how this should
be read the first time.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Any, Dict, List, Set, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

#: Rules worth keeping even though this script wrote them. "Amounts are in USD" is
#: true, useful, and collides with nothing -- deleting it would be tidiness at the
#: cost of the workspace being slightly worse.
KEEP = {
    "Amounts are in USD. Round money to two decimal places.",
}


def _seeded_content() -> Tuple[Set[str], Set[str]]:
    """What ``seed_demo_data`` writes, read from the module itself.

    A copied list would be a second source of truth for the contents of the first,
    which is the bug this script exists to clean up.
    """
    try:
        import seed_demo_data
    except ImportError as exc:  # pragma: no cover
        raise SystemExit(f"cannot import seed_demo_data: {exc}")

    rules = {
        " ".join(text.split())
        for text, _priority in getattr(seed_demo_data, "INSTRUCTIONS", [])
    }
    starters = {" ".join(q.split()) for q in getattr(seed_demo_data, "STARTERS", [])}

    if not rules and not starters:
        print(
            "note: seed_demo_data no longer defines INSTRUCTIONS or STARTERS, so there "
            "is nothing for this script to recognise. If rows are still in the "
            "workspace, delete them from the Instructions tab in the console."
        )
    return rules, starters


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


def prune_instructions(
    client: Client, texts: Set[str], email: str, *, dry_run: bool
) -> Dict[str, int]:
    base = f"/api/vanna/v2/admin/tenants/{urllib.parse.quote(client.tenant)}/instructions"
    status, payload = client.call("GET", base)
    if status != 200:
        raise SystemExit(f"cannot read instructions ({status}): {payload}")

    rows: List[Dict[str, Any]] = payload.get("instructions") or payload.get("rules") or []
    counts = {"before": len(rows), "deleted": 0, "kept_by_choice": 0}

    for row in rows:
        text = " ".join((row.get("text") or "").split())
        if row.get("origin") != "tenant":
            continue
        if (row.get("created_by") or "") != email:
            continue
        if text not in texts:
            continue
        if text in KEEP:
            counts["kept_by_choice"] += 1
            print(f"    keeping  {text[:70]}")
            continue

        if dry_run:
            counts["deleted"] += 1
            print(f"    would go {text[:70]}")
            continue

        code, result = client.call("DELETE", f"{base}/{urllib.parse.quote(str(row['id']))}")
        if code == 200:
            counts["deleted"] += 1
            print(f"    deleted  {text[:70]}")
        else:
            print(f"    FAILED {code} on {text[:50]}: {result}")

    return counts


def prune_starters(client: Client, questions: Set[str], *, dry_run: bool) -> Dict[str, int]:
    base = f"/api/vanna/v2/admin/tenants/{urllib.parse.quote(client.tenant)}/starters"
    status, payload = client.call("GET", base)
    if status != 200:
        raise SystemExit(f"cannot read starters ({status}): {payload}")

    rows: List[Dict[str, Any]] = payload.get("starters") or []
    counts = {"before": len(rows), "deleted": 0}

    for row in rows:
        question = " ".join((row.get("question") or "").split())
        if question not in questions:
            continue
        if dry_run:
            counts["deleted"] += 1
            print(f"    would go {question[:70]}")
            continue

        code, result = client.call("DELETE", f"{base}/{urllib.parse.quote(str(row['id']))}")
        if code == 200:
            counts["deleted"] += 1
            print(f"    deleted  {question[:70]}")
        else:
            print(f"    FAILED {code} on {question[:50]}: {result}")

    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://localhost:3000")
    parser.add_argument("--email", default="demo@example.com")
    parser.add_argument("--password", required=True)
    parser.add_argument("--tenant", default="chinook")
    parser.add_argument(
        "--dry-run", action="store_true", help="print what would go, delete nothing"
    )
    args = parser.parse_args()

    rules, starters = _seeded_content()
    client = Client(args.url, args.tenant)
    client.sign_in(args.email, args.password)

    mode = "dry run -- nothing will be deleted" if args.dry_run else "deleting"
    print(f"{mode}, workspace {args.tenant}\n")

    print("  instructions")
    r = prune_instructions(client, rules, args.email, dry_run=args.dry_run)
    print("\n  starter questions")
    s = prune_starters(client, starters, dry_run=args.dry_run)

    print("\n--- result ---")
    print(f"  instructions : {r['before']} -> {r['before'] - r['deleted']}")
    if r["kept_by_choice"]:
        print(f"                 ({r['kept_by_choice']} kept deliberately, see KEEP)")
    print(f"  starters     : {s['before']} -> {s['before'] - s['deleted']}")
    if args.dry_run:
        print("\n  nothing was changed. Re-run without --dry-run to apply.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
