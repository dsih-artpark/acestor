#!/bin/bash
set -euo pipefail

# Smoke test for local auth provider end-to-end flow
# Login, check session, logout, verify 401 on subsequent request

BASE_URL="http://localhost:8000"
COOKIE_JAR="/tmp/smoke-cookies.txt"
ADMIN_EMAIL="admin@example.com"
ADMIN_PASSWORD="SmokeAdminPw!1234"

# Change to webapp directory if not already there
if [ ! -f "docker-compose.yml" ]; then
    if [ -f "webapp/docker-compose.yml" ]; then
        cd webapp
    else
        echo "Error: docker-compose.yml not found. Please run from webapp/ or repo root."
        exit 1
    fi
fi

# Clean up any previous test state
rm -f "$COOKIE_JAR"

# Ensure fresh .env
if [ ! -f ".env" ]; then
    cp .env.example .env
fi

# Append auth env vars if not already present
if ! grep -q "AUTH_PROVIDER=" .env; then
    echo "AUTH_PROVIDER=local" >> .env
fi
if ! grep -q "INITIAL_ADMIN_EMAIL=" .env; then
    echo "INITIAL_ADMIN_EMAIL=$ADMIN_EMAIL" >> .env
fi
if ! grep -q "INITIAL_ADMIN_PASSWORD=" .env; then
    echo "INITIAL_ADMIN_PASSWORD=$ADMIN_PASSWORD" >> .env
fi

# Bring down and remove previous state
echo "Tearing down previous compose stack..."
docker compose down -v || true

# Bring up the stack
echo "Bringing up docker compose stack..."
docker compose up -d --build

# Wait for healthz to return 200 (up to 90s)
echo "Waiting for service to be healthy..."
TIMEOUT=90
ELAPSED=0
while [ $ELAPSED -lt $TIMEOUT ]; do
    if curl -sf "$BASE_URL/healthz" >/dev/null 2>&1; then
        echo "Service is healthy"
        break
    fi
    sleep 2
    ELAPSED=$((ELAPSED + 2))
done

if [ $ELAPSED -ge $TIMEOUT ]; then
    echo "ERROR: Service failed to become healthy after ${TIMEOUT}s"
    echo "Docker compose logs:"
    docker compose logs web || true
    docker compose down || true
    exit 1
fi

# Assertion 1: Login with correct credentials
echo "Testing login with correct credentials..."
LOGIN_RESP=$(curl -sS -c "$COOKIE_JAR" -X POST "$BASE_URL/auth/local/login" \
    -H "Content-Type: application/json" \
    -d "{\"email\":\"$ADMIN_EMAIL\",\"password\":\"$ADMIN_PASSWORD\"}" || true)

# Verify response contains expected fields
if ! echo "$LOGIN_RESP" | jq -e ".email == \"$ADMIN_EMAIL\"" >/dev/null 2>&1; then
    echo "FAIL: Login response missing or incorrect email"
    echo "Response: $LOGIN_RESP"
    docker compose logs web || true
    docker compose down || true
    exit 1
fi

if ! echo "$LOGIN_RESP" | jq -e ".is_admin == true" >/dev/null 2>&1; then
    echo "FAIL: Login response missing is_admin=true"
    echo "Response: $LOGIN_RESP"
    docker compose logs web || true
    docker compose down || true
    exit 1
fi

echo "✓ Assertion 1 passed: login returned 200 with correct email and is_admin=true"

# Assertion 2: GET /auth/me with cookie returns authenticated user
echo "Testing GET /auth/me with session cookie..."
ME_RESP=$(curl -sS -b "$COOKIE_JAR" -X GET "$BASE_URL/auth/me" || true)

if ! echo "$ME_RESP" | jq -e ".email == \"$ADMIN_EMAIL\"" >/dev/null 2>&1; then
    echo "FAIL: GET /auth/me response missing or incorrect email"
    echo "Response: $ME_RESP"
    docker compose logs web || true
    docker compose down || true
    exit 1
fi

echo "✓ Assertion 2 passed: GET /auth/me returned authenticated user with correct email"

# Assertion 3: POST /auth/logout returns 204
echo "Testing logout..."
LOGOUT_HTTP=$(curl -sS -c "$COOKIE_JAR" -b "$COOKIE_JAR" -w "\n%{http_code}" -X POST "$BASE_URL/auth/logout")
LOGOUT_STATUS=$(echo "$LOGOUT_HTTP" | tail -1)

if [ "$LOGOUT_STATUS" != "204" ]; then
    echo "FAIL: Logout returned HTTP $LOGOUT_STATUS instead of 204"
    echo "Response: $LOGOUT_HTTP"
    docker compose logs web || true
    docker compose down || true
    exit 1
fi

echo "✓ Assertion 3 passed: logout returned 204"

# Assertion 4: GET /auth/me after logout returns 401
echo "Testing GET /auth/me after logout..."
ME_AFTER_LOGOUT_HTTP=$(curl -sS -b "$COOKIE_JAR" -w "\n%{http_code}" -X GET "$BASE_URL/auth/me")
ME_AFTER_LOGOUT_STATUS=$(echo "$ME_AFTER_LOGOUT_HTTP" | tail -1)

if [ "$ME_AFTER_LOGOUT_STATUS" != "401" ]; then
    echo "FAIL: GET /auth/me after logout returned HTTP $ME_AFTER_LOGOUT_STATUS instead of 401"
    echo "Response: $ME_AFTER_LOGOUT_HTTP"
    docker compose logs web || true
    docker compose down || true
    exit 1
fi

echo "✓ Assertion 4 passed: GET /auth/me after logout returned 401"

# Assertion 5: Login with wrong password returns 401
echo "Testing login with wrong password..."
WRONG_PW_HTTP=$(curl -sS -w "\n%{http_code}" -X POST "$BASE_URL/auth/local/login" \
    -H "Content-Type: application/json" \
    -d "{\"email\":\"$ADMIN_EMAIL\",\"password\":\"WrongPassword123!\"}" || true)
WRONG_PW_STATUS=$(echo "$WRONG_PW_HTTP" | tail -1)

if [ "$WRONG_PW_STATUS" != "401" ]; then
    echo "FAIL: Login with wrong password returned HTTP $WRONG_PW_STATUS instead of 401"
    echo "Response: $WRONG_PW_HTTP"
    docker compose logs web || true
    docker compose down || true
    exit 1
fi

echo "✓ Assertion 5 passed: login with wrong password returned 401"

# Clean up and success
echo "Cleaning up..."
rm -f "$COOKIE_JAR"
docker compose down || true

echo ""
echo "================================================"
echo "smoke passed"
echo "================================================"
