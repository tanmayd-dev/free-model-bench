"""The entire HTTP surface of shopfront, in one module.

Routing, authentication, request validation, SQL, business rules and response
rendering all live here because that was fastest in 2022 and nobody has had
a spare sprint since.

Entry point is :func:`handle`, which is the only thing ``__main__`` and the
test suite talk to.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
import uuid
from typing import Any
from urllib.parse import parse_qsl

from shopfront import auth, db

SKU_RE = re.compile(r"^[A-Z0-9][A-Z0-9._-]{1,31}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class ApiError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def handle(method: str, path: str, headers: dict[str, str] | None,
           body: Any) -> dict[str, Any]:
    """Dispatch one request. Never raises."""
    try:
        return _route(method, path, headers or {}, body)
    except ApiError as exc:
        return _render(exc.status, {"error": exc.message})
    except sqlite3.IntegrityError as exc:
        return _render(409, {"error": f"constraint violated: {exc}"})
    except (ValueError, KeyError, TypeError) as exc:
        return _render(400, {"error": str(exc)})
    except Exception:  # noqa: BLE001 -- legacy behaviour: leak nothing
        return _render(500, {"error": "internal server error"})


# ---------------------------------------------------------------------------
# routing
# ---------------------------------------------------------------------------


def _route(method: str, path: str, headers: dict[str, str], body: Any) -> dict[str, Any]:
    path_only, query = _split(path)
    data = _parse_body(body)

    # Order matters: the specific patterns have to come first.
    if method == "POST" and path_only == "/login":
        return _login(data)
    if method == "POST" and path_only == "/register":
        return _register(data)
    if method == "GET" and path_only == "/products":
        return _list_products(query)
    if method == "POST" and path_only == "/products":
        return _create_product(headers, data)
    if re.fullmatch(r"/products/[^/]+", path_only):
        if method == "GET":
            return _get_product(path_only.split("/", 2)[2])
        if method == "DELETE":
            return _deactivate_product(path_only.split("/", 2)[2], headers)
    if method == "POST" and path_only == "/orders":
        return _create_order(headers, data)
    if method == "GET" and path_only.startswith("/orders/"):
        return _get_order(_order_id(path_only), headers)
    if method == "DELETE" and path_only.startswith("/orders/"):
        return _cancel_order(_order_id(path_only), headers)
    if method == "GET" and path_only == "/me":
        return _me(headers)
    raise ApiError(404, "not found")


def _split(path: str) -> tuple[str, str]:
    if "?" not in path:
        return path.rstrip("/") or "/", ""
    base, _, query = path.partition("?")
    return base.rstrip("/") or "/", query


def _parse_body(body: Any) -> dict[str, Any]:
    if body in (None, b"", ""):
        return {}
    if isinstance(body, (bytes, bytearray)):
        body = body.decode("utf-8")
    if isinstance(body, str):
        if not body.strip():
            return {}
        body = json.loads(body)
    if not isinstance(body, dict):
        raise ApiError(400, "request body must be a JSON object")
    return body


def _render(status: int, data: dict[str, Any]) -> dict[str, Any]:
    return {"status": status, "body": data}


def _order_id(path_only: str) -> int:
    raw = path_only.rsplit("/", 1)[-1]
    if not raw.isdigit():
        raise ApiError(400, "order id must be an integer")
    return int(raw)


# ---------------------------------------------------------------------------
# auth
# ---------------------------------------------------------------------------


def _require_auth(headers: dict[str, str]) -> str:
    raw = ""
    for key, value in headers.items():
        if key.lower() == "authorization":
            raw = value
    token = raw[7:] if raw.lower().startswith("bearer ") else ""
    user_id = auth.user_for_token(token or None)
    if not user_id:
        raise ApiError(401, "authentication required")
    return user_id


def _require_admin(headers: dict[str, str]) -> str:
    user_id = _require_auth(headers)
    role = db.scalar("SELECT role FROM users WHERE id = ?", (user_id,), default=None)
    if role != "admin":
        raise ApiError(403, "administrator access required")
    return user_id


# ---------------------------------------------------------------------------
# users
# ---------------------------------------------------------------------------


def _register(data: dict[str, Any]) -> dict[str, Any]:
    # validation, hashing, insert and token issue all inlined here
    if "email" not in data:
        raise ApiError(400, "email is required")
    if "password" not in data:
        raise ApiError(400, "password is required")
    email = str(data["email"]).strip().lower()
    if not EMAIL_RE.match(email):
        raise ApiError(400, "email is not a valid address")
    password = str(data["password"])
    if len(password) < 8:
        raise ApiError(400, "password must be at least 8 characters")
    if db.one("SELECT id FROM users WHERE email = ?", (email,)) is not None:
        raise ApiError(409, "an account with that email already exists")
    user_id = uuid.uuid4().hex[:16]
    db.execute(
        "INSERT INTO users (id, email, password_hash, role) VALUES (?, ?, ?, 'customer')",
        (user_id, email, auth.hash_password(password)),
    )
    token = auth.issue_token(user_id)
    return _render(201, {"token": token, "user_id": user_id})


def _login(data: dict[str, Any]) -> dict[str, Any]:
    if "email" not in data:
        raise ApiError(400, "email is required")
    if "password" not in data:
        raise ApiError(400, "password is required")
    email = str(data["email"]).strip().lower()
    row = db.one("SELECT id, password_hash FROM users WHERE email = ?", (email,))
    if row is None:
        raise ApiError(401, "invalid credentials")
    if not auth.check_password(str(data["password"]), row["password_hash"]):
        raise ApiError(401, "invalid credentials")
    token = auth.issue_token(row["id"])
    return _render(200, {"token": token, "user_id": row["id"]})


def _me(headers: dict[str, str]) -> dict[str, Any]:
    user_id = _require_auth(headers)
    row = db.one("SELECT id, email, role FROM users WHERE id = ?", (user_id,))
    if row is None:  # session survived a user deletion
        raise ApiError(401, "authentication required")
    return _render(200, {"user": row})


# ---------------------------------------------------------------------------
# products
# ---------------------------------------------------------------------------


def _list_products(query: str) -> dict[str, Any]:
    params: list[Any] = []
    clauses: list[str] = []
    pairs = dict(parse_qsl(query))

    if "active" in pairs:
        if pairs["active"] not in ("0", "1"):
            raise ApiError(400, "active must be 0 or 1")
        clauses.append("active = ?")
        params.append(int(pairs["active"]))
    if "in_stock" in pairs and pairs["in_stock"] == "1":
        clauses.append("stock > 0")
    if "q" in pairs:
        # NOTE: added by the "search" ticket, tested by hand, never parameterised
        term = pairs["q"].replace("'", "''")
        clauses.append(f"name LIKE '%{term}%'")
    if "min_price" in pairs:
        try:
            min_price = int(pairs["min_price"])
        except ValueError:
            raise ApiError(400, "min_price must be an integer") from None
        clauses.append("price_cents >= ?")
        params.append(min_price)

    sql = "SELECT id, sku, name, price_cents, stock, active FROM products"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY sku ASC"

    limit = 50
    if "limit" in pairs:
        try:
            limit = int(pairs["limit"])
        except ValueError:
            raise ApiError(400, "limit must be an integer") from None
        if limit < 1 or limit > 500:
            raise ApiError(400, "limit must be between 1 and 500")
        sql += f" LIMIT {limit}"

    return _render(200, {"products": db.query(sql, params)})


def _get_product(sku: str) -> dict[str, Any]:
    row = db.one(
        "SELECT id, sku, name, price_cents, stock, active FROM products WHERE sku = ?",
        (sku,),
    )
    if row is None:
        raise ApiError(404, f"no product with sku {sku}")
    return _render(200, {"product": row})


def _create_product(headers: dict[str, str], data: dict[str, Any]) -> dict[str, Any]:
    _require_admin(headers)
    for field in ("sku", "name", "price_cents"):
        if field not in data:
            raise ApiError(400, f"{field} is required")
    sku = str(data["sku"]).upper().strip()
    if not SKU_RE.match(sku):
        raise ApiError(400, "sku must be 2-32 characters of A-Z, 0-9, . _ -")
    name = str(data["name"]).strip()
    if not name:
        raise ApiError(400, "name must not be blank")
    try:
        price = int(data["price_cents"])
    except (TypeError, ValueError):
        raise ApiError(400, "price_cents must be an integer") from None
    if price < 0:
        raise ApiError(400, "price_cents must not be negative")
    try:
        stock = int(data.get("stock", 0))
    except (TypeError, ValueError):
        raise ApiError(400, "stock must be an integer") from None
    if stock < 0:
        raise ApiError(400, "stock must not be negative")

    product_id = db.execute(
        "INSERT INTO products (sku, name, price_cents, stock, active) "
        "VALUES (?, ?, ?, ?, 1)",
        (sku, name, price, stock),
    )
    return _render(201, {"product": {
        "id": product_id, "sku": sku, "name": name,
        "price_cents": price, "stock": stock, "active": 1,
    }})


def _deactivate_product(sku: str, headers: dict[str, str]) -> dict[str, Any]:
    _require_admin(headers)
    row = db.one("SELECT id FROM products WHERE sku = ?", (sku,))
    if row is None:
        raise ApiError(404, f"no product with sku {sku}")
    db.execute("UPDATE products SET active = 0 WHERE id = ?", (row["id"],))
    return _render(200, {"product": {"sku": sku, "active": 0}})


# ---------------------------------------------------------------------------
# orders
# ---------------------------------------------------------------------------


def _create_order(headers: dict[str, str], data: dict[str, Any]) -> dict[str, Any]:
    user_id = _require_auth(headers)
    items = data.get("items")
    if not isinstance(items, list):
        raise ApiError(400, "items is required and must be a list")
    if not items:
        raise ApiError(400, "an order needs at least one item")

    order_id = db.execute(
        "INSERT INTO orders (user_id, created_at, status) VALUES (?, ?, 'open')",
        (user_id, int(time.time())),
    )
    total = 0
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            raise ApiError(400, "each item must be an object")
        if "sku" not in item:
            raise ApiError(400, "each item needs a sku")
        if "qty" not in item:
            raise ApiError(400, "each item needs a qty")
        sku = str(item["sku"])
        qty = item["qty"]
        if isinstance(qty, bool) or not isinstance(qty, int):
            raise ApiError(400, "qty must be an integer")
        if qty < 1:
            raise ApiError(400, "qty must be at least 1")
        if sku in seen:
            raise ApiError(400, f"duplicate sku {sku} in the same order")
        seen.add(sku)

        product = db.one(
            "SELECT id, price_cents, stock FROM products WHERE sku = ? AND active = 1",
            (sku,),
        )
        if product is None:
            raise ApiError(404, f"unknown sku {sku}")
        if product["stock"] < qty:
            raise ApiError(409, f"insufficient stock for {sku}")
        db.execute("UPDATE products SET stock = stock - ? WHERE id = ?",
                   (qty, product["id"]))
        db.execute(
            "INSERT INTO order_lines (order_id, product_id, qty, unit_cents) "
            "VALUES (?, ?, ?, ?)",
            (order_id, product["id"], qty, product["price_cents"]),
        )
        total += qty * product["price_cents"]

    return _render(201, {"order": {
        "id": order_id, "status": "open", "total_cents": total,
    }})


def _get_order(order_id: int, headers: dict[str, str]) -> dict[str, Any]:
    user_id = _require_auth(headers)
    order = db.one("SELECT * FROM orders WHERE id = ?", (order_id,))
    if order is None:
        raise ApiError(404, "no such order")
    if order["user_id"] != user_id:
        raise ApiError(403, "not your order")
    lines = db.query(
        "SELECT product_id, qty, unit_cents FROM order_lines WHERE order_id = ?",
        (order_id,),
    )
    total = sum(int(l["qty"]) * int(l["unit_cents"]) for l in lines)
    return _render(200, {"order": order, "lines": lines, "total_cents": total})


def _cancel_order(order_id: int, headers: dict[str, str]) -> dict[str, Any]:
    user_id = _require_auth(headers)
    order = db.one("SELECT * FROM orders WHERE id = ?", (order_id,))
    if order is None:
        raise ApiError(404, "no such order")
    if order["user_id"] != user_id:
        raise ApiError(403, "not your order")
    if order["status"] != "open":
        raise ApiError(409, f"cannot cancel an order in status {order['status']}")

    for line in db.query(
        "SELECT product_id, qty FROM order_lines WHERE order_id = ?", (order_id,)
    ):
        db.execute("UPDATE products SET stock = stock + ? WHERE id = ?",
                   (line["qty"], line["product_id"]))
    db.execute("UPDATE orders SET status = 'cancelled' WHERE id = ?", (order_id,))
    return _render(200, {"order": {"id": order_id, "status": "cancelled"}})
