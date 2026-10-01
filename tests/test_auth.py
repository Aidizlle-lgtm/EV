from fastapi.testclient import TestClient


def test_setup_only_from_laptop(app):
    remote = TestClient(app, client=("192.168.1.50", 40000), headers={"X-EV-Request": "1"})
    r = remote.post("/api/auth/setup", json={"passcode": "abcdef123"})
    assert r.status_code == 403
    assert "laptop" in r.json()["detail"]


def test_setup_then_login_flow(client):
    assert client.get("/api/auth/status").json()["setup_required"] is True
    assert client.post("/api/auth/setup", json={"passcode": "correct-horse"}).status_code == 200
    assert client.get("/api/auth/status").json()["authenticated"] is True
    # second setup is refused
    assert client.post("/api/auth/setup", json={"passcode": "other-pass"}).status_code == 409
    client.post("/api/auth/logout")
    assert client.get("/api/tasks").status_code == 401
    assert client.post("/api/auth/login", json={"passcode": "wrong"}).status_code == 401
    assert client.post("/api/auth/login", json={"passcode": "correct-horse"}).status_code == 200
    assert client.get("/api/tasks").status_code == 200


def test_api_requires_session(client):
    assert client.get("/api/dashboard").status_code == 401
    assert client.get("/api/health").status_code == 200


def test_csrf_header_required_for_writes(authed, app):
    cookies = authed.cookies
    bare = TestClient(app, client=("127.0.0.1", 50001), cookies=cookies)
    r = bare.post("/api/tasks", json={"title": "x"})
    assert r.status_code == 403
    assert authed.post("/api/tasks", json={"title": "x"}).status_code == 200


def test_login_rate_limit(client):
    client.post("/api/auth/setup", json={"passcode": "correct-horse"})
    client.post("/api/auth/logout")
    for _ in range(5):
        client.post("/api/auth/login", json={"passcode": "nope"})
    r = client.post("/api/auth/login", json={"passcode": "correct-horse"})
    assert r.status_code == 401 and "Too many attempts" in r.json()["detail"]
    from backend import auth
    auth._failures.clear()


def test_security_headers(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "default-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["x-frame-options"] == "DENY"
