"""Thin ServiceNow Table API client."""
from __future__ import annotations

import http.cookiejar
import time

import requests


class ServiceNowError(RuntimeError):
    pass


class ServiceNowClient:
    """Auth: OAuth client credentials if client_id/client_secret are given (works on SSO instances),
    otherwise basic auth with user/password (Web Service Access Only user)."""

    def __init__(self, instance: str, user: str, password: str | None = None,
                 client_id: str | None = None, client_secret: str | None = None, timeout: int = 30):
        if instance.startswith("http"):
            self.base = instance.rstrip("/")
        else:
            self.base = f"https://{instance}.service-now.com"
        self.user = user  # user_name of the integration / OAuth application user
        self.timeout = timeout
        self.client_id, self.client_secret = client_id, client_secret
        self._token_expiry = 0.0
        self.s = requests.Session()
        self.s.headers.update({"Accept": "application/json", "Content-Type": "application/json"})
        # Don't keep a session cookie: each call then starts a fresh ServiceNow session, which
        # picks up the current-application / current-update-set preferences set in set_context().
        self.s.cookies.set_policy(http.cookiejar.DefaultCookiePolicy(allowed_domains=[]))
        if client_id and client_secret:
            self.auth_mode = "oauth"
        elif password:
            self.auth_mode = "basic"
            self.s.auth = (user, password)
        else:
            raise ServiceNowError("Set SN_CLIENT_ID + SN_CLIENT_SECRET (OAuth) or SN_PASSWORD (basic auth).")

    # ---- auth --------------------------------------------------------------
    def _ensure_token(self, force: bool = False):
        if self.auth_mode != "oauth" or (not force and time.time() < self._token_expiry - 60):
            return
        r = requests.post(
            f"{self.base}/oauth_token.do",
            data={"grant_type": "client_credentials",
                  "client_id": self.client_id, "client_secret": self.client_secret},
            headers={"Accept": "application/json"}, timeout=self.timeout,
        )
        if r.status_code >= 400:
            raise ServiceNowError(
                f"OAuth token request failed HTTP {r.status_code}: {r.text[:400]} — check the client ID/secret, "
                "that glide.oauth.inbound.client.credential.grant_type.enabled = true, and that the "
                "Application Registry record has Default Grant Type = Client Credentials and an active OAuth Application User."
            )
        tok = r.json()
        self.s.headers["Authorization"] = f"Bearer {tok['access_token']}"
        self._token_expiry = time.time() + int(tok.get("expires_in", 1800))

    # ---- low-level -------------------------------------------------------
    def _req(self, method: str, path: str, **kw):
        self._ensure_token()
        r = self.s.request(method, f"{self.base}{path}", timeout=self.timeout, **kw)
        if r.status_code == 401 and self.auth_mode == "oauth":
            self._ensure_token(force=True)
            r = self.s.request(method, f"{self.base}{path}", timeout=self.timeout, **kw)
        if r.status_code >= 400:
            raise ServiceNowError(f"{method} {path} -> HTTP {r.status_code}: {r.text[:800]}")
        return r.json().get("result", {}) if r.text else {}

    def query(self, table: str, query: str = "", fields: list[str] | None = None, limit: int = 20):
        params = {
            "sysparm_query": query,
            "sysparm_limit": str(limit),
            "sysparm_exclude_reference_link": "true",
        }
        if fields:
            params["sysparm_fields"] = ",".join(fields)
        return self._req("GET", f"/api/now/table/{table}", params=params)

    def get(self, table: str, sys_id: str, fields: list[str] | None = None):
        params = {"sysparm_exclude_reference_link": "true"}
        if fields:
            params["sysparm_fields"] = ",".join(fields)
        return self._req("GET", f"/api/now/table/{table}/{sys_id}", params=params)

    def create(self, table: str, data: dict):
        return self._req("POST", f"/api/now/table/{table}", json=data)

    def update(self, table: str, sys_id: str, data: dict):
        return self._req("PATCH", f"/api/now/table/{table}/{sys_id}", json=data)

    # ---- scope / update-set context -------------------------------------
    def user_sys_id(self) -> str:
        rows = self.query("sys_user", f"user_name={self.user}", ["sys_id"], 1)
        if not rows:
            raise ServiceNowError(f"Integration user {self.user} not found")
        return rows[0]["sys_id"]

    def scope_sys_id(self, scope: str) -> str:
        rows = self.query("sys_scope", f"scope={scope}", ["sys_id", "name"], 1)
        if not rows:
            raise ServiceNowError(f"Application scope '{scope}' not found on {self.base}")
        return rows[0]["sys_id"]

    def set_preference(self, user_id: str, name: str, value: str):
        rows = self.query("sys_user_preference", f"user={user_id}^name={name}", ["sys_id"], 1)
        if rows:
            self.update("sys_user_preference", rows[0]["sys_id"], {"value": value})
        else:
            self.create("sys_user_preference", {"user": user_id, "name": name, "value": value})

    def set_context(self, scope_id: str, update_set_name: str, description: str) -> str:
        """Create (or reuse) an in-progress update set in the scope and make it current."""
        rows = self.query(
            "sys_update_set",
            f"name={update_set_name}^application={scope_id}^state=in progress",
            ["sys_id"],
            1,
        )
        if rows:
            us_id = rows[0]["sys_id"]
        else:
            us_id = self.create(
                "sys_update_set",
                {"name": update_set_name, "application": scope_id,
                 "state": "in progress", "description": description},
            )["sys_id"]
        uid = self.user_sys_id()
        self.set_preference(uid, "apps.current_app", scope_id)
        self.set_preference(uid, "sys_update_set", us_id)
        return us_id
