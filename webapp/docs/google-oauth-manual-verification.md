# Google OAuth — manual verification checklist

Automated end-to-end coverage for the Google OAuth path is intentionally
skipped in Plan B. Testing against real Google requires a Google Cloud
project + browser interaction; testing against a mock OIDC provider adds
CI infra that we do not need yet. Instead, verify the flow by hand before
shipping the `google` provider to any deployment.

The unit-test suite (`tests/test_google_auth.py`) covers the provider gate,
callback branching, and domain enforcement using patched `authlib` calls.
This document covers what those tests cannot: the actual round-trip with
Google.

## Prerequisites

1. A Google Cloud project with an OAuth 2.0 Web Client credential.
   - Authorized JavaScript origin: `http://localhost:8000`.
   - Authorized redirect URI: `http://localhost:8000/auth/google/callback`.
2. `webapp/.env` set to:
   ```env
   AUTH_PROVIDER=google
   AUTH_SESSION_SECRET=<random 32+ chars>
   AUTH_GOOGLE_CLIENT_ID=<from Google console>
   AUTH_GOOGLE_CLIENT_SECRET=<from Google console>
   AUTH_ALLOWED_DOMAINS=<your-domain.example>
   POSTGRES_URL=postgresql+psycopg://acestor:acestor@postgres:5432/acestor
   ```
3. Stack up: `cd webapp && docker compose down -v && docker compose up -d --build`.
4. Wait for `curl -sf http://localhost:8000/healthz` to return 200.

## Checklist

### Path 1 — Allowed domain, happy case

- [ ] Open `http://localhost:8000/auth/google/login` in a browser.
- [ ] Confirm the browser is redirected to `accounts.google.com` with `hd=<your-domain>` in the URL (present only when exactly one domain is configured).
- [ ] Sign in with a Google account in the allowed domain.
- [ ] Confirm the browser lands back on `http://localhost:8000/` with a `acestor_session` cookie set (visible in DevTools → Application → Cookies).
- [ ] `curl -s http://localhost:8000/auth/me -b "acestor_session=<value>"` returns 200 with the signed-in user's email and `auth_provider: "google"`.
- [ ] `SELECT email, auth_provider, is_admin FROM users;` in Postgres shows a new row with `auth_provider='google'`, `is_admin=false`, `password_hash IS NULL`.

### Path 2 — Disallowed domain, rejection

- [ ] In a fresh incognito window (or after logging out), navigate to `/auth/google/login`.
- [ ] Sign in with a Google account whose domain is NOT in `AUTH_ALLOWED_DOMAINS`.
- [ ] Confirm the response is a 403 HTML page reading `"Access is not permitted for accounts on domain <domain>"`.
- [ ] Confirm the page does NOT list the allowed domains anywhere in the body or headers (View Source to be sure).
- [ ] Confirm no `acestor_session` cookie is set.
- [ ] Confirm no user row was created for that email.

### Path 3 — Unverified email

If you can, use a test Google Workspace account with `email_verified=false`:

- [ ] Sign in and confirm the callback returns the same 403 rejection page.

If you cannot produce an unverified-email scenario, this path is covered by `test_callback_unverified_email_rejected` in `tests/test_google_auth.py` (patched userinfo) — check the unit-test log rather than the browser.

### Path 4 — `hd` parameter with multiple domains

Set `AUTH_ALLOWED_DOMAINS=domain-a.example,domain-b.example`, restart:

- [ ] Open `/auth/google/login`; confirm the redirect URL does NOT include `hd=` (allowlist is server-enforced instead).
- [ ] Sign in with an account in `domain-a.example` — succeeds.
- [ ] Sign in with an account in `domain-b.example` — succeeds.
- [ ] Sign in with an account outside both — rejected server-side (403).

### Path 5 — Existing local user cannot log in via Google

Create a `local`-provider user via the CLI first:

```bash
cd webapp
docker compose exec -e POSTGRES_URL=postgresql+psycopg://acestor:acestor@postgres:5432/acestor web \
  python -m acestor_web.cli users create test@your-domain.example --admin --password 'LocalPw!123456'
```

Then try to log in via Google with the same email:

- [ ] Confirm the callback returns 403 rather than converting the account.

### Path 6 — Existing google user, disabled

Log in once (Path 1), then disable the user via the CLI:

```bash
docker compose exec -e POSTGRES_URL=... web python -m acestor_web.cli users disable user@your-domain.example
```

- [ ] Log out (clear cookie).
- [ ] Attempt to log in via Google as the same user.
- [ ] Confirm the callback returns 403.

## Sign-off

Verifier: ______________________  Date: ____________  Deployment target: ____________

- [ ] All six paths pass.
- [ ] No allowlist leak in any rejection response.
- [ ] Deployment's `.env` uses `AUTH_SESSION_SECRET` from `openssl rand -hex 32` (not the placeholder).
- [ ] Deployment's `SESSION_COOKIE_SECURE=true` (required in prod).
