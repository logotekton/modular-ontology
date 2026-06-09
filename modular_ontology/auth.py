from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import base64
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from .config import USERS_FILE, env

Role = Literal["admin", "member"]
UserStatus = Literal["pending", "active", "rejected"]
DEFAULT_USERS_FILE = USERS_FILE
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@dataclass(frozen=True)
class User:
    id: str
    name: str
    email: str
    company: str
    role: Role
    password_hash: str
    status: UserStatus = "active"


@dataclass
class Session:
    token: str
    user_id: str
    expires_at: datetime


def _hash_password(password: str) -> str:
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def _default_admin_password_hash(env_name: str) -> str:
    password = env(env_name)
    if password:
        return _hash_password(str(password))
    password_hash = env(f"{env_name}_HASH")
    if password_hash:
        return str(password_hash)
    return _hash_password(secrets.token_urlsafe(32))


DEFAULT_USERS: dict[str, User] = {
    "ythong@kumkangkind.com": User(
        id="admin-ythong",
        name="ythong",
        email="ythong@kumkangkind.com",
        company="Kumkang Kind",
        role="admin",
        password_hash=_default_admin_password_hash("MODULAR_ONTOLOGY_ADMIN_YTHONG_PASSWORD"),
        status="active",
    ),
    "mwhong@kumkangkind.com": User(
        id="admin-mwhong",
        name="mwhong",
        email="mwhong@kumkangkind.com",
        company="Kumkang Kind",
        role="admin",
        password_hash=_default_admin_password_hash("MODULAR_ONTOLOGY_ADMIN_MWHONG_PASSWORD"),
        status="active",
    ),
}

SESSIONS: dict[str, Session] = {}
_USERS_CACHE: tuple[str, dict[str, User]] | None = None
TOKEN_TTL_SECONDS = 12 * 60 * 60


def _users_file_path(users_file: str | Path | None = None) -> Path:
    return Path(users_file or env("MODULAR_ONTOLOGY_USERS_FILE") or DEFAULT_USERS_FILE)


def _normalize_email(email: str) -> str:
    normalized = email.strip().lower()
    if not EMAIL_RE.match(normalized):
        raise ValueError("A valid email address is required.")
    return normalized


def _base64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _base64url_decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def _session_secret() -> bytes:
    configured = env("MODULAR_ONTOLOGY_SESSION_SECRET") or env("MODULAR_ONTOLOGY_AUTH_SECRET")
    if configured:
        return str(configured).encode("utf-8")
    users_fingerprint = "|".join(
        f"{user.id}:{user.email}:{user.password_hash}:{user.status}" for user in sorted(load_users().values(), key=lambda item: item.email)
    )
    return hashlib.sha256(f"modular-ontology-session:{users_fingerprint}".encode("utf-8")).digest()


def _sign_token_payload(payload: str) -> str:
    return _base64url_encode(hmac.new(_session_secret(), payload.encode("ascii"), hashlib.sha256).digest())


def _create_signed_token(user: User, expires_at: datetime) -> str:
    payload = _base64url_encode(
        json.dumps(
            {
                "sub": user.id,
                "email": user.email,
                "exp": int(expires_at.timestamp()),
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    )
    signature = _sign_token_payload(payload)
    return f"v1.{payload}.{signature}"


def _user_from_signed_token(token: str) -> User | None:
    parts = token.split(".")
    if len(parts) != 3 or parts[0] != "v1":
        return None
    payload, signature = parts[1], parts[2]
    if not hmac.compare_digest(signature, _sign_token_payload(payload)):
        return None
    try:
        data = json.loads(_base64url_decode(payload).decode("utf-8"))
        expires_at = int(data.get("exp", 0))
        user_id = str(data.get("sub") or "")
        email = _normalize_email(str(data.get("email") or ""))
    except (ValueError, TypeError, json.JSONDecodeError):
        return None
    if expires_at <= int(datetime.now(timezone.utc).timestamp()):
        return None
    user = load_users().get(email)
    if user and user.id == user_id and user.status == "active":
        return user
    return None


def _company_from_email(email: str) -> str:
    domain = email.split("@", 1)[1].split(".", 1)[0]
    if domain == "kumkangkind":
        return "Kumkang Kind"
    return domain.replace("-", " ").replace("_", " ").title()


def _normalize_company_name(name: str) -> str:
    normalized = " ".join(name.strip().split())
    if not normalized:
        raise ValueError("Company name is required.")
    return normalized


def _load_auth_data(path: Path) -> object:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _coerce_user(item: dict[str, object]) -> User:
    email = _normalize_email(str(item["email"]))
    password_hash = item.get("password_hash") or item.get("passwordSha256")
    if not password_hash and item.get("password"):
        password_hash = _hash_password(str(item["password"]))
    if not password_hash:
        raise ValueError(f"User {email} must define password_hash or password.")
    role = str(item.get("role", "member")).lower()
    if role not in {"admin", "member"}:
        raise ValueError(f"User {email} has unsupported role {role!r}.")
    status = str(item.get("status", "active")).lower()
    if status not in {"pending", "active", "rejected"}:
        raise ValueError(f"User {email} has unsupported status {status!r}.")
    return User(
        id=str(item.get("id") or email),
        name=str(item.get("name") or email.split("@", 1)[0]),
        email=email,
        company=str(item.get("company") or _company_from_email(email)),
        role=role,  # type: ignore[arg-type]
        password_hash=str(password_hash),
        status=status,  # type: ignore[arg-type]
    )


def _serialize_user(user: User) -> dict[str, str]:
    return {
        "id": user.id,
        "name": user.name,
        "email": user.email,
        "company": user.company,
        "role": user.role,
        "password_hash": user.password_hash,
        "status": user.status,
    }


def _load_file_users(path: Path) -> dict[str, User]:
    data = _load_auth_data(path)
    raw_users = data.get("users", []) if isinstance(data, dict) else data
    if not raw_users:
        return {}
    users: dict[str, User] = {}
    for item in raw_users:
        user = _coerce_user(item)
        users[user.email] = user
    return users


def _load_file_companies(path: Path) -> set[str]:
    data = _load_auth_data(path)
    companies: set[str] = set()
    if isinstance(data, dict):
        for company in data.get("companies", []):
            companies.add(_normalize_company_name(str(company)))
    return companies


def _load_deleted_users(path: Path) -> set[str]:
    data = _load_auth_data(path)
    if not isinstance(data, dict):
        return set()
    return {str(email).strip().lower() for email in data.get("deleted_users", [])}


def _load_company_project_access(path: Path) -> dict[str, list[str]]:
    data = _load_auth_data(path)
    if not isinstance(data, dict):
        return {}
    raw_access = data.get("company_project_access", {})
    if not isinstance(raw_access, dict):
        return {}
    access: dict[str, list[str]] = {}
    for company, project_ids in raw_access.items():
        try:
            company_name = _normalize_company_name(str(company))
        except ValueError:
            continue
        if not isinstance(project_ids, list):
            continue
        normalized_ids = sorted({str(project_id).strip() for project_id in project_ids if str(project_id).strip()})
        access[company_name] = normalized_ids
    return access


def _save_file_users(
    path: Path,
    users: dict[str, User],
    companies: set[str] | None = None,
    deleted_users: set[str] | None = None,
    company_project_access: dict[str, list[str]] | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    company_names = set(companies) if companies is not None else _load_file_companies(path)
    deleted_emails = set(deleted_users) if deleted_users is not None else _load_deleted_users(path)
    project_access = (
        {company: sorted(set(project_ids)) for company, project_ids in company_project_access.items()}
        if company_project_access is not None
        else _load_company_project_access(path)
    )
    project_access = {company: project_ids for company, project_ids in project_access.items() if company in company_names}
    payload = {
        "companies": sorted(company_names),
        "deleted_users": sorted(deleted_emails),
        "company_project_access": project_access,
        "users": [_serialize_user(user) for user in sorted(users.values(), key=lambda item: item.email)],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _invalidate_users_cache() -> None:
    global _USERS_CACHE
    _USERS_CACHE = None


def invalidate_users_cache() -> None:
    _invalidate_users_cache()


def public_user(user: User) -> dict[str, object]:
    return {
        "id": user.id,
        "name": user.name,
        "email": user.email,
        "company": user.company,
        "role": user.role,
        "status": user.status,
        "internalAccess": is_internal_user(user),
    }


def is_internal_user(user: User) -> bool:
    company = user.company.casefold()
    return user.email.endswith("@kumkangkind.com") or "kumkang" in company or "금강" in user.company


def load_users(users_file: str | Path | None = None) -> dict[str, User]:
    path = _users_file_path(users_file).resolve()
    path_value = str(path)
    global _USERS_CACHE
    if _USERS_CACHE and _USERS_CACHE[0] == path_value:
        return _USERS_CACHE[1]

    deleted_users = _load_deleted_users(path)
    users = {email: user for email, user in DEFAULT_USERS.items() if email not in deleted_users}
    users.update(_load_file_users(path))
    _USERS_CACHE = (path_value, users)
    return users


def list_users() -> list[User]:
    return sorted(load_users().values(), key=lambda user: (user.role != "admin", user.email))


def list_companies() -> list[str]:
    path = _users_file_path().resolve()
    return sorted(_load_file_companies(path))


def get_company_project_access(company: str | None = None) -> dict[str, list[str]] | list[str]:
    path = _users_file_path().resolve()
    access = _load_company_project_access(path)
    if company is None:
        return access
    company_name = _normalize_company_name(company)
    return access.get(company_name, [])


def set_company_project_access(company: str, project_ids: list[str]) -> list[str]:
    company_name = _normalize_company_name(company)
    normalized_ids = sorted({str(project_id).strip() for project_id in project_ids if str(project_id).strip()})
    path = _users_file_path().resolve()
    file_users = _load_file_users(path)
    companies = _load_file_companies(path)
    companies.add(company_name)
    project_access = _load_company_project_access(path)
    project_access[company_name] = normalized_ids
    _save_file_users(path, file_users, companies, company_project_access=project_access)
    _invalidate_users_cache()
    return normalized_ids


def add_company(name: str) -> str:
    company = _normalize_company_name(name)
    path = _users_file_path().resolve()
    file_users = _load_file_users(path)
    companies = _load_file_companies(path)
    companies.add(company)
    _save_file_users(path, file_users, companies)
    _invalidate_users_cache()
    return company


def delete_company(name: str, delete_users: bool = False, actor_email: str | None = None) -> tuple[str, int]:
    company = _normalize_company_name(name)
    company_users = [user for user in load_users().values() if user.company == company]
    if actor_email and any(user.email == actor_email.strip().lower() for user in company_users):
        raise ValueError("You cannot delete the company that contains your current account.")
    if company_users and not delete_users:
        raise ValueError("Company has users. Confirm deletion with users.")
    path = _users_file_path().resolve()
    file_users = _load_file_users(path)
    companies = _load_file_companies(path)
    deleted_users = _load_deleted_users(path)
    project_access = _load_company_project_access(path)
    for user in company_users:
        file_users.pop(user.email, None)
        if user.email in DEFAULT_USERS:
            deleted_users.add(user.email)
        for token, session in list(SESSIONS.items()):
            if session.user_id == user.id:
                SESSIONS.pop(token, None)
    companies.discard(company)
    project_access.pop(company, None)
    _save_file_users(path, file_users, companies, deleted_users, project_access)
    _invalidate_users_cache()
    return company, len(company_users)


def rename_company(old_name: str, new_name: str) -> str:
    old_company = _normalize_company_name(old_name)
    new_company = _normalize_company_name(new_name)
    path = _users_file_path().resolve()
    file_users = _load_file_users(path)
    companies = _load_file_companies(path)
    project_access = _load_company_project_access(path)
    companies.discard(old_company)
    companies.add(new_company)
    if old_company in project_access:
        project_access[new_company] = project_access.pop(old_company)
    for user in load_users().values():
        if user.company != old_company:
            continue
        file_users[user.email] = User(
            id=user.id,
            name=user.name,
            email=user.email,
            company=new_company,
            role=user.role,
            password_hash=user.password_hash,
            status=user.status,
        )
    _save_file_users(path, file_users, companies, company_project_access=project_access)
    _invalidate_users_cache()
    return new_company


def register_user(email: str, password: str, name: str | None = None, company: str | None = None) -> User:
    normalized_email = _normalize_email(email)
    if len(password) < 6:
        raise ValueError("Password must be at least 6 characters.")
    if normalized_email in load_users():
        raise ValueError("This email is already registered.")

    path = _users_file_path().resolve()
    file_users = _load_file_users(path)
    deleted_users = _load_deleted_users(path)
    user = User(
        id=f"user-{secrets.token_hex(8)}",
        name=(name or normalized_email.split("@", 1)[0]).strip() or normalized_email,
        email=normalized_email,
        company=(company or _company_from_email(normalized_email)).strip() or _company_from_email(normalized_email),
        role="member",
        password_hash=_hash_password(password),
        status="pending",
    )
    file_users[normalized_email] = user
    # Re-registration clears any prior deletion tombstone so the new pending
    # signup is not hidden by a stale deleted_users entry (e.g. default admins).
    deleted_users.discard(normalized_email)
    _save_file_users(path, file_users, deleted_users=deleted_users)
    _invalidate_users_cache()
    return user


def approve_user(email: str, role: Role = "member") -> User:
    if role not in {"admin", "member"}:
        raise ValueError(f"Unsupported role {role!r}.")
    normalized_email = _normalize_email(email)
    path = _users_file_path().resolve()
    file_users = _load_file_users(path)
    user = file_users.get(normalized_email)
    if not user:
        if normalized_email in DEFAULT_USERS:
            return DEFAULT_USERS[normalized_email]
        raise KeyError(normalized_email)
    approved = User(
        id=user.id,
        name=user.name,
        email=user.email,
        company=user.company,
        role=role,
        password_hash=user.password_hash,
        status="active",
    )
    file_users[normalized_email] = approved
    _save_file_users(path, file_users)
    _invalidate_users_cache()
    return approved


def set_user_role(email: str, role: Role) -> User:
    if role not in {"admin", "member"}:
        raise ValueError(f"Unsupported role {role!r}.")
    normalized_email = _normalize_email(email)
    path = _users_file_path().resolve()
    file_users = _load_file_users(path)
    user = file_users.get(normalized_email) or DEFAULT_USERS.get(normalized_email)
    if not user:
        raise KeyError(normalized_email)
    updated = User(
        id=user.id,
        name=user.name,
        email=user.email,
        company=user.company,
        role=role,
        password_hash=user.password_hash,
        status=user.status,
    )
    file_users[normalized_email] = updated
    _save_file_users(path, file_users)
    _invalidate_users_cache()
    return updated


def delete_user(email: str, actor_email: str | None = None) -> User:
    normalized_email = _normalize_email(email)
    if actor_email and normalized_email == actor_email.strip().lower():
        raise ValueError("You cannot delete your current account.")
    path = _users_file_path().resolve()
    file_users = _load_file_users(path)
    deleted_users = _load_deleted_users(path)
    file_user = file_users.pop(normalized_email, None)
    user = file_user or DEFAULT_USERS.get(normalized_email)
    if not user or (file_user is None and normalized_email in deleted_users):
        raise KeyError(normalized_email)
    if normalized_email in DEFAULT_USERS:
        deleted_users.add(normalized_email)
    _save_file_users(path, file_users, deleted_users=deleted_users)
    for token, session in list(SESSIONS.items()):
        if session.user_id == user.id:
            SESSIONS.pop(token, None)
    _invalidate_users_cache()
    return user


def set_user_company(email: str, company: str) -> User:
    normalized_email = _normalize_email(email)
    company_name = _normalize_company_name(company)
    path = _users_file_path().resolve()
    file_users = _load_file_users(path)
    companies = _load_file_companies(path)
    if company_name not in companies:
        raise ValueError("Company must be added by an administrator before users can be assigned to it.")
    user = file_users.get(normalized_email) or DEFAULT_USERS.get(normalized_email)
    if not user:
        raise KeyError(normalized_email)
    updated = User(
        id=user.id,
        name=user.name,
        email=user.email,
        company=company_name,
        role=user.role,
        password_hash=user.password_hash,
        status=user.status,
    )
    file_users[normalized_email] = updated
    _save_file_users(path, file_users, companies)
    _invalidate_users_cache()
    return updated


def authenticate(email: str, password: str) -> tuple[str, User]:
    user = load_users().get(email.strip().lower())
    if not user:
        raise PermissionError("Invalid email or password.")
    if not hmac.compare_digest(user.password_hash, _hash_password(password)):
        raise PermissionError("Invalid email or password.")
    if user.status != "active":
        raise PermissionError("Account is waiting for administrator approval.")

    expires_at = datetime.now(timezone.utc) + timedelta(seconds=TOKEN_TTL_SECONDS)
    token = _create_signed_token(user, expires_at)
    SESSIONS[token] = Session(
        token=token,
        user_id=user.id,
        expires_at=expires_at,
    )
    return token, user


def get_user_by_token(token: str | None) -> User | None:
    if not token:
        return None
    session = SESSIONS.get(token)
    if session:
        if session.expires_at <= datetime.now(timezone.utc):
            SESSIONS.pop(token, None)
        else:
            for user in load_users().values():
                if user.id == session.user_id and user.status == "active":
                    return user
    signed_user = _user_from_signed_token(token)
    if signed_user:
        return signed_user
    return None


def clear_session(token: str | None) -> None:
    if token:
        SESSIONS.pop(token, None)


def extract_bearer_token(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    return token.strip()
