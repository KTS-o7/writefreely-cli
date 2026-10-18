"""wfc: a small command-line client for self-hosted WriteFreely blogs.

Standard library only. Talks to the WriteFreely token API under /api/.
Built to be driven by coding agents as well as by hand: no prompts when
no terminal is attached, --json everywhere, stable exit codes.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import socket
import sys
import tempfile
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, List, NoReturn, Optional, Sequence, Tuple

__version__ = "0.1.0"

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_API = 2
EXIT_AUTH = 3
EXIT_NOT_FOUND = 4
EXIT_SERVER = 5

USER_AGENT = f"writefreely-cli/{__version__}"
TIMEOUT = 30.0
# The server caches GET /api/me/posts for 4 seconds and does not clear it on writes.
POSTS_CACHE_SECONDS = 4.5

VISIBILITY = {"unlisted": 0, "public": 1, "private": 2, "protected": 4}
FONTS = ("norm", "sans", "mono", "wrap", "code")
FORMATS = ("blog", "novel", "notebook")

_POST_ID = re.compile(r"^[A-Za-z0-9]+$")
_ALIAS = re.compile(r"^[0-9A-Za-z-]+$")
_CREATED = re.compile(r"^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2}:\d{2})Z$")
_H1 = re.compile(r"^#[ \t]+(.*?)(?:[ \t]+#+)?[ \t]*$")
_HR = re.compile(r"^-{3,}[ \t]*$")

GUIDE = """\
# wfc: manage a WriteFreely blog from the terminal

wfc talks to the user's self-hosted WriteFreely blog through its API.

## Workflow

1. Write the post as a Markdown file. A first-line `# Heading` becomes the
   title. A trailing `---` line is dropped. Everything else is sent as-is.
2. `wfc new post.md` saves it as a draft and prints a preview URL.
   Drafts are unlisted, not private: anyone with the link can read one, but
   drafts are not listed on the blog, in feeds, or sent to followers.
3. Give the user the preview URL. Revise with `wfc update <id> post.md`
   (or `wfc update <id> --title "New title"`). Read back with
   `wfc show <id> --body-only`.
4. Only when the user asks to publish: `wfc publish <id> --blog <alias> --yes`.

## Rules

- Never run a risky command unless the user asked for that action in this
  conversation. Risky commands: publish, unpublish, edit-live,
  delete, pin, unpin, blog create/update/delete, account, login, logout.
- Risky commands refuse to run without `--yes` when no terminal is attached.
- Never put a password on the command line. The user runs `wfc login <url>`
  in their own terminal, or sets WF_PASSWORD.
- Use `--json` when you need to parse output.

## Commands

Read (safe):
  wfc whoami | blogs | blog show <alias>
  wfc list [--drafts | --blog <alias>]
  wfc show <id> [--body-only]
  wfc render <file|->
  wfc guide

Draft (safe, cannot make anything public):
  wfc new <file|-> [--title T]
  wfc update <id> [file|-] [--title T]       (drafts only)

Risky (ask the user first):
  wfc publish <id> --blog <alias> [--keep-date]   (re-dates to now by default)
  wfc unpublish <id>
  wfc edit-live <id> [file|-] [--title T] [--slug S]
  wfc delete <id>
  wfc pin <id> --blog <alias> [--position N] | wfc unpin <id> --blog <alias>
  wfc blog create <alias> [--title T] [--visibility V]
  wfc blog update <alias> --mathjax on|off --email-subs on|off [--title ...]
      [--visibility unlisted|public|private|protected]
      (both on|off flags are required: the server resets them on every update)
  wfc blog delete <alias>        (its posts become drafts)
  wfc account update --email E | wfc account password
  wfc login <url> | wfc logout

## Exit codes

0 ok, 1 usage error or refused, 2 API rejected the request,
3 login needed, 4 not found, 5 network or server error.
"""


# --- errors -------------------------------------------------------------------


class CLIError(Exception):
    def __init__(self, message: str, code: int = EXIT_USAGE, status: Optional[int] = None, detail: str = ""):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status
        self.detail = detail  # the server's own error_msg, if any


_SECRETS: List[str] = []


def _register_secret(value: Optional[str]) -> None:
    if value and value not in _SECRETS:
        _SECRETS.append(value)


def redact(text: str) -> str:
    for secret in _SECRETS:
        text = text.replace(secret, "***")
    return text


# --- HTTP ---------------------------------------------------------------------


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def _decode(raw: bytes) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except ValueError:
        return None


def _api_error(status: int, payload: Any) -> CLIError:
    message = ""
    if isinstance(payload, dict):
        message = str(payload.get("error_msg") or payload.get("error") or "")
    if status == 401:
        hint = "Not logged in or session expired. Run `wfc login <url>` in a terminal."
        return CLIError(f"{message} {hint}".strip(), EXIT_AUTH, status, message)
    if status == 404:
        return CLIError(message or "Not found.", EXIT_NOT_FOUND, status, message)
    if status >= 500:
        return CLIError(message or f"Server error (HTTP {status}).", EXIT_SERVER, status, message)
    return CLIError(message or f"Unexpected response (HTTP {status}).", EXIT_API, status, message)


class Client:
    def __init__(
        self,
        base_url: str,
        token: Optional[str] = None,
        reauth: Optional[Callable[[], str]] = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.reauth = reauth
        _register_secret(token)

    def call(
        self,
        method: str,
        path: str,
        body: Any = None,
        expect: Tuple[int, ...] = (200,),
        auth: bool = True,
    ) -> Any:
        status, payload = self._send(method, path, body, auth)
        if status == 401 and auth and self.reauth is not None:
            reauth, self.reauth = self.reauth, None
            self.token = reauth()
            _register_secret(self.token)
            status, payload = self._send(method, path, body, auth)
        if status not in expect:
            raise _api_error(status, payload)
        if isinstance(payload, dict) and "data" in payload:
            return payload["data"]
        return payload

    def _send(self, method: str, path: str, body: Any, auth: bool) -> Tuple[int, Any]:
        data = None if body is None else json.dumps(body).encode("utf-8")
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        req = urllib.request.Request(self.base_url + path, data=data, method=method, headers=headers)
        if auth and self.token:
            req.add_unredirected_header("Authorization", "Token " + self.token)
        try:
            with _opener.open(req, timeout=TIMEOUT) as resp:
                return resp.status, _decode(resp.read())
        except urllib.error.HTTPError as e:
            if 300 <= e.code < 400:
                where = e.headers.get("Location", "elsewhere")
                raise CLIError(
                    f"The server redirected {path} to {where}. Check the instance URL.", EXIT_API, e.code
                ) from None
            return e.code, _decode(e.read())
        except urllib.error.URLError as e:
            raise CLIError(f"Could not reach {self.base_url}: {e.reason}", EXIT_SERVER) from None
        except (socket.timeout, TimeoutError):
            raise CLIError(f"Timed out talking to {self.base_url}.", EXIT_SERVER) from None


# --- config -------------------------------------------------------------------


def config_path() -> str:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "writefreely-cli", "config.json")


def load_config() -> Dict[str, Any]:
    path = config_path()
    try:
        mode = os.stat(path).st_mode
    except FileNotFoundError:
        return {"default": None, "instances": {}}
    if os.name == "posix" and mode & 0o077:
        os.chmod(path, 0o600)
        warn(f"Tightened permissions on {path} to 0600.")
    try:
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
    except ValueError:
        raise CLIError(f"Config file {path} is not valid JSON. Fix or delete it, then log in again.") from None
    if not isinstance(cfg, dict) or not isinstance(cfg.get("instances"), dict):
        raise CLIError(f"Config file {path} has an unexpected shape. Delete it and log in again.")
    return cfg


def save_config(cfg: Dict[str, Any]) -> None:
    path = config_path()
    directory = os.path.dirname(path)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    if os.name == "posix":
        os.chmod(directory, 0o700)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".config-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
            f.write("\n")
        if os.name == "posix":
            os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def normalize_url(url: str) -> str:
    url = url.strip()
    if "://" not in url:
        url = "https://" + url
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise CLIError(f"Not a valid instance URL: {url}")
    if parts.scheme == "http" and parts.hostname not in ("localhost", "127.0.0.1", "::1"):
        raise CLIError("Refusing plain http:// (the token would travel unencrypted). Use https://.")
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))


def instance_key(url: str) -> str:
    return urllib.parse.urlsplit(normalize_url(url)).netloc


class Session:
    """The resolved instance for one command: URL, credentials, and an API client."""

    def __init__(self, url: str, token: str, alias: Optional[str], key: Optional[str], single_user: Optional[bool]):
        self.url = url
        self.alias = alias
        self.key = key
        self.single_user = single_user
        self.client = Client(url, token, reauth=self._reauth if alias and os.environ.get("WF_PASSWORD") else None)

    def _reauth(self) -> str:
        password = os.environ.get("WF_PASSWORD", "")
        _register_secret(password)
        data = Client(self.url).call("POST", "/api/auth/login", {"alias": self.alias, "pass": password}, auth=False)
        token = str(data["access_token"])
        if self.key:
            cfg = load_config()
            entry = cfg["instances"].get(self.key)
            if entry is not None:
                entry["token"] = token
                save_config(cfg)
        return token


def open_session(args: argparse.Namespace) -> Session:
    env_url, env_token = os.environ.get("WF_URL"), os.environ.get("WF_TOKEN")
    if env_url and env_token:
        return Session(normalize_url(env_url), env_token, os.environ.get("WF_ALIAS"), None, None)
    cfg = load_config()
    key = args.instance or os.environ.get("WF_INSTANCE") or cfg.get("default")
    if not key:
        raise CLIError("Not logged in. Run `wfc login <url>` in a terminal.", EXIT_AUTH)
    if "://" in key or "/" in key:
        key = instance_key(key)
    entry = cfg["instances"].get(key)
    if entry is None:
        known = ", ".join(sorted(cfg["instances"])) or "none"
        raise CLIError(f"No saved login for {key} (saved: {known}). Run `wfc login <url>`.", EXIT_AUTH)
    return Session(entry["url"], entry["token"], entry.get("alias"), key, entry.get("single_user"))


# --- small helpers ------------------------------------------------------------


def warn(message: str) -> None:
    sys.stderr.write(redact(message) + "\n")


def out(args: argparse.Namespace, data: Any, lines: Sequence[str]) -> None:
    if args.json:
        sys.stdout.write(json.dumps(data, ensure_ascii=False, separators=(",", ":")) + "\n")
    else:
        for line in lines:
            sys.stdout.write(line + "\n")


def is_tty() -> bool:
    try:
        return sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


def confirm(args: argparse.Namespace, description: str) -> None:
    """Gate for risky commands. See the design doc: the real gate is the agent harness approval."""
    if args.yes:
        return
    if not is_tty():
        raise CLIError(f"{description}\nRefusing without --yes because no terminal is attached. Re-run with --yes.")
    sys.stderr.write(description + "\n")
    try:
        answer = input("Proceed? [y/N] ")
    except EOFError:
        answer = ""
    if answer.strip().lower() not in ("y", "yes"):
        raise CLIError("Cancelled.")


def ask_secret(env: str, prompt: str) -> str:
    value = os.environ.get(env)
    if value is None:
        if not is_tty():
            raise CLIError(f"Set {env} or run this in a terminal to be prompted.")
        value = getpass.getpass(prompt)
    if not value:
        raise CLIError("Empty password.")
    _register_secret(value)
    return value


def check_id(post_id: str) -> str:
    if not _POST_ID.match(post_id):
        raise CLIError(f"Not a valid post id: {post_id!r}")
    return post_id


def check_alias(alias: str) -> str:
    if not _ALIAS.match(alias):
        raise CLIError(f"Not a valid blog alias: {alias!r}")
    return alias


def created_for_create(value: str) -> str:
    if not _CREATED.match(value):
        raise CLIError("--created must look like 2026-10-18T09:30:00Z (UTC).")
    return value


def created_for_update(value: str) -> str:
    # Create and update parse different layouts on the server.
    m = _CREATED.match(value)
    if not m:
        raise CLIError("--created must look like 2026-10-18T09:30:00Z (UTC).")
    return f"{m.group(1)} {m.group(2)}"


def title_of(post: Dict[str, Any]) -> str:
    return str(post.get("title") or "(untitled)")


# --- content ------------------------------------------------------------------


def prepare_content(text: str) -> Tuple[Optional[str], str]:
    """Apply the content rules: normalise line endings, lift a leading H1 into the
    title, drop trailing horizontal rules and blank lines. Nothing else changes."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    title = None
    first = 0
    while first < len(lines) and not lines[first].strip():
        first += 1
    if first < len(lines):
        m = _H1.match(lines[first])
        if m and m.group(1).strip():
            title = m.group(1).strip()
            first += 1
            while first < len(lines) and not lines[first].strip():
                first += 1
            lines = lines[first:]
    while lines:
        last = lines[-1]
        if not last.strip():
            lines.pop()
        elif _HR.match(last) and (len(lines) == 1 or not lines[-2].strip()):
            # A --- right under a text line is a setext heading underline, not a rule.
            lines.pop()
        else:
            break
    return title, "\n".join(lines)


def read_source(source: str) -> str:
    try:
        if source == "-":
            raw = sys.stdin.buffer.read()
        else:
            with open(source, "rb") as f:
                raw = f.read()
    except OSError as e:
        raise CLIError(f"Cannot read {source}: {e.strerror}") from None
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise CLIError(f"{source} is not valid UTF-8.") from None


def post_fields(args: argparse.Namespace, for_update: bool) -> Dict[str, Any]:
    """Build the post JSON from a source file and flags. Only given fields are included."""
    fields: Dict[str, Any] = {}
    if getattr(args, "file", None):
        title, body = prepare_content(read_source(args.file))
        if not body.strip():
            raise CLIError("The post body is empty.")
        fields["body"] = body
        if title is not None:
            fields["title"] = title
    if args.title is not None:
        fields["title"] = args.title
    if getattr(args, "slug", None):
        fields["slug"] = args.slug
    if args.created:
        fields["created"] = created_for_update(args.created) if for_update else created_for_create(args.created)
    if args.font:
        fields["font"] = args.font
    if args.lang:
        fields["lang"] = args.lang
    if args.rtl is not None:
        fields["rtl"] = args.rtl
    return fields


# --- lookups ------------------------------------------------------------------


def my_blogs(s: Session) -> List[Dict[str, Any]]:
    return list(s.client.call("GET", "/api/me/collections") or [])


def find_blog(s: Session, alias: str) -> Dict[str, Any]:
    for blog in my_blogs(s):
        if blog.get("alias") == alias:
            return blog
    raise CLIError(f"You have no blog with alias {alias!r}. See `wfc blogs`.", EXIT_NOT_FOUND)


def my_drafts(s: Session) -> List[Dict[str, Any]]:
    # page=0 returns every draft, uncached.
    return list(s.client.call("GET", "/api/me/posts?anonymous=1&page=0") or [])


def get_post(s: Session, post_id: str) -> Dict[str, Any]:
    post = s.client.call("GET", f"/api/posts/{check_id(post_id)}")
    if not isinstance(post, dict):
        raise CLIError(f"Unexpected response for post {post_id}.", EXIT_API)
    return post


def is_draft(s: Session, post_id: str) -> bool:
    return any(p.get("id") == post_id for p in my_drafts(s))


def blog_of(s: Session, post_id: str) -> Optional[str]:
    """Blog alias of a published post, from the server's (briefly cached) list of all posts."""
    for attempt in range(2):
        for p in s.client.call("GET", "/api/me/posts") or []:
            if p.get("id") == post_id and p.get("collection"):
                return str(p["collection"].get("alias"))
        if attempt == 0:
            time.sleep(POSTS_CACHE_SECONDS)
    return None


def detect_single_user(s: Session, blogs: Optional[List[Dict[str, Any]]] = None) -> bool:
    if s.single_user is None:
        for blog in blogs if blogs is not None else my_blogs(s):
            url = str(blog.get("url") or "")
            if url:
                s.single_user = urllib.parse.urlsplit(url).path.strip("/") == ""
                break
        else:
            s.single_user = False
    return bool(s.single_user)


def draft_url(s: Session, post_id: str) -> str:
    prefix = "/d/" if detect_single_user(s) else "/"
    return s.url + prefix + post_id


def published_url(blog: Dict[str, Any], slug: Optional[str], s: Session) -> str:
    base = str(blog.get("url") or f"{s.url}/{blog.get('alias')}/")
    if not base.endswith("/"):
        base += "/"
    return base + (slug or "")


def summarize(p: Dict[str, Any], blog: Optional[str] = None) -> Dict[str, Any]:
    coll = p.get("collection") or {}
    return {
        "id": p.get("id"),
        "slug": p.get("slug"),
        "title": p.get("title") or "",
        "blog": blog or coll.get("alias"),
        "created": p.get("created"),
        "updated": p.get("updated"),
    }


def summary_line(row: Dict[str, Any]) -> str:
    created = str(row.get("created") or "")[:10]
    return "\t".join([str(row["id"]), row["blog"] or "draft", created, row["title"] or "(untitled)"])


def check_results(results: Any, post_id: str, action: str) -> Dict[str, Any]:
    for item in results or []:
        if isinstance(item, dict) and item.get("id") in (post_id, None):
            code = int(item.get("code") or 0)
            if code != 200:
                msg = item.get("error_msg") or f"code {code}"
                raise CLIError(
                    f"Could not {action} {post_id}: {msg}", EXIT_NOT_FOUND if code == 404 else EXIT_API, code
                )
            return item
    raise CLIError(f"Could not {action} {post_id}: the server returned no result for it.", EXIT_API)


# --- commands: auth -----------------------------------------------------------


def cmd_login(args: argparse.Namespace) -> None:
    url = normalize_url(args.url)
    alias = args.alias or os.environ.get("WF_ALIAS")
    if not alias:
        if not is_tty():
            raise CLIError("Pass --alias or set WF_ALIAS (or run in a terminal).")
        alias = input("Alias (username): ").strip()
    password = ask_secret("WF_PASSWORD", "Password: ")
    client = Client(url)
    try:
        data = client.call("POST", "/api/auth/login", {"alias": alias, "pass": password}, auth=False)
    except CLIError as e:
        if e.status == 401:
            raise CLIError(f"Login failed: {e.detail or 'wrong alias or password.'}", EXIT_AUTH, 401) from None
        raise
    token = str((data or {}).get("access_token") or "")
    if not token:
        raise CLIError("Login succeeded but the server returned no token.", EXIT_API)
    client.token = token
    _register_secret(token)
    me = client.call("GET", "/api/me")
    username = str((me or {}).get("username") or alias)
    s = Session(url, token, username, None, None)
    single_user = detect_single_user(s)
    key = instance_key(url)
    cfg = load_config()
    cfg["instances"][key] = {"url": url, "alias": username, "token": token, "single_user": single_user}
    cfg["default"] = key
    save_config(cfg)
    out(args, {"instance": key, "username": username}, [f"Logged in to {key} as {username}."])


def cmd_logout(args: argparse.Namespace) -> None:
    s = open_session(args)
    try:
        s.client.reauth = None
        s.client.call("DELETE", "/api/auth/me", expect=(204, 200))
    except CLIError as e:
        if e.code != EXIT_AUTH:
            raise
    if s.key:
        cfg = load_config()
        cfg["instances"].pop(s.key, None)
        if cfg.get("default") == s.key:
            cfg["default"] = next(iter(sorted(cfg["instances"])), None)
        save_config(cfg)
    out(args, {"instance": s.key or s.url, "logged_out": True}, [f"Logged out of {s.key or s.url}."])


def cmd_whoami(args: argparse.Namespace) -> None:
    s = open_session(args)
    me = s.client.call("GET", "/api/me") or {}
    host = urllib.parse.urlsplit(s.url).netloc
    data = {"username": me.get("username"), "instance": host, "url": s.url}
    out(args, data, [f"{me.get('username')} @ {host}"])


# --- commands: read -----------------------------------------------------------


def cmd_blogs(args: argparse.Namespace) -> None:
    s = open_session(args)
    blogs = my_blogs(s)
    rows = [
        {"alias": b.get("alias"), "title": b.get("title"), "public": bool(b.get("public")), "url": b.get("url")}
        for b in blogs
    ]
    lines = [f"{r['alias']}\t{'public' if r['public'] else 'not public'}\t{r['title']}" for r in rows]
    out(args, rows, lines or ["No blogs."])


def cmd_blog_show(args: argparse.Namespace) -> None:
    s = open_session(args)
    alias = check_alias(args.alias)
    blog = s.client.call("GET", f"/api/collections/{alias}") or {}
    keys = ("alias", "title", "description", "public", "url", "format", "total_posts", "views")
    data = {k: blog.get(k) for k in keys}
    out(args, data, [f"{k}: {data[k]}" for k in keys if data[k] not in (None, "")])


def cmd_list(args: argparse.Namespace) -> None:
    s = open_session(args)
    if args.blog:
        alias = check_alias(args.blog)
        posts: List[Dict[str, Any]] = []
        page = args.page or 1
        while True:
            coll = s.client.call("GET", f"/api/collections/{alias}/posts?page={page}") or {}
            batch = coll.get("posts") or []
            posts.extend(batch)
            if args.page or not batch or len(posts) >= int(coll.get("total_posts") or 0) or page >= 1000:
                break
            page += 1
        rows = [summarize(p, alias) for p in posts]
    elif args.drafts:
        rows = [summarize(p) for p in my_drafts(s)]
    else:
        rows = [summarize(p) for p in s.client.call("GET", "/api/me/posts") or []]
    rows.sort(key=lambda r: str(r.get("created") or ""), reverse=True)
    out(args, rows, [summary_line(r) for r in rows] or ["No posts."])


def cmd_show(args: argparse.Namespace) -> None:
    s = open_session(args)
    post = get_post(s, args.id)
    if args.body_only:
        if args.json:
            out(args, {"id": post.get("id"), "body": post.get("body", "")}, [])
        else:
            sys.stdout.write(str(post.get("body", "")) + "\n")
        return
    draft = is_draft(s, post["id"])
    blog_alias = None if draft else blog_of(s, post["id"])
    if draft:
        url: Optional[str] = draft_url(s, post["id"])
        status = "draft"
    elif blog_alias:
        url = published_url(find_blog(s, blog_alias), post.get("slug"), s)
        status = "published"
    else:
        url, status = None, "published"
    data = {
        "id": post.get("id"),
        "title": post.get("title") or "",
        "status": status,
        "blog": blog_alias,
        "url": url,
        "slug": post.get("slug"),
        "created": post.get("created"),
        "updated": post.get("updated"),
        "views": post.get("views"),
        "body": post.get("body", ""),
    }
    where = "draft (unlisted)" if draft else f"published in {blog_alias or 'a blog'}"
    lines = [f"id: {data['id']}", f"title: {title_of(post)}", f"status: {where}"]
    if url:
        lines.append(f"url: {url}")
    lines += [f"created: {data['created']}", f"updated: {data['updated']}", "", str(data["body"])]
    out(args, data, lines)


def cmd_render(args: argparse.Namespace) -> None:
    s = open_session(args)
    _, body = prepare_content(read_source(args.file))
    data = s.client.call("POST", "/api/markdown", {"raw_body": body}, auth=False) or {}
    html = str(data.get("body", ""))
    out(args, {"html": html}, [html])


def cmd_guide(args: argparse.Namespace) -> None:
    out(args, {"guide": GUIDE}, [GUIDE.rstrip("\n")])


# --- commands: drafts ---------------------------------------------------------


def cmd_new(args: argparse.Namespace) -> None:
    s = open_session(args)
    fields = post_fields(args, for_update=False)
    post = s.client.call("POST", "/api/posts", fields, expect=(201,)) or {}
    post_id = str(post.get("id"))
    url = draft_url(s, post_id)
    data = {"id": post_id, "title": post.get("title") or "", "status": "draft", "url": url}
    out(args, data, [f"Created draft {post_id}: {title_of(post)}", f"Preview (unlisted): {url}"])


def _update(args: argparse.Namespace, published: bool) -> None:
    s = open_session(args)
    post_id = check_id(args.id)
    fields = post_fields(args, for_update=True)
    if not fields:
        raise CLIError("Nothing to update. Give a file and/or --title, --slug, --created, --font, --lang, --rtl.")
    post = get_post(s, post_id)
    draft = is_draft(s, post_id)
    if draft and published:
        raise CLIError(f"Post {post_id} is a draft. Use `wfc update {post_id} ...`.")
    if not draft and not published:
        raise CLIError(
            f"Post {post_id} is published, so changes would be live immediately. "
            f"Use `wfc edit-live {post_id} ...` if the user asked for that."
        )
    if published:
        confirm(args, f'Update published post "{title_of(post)}" ({post_id}). Changes are live immediately.')
    result = s.client.call("POST", f"/api/posts/{post_id}", fields) or {}
    data = {"id": post_id, "title": result.get("title", post.get("title")) or "", "updated": sorted(fields)}
    out(args, data, [f"Updated {post_id}: {', '.join(sorted(fields))}."])


def cmd_update(args: argparse.Namespace) -> None:
    _update(args, published=False)


def cmd_edit_live(args: argparse.Namespace) -> None:
    _update(args, published=True)


# --- commands: publishing -----------------------------------------------------


def cmd_publish(args: argparse.Namespace) -> None:
    s = open_session(args)
    post_id, alias = check_id(args.id), check_alias(args.blog)
    post = get_post(s, post_id)
    if not is_draft(s, post_id):
        raise CLIError(f"Post {post_id} is not one of your drafts (already published?). See `wfc show {post_id}`.")
    blog = find_blog(s, alias)
    public = bool(blog.get("public"))
    reach = (
        "The blog is PUBLIC: the post will be visible to everyone and sent to fediverse followers."
        if public
        else "The blog is not public."
    )
    dated = " It keeps its original date." if args.keep_date else " Its date is set to now."
    confirm(args, f'Publish "{title_of(post)}" ({post_id}) to blog "{alias}". {reach}{dated}')
    if not args.keep_date:
        # Moving a draft into a blog keeps the date it was first saved; re-date it first so
        # followers and the blog see today's date. The update endpoint wants this layout.
        now = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
        s.client.call("POST", f"/api/posts/{post_id}", {"created": now})
    results = s.client.call("POST", f"/api/collections/{alias}/collect", [{"id": post_id}])
    item = check_results(results, post_id, "publish")
    slug = (item.get("post") or {}).get("slug") or post.get("slug")
    url = published_url(blog, slug, s)
    data = {"id": post_id, "blog": alias, "public": public, "url": url}
    out(args, data, [f"Published {post_id} to {alias}{' (public)' if public else ''}: {url}"])


def cmd_unpublish(args: argparse.Namespace) -> None:
    s = open_session(args)
    post_id = check_id(args.id)
    post = get_post(s, post_id)
    if is_draft(s, post_id):
        raise CLIError(f"Post {post_id} is already a draft.")
    confirm(
        args,
        f'Move "{title_of(post)}" ({post_id}) back to drafts. Fediverse followers keep any copy they already received.',
    )
    check_results(s.client.call("POST", "/api/posts/disperse", [post_id]), post_id, "unpublish")
    out(args, {"id": post_id, "status": "draft"}, [f"Moved {post_id} back to drafts."])


def cmd_delete(args: argparse.Namespace) -> None:
    s = open_session(args)
    post_id = check_id(args.id)
    post = get_post(s, post_id)
    confirm(args, f'Permanently delete "{title_of(post)}" ({post_id}). This cannot be undone.')
    s.client.call("DELETE", f"/api/posts/{post_id}", expect=(204,))
    out(args, {"id": post_id, "deleted": True}, [f"Deleted {post_id}."])


def _pin(args: argparse.Namespace, pinning: bool) -> None:
    s = open_session(args)
    post_id, alias = check_id(args.id), check_alias(args.blog)
    verb = "pin" if pinning else "unpin"
    confirm(args, f"{verb.capitalize()} post {post_id} on blog {alias}.")
    item: Dict[str, Any] = {"id": post_id}
    if pinning:
        item["position"] = args.position
    check_results(s.client.call("POST", f"/api/collections/{alias}/{verb}", [item]), post_id, verb)
    out(args, {"id": post_id, "blog": alias, "pinned": pinning}, [f"{verb.capitalize()}ned {post_id} on {alias}."])


def cmd_pin(args: argparse.Namespace) -> None:
    _pin(args, True)


def cmd_unpin(args: argparse.Namespace) -> None:
    _pin(args, False)


# --- commands: blogs ----------------------------------------------------------


def blog_settings(args: argparse.Namespace) -> Dict[str, Any]:
    fields: Dict[str, Any] = {}
    for name in ("title", "description", "signature", "monetization_pointer", "verification_link", "letter_reply"):
        value = getattr(args, name, None)
        if value is not None:
            fields[name] = value
    # The server clears MathJax and email subscriptions on every update unless they are sent as true,
    # and no read endpoint returns them, so `blog update` makes the caller state both.
    for name in ("mathjax", "email_subs"):
        value = getattr(args, name, None)
        if value is not None:
            fields[name] = value == "on"
    if getattr(args, "style_sheet", None):
        fields["style_sheet"] = read_source(args.style_sheet)
    if getattr(args, "script", None):
        fields["script"] = read_source(args.script)
    if getattr(args, "format", None):
        # The server field is a sql.NullString, so JSON must send its struct form.
        fields["format"] = {"String": args.format, "Valid": True}
    if getattr(args, "visibility", None):
        # The JSON key for visibility is "public" (struct tag), holding the bitmask value.
        fields["public"] = VISIBILITY[args.visibility]
        if args.visibility == "protected":
            fields["password"] = ask_secret("WF_BLOG_PASSWORD", "Blog password: ")
    return fields


def cmd_blog_create(args: argparse.Namespace) -> None:
    s = open_session(args)
    alias = check_alias(args.alias)
    note = (
        f" Then set visibility to {args.visibility}."
        if args.visibility
        else (" It starts at the instance's default visibility, which may be public.")
    )
    confirm(args, f"Create blog {alias}.{note}")
    body = {"alias": alias, "title": args.title or alias}
    blog = s.client.call("POST", "/api/collections", body, expect=(201,)) or {}
    public = bool(blog.get("public"))
    if args.visibility:
        s.client.call("POST", f"/api/collections/{alias}", blog_settings(args))
        public = args.visibility == "public"
    data = {"alias": alias, "title": blog.get("title") or body["title"], "public": public}
    out(args, data, [f"Created blog {alias} ({'public' if public else 'not public'})."])


def cmd_blog_update(args: argparse.Namespace) -> None:
    s = open_session(args)
    alias = check_alias(args.alias)
    fields = blog_settings(args)
    if not set(fields) - {"mathjax", "email_subs"}:
        raise CLIError("Nothing to update. See `wfc blog update --help`.")
    changes = ", ".join(sorted(k for k in fields if k not in ("password", "mathjax", "email_subs")))
    changes += f" (mathjax {args.mathjax}, email_subs {args.email_subs})"
    extra = ""
    if args.visibility == "public":
        extra = " The blog and all its posts will become PUBLIC."
    confirm(args, f"Update blog {alias}: {changes}.{extra}")
    s.client.call("POST", f"/api/collections/{alias}", fields)
    updated = sorted(k for k in fields if k != "password")
    out(args, {"alias": alias, "updated": updated}, [f"Updated blog {alias}: {changes}."])


def cmd_blog_delete(args: argparse.Namespace) -> None:
    s = open_session(args)
    alias = check_alias(args.alias)
    confirm(args, f"Delete blog {alias}. Its posts are kept and become drafts.")
    s.client.call("DELETE", f"/api/collections/{alias}", expect=(204,))
    out(args, {"alias": alias, "deleted": True}, [f"Deleted blog {alias}. Its posts are now drafts."])


# --- commands: account --------------------------------------------------------


def cmd_account_update(args: argparse.Namespace) -> None:
    s = open_session(args)
    if not args.email:
        raise CLIError("Nothing to update. Use --email. (The API does not allow changing the username.)")
    confirm(args, f"Change the account email to {args.email}.")
    s.client.call("POST", "/api/me/self", {"email": args.email})
    out(args, {"email": args.email}, ["Account email updated."])


def cmd_account_password(args: argparse.Namespace) -> None:
    s = open_session(args)
    current = ask_secret("WF_PASSWORD", "Current password: ")
    new = ask_secret("WF_NEW_PASSWORD", "New password: ")
    if "WF_NEW_PASSWORD" not in os.environ and getpass.getpass("New password again: ") != new:
        raise CLIError("The new passwords do not match.")
    confirm(args, "Change the account password.")
    s.client.call("POST", "/api/me/self", {"current_pass": current, "new_pass": new})
    out(args, {"password_changed": True}, ["Password changed."])


# --- argument parsing ---------------------------------------------------------


class _UsageError(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        raise _UsageError(f"{self.prog}: error: {message}")


EPILOG = """\
tiers:
  read     whoami, blogs, blog show, list, show, render, guide
  draft    new, update                      (cannot make anything public)
  risky    publish, unpublish, edit-live, delete, pin, unpin,
           blog create|update|delete, account, login, logout

exit codes: 0 ok, 1 usage/refused, 2 API rejected, 3 login needed,
            4 not found, 5 network/server error

Run `wfc guide` for the full workflow (written for coding agents too).
"""


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="print JSON")
    common.add_argument("--instance", metavar="HOST", default=argparse.SUPPRESS, help="saved instance to use")
    risky = argparse.ArgumentParser(add_help=False)
    risky.add_argument("-y", "--yes", action="store_true", help="skip the prompt (required with no terminal)")
    postopts = argparse.ArgumentParser(add_help=False)
    postopts.add_argument("--title", help="post title (overrides a leading # heading)")
    postopts.add_argument("--created", metavar="YYYY-MM-DDTHH:MM:SSZ", help="publication date, UTC")
    postopts.add_argument("--font", choices=FONTS, help="post appearance")
    postopts.add_argument("--lang", metavar="CODE", help="two-letter language code")
    postopts.add_argument("--rtl", action=argparse.BooleanOptionalAction, default=None, help="right-to-left text")
    blogopts = argparse.ArgumentParser(add_help=False)
    blogopts.add_argument("--visibility", choices=list(VISIBILITY), help="who can see the blog")

    p = _Parser(
        prog="wfc",
        description="Manage a self-hosted WriteFreely blog from the terminal.",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--version", action="version", version=f"wfc {__version__}")
    p.add_argument("--json", action="store_true", help="print JSON")
    p.add_argument("--instance", metavar="HOST", help="saved instance to use (default: last login)")
    sub = p.add_subparsers(dest="command", metavar="COMMAND")

    def add(name: str, func: Callable[[argparse.Namespace], None], help: str, parents=(), parser=sub):  # type: ignore[no-untyped-def]
        sp = parser.add_parser(name, help=help, description=help, parents=[common, *parents])
        sp.set_defaults(func=func)
        return sp

    sp = add("login", cmd_login, "log in and save a token (run in a terminal)")
    sp.add_argument("url", help="instance URL, e.g. https://write.example.com")
    sp.add_argument("--alias", help="your alias (username); else prompt or WF_ALIAS")
    add("logout", cmd_logout, "invalidate the token and forget this instance")
    add("whoami", cmd_whoami, "show the logged-in user")
    add("blogs", cmd_blogs, "list your blogs")

    sp = add("list", cmd_list, "list posts (no bodies)")
    group = sp.add_mutually_exclusive_group()
    group.add_argument("--drafts", action="store_true", help="drafts only")
    group.add_argument("--blog", metavar="ALIAS", help="posts in one blog")
    sp.add_argument("--page", type=int, help="with --blog: one page only")

    sp = add("show", cmd_show, "show one post")
    sp.add_argument("id")
    sp.add_argument("--body-only", action="store_true", help="print only the Markdown body")

    sp = add("render", cmd_render, "render Markdown to HTML on the server")
    sp.add_argument("file", help="Markdown file, or - for stdin")
    add("guide", cmd_guide, "print the workflow guide for people and agents")

    sp = add("new", cmd_new, "create a draft from a Markdown file", parents=[postopts])
    sp.add_argument("file", help="Markdown file, or - for stdin")

    sp = add("update", cmd_update, "update a draft (only the fields given)", parents=[postopts])
    sp.add_argument("id")
    sp.add_argument("file", nargs="?", help="Markdown file, or - for stdin")

    sp = add("edit-live", cmd_edit_live, "update a published post (live)", parents=[postopts, risky])
    sp.add_argument("id")
    sp.add_argument("file", nargs="?", help="Markdown file, or - for stdin")
    sp.add_argument("--slug", help="new URL slug")

    sp = add("publish", cmd_publish, "move a draft into a blog", parents=[risky])
    sp.add_argument("id")
    sp.add_argument("--blog", metavar="ALIAS", required=True, help="target blog (required)")
    sp.add_argument("--keep-date", action="store_true", help="keep the draft's original date instead of now")
    sp = add("unpublish", cmd_unpublish, "move a published post back to drafts", parents=[risky])
    sp.add_argument("id")
    sp = add("delete", cmd_delete, "permanently delete a post", parents=[risky])
    sp.add_argument("id")
    sp = add("pin", cmd_pin, "pin a post on a blog", parents=[risky])
    sp.add_argument("id")
    sp.add_argument("--blog", metavar="ALIAS", required=True)
    sp.add_argument("--position", type=int, default=1)
    sp = add("unpin", cmd_unpin, "unpin a post", parents=[risky])
    sp.add_argument("id")
    sp.add_argument("--blog", metavar="ALIAS", required=True)

    blog = sub.add_parser("blog", help="show, create, update or delete a blog")
    bsub = blog.add_subparsers(dest="blog_command", metavar="ACTION", required=True)
    sp = add("show", cmd_blog_show, "show a blog", parser=bsub)
    sp.add_argument("alias")
    sp = add("create", cmd_blog_create, "create a blog", parents=[risky, blogopts], parser=bsub)
    sp.add_argument("alias")
    sp.add_argument("--title")
    sp = add("update", cmd_blog_update, "change blog settings", parents=[risky, blogopts], parser=bsub)
    sp.add_argument("alias")
    sp.add_argument("--title")
    sp.add_argument("--description")
    sp.add_argument("--signature", help="text appended to every post")
    sp.add_argument("--style-sheet", metavar="FILE", help="custom CSS file")
    sp.add_argument("--script", metavar="FILE", help="custom JavaScript file")
    sp.add_argument("--format", choices=FORMATS)
    sp.add_argument("--monetization-pointer", metavar="POINTER")
    sp.add_argument("--verification-link", metavar="URL", help="rel=me link")
    sp.add_argument("--letter-reply", metavar="EMAIL", help="reply-to address for email newsletters")
    onoff = "required: the server turns it off unless it is sent, and cannot report its current value"
    sp.add_argument("--mathjax", choices=("on", "off"), required=True, help=onoff)
    sp.add_argument("--email-subs", choices=("on", "off"), required=True, help=onoff)
    sp = add("delete", cmd_blog_delete, "delete a blog (its posts become drafts)", parents=[risky], parser=bsub)
    sp.add_argument("alias")

    account = sub.add_parser("account", help="change your account email or password")
    asub = account.add_subparsers(dest="account_command", metavar="ACTION", required=True)
    sp = add("update", cmd_account_update, "change your email", parents=[risky], parser=asub)
    sp.add_argument("--email")
    add("password", cmd_account_password, "change your password", parents=[risky], parser=asub)
    return p


# --- entry point --------------------------------------------------------------


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except _UsageError as e:
        sys.stderr.write(str(e) + "\n")
        return EXIT_USAGE
    if not getattr(args, "func", None):
        parser.print_help(sys.stderr)
        return EXIT_USAGE
    if not hasattr(args, "yes"):
        args.yes = False
    try:
        args.func(args)
        return EXIT_OK
    except CLIError as e:
        if args.json:
            sys.stderr.write(json.dumps({"error": redact(e.message), "status": e.status}) + "\n")
        else:
            sys.stderr.write("wfc: " + redact(e.message) + "\n")
        return e.code
    except KeyboardInterrupt:
        sys.stderr.write("Interrupted.\n")
        return 130
    except Exception as e:  # never show a raw traceback; it could carry request details
        if os.environ.get("WFC_DEBUG"):
            sys.stderr.write(redact(traceback.format_exc()))
        sys.stderr.write(redact(f"wfc: unexpected error: {type(e).__name__}: {e}") + "\n")
        return EXIT_API


def run() -> None:
    sys.exit(main())


if __name__ == "__main__":
    run()
