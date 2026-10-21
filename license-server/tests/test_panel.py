from __future__ import annotations


def test_panel_is_served(client):
    response = client.get("/admin")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers["cache-control"] == "no-store"
    body = response.text
    assert "Central Admin" in body
    assert "/admin/assets/panel.js" in body
    assert "/admin/assets/panel.css" in body


def test_panel_trailing_slash_also_serves_the_page(client):
    assert client.get("/admin/").status_code == 200


def test_panel_assets_are_served(client):
    css = client.get("/admin/assets/panel.css")
    assert css.status_code == 200
    assert "text/css" in css.headers["content-type"]

    script = client.get("/admin/assets/panel.js")
    assert script.status_code == 200
    assert "javascript" in script.headers["content-type"]


def test_panel_contains_no_secrets_or_credentials(client):
    body = client.get("/admin").text.lower()
    assert "password_hash" not in body
    assert "access_token" not in body
    assert "adminpass123" not in body

    script = client.get("/admin/assets/panel.js").text.lower()
    assert "adminpass123" not in script
    assert "eyj" not in script  # no baked-in bearer token


def test_panel_cannot_read_accounts_without_a_token(client):
    assert client.get("/v1/admin/accounts").status_code == 401

def test_root_opens_the_admin_ui(client):
    response = client.get("/", follow_redirects=False)
    assert response.status_code in (307, 308)
    assert response.headers["location"] == "/admin"
    assert client.get("/favicon.ico").status_code == 204
