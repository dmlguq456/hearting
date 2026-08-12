#!/usr/bin/env python3
"""mm — Mattermost CLI wrapper for agent (Claude/Codex) and human use.

Auth: personal access token, loaded from ~/.config/mattermost/env
  MM_URL=https://your-mattermost-server.example.com
  MM_TOKEN=xxxxxxxxxxxxxxxxxxxxxxxxxx
Environment variables MM_URL / MM_TOKEN override the file.

Docs: https://developers.mattermost.com/integrate/reference/personal-access-token/
"""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

CONFIG_PATH = os.path.expanduser("~/.config/mattermost/env")

USAGE = """\
mm — Mattermost CLI (personal access token)

usage:
  mm me                          내 계정 확인 (인증 테스트)
  mm teams                       내 팀 목록
  mm channels [team]             내 채널 목록 (팀 지정 가능)
  mm read <channel> [N]          채널 최근 N개 메시지 (기본 20)
  mm post <channel> <message>    채널에 메시지 게시
  mm reply <post_id> <message>   스레드에 답글 게시
  mm search <terms>              메시지 검색
  mm users <term>                사용자 검색
  mm dm <username> <message>     사용자에게 DM 전송
  mm api <METHOD> <path> [json]  원시 API 호출 (예: mm api GET /users/me)

channel은 채널 name(URL 슬러그) 또는 display name. 팀이 여럿이면 team:channel 형식 가능.
설정 파일: ~/.config/mattermost/env  (MM_URL, MM_TOKEN)

쓰기 명령(post/reply/dm, api의 비-GET 메서드)은 기본 차단.
사용자가 명시적으로 요청한 경우에만 MM_ALLOW_WRITE=1 을 붙여 실행.
"""


def read_config_file():
    cfg = {}
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                cfg[k.strip()] = v.strip().strip('"').strip("'")
    return cfg


def load_config():
    cfg = read_config_file()
    url = os.environ.get("MM_URL") or cfg.get("MM_URL", "")
    token = os.environ.get("MM_TOKEN") or cfg.get("MM_TOKEN", "")
    if not url or not token:
        sys.exit(
            "mm: 자격증명이 없습니다. %s 에 MM_URL, MM_TOKEN을 설정하세요.\n"
            "토큰 발급: Mattermost 프로필 > 보안 > 개인 액세스 토큰 (관리자가 기능/계정 권한을 먼저 활성화해야 함)\n"
            "참고: https://developers.mattermost.com/integrate/reference/personal-access-token/" % CONFIG_PATH
        )
    return url.rstrip("/"), token


def ensure_write_allowed(what):
    """에이전트(harness) 기본 쓰기 금지 게이트. 사용자가 명시적으로 요청한 경우에만 해제."""
    if os.environ.get("MM_ALLOW_WRITE") == "1" or read_config_file().get("MM_ALLOW_WRITE") == "1":
        return
    sys.exit(
        "mm: 쓰기 작업(%s)은 기본 차단되어 있습니다.\n"
        "사용자가 명시적으로 게시를 요청한 경우에만 MM_ALLOW_WRITE=1 을 붙여 다시 실행하세요.\n"
        "예: MM_ALLOW_WRITE=1 mm %s ..." % (what, what)
    )


def api(method, path, body=None):
    url, token = load_config()
    full = url + "/api/v4" + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(full, data=data, method=method)
    req.add_header("Authorization", "Bearer " + token)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        try:
            detail = json.loads(detail).get("message", detail)
        except (ValueError, AttributeError):
            pass
        sys.exit("mm: HTTP %d %s — %s (%s %s)" % (e.code, e.reason, detail, method, path))
    except urllib.error.URLError as e:
        sys.exit("mm: 연결 실패 — %s (%s)" % (e.reason, url))


def my_teams():
    return api("GET", "/users/me/teams")


def resolve_channel(spec):
    """channel spec ('name', 'display name', 'team:name') -> channel dict."""
    team_name = None
    if ":" in spec:
        team_name, _, spec = spec.partition(":")
    teams = my_teams()
    if team_name:
        teams = [t for t in teams if t["name"] == team_name] or sys.exit(
            "mm: 팀 '%s' 을 찾을 수 없습니다" % team_name
        )
    # exact name (URL slug) lookup per team first
    for t in teams:
        try:
            return api("GET", "/teams/%s/channels/name/%s" % (t["id"], urllib.parse.quote(spec)))
        except SystemExit:
            pass
    # fallback: match display_name among my channel memberships
    matches = []
    for t in teams:
        for c in api("GET", "/users/me/teams/%s/channels" % t["id"]):
            if c.get("display_name", "").lower() == spec.lower():
                matches.append(c)
    if len(matches) == 1:
        return matches[0]
    if matches:
        sys.exit("mm: 채널명 '%s' 이 여러 팀에 있습니다. team:channel 형식으로 지정하세요." % spec)
    sys.exit("mm: 채널 '%s' 을 찾을 수 없습니다 (mm channels 로 목록 확인)" % spec)


def usernames_for(user_ids):
    if not user_ids:
        return {}
    users = api("POST", "/users/ids", sorted(user_ids))
    return {u["id"]: u["username"] for u in users}


def print_posts(order, posts):
    id2name = usernames_for({posts[pid]["user_id"] for pid in order})
    for pid in reversed(order):  # API returns newest first
        p = posts[pid]
        import datetime

        ts = datetime.datetime.fromtimestamp(p["create_at"] / 1000).strftime("%Y-%m-%d %H:%M")
        root = "  ↳ " if p.get("root_id") else ""
        print("[%s] %s%s: %s  (id:%s)" % (ts, root, id2name.get(p["user_id"], "?"), p["message"], p["id"]))
        for f in p.get("metadata", {}).get("files", []) or []:
            print("        📎 %s" % f.get("name"))


def main():
    args = sys.argv[1:]
    if not args or args[0] in ("help", "-h", "--help"):
        print(USAGE)
        return
    cmd = args[0]

    if cmd == "me":
        me = api("GET", "/users/me")
        print("%s (%s) — %s" % (me["username"], me.get("email", ""), me["id"]))
    elif cmd == "teams":
        for t in my_teams():
            print("%s\t%s\t(id:%s)" % (t["name"], t["display_name"], t["id"]))
    elif cmd == "channels":
        teams = my_teams()
        if len(args) > 1:
            teams = [t for t in teams if t["name"] == args[1]] or sys.exit("mm: 팀 '%s' 없음" % args[1])
        for t in teams:
            for c in sorted(api("GET", "/users/me/teams/%s/channels" % t["id"]), key=lambda c: c["name"]):
                kind = {"O": "공개", "P": "비공개", "D": "DM", "G": "그룹"}.get(c["type"], c["type"])
                print("%s:%s\t%s\t[%s]" % (t["name"], c["name"], c.get("display_name", ""), kind))
    elif cmd == "read":
        if len(args) < 2:
            sys.exit("usage: mm read <channel> [N]")
        n = int(args[2]) if len(args) > 2 else 20
        ch = resolve_channel(args[1])
        data = api("GET", "/channels/%s/posts?per_page=%d" % (ch["id"], n))
        print_posts(data["order"], data["posts"])
    elif cmd == "post":
        if len(args) < 3:
            sys.exit("usage: mm post <channel> <message>")
        ensure_write_allowed("post")
        ch = resolve_channel(args[1])
        p = api("POST", "/posts", {"channel_id": ch["id"], "message": " ".join(args[2:])})
        print("게시됨: %s (id:%s)" % (ch.get("display_name") or ch["name"], p["id"]))
    elif cmd == "reply":
        if len(args) < 3:
            sys.exit("usage: mm reply <post_id> <message>")
        ensure_write_allowed("reply")
        root = api("GET", "/posts/%s" % args[1])
        root_id = root.get("root_id") or root["id"]
        p = api("POST", "/posts", {"channel_id": root["channel_id"], "message": " ".join(args[2:]), "root_id": root_id})
        print("답글 게시됨 (id:%s)" % p["id"])
    elif cmd == "search":
        if len(args) < 2:
            sys.exit("usage: mm search <terms>")
        terms = " ".join(args[1:])
        for t in my_teams():
            data = api("POST", "/teams/%s/posts/search" % t["id"], {"terms": terms, "is_or_search": False})
            if data["order"]:
                print("## 팀 %s" % t["name"])
                print_posts(data["order"], data["posts"])
    elif cmd == "users":
        if len(args) < 2:
            sys.exit("usage: mm users <term>")
        for u in api("POST", "/users/search", {"term": args[1]}):
            print("%s\t%s %s\t%s" % (u["username"], u.get("first_name", ""), u.get("last_name", ""), u.get("email", "")))
    elif cmd == "dm":
        if len(args) < 3:
            sys.exit("usage: mm dm <username> <message>")
        ensure_write_allowed("dm")
        me = api("GET", "/users/me")
        other = api("GET", "/users/username/%s" % urllib.parse.quote(args[1]))
        ch = api("POST", "/channels/direct", [me["id"], other["id"]])
        p = api("POST", "/posts", {"channel_id": ch["id"], "message": " ".join(args[2:])})
        print("DM 전송됨 → %s (id:%s)" % (other["username"], p["id"]))
    elif cmd == "api":
        if len(args) < 3:
            sys.exit("usage: mm api <METHOD> </path> [json-body]")
        method = args[1].upper()
        # 검색류 POST 엔드포인트는 읽기 작업이므로 예외
        read_only_posts = ("/search", "/users/ids", "/users/search")
        if method not in ("GET", "HEAD", "OPTIONS") and not (
            method == "POST" and args[2].split("?")[0].endswith(read_only_posts)
        ):
            ensure_write_allowed("api %s" % method)
        body = json.loads(args[3]) if len(args) > 3 else None
        out = api(method, args[2], body)
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        sys.exit("mm: 알 수 없는 명령 '%s'\n\n%s" % (cmd, USAGE))


if __name__ == "__main__":
    main()
