#!/usr/bin/env python3
"""Local browser for Claude Code conversation history.

Scans ~/.claude/projects/*/*.jsonl, serves a small JSON API plus the UI.
Run:  python3 server.py   then open http://localhost:4747
"""
import json
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

PROJECTS_DIR = os.path.expanduser("~/.claude/projects")
HERE = os.path.dirname(os.path.abspath(__file__))
PORT = 4747

# USD per 1M tokens: (input, output). Cache read = 0.1x input, cache write = 1.25x input.
PRICES = {
    "fable": (10.0, 50.0),
    "mythos": (10.0, 50.0),
    "opus": (5.0, 25.0),
    "sonnet": (3.0, 15.0),
    "haiku": (1.0, 5.0),
}


def usage_cost(model, usage):
    m = model or ""
    in_p, out_p = next((p for k, p in PRICES.items() if k in m), PRICES["opus"])
    return (
        (usage.get("input_tokens", 0) or 0) * in_p
        + (usage.get("output_tokens", 0) or 0) * out_p
        + (usage.get("cache_read_input_tokens", 0) or 0) * in_p * 0.1
        + (usage.get("cache_creation_input_tokens", 0) or 0) * in_p * 1.25
    ) / 1_000_000


def scan_usage_lines(path):
    """Sum API-equivalent cost of all assistant messages in one jsonl file."""
    cost = 0.0
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            if '"type":"assistant"' not in line or '"usage"' not in line:
                continue
            try:
                msg = json.loads(line).get("message", {})
            except ValueError:
                continue
            usage = msg.get("usage")
            if usage:
                cost += usage_cost(msg.get("model", ""), usage)
    return cost


TS_RE = re.compile(r'"timestamp":"([^"]+)"')
CWD_RE = re.compile(r'"cwd":"([^"]+)"')
CMD_RE = re.compile(r"<command-name>(.*?)</command-name>", re.S)

# user-message texts that are plumbing, not something the user typed
NOISE_PREFIXES = (
    "Caveat:", "<command-", "<local-command", "<system-reminder",
    "[Request interrupted",
)

_index_cache = {}  # path -> ((mtime, size), meta)


def first_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                return block.get("text", "")
    return ""


def scan_session(path):
    """Cheap single pass over one jsonl file for index metadata."""
    st = os.stat(path)
    key = (st.st_mtime, st.st_size)
    cached = _index_cache.get(path)
    if cached and cached[0] == key:
        return cached[1]

    title = None
    subtext = None
    cwd = None
    first_ts = None
    last_ts = None
    prompts = 0
    user_lines = 0
    cost = 0.0

    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            if '"type":"assistant"' in line and '"usage"' in line:
                try:
                    msg = json.loads(line).get("message", {})
                    if msg.get("usage"):
                        cost += usage_cost(msg.get("model", ""), msg["usage"])
                except ValueError:
                    pass
            if '"timestamp"' in line:
                m = TS_RE.search(line)
                if m:
                    if first_ts is None:
                        first_ts = m.group(1)
                    last_ts = m.group(1)
            if cwd is None and '"cwd"' in line:
                m = CWD_RE.search(line)
                if m:
                    cwd = m.group(1)
            if '"promptSource":"typed"' in line:
                prompts += 1
            elif ('"type":"user"' in line and '"role":"user"' in line
                  and '"tool_use_id"' not in line
                  and '"isSidechain":true' not in line
                  and '"isMeta":true' not in line):
                user_lines += 1
            if '"aiTitle"' in line:
                try:
                    title = json.loads(line).get("aiTitle") or title
                except ValueError:
                    pass
            if subtext is None and '"type":"user"' in line and '"role":"user"' in line:
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if obj.get("isSidechain") or obj.get("isMeta"):
                    continue
                txt = first_text(obj.get("message", {}).get("content")).strip()
                if txt and not txt.startswith(NOISE_PREFIXES):
                    subtext = re.sub(r"\s+", " ", txt)[:280]

    # subagent / workflow transcripts live in a companion dir named after the session
    session_dir = path[:-len(".jsonl")]
    if os.path.isdir(session_dir):
        for root, _dirs, files in os.walk(session_dir):
            for fn in files:
                if fn.endswith(".jsonl"):
                    try:
                        cost += scan_usage_lines(os.path.join(root, fn))
                    except OSError:
                        pass

    meta = None
    if title or subtext:
        meta = {
            "id": os.path.splitext(os.path.basename(path))[0],
            "title": title or (subtext[:64] if subtext else "Untitled"),
            "subtext": subtext or "",
            "cwd": cwd or "",
            "firstTs": first_ts,
            "lastTs": last_ts,
            "prompts": prompts or user_lines,
            "cost": round(cost, 4),
        }
    _index_cache[path] = (key, meta)
    return meta


def build_index():
    projects = []
    for name in sorted(os.listdir(PROJECTS_DIR)):
        pdir = os.path.join(PROJECTS_DIR, name)
        if not os.path.isdir(pdir):
            continue
        sessions = []
        for fn in os.listdir(pdir):
            if not fn.endswith(".jsonl"):
                continue
            try:
                meta = scan_session(os.path.join(pdir, fn))
            except OSError:
                continue
            if meta:
                sessions.append(meta)
        if sessions:
            sessions.sort(key=lambda s: s["lastTs"] or "", reverse=True)
            cwd = next((s["cwd"] for s in sessions if s["cwd"]), "")
            projects.append({"dir": name, "cwd": cwd, "sessions": sessions})
    return {"home": os.path.expanduser("~"), "projects": projects}


def tool_detail(inp):
    for k in ("command", "file_path", "pattern", "url", "query", "description",
              "prompt", "skill"):
        v = inp.get(k)
        if isinstance(v, str) and v.strip():
            return re.sub(r"\s+", " ", v.strip())[:120]
    return ""


def load_session(path):
    """Full parse of one session for the conversation view."""
    messages = []
    model = None
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            t = obj.get("type")
            if t not in ("user", "assistant") or obj.get("isSidechain"):
                continue
            msg = obj.get("message", {})
            ts = obj.get("timestamp")

            if t == "user":
                content = msg.get("content")
                raw = []
                if isinstance(content, str):
                    raw = [content]
                elif isinstance(content, list):
                    raw = [b.get("text", "") for b in content
                           if isinstance(b, dict) and b.get("type") == "text"]
                blocks = []
                for txt in raw:
                    s = txt.strip()
                    if not s:
                        continue
                    if s.startswith("<command-name>"):
                        m = CMD_RE.search(s)
                        blocks.append({"type": "command",
                                       "text": (m.group(1).strip() if m else "command")})
                    elif s.startswith("[Request interrupted"):
                        blocks.append({"type": "interrupt", "text": "interrupted"})
                    elif s.startswith(NOISE_PREFIXES):
                        continue
                    else:
                        blocks.append({"type": "text", "text": txt})
                if blocks:
                    messages.append({"role": "user", "ts": ts, "blocks": blocks})
            else:
                model = msg.get("model") or model
                blocks = []
                for b in msg.get("content") or []:
                    if not isinstance(b, dict):
                        continue
                    bt = b.get("type")
                    if bt == "text" and b.get("text", "").strip():
                        blocks.append({"type": "text", "text": b["text"]})
                    elif bt == "tool_use":
                        blocks.append({"type": "tool", "name": b.get("name", "?"),
                                       "detail": tool_detail(b.get("input") or {})})
                if blocks:
                    messages.append({"role": "assistant", "ts": ts, "blocks": blocks})
    return {"model": model, "messages": messages}


# ---------------------------------------------------------------- stats
from collections import Counter
from datetime import datetime, timedelta

STOPWORDS = set("""the a an and or but if then else for of in on at to from with by is are was
were be been being it its it's this that these those i you u we they he she me my your our
their them him her do does did done can could should would will just so very really too also
not no yes ok okay now want need make made like get got go going have has had see say said
all some any more most much many lot bit when what which who whom how why where there here
out up down over under again once than as because while about into through after before
right left new old same other another each both few own""".split())

TICS = ["like", "btw", "bro", "pls", "plz", "yaar", "bhai", "lol", "idk", "asap", "u"]
SWEARS = ["fuck", "fucking", "shit", "wtf", "damn", "bullshit", "crap"]

PHRASES = {
    "absolutely_right": re.compile(r"you'?re absolutely right|you are absolutely right", re.I),
    "apologize": re.compile(r"\bi apologi[sz]e\b|\bsorry\b", re.I),
    "perfect": re.compile(r"\bperfect[.!]", re.I),
    "great_question": re.compile(r"\bgreat (question|idea|catch)\b", re.I),
}

WORD_RE = re.compile(r"[a-zA-Z']+")

_stats_cache = {"key": None, "data": None}


def _iter_session_files():
    for name in sorted(os.listdir(PROJECTS_DIR)):
        pdir = os.path.join(PROJECTS_DIR, name)
        if not os.path.isdir(pdir):
            continue
        for fn in os.listdir(pdir):
            if fn.endswith(".jsonl"):
                yield name, os.path.join(pdir, fn)


def _count_subagents():
    n = 0
    for root, dirs, files in os.walk(PROJECTS_DIR):
        if "subagents" in root:
            n += sum(1 for f in files if f.startswith("agent-") and f.endswith(".jsonl"))
    return n


def _local_dt(ts):
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone()
    except ValueError:
        return None


def build_stats():
    files = list(_iter_session_files())
    key = tuple(sorted((p, os.path.getmtime(p)) for _, p in files))
    if _stats_cache["key"] == key:
        return _stats_cache["data"]

    prompts = 0
    prompt_words = 0
    longest_prompt = {"chars": 0, "text": "", "title": ""}
    caps_rage = 0
    interruptions = 0
    please = thanks = 0
    swears = Counter()
    tics = Counter()
    words = Counter()
    by_hour = [0] * 24
    by_weekday = [0] * 7
    by_date = Counter()
    night_owl = 0

    tools = Counter()
    bash_cmds = Counter()
    bash_total = 0
    git_commits = 0
    longest_bash = ""
    edited_files = Counter()
    tool_errors = 0
    phrase_hits = Counter()
    models = Counter()
    in_tokens = out_tokens = cache_tokens = 0
    cost_by_model = Counter()

    sessions = []
    first_ts_all = None
    last_ts_all = None

    for proj, path in files:
        title = None
        s_first = s_last = None
        s_prompts = 0
        s_msgs = 0
        s_cost = 0.0
        try:
            fh = open(path, encoding="utf-8", errors="replace")
        except OSError:
            continue
        with fh:
            for line in fh:
                if '"aiTitle"' in line:
                    try:
                        title = json.loads(line).get("aiTitle") or title
                    except ValueError:
                        pass
                    continue
                if '"[Request interrupted' in line or '[Request interrupted' in line:
                    interruptions += 1
                is_user = '"type":"user"' in line and '"role":"user"' in line
                is_asst = '"type":"assistant"' in line
                if not (is_user or is_asst):
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if obj.get("isSidechain"):
                    continue
                ts = obj.get("timestamp")
                if ts:
                    if s_first is None:
                        s_first = ts
                    s_last = ts
                msg = obj.get("message", {})
                s_msgs += 1

                if is_user:
                    content = msg.get("content")
                    txt = ""
                    if isinstance(content, str):
                        txt = content
                    elif isinstance(content, list):
                        for b in content:
                            if not isinstance(b, dict):
                                continue
                            if b.get("type") == "tool_result" and b.get("is_error"):
                                tool_errors += 1
                            elif b.get("type") == "text":
                                txt += b.get("text", "") + " "
                    txt = txt.strip()
                    if not txt or txt.startswith(NOISE_PREFIXES) or obj.get("isMeta"):
                        continue
                    prompts += 1
                    s_prompts += 1
                    toks = WORD_RE.findall(txt.lower())
                    prompt_words += len(toks)
                    if len(txt) > longest_prompt["chars"]:
                        longest_prompt = {"chars": len(txt),
                                          "text": re.sub(r"\s+", " ", txt)[:400],
                                          "title": title or ""}
                    letters = [c for c in txt if c.isalpha()]
                    if len(letters) > 12 and sum(c.isupper() for c in letters) / len(letters) > 0.6:
                        caps_rage += 1
                    low = " " + txt.lower() + " "
                    please += low.count("please") + low.count(" pls ") + low.count(" plz ")
                    thanks += low.count("thank") + low.count(" thx ")
                    for w in toks:
                        if w in SWEARS:
                            swears[w] += 1
                        if w in TICS:
                            tics[w] += 1
                    # document frequency, so one pasted log can't flood the chart
                    for w in set(toks):
                        if w not in STOPWORDS and len(w) >= 3:
                            words[w] += 1
                    dt = _local_dt(ts) if ts else None
                    if dt:
                        by_hour[dt.hour] += 1
                        by_weekday[dt.weekday()] += 1
                        by_date[dt.strftime("%Y-%m-%d")] += 1
                        if dt.hour < 5:
                            night_owl += 1
                else:
                    model = msg.get("model", "")
                    if model and "synthetic" not in model:
                        models[model] += 1
                    usage = msg.get("usage") or {}
                    in_tokens += usage.get("input_tokens", 0) or 0
                    out_tokens += usage.get("output_tokens", 0) or 0
                    cache_tokens += usage.get("cache_read_input_tokens", 0) or 0
                    if usage:
                        c = usage_cost(msg.get("model", ""), usage)
                        s_cost += c
                        cost_by_model[msg.get("model") or "?"] += c
                    for b in msg.get("content") or []:
                        if not isinstance(b, dict):
                            continue
                        bt = b.get("type")
                        if bt == "tool_use":
                            name = b.get("name", "?")
                            tools[name] += 1
                            inp = b.get("input") or {}
                            if name == "Bash":
                                cmd = inp.get("command", "")
                                bash_total += 1
                                if cmd:
                                    base = cmd.strip().split()[0] if cmd.strip() else "?"
                                    bash_cmds[os.path.basename(base)] += 1
                                    if "git commit" in cmd:
                                        git_commits += 1
                                    if len(cmd) > len(longest_bash):
                                        longest_bash = cmd
                            elif name in ("Edit", "Write", "NotebookEdit"):
                                fp = inp.get("file_path", "")
                                if fp:
                                    edited_files[fp] += 1
                        elif bt == "text":
                            t = b.get("text", "")
                            for k, pat in PHRASES.items():
                                phrase_hits[k] += len(pat.findall(t))

        session_dir = path[:-len(".jsonl")]
        if os.path.isdir(session_dir):
            for root, _dirs, fns in os.walk(session_dir):
                for fn in fns:
                    if fn.endswith(".jsonl"):
                        try:
                            sub = scan_usage_lines(os.path.join(root, fn))
                            s_cost += sub
                            cost_by_model["(sub-agents)"] += sub
                        except OSError:
                            pass

        if s_msgs:
            dur = 0
            if s_first and s_last:
                a, b2 = _local_dt(s_first), _local_dt(s_last)
                if a and b2:
                    dur = (b2 - a).total_seconds()
            sessions.append({"title": title or "(untitled)", "proj": proj,
                             "prompts": s_prompts, "msgs": s_msgs, "dur": dur,
                             "cost": s_cost})
            if s_first and (first_ts_all is None or s_first < first_ts_all):
                first_ts_all = s_first
            if s_last and (last_ts_all is None or s_last > last_ts_all):
                last_ts_all = s_last

    # streak
    dates = sorted(by_date)
    streak = best_streak = 0
    prev = None
    for d in dates:
        cur = datetime.strptime(d, "%Y-%m-%d").date()
        streak = streak + 1 if prev and (cur - prev).days == 1 else 1
        best_streak = max(best_streak, streak)
        prev = cur

    home = os.path.expanduser("~")
    busiest = by_date.most_common(1)
    data = {
        "range": {"first": first_ts_all, "last": last_ts_all},
        "totals": {
            "sessions": sum(1 for x in sessions if x["prompts"]),
            "prompts": prompts,
            "promptWords": prompt_words,
            "outTokens": out_tokens,
            "inTokens": in_tokens,
            "cacheTokens": cache_tokens,
            "toolCalls": sum(tools.values()),
            "subagents": _count_subagents(),
            "activeDays": len(by_date),
        },
        "phrases": dict(phrase_hits),
        "manners": {"please": please, "thanks": thanks,
                    "swears": sum(swears.values()), "swearTop": swears.most_common(5),
                    "capsRage": caps_rage, "interruptions": interruptions},
        "tics": tics.most_common(8),
        "topWords": words.most_common(14),
        "clock": {"byHour": by_hour, "byWeekday": by_weekday,
                  "nightOwl": night_owl,
                  "busiestDay": {"date": busiest[0][0], "n": busiest[0][1]} if busiest else None,
                  "bestStreak": best_streak},
        "toolbox": {"tools": tools.most_common(12), "bash": bash_total,
                    "bashTop": bash_cmds.most_common(10), "gitCommits": git_commits,
                    "longestBash": longest_bash[:300], "errors": tool_errors,
                    "filesTop": [(p.replace(home, "~"), n) for p, n in edited_files.most_common(8)]},
        "models": models.most_common(6),
        "spend": {
            "total": round(sum(cost_by_model.values()), 2),
            "byModel": [(m, round(c, 2)) for m, c in cost_by_model.most_common(8)
                        if c >= 0.01 and "synthetic" not in m],
            "priciest": sorted(
                ({"title": s["title"], "cost": round(s["cost"], 2)} for s in sessions),
                key=lambda x: x["cost"], reverse=True)[:3],
        },
        "records": {
            "longestPrompt": longest_prompt,
            "marathon": max(sessions, key=lambda s: s["dur"], default=None),
            "chattiest": max(sessions, key=lambda s: s["prompts"], default=None),
        },
    }
    _stats_cache["key"] = key
    _stats_cache["data"] = data
    return data


SAFE_DIR = re.compile(r"^[A-Za-z0-9._-]+$")
SAFE_ID = re.compile(r"^[A-Za-z0-9-]+$")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _json(self, payload, status=200):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/":
            with open(os.path.join(HERE, "index.html"), "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif url.path == "/api/index":
            self._json(build_index())
        elif url.path == "/api/stats":
            self._json(build_stats())
        elif url.path == "/api/session":
            q = parse_qs(url.query)
            d = (q.get("dir") or [""])[0]
            sid = (q.get("id") or [""])[0]
            if not (SAFE_DIR.match(d) and SAFE_ID.match(sid)):
                return self._json({"error": "bad params"}, 400)
            path = os.path.join(PROJECTS_DIR, d, sid + ".jsonl")
            if not os.path.isfile(path):
                return self._json({"error": "not found"}, 404)
            self._json(load_session(path))
        else:
            self._json({"error": "not found"}, 404)


if __name__ == "__main__":
    print(f"claude-archive · http://localhost:{PORT}")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
