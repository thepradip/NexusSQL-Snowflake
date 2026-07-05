"""
Build the deterministic fixture database for the scoring regression gate.

A tiny e-commerce schema with seeded rows — no randomness, no network.
CI rebuilds it on every run; scores against it must match baseline.json
within tolerance or the gate fails.
"""

import sqlite3
from pathlib import Path

DB = Path(__file__).resolve().parent / "fixture.db"


def build(path: Path = DB) -> Path:
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(path)
    c = conn.cursor()
    c.executescript("""
    CREATE TABLE customers (
        id INTEGER PRIMARY KEY, name TEXT, country TEXT, active INTEGER);
    CREATE TABLE orders (
        id INTEGER PRIMARY KEY, customer_id INTEGER, amount REAL, status TEXT,
        FOREIGN KEY(customer_id) REFERENCES customers(id));
    CREATE TABLE products (
        id INTEGER PRIMARY KEY, name TEXT, price REAL, category TEXT);
    CREATE TABLE order_items (
        order_id INTEGER, product_id INTEGER, qty INTEGER,
        FOREIGN KEY(order_id) REFERENCES orders(id),
        FOREIGN KEY(product_id) REFERENCES products(id));
    """)
    c.executemany("INSERT INTO customers VALUES (?,?,?,?)", [
        (1, "Asha", "IN", 1), (2, "Bruno", "BR", 1), (3, "Chen", "CN", 0),
        (4, "Dana", "US", 1), (5, "Eeva", "FI", 1), (6, "Femi", "NG", 0),
    ])
    c.executemany("INSERT INTO orders VALUES (?,?,?,?)", [
        (1, 1, 120.0, "shipped"), (2, 1, 35.5, "pending"), (3, 2, 210.0, "shipped"),
        (4, 3, 15.0, "cancelled"), (5, 4, 99.9, "shipped"), (6, 5, 42.0, "pending"),
        (7, 5, 300.0, "shipped"), (8, 2, 18.25, "cancelled"),
    ])
    c.executemany("INSERT INTO products VALUES (?,?,?,?)", [
        (1, "keyboard", 49.0, "electronics"), (2, "mug", 9.5, "kitchen"),
        (3, "monitor", 199.0, "electronics"), (4, "notebook", 4.25, "office"),
    ])
    c.executemany("INSERT INTO order_items VALUES (?,?,?)", [
        (1, 1, 1), (1, 2, 2), (2, 4, 5), (3, 3, 1), (5, 1, 2),
        (6, 2, 1), (7, 3, 1), (7, 1, 1), (8, 4, 3),
    ])
    conn.commit()
    conn.close()
    return path


if __name__ == "__main__":
    print(build())
