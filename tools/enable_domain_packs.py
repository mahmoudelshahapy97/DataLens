#!/usr/bin/env python3
"""Opt each workspace into the starter-library pack that suits its domain.

    python tools/enable_domain_packs.py --password ...
    python tools/enable_domain_packs.py --password ... --revoke

Enabling a pack *copies* its rules into the workspace, which then owns them. That is
the mechanism, and it has a consequence that governs the whole mapping below:

**No pack may be enabled on more than one workspace.**

Copied rules are byte-identical wherever they land. Two workspaces sharing a pack
therefore share five rule strings, and
``test_the_platform_baseline_is_shared_and_that_is_not_a_leak`` asserts that any text
two workspaces share is a *platform baseline* phrase -- so a pack on both trips a
test whose failure message says "workspaces share text that is not a platform rule"
and points at content, not at pack enablement. It is a long walk back from there.

``pagila`` and ``world`` are the pair that test inspects, so they get nothing at all.
The one-workspace-per-pack rule makes the whole question moot for the other six.

Three packs stay enabled nowhere -- banking, manufacturing and education have no
database in this deployment. That is not an oversight: a library is meant to hold
more than any one workspace uses, and they are still visible and previewable in the
console for anyone who wants them.
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

#: workspace -> the packs to copy in. One pack per workspace; see the module docstring.
ASSIGNMENT: Dict[str, List[str]] = {
    "chinook": ["retail-operations"],
    "northwind": ["logistics-operations"],
    "healthcare": ["healthcare-operations"],
    "ecommerce": ["warehouse-inventory"],
    "booking": ["hospitality-operations"],
    "employees": ["workforce-analytics"],
    # Deliberately empty -- the e2e isolation canary compares exactly these two.
    "world": [],
    "pagila": [],
}


class Client:
    """A session that holds cookies, echoes CSRF, and names the workspace."""

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
        self.call("GET", "/")  # issues the CSRF cookie the login POST needs
        status, payload = self.call(
            "POST",
            "/api/vanna/v2/auth/login",
            {"email": email, "password": password, "tenant": self.tenant},
        )
        if status != 200:
            raise SystemExit(f"sign-in failed for {self.tenant} ({status}): {payload}")


def apply_to(client: Client, packs: List[str], *, revoke: bool) -> Dict[str, int]:
    base = f"/api/vanna/v2/admin/tenants/{urllib.parse.quote(client.tenant)}/instruction-packs"
    counts = {"ok": 0, "failed": 0, "skipped_rules": 0}

    for pack in packs:
        quoted = urllib.parse.quote(pack)
        if revoke:
            status, result = client.call("DELETE", f"{base}/{quoted}")
            detail = (
                f"removed {result.get('removed')}, kept {result.get('kept')} you had edited"
                if status == 200
                else result.get("detail") or result
            )
        else:
            status, result = client.call("POST", f"{base}/{quoted}/enable")
            skipped = result.get("skipped") if status == 200 else None
            counts["skipped_rules"] += skipped or 0
            detail = (
                f"added {result.get('added')}"
                + (f", SKIPPED {skipped}" if skipped else "")
                if status == 200
                else result.get("detail") or result
            )

        if status == 200:
            counts["ok"] += 1
            print(f"  {client.tenant:<11} {pack:<26} {detail}")
        else:
            counts["failed"] += 1
            print(f"  {client.tenant:<11} {pack:<26} FAILED {status}: {detail}")

    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://localhost:3000")
    parser.add_argument("--email", default="demo@example.com")
    parser.add_argument("--password", required=True)
    parser.add_argument(
        "--revoke",
        action="store_true",
        help="remove the packs again, keeping any rule the workspace has edited",
    )
    parser.add_argument(
        "--only",
        default="",
        help="comma-separated workspaces, instead of every one in the assignment",
    )
    args = parser.parse_args()

    wanted = ASSIGNMENT
    if args.only:
        names = [t.strip() for t in args.only.split(",") if t.strip()]
        if unknown := [t for t in names if t not in ASSIGNMENT]:
            raise SystemExit(f"not in the assignment: {', '.join(unknown)}")
        wanted = {t: ASSIGNMENT[t] for t in names}

    print(f"{'revoking' if args.revoke else 'enabling'} packs on {args.url}")
    total = {"ok": 0, "failed": 0, "skipped_rules": 0}

    for tenant, packs in wanted.items():
        if not packs:
            print(f"  {tenant:<11} {'(none by design -- isolation canary)':<26}")
            continue
        client = Client(args.url, tenant)
        client.sign_in(args.email, args.password)
        counts = apply_to(client, packs, revoke=args.revoke)
        for key in total:
            total[key] += counts[key]

    print(f"\n  applied {total['ok']}, failed {total['failed']}")
    if total["skipped_rules"]:
        # Worth stopping for. A skip means the text already existed in that
        # workspace, so the rule you believe you just added is not there.
        print(
            f"  {total['skipped_rules']} rule(s) were SKIPPED as already present -- "
            "check for a collision with a domain rule or another pack"
        )
    return 1 if total["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
