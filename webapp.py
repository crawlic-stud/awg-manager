from __future__ import annotations

import csv
import hmac
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta
from io import StringIO

from dotenv import load_dotenv
from flask import Flask, flash, redirect, render_template, request, session, url_for

from mail import DEFAULT_EMAIL_BODY_TEMPLATE, DEFAULT_EMAIL_SUBJECT_TEMPLATE, send_email
from wg_manager import WGManager

load_dotenv()

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.getenv("DB_PATH", os.path.join(APP_DIR, "data", "wg_manager.sqlite3"))
DEFAULT_SERVER_NAME = os.getenv("SERVER_NAME", "AWG Server")
AUTH_PASSWORD = os.getenv("AUTH_PASSWORD", "")


def now_iso() -> str:
    return datetime.utcnow().isoformat(timespec="seconds")


def get_db() -> sqlite3.Connection:
    db_dir = os.path.dirname(DB_PATH)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def table_columns(db: sqlite3.Connection, table_name: str) -> list[str]:
    try:
        return [row[1] for row in db.execute(f"PRAGMA table_info({table_name})").fetchall()]
    except sqlite3.DatabaseError:
        return []


def create_servers_table(db: sqlite3.Connection) -> None:
    db.execute(
        """
        CREATE TABLE servers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            host TEXT NOT NULL,
            port INTEGER NOT NULL,
            login TEXT NOT NULL,
            password TEXT NOT NULL DEFAULT '',
            active INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )


def create_email_accounts_table(db: sqlite3.Connection) -> None:
    db.execute(
        """
        CREATE TABLE email_accounts (
            server_id INTEGER NOT NULL,
            client_id TEXT NOT NULL,
            client_name TEXT NOT NULL,
            email_address TEXT NOT NULL,
            vpn_link TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (server_id, client_id)
        )
        """
    )


def ensure_active_server(db: sqlite3.Connection) -> int | None:
    active = db.execute("SELECT id FROM servers WHERE active = 1 ORDER BY id LIMIT 1").fetchone()
    if active:
        return int(active["id"])

    first = db.execute("SELECT id FROM servers ORDER BY id LIMIT 1").fetchone()
    if not first:
        return None

    server_id = int(first["id"])
    db.execute("UPDATE servers SET active = CASE WHEN id = ? THEN 1 ELSE 0 END", (server_id,))
    return server_id


def seed_default_server(db: sqlite3.Connection) -> None:
    if db.execute("SELECT COUNT(*) AS count FROM servers").fetchone()["count"]:
        return

    host = (os.getenv("VPN_ENDPOINT_HOST") or "").strip()
    login = (os.getenv("SSH_USER") or "").strip()
    if not host or not login:
        return

    now = now_iso()
    db.execute(
        """
        INSERT INTO servers (name, host, port, login, password, active, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, 1, ?, ?)
        """,
        (
            DEFAULT_SERVER_NAME,
            host,
            int(os.getenv("SSH_PORT", "22")),
            login,
            os.getenv("SSH_PASSWORD") or "",
            now,
            now,
        ),
    )


def init_db() -> None:
    with closing(get_db()) as db:
        server_columns = table_columns(db, "servers")
        if not server_columns:
            create_servers_table(db)
        elif set(server_columns) != {
            "id",
            "name",
            "host",
            "port",
            "login",
            "password",
            "active",
            "created_at",
            "updated_at",
        }:
            db.execute("ALTER TABLE servers RENAME TO servers_old")
            create_servers_table(db)
            old_columns = table_columns(db, "servers_old")
            source_name = "name" if "name" in old_columns else f"'{DEFAULT_SERVER_NAME}'"
            source_host = "host" if "host" in old_columns else "''"
            source_port = "port" if "port" in old_columns else "22"
            source_login = "login" if "login" in old_columns else "''"
            source_password = "password" if "password" in old_columns else "''"
            source_active = "active" if "active" in old_columns else "0"
            source_created = "created_at" if "created_at" in old_columns else f"'{now_iso()}'"
            source_updated = "updated_at" if "updated_at" in old_columns else f"'{now_iso()}'"
            db.execute(
                f"""
                INSERT INTO servers (name, host, port, login, password, active, created_at, updated_at)
                SELECT {source_name}, {source_host}, {source_port}, {source_login}, {source_password}, {source_active}, {source_created}, {source_updated}
                FROM servers_old
                """
            )
            db.execute("DROP TABLE servers_old")

        seed_default_server(db)
        ensure_active_server(db)

        email_columns = table_columns(db, "email_accounts")
        if not email_columns:
            create_email_accounts_table(db)
        elif set(email_columns) != {
            "server_id",
            "client_id",
            "client_name",
            "email_address",
            "vpn_link",
            "created_at",
            "updated_at",
        }:
            db.execute("ALTER TABLE email_accounts RENAME TO email_accounts_old")
            create_email_accounts_table(db)
            old_columns = table_columns(db, "email_accounts_old")
            default_server_id = ensure_active_server(db) or 1
            source_server_id = "server_id" if "server_id" in old_columns else str(default_server_id)
            source_vpn = "vpn_link" if "vpn_link" in old_columns else "''"
            source_created = "created_at" if "created_at" in old_columns else f"'{now_iso()}'"
            source_updated = "updated_at" if "updated_at" in old_columns else f"'{now_iso()}'"
            db.execute(
                f"""
                INSERT INTO email_accounts (
                    server_id, client_id, client_name, email_address, vpn_link, created_at, updated_at
                )
                SELECT
                    {source_server_id},
                    client_id,
                    client_name,
                    email_address,
                    {source_vpn},
                    {source_created},
                    {source_updated}
                FROM email_accounts_old
                """
            )
            db.execute("DROP TABLE email_accounts_old")

        db.commit()


def fetch_servers() -> list[sqlite3.Row]:
    with closing(get_db()) as db:
        return db.execute("SELECT * FROM servers ORDER BY active DESC, id ASC").fetchall()


def fetch_server(server_id: int) -> sqlite3.Row | None:
    with closing(get_db()) as db:
        return db.execute("SELECT * FROM servers WHERE id = ?", (server_id,)).fetchone()


def select_server(servers: list[sqlite3.Row], requested_id: int | None) -> sqlite3.Row | None:
    if requested_id is not None:
        for server in servers:
            if int(server["id"]) == requested_id:
                return server
        return None

    for server in servers:
        if int(server["active"]) == 1:
            return server
    return servers[0] if servers else None


def create_server(name: str, host: str, port: int, login: str, password: str, active: bool = False) -> int:
    with closing(get_db()) as db:
        now = now_iso()
        db.execute(
            """
            INSERT INTO servers (name, host, port, login, password, active, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (name, host, port, login, password, 1 if active else 0, now, now),
        )
        server_id = int(db.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
        if active:
            db.execute("UPDATE servers SET active = CASE WHEN id = ? THEN 1 ELSE 0 END", (server_id,))
        elif db.execute("SELECT COUNT(*) AS count FROM servers WHERE active = 1").fetchone()["count"] == 0:
            db.execute("UPDATE servers SET active = CASE WHEN id = ? THEN 1 ELSE 0 END", (server_id,))
        db.commit()
    return server_id


def set_active_server(server_id: int) -> None:
    with closing(get_db()) as db:
        row = db.execute("SELECT id FROM servers WHERE id = ?", (server_id,)).fetchone()
        if not row:
            raise KeyError(f"server not found: {server_id}")
        db.execute("UPDATE servers SET active = CASE WHEN id = ? THEN 1 ELSE 0 END", (server_id,))
        db.commit()


def delete_server_record(server_id: int) -> int | None:
    with closing(get_db()) as db:
        server = db.execute("SELECT active FROM servers WHERE id = ?", (server_id,)).fetchone()
        if not server:
            raise KeyError(f"server not found: {server_id}")

        was_active = int(server["active"]) == 1
        db.execute("DELETE FROM email_accounts WHERE server_id = ?", (server_id,))
        db.execute("DELETE FROM servers WHERE id = ?", (server_id,))

        remaining = db.execute("SELECT id FROM servers ORDER BY id LIMIT 1").fetchone()
        next_active_id = int(remaining["id"]) if remaining else None
        if next_active_id is not None:
            db.execute("UPDATE servers SET active = CASE WHEN id = ? THEN 1 ELSE 0 END", (next_active_id,))
        elif was_active:
            pass

        db.commit()
        return next_active_id


def make_manager(server: sqlite3.Row) -> WGManager:
    return WGManager(
        host=server["host"],
        username=server["login"],
        password=server["password"] or None,
        port=int(server["port"]),
        endpoint_host=server["host"],
    )


def fetch_email_map(server_id: int) -> dict[str, sqlite3.Row]:
    with closing(get_db()) as db:
        rows = db.execute("SELECT * FROM email_accounts WHERE server_id = ?", (server_id,)).fetchall()
    return {row["client_id"]: row for row in rows}


def upsert_email(server_id: int, client_id: str, client_name: str, email_address: str, vpn_link: str = "") -> None:
    now = now_iso()
    with closing(get_db()) as db:
        db.execute(
            """
            INSERT INTO email_accounts (
                server_id, client_id, client_name, email_address, vpn_link, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(server_id, client_id) DO UPDATE SET
                client_name=excluded.client_name,
                email_address=excluded.email_address,
                vpn_link=excluded.vpn_link,
                updated_at=excluded.updated_at
            """,
            (server_id, client_id, client_name, email_address, vpn_link, now, now),
        )
        db.commit()


def save_vpn_link(server_id: int, client_id: str, client_name: str, vpn_link: str) -> None:
    with closing(get_db()) as db:
        row = db.execute(
            "SELECT email_address FROM email_accounts WHERE server_id = ? AND client_id = ?",
            (server_id, client_id),
        ).fetchone()
    email_address = row["email_address"] if row else ""
    upsert_email(server_id, client_id, client_name, email_address, vpn_link=vpn_link)


def delete_client_record(server_id: int, client_id: str) -> None:
    with closing(get_db()) as db:
        db.execute(
            "DELETE FROM email_accounts WHERE server_id = ? AND client_id = ?",
            (server_id, client_id),
        )
        db.commit()


def parse_client_csv(text: str) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    reader = csv.reader(StringIO(text))
    for raw in reader:
        if not raw:
            continue
        if len(raw) < 2:
            raise ValueError("CSV must contain name,email")
        name = (raw[0] or "").strip()
        email = (raw[1] or "").strip()
        if not name and not email:
            continue
        if name.lower() == "name" and email.lower() == "email" and not rows:
            continue
        if not name or not email:
            raise ValueError("Each row must contain name,email")
        rows.append((name, email))
    return rows


def load_clients_with_mail(manager: WGManager, server: sqlite3.Row):
    server_id = int(server["id"])
    clients = manager.list_clients()
    email_map = fetch_email_map(server_id)
    mail_targets = []

    for client in clients:
        client.raw = client.raw or {}
        stored = email_map.get(client.client_id)
        client.vpn_link = (stored["vpn_link"] if stored else "") or client.vpn_link
        if not client.vpn_link:
            try:
                if client.private_key and client.ip:
                    config = manager._build_client_config(
                        client.name, client.ip, client.private_key, client.psk or manager._server_psk()
                    )
                    client.vpn_link = manager.get_vpn_link_from_config(config, server["name"])
                else:
                    client.vpn_link = manager.get_vpn_link(client.client_id)
            except Exception:
                client.vpn_link = ""
        client.raw["vpn_link"] = client.vpn_link
        save_vpn_link(server_id, client.client_id, client.name, client.vpn_link)
        email = email_map.get(client.client_id)
        client.raw["email"] = dict(email) if email else None
        email_address = (email["email_address"] if email else "").strip()
        if email_address and client.vpn_link:
            mail_targets.append(
                {
                    "client_id": client.client_id,
                    "name": client.name,
                    "email_address": email_address,
                    "vpn_link": client.vpn_link,
                    "sendable": True,
                }
            )

    return clients, email_map, mail_targets


app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "wg-manager-dev")
app.config.update(
    PERMANENT_SESSION_LIFETIME=timedelta(hours=24),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
)


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        password = request.form.get("password", "")
        if AUTH_PASSWORD and hmac.compare_digest(password, AUTH_PASSWORD):
            session.clear()
            session.permanent = True
            session["authenticated"] = True
            next_url = request.args.get("next") or request.form.get("next")
            if not next_url or not next_url.startswith("/") or next_url.startswith("//"):
                next_url = url_for("index")
            return redirect(next_url)
        return render_template("login.html", error="Invalid password."), 401

    return render_template("login.html")


@app.before_request
def _ensure_db() -> None:
    init_db()


@app.before_request
def _require_authentication():
    if request.endpoint == "login" or request.endpoint == "static":
        return None
    if session.get("authenticated") is not True:
        next_url = request.full_path.rstrip("?")
        return redirect(url_for("login", next=next_url))
    return None


def redirect_to_server(server_id: int | None) -> str:
    if server_id is None:
        return url_for("index")
    return url_for("index", server_id=server_id)


@app.route("/", methods=["GET"])
def index():
    servers = fetch_servers()
    requested_server_id = request.args.get("server_id", type=int)
    selected_server = select_server(servers, requested_server_id)
    error = None
    clients = []
    mail_targets = []

    if requested_server_id is not None and selected_server is None:
        error = f"Server {requested_server_id} not found."
    elif selected_server is None:
        error = "No servers configured. Add one below."
    else:
        try:
            manager = make_manager(selected_server)
            manager.connect()
            clients, _, mail_targets = load_clients_with_mail(manager, selected_server)
        except Exception as exc:
            error = f"{selected_server['name']}: {exc}"

    return render_template(
        "index.html",
        servers=servers,
        selected_server=selected_server,
        selected_server_id=selected_server["id"] if selected_server else None,
        selected_server_name=selected_server["name"] if selected_server else DEFAULT_SERVER_NAME,
        clients=clients,
        error=error,
        total_clients=len(clients),
        linked_emails=sum(1 for client in clients if (client.raw.get("email") or {}).get("email_address")),
        eligible_mail_count=sum(1 for item in mail_targets if item["sendable"]),
        mail_targets=mail_targets,
        mail_subject_template=DEFAULT_EMAIL_SUBJECT_TEMPLATE,
        mail_body_template=DEFAULT_EMAIL_BODY_TEMPLATE,
    )


@app.post("/servers/add")
def add_server():
    name = (request.form.get("name") or "").strip() or DEFAULT_SERVER_NAME
    host = (request.form.get("host") or "").strip()
    login = (request.form.get("login") or "").strip()
    password = request.form.get("password") or ""
    port_text = (request.form.get("port") or "22").strip()

    if not host or not login:
        flash("Host and login are required.", "error")
        return redirect(url_for("index"))

    try:
        port = int(port_text)
    except ValueError:
        flash("Port must be a number.", "error")
        return redirect(url_for("index"))

    try:
        with closing(get_db()) as db:
            has_servers = db.execute("SELECT COUNT(*) AS count FROM servers").fetchone()["count"] > 0
        server_id = create_server(name, host, port, login, password, active=not has_servers)
        flash(f"Added server {name}.", "success")
        return redirect(url_for("index", server_id=server_id))
    except Exception as exc:
        flash(str(exc), "error")
        return redirect(url_for("index"))


@app.post("/servers/<int:server_id>/activate")
def activate_server(server_id: int):
    try:
        set_active_server(server_id)
        flash("Active server updated.", "success")
    except Exception as exc:
        flash(str(exc), "error")
    return redirect(url_for("index", server_id=server_id))


@app.post("/servers/<int:server_id>/check")
def check_server(server_id: int):
    server = fetch_server(server_id)
    if not server:
        flash(f"Server {server_id} not found.", "error")
        return redirect(url_for("index"))

    try:
        manager = make_manager(server)
        manager.connect()
        flash(f"{server['name']} is available.", "success")
    except Exception as exc:
        flash(f"{server['name']} unavailable: {exc}", "error")
    return redirect(url_for("index", server_id=server_id))


@app.post("/servers/<int:server_id>/delete")
def delete_server(server_id: int):
    try:
        next_active_id = delete_server_record(server_id)
        flash("Server deleted.", "success")
        if next_active_id is not None:
            return redirect(url_for("index", server_id=next_active_id))
    except Exception as exc:
        flash(str(exc), "error")
    return redirect(url_for("index"))


@app.post("/servers/<int:server_id>/clients/<client_id>/email")
def save_email(server_id: int, client_id: str):
    email_address = (request.form.get("email_address") or "").strip()
    if not email_address:
        flash("Email address is required.", "error")
        return redirect(url_for("index", server_id=server_id))

    server = fetch_server(server_id)
    if not server:
        flash(f"Server {server_id} not found.", "error")
        return redirect(url_for("index"))

    try:
        manager = make_manager(server)
        manager.connect()
        client = manager.get_client(client_id)
        existing = fetch_email_map(server_id).get(client_id)
        vpn_link = existing["vpn_link"] if existing else ""
        upsert_email(server_id, client_id, client.name, email_address, vpn_link=vpn_link)
        flash(f"Saved email for {client.name}.", "success")
    except Exception as exc:
        flash(str(exc), "error")
    return redirect(url_for("index", server_id=server_id))


@app.post("/servers/<int:server_id>/import-csv")
def import_csv(server_id: int):
    server = fetch_server(server_id)
    if not server:
        flash(f"Server {server_id} not found.", "error")
        return redirect(url_for("index"))

    try:
        text = (request.form.get("csv_text") or "").strip()
        if not text:
            flash("Paste CSV text first.", "error")
            return redirect(url_for("index", server_id=server_id))

        entries = parse_client_csv(text)
        if not entries:
            flash("CSV is empty.", "error")
            return redirect(url_for("index", server_id=server_id))

        manager = make_manager(server)
        manager.connect()
        existing_clients = {client.name: client for client in manager.list_clients()}
        imported = 0

        for name, email in entries:
            client = existing_clients.get(name)
            if client is None:
                client = manager.add_client(name)
                existing_clients[name] = client
            vpn_link = client.vpn_link or ""
            if not vpn_link:
                try:
                    vpn_link = manager.get_vpn_link(client.client_id)
                except Exception:
                    vpn_link = ""
            upsert_email(server_id, client.client_id, client.name, email, vpn_link=vpn_link)
            imported += 1

        flash(f"Imported {imported} users.", "success")
    except Exception as exc:
        flash(str(exc), "error")
    return redirect(url_for("index", server_id=server_id))


@app.post("/servers/<int:server_id>/send-mail")
def send_mail_route(server_id: int):
    server = fetch_server(server_id)
    if not server:
        flash(f"Server {server_id} not found.", "error")
        return redirect(url_for("index"))

    subject_template = (request.form.get("subject_template") or DEFAULT_EMAIL_SUBJECT_TEMPLATE).strip()
    body_template = (request.form.get("body_template") or DEFAULT_EMAIL_BODY_TEMPLATE).strip()
    selected_ids = set(request.form.getlist("recipient_ids"))

    try:
        manager = make_manager(server)
        manager.connect()
        _, _, mail_targets = load_clients_with_mail(manager, server)
        allowed = {item["client_id"]: item for item in mail_targets if item["sendable"]}
        recipients = [allowed[client_id] for client_id in selected_ids if client_id in allowed]

        if not recipients:
            flash("Select at least one user with both email and VPN key.", "error")
            return redirect(url_for("index", server_id=server_id))

        sent = 0
        failed: list[str] = []
        for recipient in recipients:
            try:
                send_email(
                    to=recipient["email_address"],
                    vpn_link=recipient["vpn_link"],
                    client_name=recipient["name"],
                    server_name=server["name"],
                    subject_template=subject_template,
                    body_template=body_template,
                )
                sent += 1
            except Exception as exc:
                failed.append(f"{recipient['name']}: {exc}")

        if sent:
            flash(f"Sent {sent} email(s).", "success")
        if failed:
            flash("Some emails failed: " + "; ".join(failed), "error")
    except Exception as exc:
        flash(str(exc), "error")
    return redirect(url_for("index", server_id=server_id))


@app.post("/servers/<int:server_id>/clients/<client_id>/delete")
def delete_client(server_id: int, client_id: str):
    server = fetch_server(server_id)
    if not server:
        flash(f"Server {server_id} not found.", "error")
        return redirect(url_for("index"))

    try:
        manager = make_manager(server)
        manager.connect()
        client = manager.get_client(client_id)
        manager.delete_client(client_id)
        delete_client_record(server_id, client_id)
        flash(f"Deleted {client.name}.", "success")
    except Exception as exc:
        flash(str(exc), "error")
    return redirect(url_for("index", server_id=server_id))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")), debug=True)
