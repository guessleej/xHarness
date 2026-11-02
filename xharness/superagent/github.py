"""The GitHub Source: one repository, read by polling (no public address needed),
written through comments and pull requests the system makes on the worker's
behalf -- a worker never holds the token.

What it reads, each round:
  1. the agent account's notifications in the repository (mentions, review
     requests, threads it takes part in), and for each the issue or pull
     request with its comments since the cursor;
  2. with a "receive every message" scope switched on: new issues, new pull
     requests, all conversation comments, the default branch's CI;
  3. every Thread a ticket subscribes to (live, or ended within 14 days): new
     comments, inline review comments and reviews, the pull request's merge or
     close, and CI changes on its head commit.

Trigger ids are derived from content (comment id + updated time), so a round
that overlaps the previous one stores nothing twice. Cursors step back five
seconds for events GitHub lists late.

Rows of the admission table (receiving scopes): issues, pulls, comments, ci.
"""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess  # nosec B404 - git with fixed argv
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .model import Doc, key, live, now_iso

KIND = "github"
ROWS = ("issues", "pulls", "comments", "ci")
HTTP_TIMEOUT = 20
HORIZON_DAYS = 14
OVERLAP_SECONDS = 5
REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
TOKEN_SHAPE = re.compile(r"(gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})")


class GitHubError(RuntimeError):
    def __init__(self, status: int, message: str) -> None:
        self.status = status
        super().__init__(f"GitHub {status}: {TOKEN_SHAPE.sub('[redacted]', message)[:300]}")


def scrub(text: str) -> str:
    return TOKEN_SHAPE.sub("[redacted]", text)


def check_config(config: Doc) -> Doc:
    repository = str(config.get("repository", "")).strip()
    if not REPOSITORY.match(repository):
        raise ValueError("repository: write owner/name")
    api = str(config.get("api", "https://api.github.com")).rstrip("/")
    parsed = urllib.parse.urlparse(api)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError("api: an https:// address (GitHub Enterprise: https://host/api/v3)")
    receive = config.get("receive") or {}
    interval = int(config.get("interval", 60))
    if not 20 <= interval <= 3600:
        raise ValueError("interval: 20 to 3600 seconds")
    return {"repository": repository, "api": api, "login": str(config.get("login", "")).strip(),
            "receive": {row: bool(receive.get(row)) for row in ROWS}, "interval": interval,
            "defaultBranch": str(config.get("defaultBranch", "")).strip()}


class Client:
    """A small GitHub REST client; the token goes only in the Authorization header."""

    def __init__(self, api: str, token: Callable[[], str]) -> None:
        self.api = api.rstrip("/")
        self._token = token

    def request(self, method: str, path: str, body: Doc | None = None, params: Doc | None = None) -> tuple[Any, dict[str, str]]:
        url = path if path.startswith("https://") else f"{self.api}/{path.lstrip('/')}"
        if not url.startswith(self.api + "/"):
            raise GitHubError(0, f"refusing a URL outside {self.api}")
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": f"Bearer {self._token()}", "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "xharness-superagent", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:  # nosec B310 - https API host checked above
                raw, headers = response.read(), {k.lower(): v for k, v in response.headers.items()}
        except urllib.error.HTTPError as error:
            raise GitHubError(error.code, error.read().decode("utf-8", "replace")) from None
        except (urllib.error.URLError, OSError) as error:
            raise GitHubError(0, str(error)) from None
        text = raw.decode("utf-8", "replace")
        return (json.loads(text) if text.strip() else None), headers

    def get(self, path: str, **params: Any) -> Any:
        return self.request("GET", path, params=params or None)[0]

    def pages(self, path: str, limit: int = 10, **params: Any) -> list[Any]:
        out: list[Any] = []
        url: str | None = path
        query: Doc | None = {"per_page": 100, **params}
        for _ in range(limit):
            if not url:
                break
            items, headers = self.request("GET", url, params=query)
            out.extend(items or [])
            match = re.search(r'<([^>]+)>;\s*rel="next"', headers.get("link", ""))
            url, query = (match.group(1), None) if match else (None, None)
        return out

    def post(self, path: str, body: Doc) -> Any:
        return self.request("POST", path, body=body)[0]


def human_login(user: Doc | None) -> bool:
    user = user or {"login": "ghost", "type": "User"}
    return not user.get("login", "").endswith("[bot]") and user.get("type", "User") == "User"


def mentions(text: str, login: str) -> bool:
    return bool(login) and re.search(rf"(?:^|[^a-zA-Z0-9_@])@{re.escape(login)}(?![a-zA-Z0-9_-])", text or "", re.I) is not None


def _shift(iso: str, seconds: float) -> str:
    moment = datetime.fromisoformat(iso.replace("Z", "+00:00")) + timedelta(seconds=seconds)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class GitHubSource:
    kind = KIND

    def __init__(self, source_id: str, config: Doc, client: Client, store: Any, receive: Callable[[Doc], str],
                 bind: Callable[[str, Doc, bool], Any], closed: Callable[[Doc, str, str], None]) -> None:
        self.id = source_id
        self.config = check_config(config)
        self.repo = self.config["repository"]
        self.client = client
        self.store = store
        self.receive = receive
        self.bind = bind
        self.closed = closed
        self.login = self.config["login"]
        self.health: Doc = {"state": "starting", "detail": "", "at": now_iso()}

    # The domain's view

    def scope(self, meta: Doc) -> Doc:
        if meta.get("repository") and meta["repository"].lower() != self.repo.lower():
            return {"listened": False, "reason": "otherRepository"}
        row = "ci" if meta.get("ci") else meta.get("row") or "comments"
        return {"listened": True, "observed": bool(self.config["receive"].get(row)), "row": row}

    def thread_ref(self, number: int | str) -> Doc:
        return {"source": self.id, "id": str(number)}

    def channel(self, number: int, url: str = "") -> Doc:
        return {"source": self.id, "thread": f"{self.id}:{number}", "address": {"number": str(number), **({"url": url} if url else {})}}

    def _actor(self, user: Doc | None) -> Doc:
        user = user or {"login": "ghost", "type": "User"}
        return {"actor": {"source": self.id, "id": user["login"]},
                "human": human_login(user) and user["login"].lower() != self.login.lower()}

    def issue_trigger(self, issue: Doc, pr: bool) -> Doc:
        body = issue.get("body") or ""
        trigger = {"id": key(self.id, "issue", str(issue["number"])), "source": self.id, "threads": [self.thread_ref(issue["number"])],
                   **self._actor(issue.get("user")), "at": issue["created_at"], "text": f"{issue['title']}\n\n{body}",
                   "channel": self.channel(issue["number"], issue.get("html_url", "")), "url": issue.get("html_url", ""),
                   "meta": {"repository": self.repo, "number": issue["number"], "isPr": pr, "kind": "pull" if pr else "issue",
                            "row": "pulls" if pr else "issues", "title": issue["title"]}}
        if mentions(body, self.login) or mentions(issue["title"], self.login):
            trigger["addressed"] = "mention"
        return trigger

    def comment_trigger(self, comment: Doc, number: int, pr: bool, kind: str) -> Doc:
        at = comment.get("updated_at") or comment.get("submitted_at") or comment.get("created_at") or now_iso()
        version = comment.get("updated_at") or comment.get("submitted_at") or ""
        text = comment.get("body") or ""
        if kind == "review" and comment.get("state") not in (None, "COMMENTED"):
            text = f"{comment['state']} review of commit {comment.get('commit_id', '')[:12]}\n\n{text}".strip()
        if kind == "inline" and comment.get("path"):
            hunk = "\n".join((comment.get("diff_hunk") or "").split("\n")[-3:])
            text = f"{comment['path']}:{comment.get('line') or comment.get('original_line') or ''}\n{hunk}\n\n{text}"
        trigger = {"id": key(self.id, "comment", str(comment["id"]), version), "source": self.id,
                   "threads": [self.thread_ref(number)], **self._actor(comment.get("user")), "at": at, "text": text,
                   "channel": self.channel(number, comment.get("html_url", "")), "url": comment.get("html_url", ""),
                   "meta": {"repository": self.repo, "number": number, "isPr": pr, "kind": kind, "row": "comments",
                            "commentId": comment["id"]}}
        if mentions(comment.get("body") or "", self.login):
            trigger["addressed"] = "mention"
        return trigger

    def ci_trigger(self, number: int | None, sha: str, summary: str, url: str, failed: bool) -> Doc:
        thread = self.thread_ref(number) if number else {"source": self.id, "id": f"ci:{self.config['defaultBranch'] or 'default'}"}
        trigger = {"id": key(self.id, "ci", thread["id"], sha, summary), "source": self.id, "threads": [thread],
                   "actor": {"source": self.id, "id": ""}, "human": False, "at": now_iso(), "text": summary, "url": url,
                   "meta": {"repository": self.repo, "ci": True, "sha": sha, "failed": failed, "row": "ci",
                            **({"number": number, "isPr": True} if number else {"branch": self.config["defaultBranch"]})}}
        if number:
            trigger["channel"] = self.channel(number)
        return trigger

    # Polling

    def _state(self, name: str, default: Any = None) -> Any:
        return self.store.kv(f"source:{self.id}:{name}", default)

    def _set_state(self, name: str, value: Any) -> None:
        self.store.set_kv(f"source:{self.id}:{name}", value)

    def ensure_login(self) -> None:
        if not self.login:
            self.login = self.client.get("user")["login"]
        if not self.config["defaultBranch"]:
            self.config["defaultBranch"] = self.client.get(f"repos/{self.repo}").get("default_branch", "main")

    def poll(self) -> int:
        """One round; returns how many Triggers were stored. Failures are recorded as the Source's health."""
        started = now_iso()
        stored = 0
        try:
            self.ensure_login()
            stored += self._notifications(started)
            stored += self._streams(started)
            stored += self._subscribed()
            if self.config["receive"]["ci"]:
                stored += self._branch_ci()
            self.health = {"state": "ready", "detail": f"{stored} new events", "at": started}
        except GitHubError as error:
            self.health = {"state": "error", "detail": str(error), "at": started}
        return stored

    def _take(self, trigger: Doc) -> int:
        return 1 if self.receive(trigger) == "stored" else 0

    def _notifications(self, started: str) -> int:
        since = self._state("since") or _shift(started, -3600)
        stored = 0
        for note in self.client.pages("notifications", all="true", since=since, limit=5):
            if (note.get("repository") or {}).get("full_name", "").lower() != self.repo.lower():
                continue
            match = re.search(r"/(issues|pulls)/(\d+)$", (note.get("subject") or {}).get("url") or "")
            if match:
                stored += self._read_thread(int(match.group(2)), since)
        self._set_state("since", _shift(started, -OVERLAP_SECONDS))
        return stored

    def _read_thread(self, number: int, since: str) -> int:
        issue = self.client.get(f"repos/{self.repo}/issues/{number}")
        pr = bool(issue.get("pull_request"))
        stored = 0
        if issue.get("created_at", "") >= since and (issue.get("user") or {}).get("login", "").lower() != self.login.lower():
            stored += self._take(self.issue_trigger(issue, pr))
        stored += self._comments(number, pr, since)
        return stored

    def _comments(self, number: int, pr: bool, since: str) -> int:
        stored = 0
        for comment in self.client.pages(f"repos/{self.repo}/issues/{number}/comments", since=since):
            stored += self._take(self.comment_trigger(comment, number, pr, "conversation"))
        if pr:
            for comment in self.client.pages(f"repos/{self.repo}/pulls/{number}/comments", since=since):
                stored += self._take(self.comment_trigger(comment, number, True, "inline"))
            for review in self.client.pages(f"repos/{self.repo}/pulls/{number}/reviews"):
                if (review.get("submitted_at") or "") >= since and (review.get("body") or review.get("state") != "COMMENTED"):
                    stored += self._take(self.comment_trigger(review, number, True, "review"))
        return stored

    def _streams(self, started: str) -> int:
        receive = self.config["receive"]
        stored = 0
        if receive["issues"] or receive["pulls"]:
            since = self._state("stream-issues") or started
            for issue in self.client.pages(f"repos/{self.repo}/issues", state="all", sort="created", direction="asc", since=since):
                pr = bool(issue.get("pull_request"))
                if issue.get("created_at", "") >= since and receive["pulls" if pr else "issues"] \
                        and (issue.get("user") or {}).get("login", "").lower() != self.login.lower():
                    stored += self._take(self.issue_trigger(issue, pr))
            self._set_state("stream-issues", _shift(started, -OVERLAP_SECONDS))
        if receive["comments"]:
            since = self._state("stream-comments") or started
            for comment in self.client.pages(f"repos/{self.repo}/issues/comments", sort="updated", direction="asc", since=since):
                number = int(str(comment.get("issue_url", "")).rsplit("/", 1)[-1] or 0)
                if number:
                    stored += self._take(self.comment_trigger(comment, number, "/pull/" in comment.get("html_url", ""), "conversation"))
            self._set_state("stream-comments", _shift(started, -OVERLAP_SECONDS))
        return stored

    def _polled(self, thread: Doc) -> bool:
        tickets = [self.store.ticket(i) for i in thread["subscriptions"]]
        if any(t and live(t) for t in tickets):
            return True
        times = [thread.get("lastAt") or thread["createdAt"], *[(t or {}).get("ended", {}).get("at", "") for t in tickets]]
        latest = max(t for t in times if t)
        return latest >= _shift(now_iso(), -HORIZON_DAYS * 86400)

    def _subscribed(self) -> int:
        stored = 0
        for thread in self.store.threads(limit=500, subscribed=True):
            if thread["ref"]["source"] != self.id or not thread["ref"]["id"].isdigit() or not self._polled(thread):
                continue
            number = int(thread["ref"]["id"])
            checkpoint = f"thread:{number}"
            since = self._state(checkpoint) or thread["createdAt"]
            started = now_iso()
            try:
                issue = self.client.get(f"repos/{self.repo}/issues/{number}")
                pr = bool(issue.get("pull_request"))
                stored += self._comments(number, pr, since)
                if pr:
                    stored += self._pull_state(number)
                elif issue.get("state") == "closed":
                    self.closed(self.thread_ref(number), "closed", issue.get("closed_at") or started)
                self._set_state(checkpoint, _shift(started, -OVERLAP_SECONDS))
            except GitHubError as error:
                self._set_state(f"thread-error:{number}", str(error))
        return stored

    def _pull_state(self, number: int) -> int:
        pull = self.client.get(f"repos/{self.repo}/pulls/{number}")
        if pull.get("merged_at"):
            self.closed(self.thread_ref(number), "merged", pull["merged_at"])
        elif pull.get("state") == "closed":
            self.closed(self.thread_ref(number), "closed", pull.get("closed_at") or now_iso())
        sha = (pull.get("head") or {}).get("sha", "")
        if not sha:
            return 0
        summary, failed = self._checks(sha)
        if not summary:
            return 0
        fingerprint = key(sha, summary)
        if self._state(f"ci:{number}") == fingerprint:
            return 0
        self._set_state(f"ci:{number}", fingerprint)
        return self._take(self.ci_trigger(number, sha, summary, pull.get("html_url", ""), failed))

    def _checks(self, sha: str) -> tuple[str, bool]:
        """A one-line CI summary once every check finished; empty while any still runs."""
        runs = (self.client.get(f"repos/{self.repo}/commits/{sha}/check-runs", per_page=100) or {}).get("check_runs", [])
        if not runs or any(r.get("status") != "completed" for r in runs):
            return "", False
        bad = [r["name"] for r in runs if r.get("conclusion") not in ("success", "neutral", "skipped")]
        if bad:
            return f"CI failed on {sha[:12]}: {', '.join(sorted(bad))}", True
        return f"CI passed on {sha[:12]} ({len(runs)} checks)", False

    def _branch_ci(self) -> int:
        branch = self.config["defaultBranch"]
        sha = self.client.get(f"repos/{self.repo}/commits/{branch}").get("sha", "")
        summary, failed = self._checks(sha) if sha else ("", False)
        previous = self._state("ci-branch") or {}
        if not summary or previous.get("sha") == sha:
            return 0
        self._set_state("ci-branch", {"sha": sha, "failed": failed})
        # Only a change matters: a new failure, or a recovery after one.
        if failed or previous.get("failed"):
            url = f"https://github.com/{self.repo}/commit/{sha}"
            return self._take(self.ci_trigger(None, sha, f"{summary} (default branch {branch})", url, failed))
        return 0

    # Writing

    def send(self, message: Doc) -> str:
        """Post a comment (or a review-thread reply); a marker in the body keeps a retry from posting twice."""
        number = int(message["channel"]["address"]["number"])
        marker = f"<!-- xharness:{message['id']} -->"
        url = message["channel"]["address"].get("url", "")
        root = None
        match = re.search(r"#discussion_r(\d+)$", url)
        if match:
            try:
                source = self.client.get(f"repos/{self.repo}/pulls/comments/{match.group(1)}")
                root = source.get("in_reply_to_id") or source.get("id")
            except GitHubError as error:
                if error.status not in (404, 410):
                    raise
        path = f"repos/{self.repo}/pulls/{number}/comments" if root else f"repos/{self.repo}/issues/{number}/comments"
        try:
            for comment in self.client.pages(path, limit=5):
                if marker in (comment.get("body") or "") and (not root or comment.get("in_reply_to_id") == root):
                    return "sent"
            target = f"{path}/{root}/replies" if root else path
            self.client.post(target, {"body": f"{message['text']}\n\n{marker}"})
        except GitHubError as error:
            if error.status == 410 or (error.status == 403 and "locked" in str(error).lower()):
                return "gone"
            raise
        return "sent"

    def open_pull_request(self, branch: str, title: str, body: str, base: str = "") -> Doc:
        base = base or self.config["defaultBranch"]
        existing = self.client.get(f"repos/{self.repo}/pulls", head=f"{self.repo.split('/')[0]}:{branch}", state="open")
        if existing:
            return existing[0]
        return self.client.post(f"repos/{self.repo}/pulls", {"title": title, "body": body, "head": branch, "base": base})

    def link(self, address: Doc) -> str:
        return address.get("url") or f"https://github.com/{self.repo}/issues/{address.get('number', '')}"


# The checkout tickets work in. The token reaches git only through the child's environment
# (GIT_CONFIG_*), never its arguments, a file, or the worker's sandbox.

def _git_env(token: str) -> dict[str, str]:
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "http.extraheader",
            "GIT_CONFIG_VALUE_0": f"Authorization: Basic {basic}", "GIT_TERMINAL_PROMPT": "0"}


def git(args: list[str], cwd: str, token: str = "", timeout: int = 600) -> str:
    env = {**os.environ, **(_git_env(token) if token else {})}
    done = subprocess.run(["git", "-c", "user.name=xharness", "-c", "user.email=xharness@localhost", *args],  # nosec B603 B607
                          cwd=cwd, capture_output=True, text=True, errors="replace", timeout=timeout, env=env)
    if done.returncode != 0:
        raise RuntimeError(f"git {args[0]} failed: {scrub((done.stderr or done.stdout).strip())[:400]}")
    return done.stdout


def ensure_clone(repository: str, root: str, token: str, web: str = "https://github.com") -> str:
    """A local clone of the repository under `root`, fetched up to date."""
    path = os.path.join(root, repository.replace("/", "__"))
    if not os.path.isdir(os.path.join(path, ".git")):
        os.makedirs(root, exist_ok=True)
        git(["clone", "--quiet", f"{web}/{repository}.git", path], root, token)
    else:
        git(["fetch", "--quiet", "--prune", "origin"], path, token)
    return path


def ticket_worktree(clone: str, work_root: str, name: str, base: str) -> str:
    """A worktree for one ticket on branch xharness/<name>, from origin/<base>; reused when it exists."""
    path = os.path.join(work_root, name)
    if os.path.isdir(path):
        return path
    os.makedirs(work_root, exist_ok=True)
    git(["worktree", "add", "-B", f"xharness/{name}", path, f"origin/{base}"], clone)
    return path


def push_branch(path: str, name: str, token: str) -> None:
    git(["push", "--quiet", "--force-with-lease", "origin", f"HEAD:refs/heads/xharness/{name}"], path, token)


def wait_seconds(started: float, interval: int) -> float:
    return max(0.0, interval - (time.time() - started))
