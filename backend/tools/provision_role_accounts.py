#!/usr/bin/env python3
"""Create the four QA accounts the role screenshot run signs in as.

    python tools/provision_role_accounts.py --password <platform-admin-password>
    python tools/provision_role_accounts.py --password ... --revoke

Every screenshot in `artifacts/` until now was taken as one account: the platform
admin, who can see everything. That is the least interesting view of a product whose
whole selling point is that different people see different things. These four accounts
make the differences visible.

| account | workspace role | what it should prove |
|---|---|---|
| `qa.admin@example.com`    | chinook **admin**   | the console, members, permissions |
| `qa.analyst@example.com`  | chinook **analyst** | can save work; no console |
| `qa.viewer@example.com`   | chinook **viewer**  | read-only; no save, no console |
| `qa.outsider@example.com` | **world** only      | chinook is invisible, as 404 not 403 |

The outsider is given a real membership in a *different* workspace rather than none at
all. A user with no memberships proves very little -- the interesting question is
whether somebody who legitimately uses `world` can reach `chinook`, and the answer has
to be a 404, because a 403 confirms the workspace exists to somebody with no business
knowing that.

## Why this takes three calls per account

`POST /admin/accounts` deliberately generates the password itself and sets
`must_change`, so an admin never knows another person's credential. That is right, and
it means a QA account cannot be created with a known password in one step. The flow is:

1. `POST /admin/accounts` -- returns the generated password, but **only when SMTP is
   not configured**; a deployment with mail sends it and returns an empty string, so
   this script cannot work against one. It says so rather than failing obscurely.
2. `POST /admin/tenants/{id}/users` -- grants the role. This has to come *before* the
   password change: `/auth/password` resolves the caller, resolving a caller means
   resolving a workspace, and an account with no membership anywhere is refused with
   "is not a member of 'demo'" before the password is even looked at.
3. Sign in as them -- a temporary password buys a *restricted* session, scoped to one
   action and valid for an hour.
4. `POST /auth/password` -- sets the known password and promotes the session.

Re-running is safe: an existing account is reset to a fresh temporary password and
taken through the same flow, so the known password is restored.

`--revoke` removes the workspace memberships and disables the accounts. There is no
account-delete endpoint by design, so disabled is as far as this goes.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar
from typing import Any, Dict, List, Optional, Tuple

#: Shared across the four, because these are throwaway QA logins on a local stack and
#: four secrets to keep track of is four chances to leak one. It satisfies the server's
#: strength rules and contains none of the email local parts, which `_weak` rejects.
PASSWORD = "Harbour-Lantern-2026!"

#: (email, full name, workspace, role). A role of None means "no membership here".
ACCOUNTS: List[Tuple[str, str, str, Optional[str]]] = [
    ("qa.admin@example.com", "QA Admin", "chinook", "admin"),
    ("qa.analyst@example.com", "QA Analyst", "chinook", "analyst"),
    ("qa.viewer@example.com", "QA Viewer", "chinook", "viewer"),
    # Belongs to `world`, and must never see `chinook`.
    ("qa.outsider@example.com", "QA Outsider", "world", "analyst"),
]

#: The workspace the screenshot run photographs. The outsider is the one account with
#: no membership in it.
TARGET = "chinook"


class Client:
    def __init__(self, base: str, tenant: str = "") -> None:
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
        if self.tenant:
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

    def sign_in(self, email: str, password: str, tenant: str = "") -> Tuple[int, Any]:
        self.call("GET", "/")
        body: Dict[str, Any] = {"email": email, "password": password}
        if tenant:
            body["tenant"] = tenant
        return self.call("POST", "/api/vanna/v2/auth/login", body)


def _detail(payload: Any) -> str:
    if isinstance(payload, dict):
        return str(payload.get("detail") or payload)[:160]
    return str(payload)[:160]


def issue_temporary(admin: Client, email: str, full_name: str) -> Optional[str]:
    """Create or reset the account and return its temporary password."""
    status, payload = admin.call(
        "POST", "/api/vanna/v2/admin/accounts", {"email": email, "full_name": full_name}
    )
    if status == 409:
        status, payload = admin.call(
            "POST", f"/api/vanna/v2/admin/accounts/{urllib.parse.quote(email)}/reset"
        )
        if status != 200:
            print(f"    reset failed ({status}): {_detail(payload)}")
            return None
    elif status != 200:
        print(f"    create failed ({status}): {_detail(payload)}")
        return None

    temporary = (payload or {}).get("temporary_password") or ""
    if not temporary:
        # The deployment has SMTP configured, so the credential went to an inbox
        # this script cannot read. Say so plainly rather than failing on the login.
        print(
            "    the temporary password was mailed rather than returned -- this "
            "script needs a deployment with no SMTP configured"
        )
        return None
    return temporary


def set_password(base: str, email: str, temporary: str, workspace: str) -> bool:
    """Exchange the temporary password for the known one.

    Ordered after the membership grant on purpose. `/auth/password` resolves the
    caller before it does anything, and resolving a caller means resolving a
    workspace -- so a brand-new account with no membership anywhere is refused with
    "is not a member of 'demo'" before the password is ever considered. The workspace
    has to exist for them first.
    """
    session = Client(base, tenant=workspace)
    code, result = session.sign_in(email, temporary, tenant=workspace)
    if code != 200:
        print(f"    could not sign in with the temporary password ({code}): {_detail(result)}")
        return False

    code, result = session.call(
        "POST",
        "/api/vanna/v2/auth/password",
        {"current_password": temporary, "new_password": PASSWORD},
    )
    if code != 200:
        print(f"    could not set the password ({code}): {_detail(result)}")
        return False
    return True


def grant(admin: Client, email: str, workspace: str, role: str) -> bool:
    path = f"/api/vanna/v2/admin/tenants/{urllib.parse.quote(workspace)}/users"
    status, payload = admin.call("POST", path, {"email": email, "role": role})
    if status == 200:
        return True
    # Already a member: move them to the role we want rather than leaving whatever
    # a previous run set.
    code, listing = admin.call("GET", path)
    if code == 200:
        for row in listing.get("users") or []:
            if (row.get("email") or "").lower() == email:
                patch = f"{path}/{urllib.parse.quote(str(row['id']))}"
                fixed, result = admin.call("PATCH", patch, {"role": role})
                if fixed == 200:
                    return True
                print(f"    could not set role ({fixed}): {_detail(result)}")
                return False
    print(f"    could not add to {workspace} ({status}): {_detail(payload)}")
    return False


def revoke(admin: Client) -> None:
    for email, _name, workspace, _role in ACCOUNTS:
        for scope in {workspace, TARGET}:
            path = f"/api/vanna/v2/admin/tenants/{urllib.parse.quote(scope)}/users"
            code, listing = admin.call("GET", path)
            for row in (listing.get("users") or []) if code == 200 else []:
                if (row.get("email") or "").lower() == email:
                    admin.call("DELETE", f"{path}/{urllib.parse.quote(str(row['id']))}")
                    print(f"  removed {email} from {scope}")
        status, _ = admin.call(
            "PATCH",
            f"/api/vanna/v2/admin/accounts/{urllib.parse.quote(email)}",
            {"is_active": False},
        )
        print(f"  {'disabled' if status == 200 else 'could not disable'} {email}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://localhost:3000")
    parser.add_argument("--admin-email", default="demo@example.com")
    parser.add_argument("--password", required=True, help="the platform admin's password")
    parser.add_argument(
        "--revoke",
        action="store_true",
        help="remove the memberships and disable the accounts",
    )
    args = parser.parse_args()

    admin = Client(args.url)
    status, payload = admin.sign_in(args.admin_email, args.password)
    if status != 200:
        raise SystemExit(f"platform admin sign-in failed ({status}): {_detail(payload)}")

    if args.revoke:
        print("revoking the QA role accounts")
        revoke(admin)
        return 0

    print(f"provisioning {len(ACCOUNTS)} QA accounts on {args.url}\n")
    ok = 0
    for email, full_name, workspace, role in ACCOUNTS:
        print(f"  {email}")
        temporary = issue_temporary(admin, email, full_name)
        if temporary is None:
            continue
        if role and not grant(admin, email, workspace, role):
            continue
        if not set_password(args.url, email, temporary, workspace):
            continue
        note = "" if workspace == TARGET else f"  (no membership in {TARGET} -- by design)"
        print(f"    {workspace} / {role}{note}")
        ok += 1

    print(f"\n  provisioned {ok}/{len(ACCOUNTS)}")
    print(f"  password for all four: {PASSWORD}")
    if ok == len(ACCOUNTS):
        print("\n  now: pytest tests/e2e/test_roles.py -m e2e")
    return 0 if ok == len(ACCOUNTS) else 1


if __name__ == "__main__":
    sys.exit(main())
