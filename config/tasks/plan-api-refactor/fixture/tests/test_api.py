"""Visible tests: the contract the refactor must not break."""

from __future__ import annotations

import json

import pytest

import shopfront.auth as auth
from shopfront import api, connect


@pytest.fixture(autouse=True)
def fresh_env(tmp_path, monkeypatch):
    """Each test gets its own database file and an empty session table."""
    monkeypatch.chdir(tmp_path)
    connect().close()
    auth.SESSIONS.clear()
    yield
    auth.SESSIONS.clear()


def call(method, path, headers=None, body=None):
    return api.handle(method, path, headers or {}, body)


def register(email="a@example.com", password="correct horse"):
    resp = call("POST", "/register", body={"email": email, "password": password})
    assert resp["status"] == 201, resp
    return {"Authorization": f"Bearer {resp['body']['token']}"}


def seed(sku="WID-1", name="Widget", price=500, stock=10):
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO products (sku, name, price_cents, stock) VALUES (?, ?, ?, ?)",
            (sku, name, price, stock),
        )
        conn.commit()
    finally:
        conn.close()


def make_admin():
    headers = register("root@example.com")
    conn = connect()
    try:
        conn.execute("UPDATE users SET role = 'admin' WHERE email = ?",
                     ("root@example.com",))
        conn.commit()
    finally:
        conn.close()
    return headers


# --- routing ---------------------------------------------------------------


def test_unknown_route_is_404():
    assert call("GET", "/nope")["status"] == 404


def test_unknown_method_on_known_path_is_404():
    assert call("PUT", "/products")["status"] == 404


def test_trailing_slash_is_normalised():
    assert call("GET", "/products/")["status"] == 200


# --- users -----------------------------------------------------------------


def test_register_then_me_round_trip():
    headers = register()
    resp = call("GET", "/me", headers)
    assert resp["status"] == 200
    assert resp["body"]["user"]["email"] == "a@example.com"


def test_me_without_token_is_401():
    assert call("GET", "/me")["status"] == 401


def test_register_rejects_bad_email():
    resp = call("POST", "/register", body={"email": "nope", "password": "longenough"})
    assert resp["status"] == 400


def test_register_rejects_short_password():
    resp = call("POST", "/register", body={"email": "b@example.com", "password": "short"})
    assert resp["status"] == 400


def test_register_rejects_duplicate_email():
    register("dup@example.com")
    resp = call("POST", "/register",
                body={"email": "dup@example.com", "password": "longenough"})
    assert resp["status"] == 409


def test_login_rejects_wrong_password():
    register("c@example.com")
    resp = call("POST", "/login",
                body={"email": "c@example.com", "password": "wrong wrong"})
    assert resp["status"] == 401
    assert resp["body"]["error"] == "invalid credentials"


def test_login_success_returns_token():
    register("d@example.com")
    resp = call("POST", "/login",
                body={"email": "d@example.com", "password": "correct horse"})
    assert resp["status"] == 200
    assert resp["body"]["token"]


# --- products --------------------------------------------------------------


def test_list_products_is_empty_by_default():
    resp = call("GET", "/products")
    assert resp["status"] == 200
    assert resp["body"]["products"] == []


def test_create_product_requires_admin():
    headers = register()
    resp = call("POST", "/products", headers, {"sku": "AB-1", "name": "x", "price_cents": 1})
    assert resp["status"] == 403


def test_create_and_fetch_product():
    seed()
    resp = call("GET", "/products/WID-1")
    assert resp["status"] == 200
    assert resp["body"]["product"]["price_cents"] == 500


def test_get_unknown_sku_is_404():
    assert call("GET", "/products/NOPE")["status"] == 404


def test_search_filters_by_name():
    seed("AAA-1", "Brass fitting")
    seed("BBB-1", "Steel bracket")
    resp = call("GET", "/products?q=brass")
    assert [p["sku"] for p in resp["body"]["products"]] == ["AAA-1"]


def test_min_price_filters():
    seed("AAA-1", "Cheap", price=100)
    seed("BBB-1", "Pricey", price=9000)
    resp = call("GET", "/products?min_price=1000")
    assert [p["sku"] for p in resp["body"]["products"]] == ["BBB-1"]


def test_limit_validation():
    resp = call("GET", "/products?limit=0")
    assert resp["status"] == 400


# --- orders ----------------------------------------------------------------


def test_order_needs_auth():
    resp = call("POST", "/orders", body={"items": [{"sku": "WID-1", "qty": 1}]})
    assert resp["status"] == 401


def test_order_happy_path_deducts_stock():
    seed(stock=10)
    headers = register()
    resp = call("POST", "/orders", headers,
                {"items": [{"sku": "WID-1", "qty": 3}]})
    assert resp["status"] == 201, resp
    assert resp["body"]["order"]["total_cents"] == 1500

    assert call("GET", "/products/WID-1")["body"]["product"]["stock"] == 7


def test_order_rejects_insufficient_stock():
    seed(stock=2)
    headers = register()
    resp = call("POST", "/orders", headers,
                {"items": [{"sku": "WID-1", "qty": 5}]})
    assert resp["status"] == 409


def test_order_rejects_unknown_sku():
    headers = register()
    resp = call("POST", "/orders", headers,
                {"items": [{"sku": "GHOST", "qty": 1}]})
    assert resp["status"] == 404


def test_order_rejects_bad_qty():
    seed()
    headers = register()
    resp = call("POST", "/orders", headers,
                {"items": [{"sku": "WID-1", "qty": 0}]})
    assert resp["status"] == 400


def test_order_rejects_duplicate_sku():
    seed()
    headers = register()
    resp = call("POST", "/orders", headers,
                {"items": [{"sku": "WID-1", "qty": 1}, {"sku": "WID-1", "qty": 2}]})
    assert resp["status"] == 400


def test_cannot_read_another_users_order():
    seed()
    headers_a = register("a1@example.com")
    headers_b = register("a2@example.com")
    order_id = call("POST", "/orders", headers_a,
                    {"items": [{"sku": "WID-1", "qty": 1}]})["body"]["order"]["id"]

    assert call("GET", f"/orders/{order_id}", headers_a)["status"] == 200
    assert call("GET", f"/orders/{order_id}", headers_b)["status"] == 403


def test_cancel_restocks_and_is_final():
    seed(stock=10)
    headers = register()
    order_id = call("POST", "/orders", headers,
                    {"items": [{"sku": "WID-1", "qty": 4}]})["body"]["order"]["id"]

    resp = call("DELETE", f"/orders/{order_id}", headers)
    assert resp["status"] == 200
    assert call("GET", "/products/WID-1")["body"]["product"]["stock"] == 10
    assert call("DELETE", f"/orders/{order_id}", headers)["status"] == 409


def test_order_id_must_be_numeric():
    headers = register()
    assert call("GET", "/orders/abc", headers)["status"] == 400
