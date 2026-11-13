#!/usr/bin/env python3
"""Generate the animated SVG assets and dynamic README blocks of the profile.

Identity text lives in profile.toml; everything else (pinned projects, recent
activity, languages, stars, contributions) is pulled from the GitHub GraphQL API.

Run: python3 scripts/generate_assets.py
Needs GITHUB_TOKEN in the environment, or an authenticated gh CLI.
"""
import json
import os
import re
import subprocess
import textwrap
import tomllib
import urllib.request
from datetime import datetime
from functools import cache
from html import escape
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "assets"
README = ROOT / "README.md"
PROFILE = tomllib.loads((ROOT / "profile.toml").read_text(encoding="utf-8"))
SANS = "'Segoe UI', Ubuntu, 'Helvetica Neue', Arial, sans-serif"
MONO = "'JetBrains Mono', 'Fira Code', 'Cascadia Code', Consolas, 'Courier New', monospace"

VIOLET, CYAN, PINK, GREEN, AMBER = "#a78bfa", "#22d3ee", "#f472b6", "#34d399", "#fbbf24"
PALETTE = [VIOLET, CYAN, GREEN, AMBER, PINK, "#fb7185"]

USER = PROFILE["pseudo"]
RECENT_COUNT = 5
FIVEM_PATTERN = re.compile(r"fivem|gta|lsms|failyv|regie|cfx|citizenfx", re.I)
# Overrides for languages whose GitHub color is unreadable on the dark theme or clashes with the palette.
LANG_COLORS = {
    "TypeScript": "#3178c6", "Python": "#10b981", "Go": "#00ADD8", "JavaScript": "#f7df1e",
    "C#": "#9b4f96", "Lua": "#5b6bff", "Java": "#b07219", "Kotlin": "#A97BFF", "HTML": "#e34c26",
}

REPOS_QUERY = """
fragment card on Repository {
  name description url homepageUrl stargazerCount pushedAt isPrivate
  primaryLanguage { name color }
  repositoryTopics(first: 20) { nodes { topic { name } } }
}
query($login: String!, $after: String) {
  user(login: $login) {
    followers { totalCount }
    contributionsCollection { contributionCalendar { totalContributions } }
    pinnedItems(first: 6, types: REPOSITORY) { nodes { ...card } }
    repositories(first: 100, after: $after, privacy: PUBLIC, ownerAffiliations: OWNER) {
      pageInfo { hasNextPage endCursor }
      nodes { ...card isFork isArchived languages(first: 20) { nodes { name } } }
    }
  }
}"""


def github_token() -> str:
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        return token
    return subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, check=True).stdout.strip()


def topics(repo: dict) -> list[str]:
    return [t["topic"]["name"] for t in repo["repositoryTopics"]["nodes"]]


def lang_color(lang: dict | None) -> str:
    if not lang:
        return "#8b949e"
    return LANG_COLORS.get(lang["name"]) or lang["color"] or "#8b949e"


def short_date(iso: str) -> str:
    d = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return f"{d.day} {d:%b %Y}"


@cache
def github() -> dict:
    """Public profile data for USER, pulled from the GitHub GraphQL API."""
    repos, after = [], None
    while True:
        body = json.dumps({"query": REPOS_QUERY, "variables": {"login": USER, "after": after}}).encode()
        req = urllib.request.Request("https://api.github.com/graphql", data=body, headers={"Authorization": f"Bearer {github_token()}"})
        with urllib.request.urlopen(req) as r:
            payload = json.load(r)
        if "errors" in payload:
            raise RuntimeError(f"GitHub GraphQL error: {payload['errors']}")
        user = payload["data"]["user"]
        page = user["repositories"]
        repos += page["nodes"]
        if not page["pageInfo"]["hasNextPage"]:
            break
        after = page["pageInfo"]["endCursor"]

    own = [r for r in repos if not r["isFork"]]
    counts: dict[str, int] = {}
    colors: dict[str, str] = {}
    for r in own:
        if r["primaryLanguage"]:
            name = r["primaryLanguage"]["name"]
            counts[name] = counts.get(name, 0) + 1
            colors[name] = lang_color(r["primaryLanguage"])
    ranked = sorted(counts.items(), key=lambda kv: -kv[1])
    top = [(name, n, colors[name]) for name, n in ranked[:6]]
    other = sum(n for _, n in ranked[6:])
    if other:
        top.append(("Other", other, "#6e7681"))

    def text(r: dict) -> str:
        return f'{r["name"]} {r["description"] or ""} {" ".join(topics(r))}'

    recent = sorted(
        (r for r in own if not r["isArchived"] and r["name"].lower() != USER.lower()),
        key=lambda r: r["pushedAt"], reverse=True,
    )[:RECENT_COUNT]

    return {
        "total": len(repos),
        "languages": top,
        "language_count": len(counts),
        "typescript": counts.get("TypeScript", 0),
        "docker": sum(any(l["name"] == "Dockerfile" for l in r["languages"]["nodes"]) for r in own),
        "live": sum(bool(r["homepageUrl"]) and "github.com" not in r["homepageUrl"] for r in own),
        "fivem": sum(bool(FIVEM_PATTERN.search(text(r))) for r in own),
        "stars": {r["name"]: r["stargazerCount"] for r in repos},
        "total_stars": sum(r["stargazerCount"] for r in own),
        "followers": user["followers"]["totalCount"],
        "contributions": user["contributionsCollection"]["contributionCalendar"]["totalContributions"],
        "pinned": [r for r in user["pinnedItems"]["nodes"] if r and not r["isPrivate"]],
        "recent": recent,
    }


def write(name: str, content: str) -> None:
    (OUT / name).write_text(content.strip() + "\n", encoding="utf-8")
    print(f"wrote assets/{name}")


def replace_block(readme: str, name: str, content: str) -> str:
    """Swap the text between <!-- name:start --> and <!-- name:end --> markers."""
    start, end = f"<!-- {name}:start -->", f"<!-- {name}:end -->"
    pattern = re.compile(re.escape(start) + r".*?" + re.escape(end), re.S)
    if not pattern.search(readme):
        raise RuntimeError(f"README.md is missing the {start} / {end} markers")
    return pattern.sub(lambda _: f"{start}\n{content.strip()}\n{end}", readme)


def clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip(" ,.;:—-") + "…"


def wrap(text: str, width: int, lines: int) -> list[str]:
    """Word-wrap text into at most `lines` lines, ellipsizing what does not fit."""
    return textwrap.wrap(text, width, max_lines=lines, placeholder=" …")


def homepage(repo: dict) -> str | None:
    url = repo["homepageUrl"]
    return url if url and "github.com" not in url else None


# --------------------------------------------------------------------------- header
def header() -> str:
    roles = PROFILE["roles"]

    # Location, company and status chips, laid out from their text widths.
    def chip_w(text: str, pad: int) -> int:
        return round(pad + len(text) * 8.2 + 18)

    loc, company, status = PROFILE["location"], PROFILE["company"], PROFILE["status"]
    x1 = 80
    w1 = chip_w(loc, 46)
    x2 = x1 + w1 + 12
    w2 = chip_w(company, 44)
    x3 = x2 + w2 + 12
    w3 = chip_w(status, 36)
    slot, cycle = 4.0, 4.0 * len(roles)
    role_svg = []
    for i, role in enumerate(roles):
        tw = len(role) * 16
        w = tw + 8
        s = i * slot
        kt = [0, s, s + 1.3, s + 3.3, s + 3.7, cycle]
        kt = ";".join(f"{t / cycle:.4f}" for t in kt)
        role_svg.append(f"""
    <clipPath id="c{i}"><rect x="80" y="236" height="40" width="0">
      <animate attributeName="width" dur="{cycle}s" repeatCount="indefinite" calcMode="linear"
        keyTimes="{kt}" values="0;0;{w:.0f};{w:.0f};0;0"/></rect></clipPath>
    <g clip-path="url(#c{i})"><text x="82" y="266" class="role" textLength="{tw}" lengthAdjust="spacing">{escape(role)}</text></g>
    <rect y="240" width="3" height="32" fill="{CYAN}" opacity="0">
      <animate attributeName="x" dur="{cycle}s" repeatCount="indefinite"
        keyTimes="{kt}" values="82;82;{82 + w:.0f};{82 + w:.0f};82;82"/>
      <animate attributeName="opacity" dur="{cycle}s" repeatCount="indefinite" calcMode="discrete"
        keyTimes="0;{s / cycle:.4f};{(s + 3.8) / cycle:.4f}" values="0;1;0"/></rect>""")

    stars = "".join(
        f'<circle cx="{x}" cy="{y}" r="{r}" fill="#fff" class="tw" style="animation-delay:{d}s"/>'
        for x, y, r, d in [
            (640, 60, 1.2, 0), (720, 330, 1, 1.2), (560, 350, 1.4, 2.1), (1150, 40, 1, 0.6),
            (1100, 360, 1.3, 1.8), (470, 40, 1, 2.6), (1170, 200, 1.1, 0.3), (610, 200, 0.9, 1.5),
        ]
    )

    orbit_icons = [("TS", "#3178c6"), ("Go", "#00add8"), ("5M", "#f40552"), ("Lua", "#5b6bff")]
    orbit = "".join(
        f"""<g transform="rotate({i * 90})"><g transform="translate(0 -120)">
          <g transform="rotate({-i * 90})"><g class="counter"><circle r="22" fill="#0d1117" stroke="{c}" stroke-width="2"/>
          <text y="6" text-anchor="middle" class="orb" fill="{c}">{t}</text></g></g></g></g>"""
        for i, (t, c) in enumerate(orbit_icons)
    )

    return f"""
<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="400" viewBox="0 0 1200 400" fill="none">
  <style>
    .name {{ font: 800 76px {SANS}; letter-spacing: -2px; }}
    .aka  {{ font: 500 22px {MONO}; fill: #8b949e; }}
    .cmd  {{ font: 500 18px {MONO}; fill: {GREEN}; }}
    .role {{ font: 600 26px {MONO}; fill: #e6edf3; }}
    .chip {{ font: 600 15px {SANS}; fill: #c9d1d9; }}
    .orb  {{ font: 800 17px {SANS}; }}
    .blob {{ animation: drift 14s ease-in-out infinite alternate; transform-box: fill-box; transform-origin: center; }}
    .b2 {{ animation-duration: 18s; animation-delay: -6s; }}
    .b3 {{ animation-duration: 22s; animation-delay: -3s; }}
    @keyframes drift {{ 0% {{ transform: translate(0,0) scale(1); }} 50% {{ transform: translate(60px,-30px) scale(1.15); }} 100% {{ transform: translate(-40px,30px) scale(.9); }} }}
    .tw {{ animation: tw 3s ease-in-out infinite; }}
    @keyframes tw {{ 0%,100% {{ opacity: .15; }} 50% {{ opacity: 1; }} }}
    .spin {{ animation: spin 40s linear infinite; transform-origin: 960px 200px; }}
    .spin-r {{ animation: spin 60s linear infinite reverse; transform-origin: 960px 200px; }}
    .counter {{ animation: spin 40s linear infinite reverse; }}
    @keyframes spin {{ to {{ transform: rotate(360deg); }} }}
    .pulse {{ animation: pulse 3s ease-in-out infinite; transform-origin: 960px 200px; }}
    @keyframes pulse {{ 0%,100% {{ transform: scale(1); opacity: .9; }} 50% {{ transform: scale(1.06); opacity: 1; }} }}
    .fade {{ opacity: 0; animation: in .9s ease-out forwards; }}
    @keyframes in {{ from {{ opacity: 0; transform: translateY(12px); }} to {{ opacity: 1; transform: none; }} }}
  </style>
  <defs>
    <linearGradient id="bg" x1="0" y1="0" x2="1200" y2="400" gradientUnits="userSpaceOnUse">
      <stop stop-color="#0b0f19"/><stop offset="1" stop-color="#111827"/>
    </linearGradient>
    <linearGradient id="txt" x1="0" y1="0" x2="1" y2="0">
      <stop stop-color="{VIOLET}"/><stop offset=".5" stop-color="{CYAN}"/><stop offset="1" stop-color="{PINK}"/>
      <animateTransform attributeName="gradientTransform" type="translate" values="-.5 0;.5 0;-.5 0" dur="8s" repeatCount="indefinite"/>
    </linearGradient>
    <linearGradient id="ring" x1="0" y1="0" x2="1" y2="1">
      <stop stop-color="{VIOLET}"/><stop offset="1" stop-color="{CYAN}" stop-opacity=".2"/>
    </linearGradient>
    <radialGradient id="core"><stop stop-color="{VIOLET}" stop-opacity=".9"/><stop offset="1" stop-color="{VIOLET}" stop-opacity="0"/></radialGradient>
    <filter id="blur" x="-50%" y="-50%" width="200%" height="200%"><feGaussianBlur stdDeviation="60"/></filter>
    <pattern id="grid" width="40" height="40" patternUnits="userSpaceOnUse">
      <path d="M40 0H0V40" stroke="#ffffff" stroke-opacity=".05"/>
    </pattern>
    <radialGradient id="gridfade" cx=".5" cy=".5" r=".7"><stop stop-color="#fff"/><stop offset="1" stop-color="#fff" stop-opacity="0"/></radialGradient>
    <mask id="gm"><rect width="1200" height="400" fill="url(#gridfade)"/></mask>
    <clipPath id="frame"><rect width="1200" height="400" rx="24"/></clipPath>
  </defs>

  <g clip-path="url(#frame)">
    <rect width="1200" height="400" fill="url(#bg)"/>
    <g filter="url(#blur)" opacity=".55">
      <circle class="blob" cx="260" cy="90" r="140" fill="{VIOLET}"/>
      <circle class="blob b2" cx="900" cy="330" r="150" fill="{CYAN}"/>
      <circle class="blob b3" cx="640" cy="40" r="110" fill="{PINK}"/>
    </g>
    <rect width="1200" height="400" fill="url(#grid)" mask="url(#gm)"/>
    {stars}

    <!-- orbit -->
    <circle cx="960" cy="200" r="70" fill="url(#core)" class="pulse"/>
    <g class="spin-r"><circle cx="960" cy="200" r="160" stroke="url(#ring)" stroke-opacity=".35" stroke-dasharray="2 10"/></g>
    <circle cx="960" cy="200" r="120" stroke="url(#ring)" stroke-width="1.5" stroke-opacity=".6"/>
    <g class="spin"><g transform="translate(960 200)">{orbit}</g></g>
    <text x="960" y="214" text-anchor="middle" font-family="{MONO}" font-weight="800" font-size="40" fill="#fff">&lt;/&gt;</text>

    <!-- text -->
    <text x="80" y="92" class="cmd fade">~/{escape(USER.lower())} <tspan fill="#8b949e">$</tspan> <tspan fill="#e6edf3">whoami</tspan></text>
    <text x="76" y="172" class="name fade" style="animation-delay:.2s" fill="url(#txt)">{escape(PROFILE["name"])}</text>
    <text x="82" y="210" class="aka fade" style="animation-delay:.4s">a.k.a. <tspan fill="{VIOLET}" font-weight="700">{escape(USER)}</tspan></text>
    {"".join(role_svg)}

    <g class="fade" style="animation-delay:.8s">
      <rect x="{x1}" y="310" width="{w1}" height="38" rx="19" fill="#ffffff" fill-opacity=".06" stroke="#ffffff" stroke-opacity=".12"/>
      <path d="M{x1 + 24} 321a7 7 0 0 1 14 0c0 5-7 13-7 13s-7-8-7-13z" fill="{PINK}"/><circle cx="{x1 + 31}" cy="321" r="2.5" fill="#0b0f19"/>
      <text x="{x1 + 46}" y="335" class="chip">{escape(loc)}</text>
      <rect x="{x2}" y="310" width="{w2}" height="38" rx="19" fill="#ffffff" fill-opacity=".06" stroke="#ffffff" stroke-opacity=".12"/>
      <rect x="{x2 + 20}" y="321" width="16" height="12" rx="2" fill="#ff7900"/><rect x="{x2 + 24}" y="317" width="8" height="5" rx="1.5" stroke="#ff7900" stroke-width="2"/>
      <text x="{x2 + 44}" y="335" class="chip">{escape(company)}</text>
      <rect x="{x3}" y="310" width="{w3}" height="38" rx="19" fill="{GREEN}" fill-opacity=".1" stroke="{GREEN}" stroke-opacity=".4"/>
      <circle cx="{x3 + 22}" cy="329" r="5" fill="{GREEN}"><animate attributeName="opacity" values="1;.2;1" dur="1.6s" repeatCount="indefinite"/></circle>
      <text x="{x3 + 36}" y="335" class="chip" fill="{GREEN}">{escape(status)}</text>
    </g>
  </g>
  <rect x=".5" y=".5" width="1199" height="399" rx="23.5" stroke="#ffffff" stroke-opacity=".08"/>
</svg>"""


# --------------------------------------------------------------------------- about (editor)
def about() -> str:
    K, S, P, C, N, D = "#ff7b72", "#a5d6ff", "#79c0ff", "#8b949e", "#d2a8ff", "#e6edf3"
    var = PROFILE["name"].split()[0].lower()

    def field(key: str, value: str | list[str]) -> list:
        if isinstance(value, str):
            return [(P, f"  {key}"), (D, ": "), (S, json.dumps(value, ensure_ascii=False)), (D, ",")]
        items = [part for i, v in enumerate(value) for part in ([(D, ", ")] if i else []) + [(S, json.dumps(v, ensure_ascii=False))]]
        return [(P, f"  {key}"), (D, ": [")] + items + [(D, "],")]

    lines = [
        [(K, "const "), (N, var), (D, " = {")],
        field("pseudo", USER),
        *(field(k, v) for k, v in PROFILE["about"].items()),
        [(P, "  repos"), (D, ": "), (AMBER, str(github()["total"])), (D, ",  "), (C, "// public — plus private ones")],
        [(D, "};")],
        [],
        [(N, var), (D, "."), (P, "ship"), (D, "("), (S, '"something cool"'), (D, ");  "), (C, "// ✔ done in 42ms")],
    ]
    rows = []
    for i, parts in enumerate(lines):
        y = 104 + i * 30
        spans = "".join(f'<tspan fill="{c}">{escape(t)}</tspan>' for c, t in parts)
        rows.append(
            f'<g class="ln" style="animation-delay:{0.25 + i * 0.18:.2f}s">'
            f'<text x="36" y="{y}" class="num">{i + 1:>2}</text>'
            f'<text x="84" y="{y}" class="code" xml:space="preserve">{spans}</text></g>'
        )
    h = 104 + len(lines) * 30 + 10
    return f"""
<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="{h}" viewBox="0 0 1200 {h}" fill="none">
  <style>
    .code {{ font: 500 19px {MONO}; }}
    .num  {{ font: 500 17px {MONO}; fill: #484f58; }}
    .tab  {{ font: 500 15px {SANS}; fill: #c9d1d9; }}
    .ln {{ opacity: 0; animation: in .5s ease-out forwards; }}
    @keyframes in {{ from {{ opacity: 0; transform: translateX(-8px); }} to {{ opacity: 1; transform: none; }} }}
    .cur {{ animation: blink 1s steps(1) infinite; }}
    @keyframes blink {{ 50% {{ opacity: 0; }} }}
  </style>
  <defs>
    <linearGradient id="edge" x1="0" y1="0" x2="1200" y2="0" gradientUnits="userSpaceOnUse">
      <stop stop-color="{VIOLET}"/><stop offset=".5" stop-color="{CYAN}"/><stop offset="1" stop-color="{PINK}"/>
    </linearGradient>
  </defs>
  <rect x="1" y="1" width="1198" height="{h - 2}" rx="18" fill="#0d1117" stroke="#30363d"/>
  <path d="M1 19a18 18 0 0 1 18-18h1162a18 18 0 0 1 18 18v33H1z" fill="#161b22"/>
  <rect x="1" y="1" width="1198" height="3" rx="1.5" fill="url(#edge)"/>
  <circle cx="30" cy="27" r="7" fill="#ff5f57"/><circle cx="54" cy="27" r="7" fill="#febc2e"/><circle cx="78" cy="27" r="7" fill="#28c840"/>
  <rect x="110" y="12" width="150" height="41" rx="8" fill="#0d1117"/>
  <text x="130" y="38" class="tab"><tspan fill="#3178c6" font-weight="800">TS</tspan>  about.ts</text>
  <text x="1170" y="38" text-anchor="end" class="tab" fill="#8b949e">~/Wiibleyde</text>
  {"".join(rows)}
</svg>"""


# --------------------------------------------------------------------------- languages bar
def languages() -> str:
    gh = github()
    data = gh["languages"]
    total = sum(n for _, n, _ in data)
    x0, width = 40, 1120
    segs, legend, x = [], [], x0
    for i, (name, n, c) in enumerate(data):
        w = width * n / total
        segs.append(
            f'<rect x="{x:.1f}" y="118" width="{max(w - 3, 1):.1f}" height="18" rx="4" fill="{c}" '
            f'class="seg" style="animation-delay:{0.1 + i * 0.12:.2f}s;transform-origin:{x:.1f}px 127px"/>'
        )
        x += w
        lx, ly = 40 + (i % 4) * 285, 180 + (i // 4) * 34
        legend.append(
            f'<g class="lg" style="animation-delay:{0.6 + i * 0.08:.2f}s"><circle cx="{lx + 7}" cy="{ly - 6}" r="7" fill="{c}"/>'
            f'<text x="{lx + 24}" y="{ly}" class="lname">{escape(name)}</text>'
            f'<text x="{lx + 150}" y="{ly}" class="lpct">{n / total * 100:.1f}%</text></g>'
        )
    stats = [
        (gh["total"], "public repositories", VIOLET), (gh["typescript"], "TypeScript projects", CYAN),
        (gh["docker"], "Dockerized repos", PINK), (gh["live"], "live deployments", GREEN),
        (gh["contributions"], "contributions, last 12 months", AMBER), (gh["total_stars"], "stars earned", VIOLET),
        (gh["followers"], "followers", CYAN), (gh["language_count"], "languages shipped", PINK),
    ]
    stat_svg = "".join(
        f'<g class="lg" style="animation-delay:{0.9 + i * 0.1:.2f}s"><text x="{40 + (i % 4) * 285}" y="{296 + (i // 4) * 84}" class="big" fill="{c}">{v:,}</text>'
        f'<text x="{40 + (i % 4) * 285}" y="{322 + (i // 4) * 84}" class="small">{t}</text></g>'
        for i, (v, t, c) in enumerate(stats)
    )
    h = 350 + 84 * ((len(stats) - 1) // 4)
    return f"""
<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="{h}" viewBox="0 0 1200 {h}" fill="none">
  <style>
    .title {{ font: 700 24px {SANS}; fill: #e6edf3; }}
    .sub   {{ font: 400 15px {SANS}; fill: #8b949e; }}
    .lname {{ font: 600 16px {SANS}; fill: #c9d1d9; }}
    .lpct  {{ font: 500 15px {MONO}; fill: #8b949e; }}
    .big   {{ font: 800 40px {SANS}; }}
    .small {{ font: 500 15px {SANS}; fill: #8b949e; }}
    .seg {{ transform: scaleX(0); animation: grow .8s cubic-bezier(.2,.8,.2,1) forwards; }}
    @keyframes grow {{ to {{ transform: scaleX(1); }} }}
    .lg {{ opacity: 0; animation: in .6s ease-out forwards; }}
    @keyframes in {{ from {{ opacity: 0; transform: translateY(6px); }} to {{ opacity: 1; transform: none; }} }}
  </style>
  <rect x="1" y="1" width="1198" height="{h - 2}" rx="18" fill="#0d1117" stroke="#30363d"/>
  <text x="40" y="60" class="title">What I build with</text>
  <text x="40" y="88" class="sub">Primary language across my public repositories — pulled from the GitHub API</text>
  <rect x="40" y="118" width="1120" height="18" rx="4" fill="#161b22"/>
  {"".join(segs)}
  {"".join(legend)}
  <path d="M40 246H1160" stroke="#21262d"/>
  {stat_svg}
</svg>"""


# --------------------------------------------------------------------------- project cards (pinned repos)
# Chip colors for languages/topics; topics are matched on their slug (lowercase, alphanumerics only).
TECH = {
    "Go": "#00ADD8", "TypeScript": "#3178c6", "JavaScript": "#f7df1e", "Python": "#10b981", "Rust": "#dea584",
    "Lua": "#5b6bff", "C#": "#9b4f96", "Next.js": "#e6edf3", "React": "#61dafb", "Tailwind": "#38bdf8",
    "Supabase": "#3ecf8e", "Prisma": "#5a67d8", "PostgreSQL": "#336791", "Docker": "#2496ed", "Bun": "#fbf0df",
    "Node.js": "#5fa04e", "Vercel": "#e6edf3", "Gemini": "#8e75ff", "Fiber": "#00acd7", "Lavalink": "#f472b6",
    "Discord": "#5865f2", "disgo": "#5865f2", "VS Code": "#23a9f2", "Three.js": "#e6edf3", "GSAP": "#88ce02",
    "Socket.io": "#e6edf3", "FiveM": "#f40552", "OBS": "#e6edf3", "Twitch": "#9146ff", "FFmpeg": "#5cb85c",
    "NDI": "#e6edf3", "WebRTC": "#e6edf3", "egui": "#e6edf3",
}
TOPIC_ALIASES = {"vscode": "VS Code", "tailwindcss": "Tailwind", "postgres": "PostgreSQL", "golang": "Go", "nodejs": "Node.js"}


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


TECH_BY_SLUG = {slug(label): label for label in TECH} | TOPIC_ALIASES


def glyph(name: str) -> str:
    words = re.findall(r"[A-Z]?[a-z0-9]+|[A-Z]+(?![a-z])", name)
    return "".join(w[0] for w in words[:2]).upper() if len(words) > 1 else name[:2].capitalize()


def chips_for(repo: dict) -> list[str]:
    """Primary language first, then topics that are known techs, then the remaining topics."""
    seen = {slug(repo["name"]), slug(USER)}
    known, other = [], []
    lang = repo["primaryLanguage"]["name"] if repo["primaryLanguage"] else None
    for t in ([lang] if lang else []) + topics(repo):
        s = slug(t)
        label = TECH_BY_SLUG.get(s)
        if s in seen or (label and slug(label) in seen):
            continue
        seen |= {s, slug(label or t)}
        (known if label else other).append(label or t)
    return known + other


CARD_W, CARD_H = 590, 250


def card(repo: dict, accent: str) -> str:
    title = repo["name"]
    chips, x = [], 32
    for t in chips_for(repo):
        w = len(t) * 8.2 + 34
        if x + w > CARD_W - 32:
            break
        c = TECH.get(t, "#8b949e")
        chips.append(
            f'<rect x="{x:.0f}" y="196" width="{w:.0f}" height="30" rx="15" fill="{c}" fill-opacity=".12" stroke="{c}" stroke-opacity=".45"/>'
            f'<circle cx="{x + 15:.0f}" cy="211" r="4.5" fill="{c}"/>'
            f'<text x="{x + 26:.0f}" y="216" class="chip">{escape(t)}</text>'
        )
        x += w + 10
    desc = wrap(repo["description"] or "", 68, 3)
    desc_svg = "".join(f'<text x="32" y="{122 + i * 26}" class="desc">{escape(l)}</text>' for i, l in enumerate(desc))
    meta = f'★ {repo["stargazerCount"]} · pushed {short_date(repo["pushedAt"])}'

    # Homepage domain as a badge, shortened so it never runs into the title.
    badge_svg = ""
    if url := homepage(repo):
        room = int((CARD_W - 32 - (96 + len(title) * 17) - 16 - 26) / 8) - 2
        host = urlparse(url).netloc.removeprefix("www.")
        host = next((h for h in (host, ".".join(host.split(".")[-2:])) if len(h) <= room), clip(host, room))
        if room >= 6:
            badge = "↗ " + host
            bw = len(badge) * 8 + 26
            badge_svg = (f'<rect x="{CARD_W - 32 - bw}" y="36" width="{bw}" height="28" rx="14" fill="{accent}" fill-opacity=".12"/>'
                         f'<text x="{CARD_W - 32 - bw / 2}" y="55" text-anchor="middle" class="bdg" fill="{accent}">{escape(badge)}</text>')
    return f"""
<svg xmlns="http://www.w3.org/2000/svg" width="{CARD_W}" height="{CARD_H}" viewBox="0 0 {CARD_W} {CARD_H}" fill="none">
  <style>
    .t    {{ font: 800 28px {SANS}; fill: #f0f6fc; }}
    .meta {{ font: 500 13px {MONO}; fill: #6e7681; }}
    .desc {{ font: 400 16.5px {SANS}; fill: #9da7b3; }}
    .chip {{ font: 600 13px {SANS}; fill: #c9d1d9; }}
    .bdg  {{ font: 700 13px {MONO}; }}
    .glow {{ animation: g 6s ease-in-out infinite; transform-box: fill-box; transform-origin: center; }}
    @keyframes g {{ 0%,100% {{ opacity: .35; transform: scale(1); }} 50% {{ opacity: .6; transform: scale(1.2); }} }}
    .sh {{ animation: sh 5s ease-in-out infinite; }}
    @keyframes sh {{ 0% {{ transform: translateX(-700px); }} 60%,100% {{ transform: translateX(700px); }} }}
  </style>
  <defs>
    <radialGradient id="rg"><stop stop-color="{accent}"/><stop offset="1" stop-color="{accent}" stop-opacity="0"/></radialGradient>
    <linearGradient id="bd" x1="0" y1="0" x2="{CARD_W}" y2="{CARD_H}" gradientUnits="userSpaceOnUse">
      <stop stop-color="{accent}" stop-opacity=".8"/><stop offset=".45" stop-color="#30363d"/><stop offset="1" stop-color="#30363d"/>
    </linearGradient>
    <linearGradient id="shine" x1="0" y1="0" x2="1" y2="0">
      <stop stop-color="#fff" stop-opacity="0"/><stop offset=".5" stop-color="#fff" stop-opacity=".06"/><stop offset="1" stop-color="#fff" stop-opacity="0"/>
    </linearGradient>
    <clipPath id="cl"><rect x="1" y="1" width="{CARD_W - 2}" height="{CARD_H - 2}" rx="18"/></clipPath>
  </defs>
  <g clip-path="url(#cl)">
    <rect width="{CARD_W}" height="{CARD_H}" fill="#0d1117"/>
    <circle cx="560" cy="10" r="170" fill="url(#rg)" class="glow"/>
    <rect class="sh" x="0" y="0" width="220" height="{CARD_H}" fill="url(#shine)" transform="skewX(-20)"/>
  </g>
  <rect x="1" y="1" width="{CARD_W - 2}" height="{CARD_H - 2}" rx="18" stroke="url(#bd)" stroke-width="1.5"/>
  <rect x="32" y="32" width="48" height="48" rx="12" fill="{accent}" fill-opacity=".14" stroke="{accent}" stroke-opacity=".5"/>
  <text x="56" y="65" text-anchor="middle" font-family="{MONO}" font-weight="800" font-size="22" fill="{accent}">{escape(glyph(title))}</text>
  <text x="96" y="60" class="t">{escape(title)}</text>
  <text x="97" y="82" class="meta">{escape(meta)}</text>
  {badge_svg}
  {desc_svg}
  {"".join(chips)}
</svg>"""


def featured_block(pinned: list[dict]) -> str:
    links = "\n".join(
        f'  <a href="{escape(homepage(r) or r["url"])}"><img src="./assets/project-{r["name"].lower()}.svg" width="49%" '
        f'alt="{escape(r["name"] + (" — " + r["description"] if r["description"] else ""))}" /></a>'
        for r in pinned
    )
    return f'<p align="center">\n{links}\n</p>'


# --------------------------------------------------------------------------- recent activity rows
RECENT_W, RECENT_H = 1200, 84


def recent_row(repo: dict, i: int) -> str:
    c = lang_color(repo["primaryLanguage"])
    lang = repo["primaryLanguage"]["name"] if repo["primaryLanguage"] else "—"
    desc = (wrap(repo["description"] or "", 100, 1) or ["No description yet."])[0]
    return f"""
<svg xmlns="http://www.w3.org/2000/svg" width="{RECENT_W}" height="{RECENT_H}" viewBox="0 0 {RECENT_W} {RECENT_H}" fill="none">
  <style>
    .n  {{ font: 700 20px {SANS}; fill: #f0f6fc; }}
    .d  {{ font: 400 15px {SANS}; fill: #8b949e; }}
    .m  {{ font: 500 14px {MONO}; fill: #8b949e; }}
    .in {{ opacity: 0; animation: in .6s ease-out forwards; animation-delay: {0.1 + i * 0.12:.2f}s; }}
    @keyframes in {{ from {{ opacity: 0; transform: translateX(-8px); }} to {{ opacity: 1; transform: none; }} }}
    .live {{ animation: blink 1.6s ease-in-out infinite; }}
    @keyframes blink {{ 50% {{ opacity: .2; }} }}
  </style>
  <g class="in">
    <rect x="1" y="1" width="{RECENT_W - 2}" height="{RECENT_H - 10}" rx="14" fill="#0d1117" stroke="#30363d"/>
    <rect x="1" y="18" width="3" height="{RECENT_H - 46}" rx="1.5" fill="{c}"/>
    <circle cx="36" cy="31" r="6" fill="{c}"{' class="live"' if i == 0 else ""}/>
    <text x="54" y="38" class="n">{escape(repo["name"])}</text>
    <text x="54" y="60" class="d">{escape(desc)}</text>
    <text x="{RECENT_W - 32}" y="38" text-anchor="end" class="m">pushed {short_date(repo["pushedAt"])}</text>
    <text x="{RECENT_W - 32}" y="60" text-anchor="end" class="m">{escape(lang)} · ★ {repo["stargazerCount"]}</text>
  </g>
</svg>"""


def recent_block(recent: list[dict]) -> str:
    return "\n".join(
        f'<a href="{escape(r["url"])}"><img src="./assets/recent-{i + 1}.svg" width="100%" '
        f'alt="{escape(r["name"] + (" — " + r["description"] if r["description"] else ""))}" /></a>'
        for i, r in enumerate(recent)
    )


# --------------------------------------------------------------------------- divider / footer
def divider(label: str) -> str:
    return f"""
<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="70" viewBox="0 0 1200 70" fill="none">
  <style>
    .l {{ font: 800 26px {SANS}; letter-spacing: 1px; }}
    .d {{ stroke-dasharray: 520; stroke-dashoffset: 520; animation: draw 1.4s ease-out forwards; }}
    @keyframes draw {{ to {{ stroke-dashoffset: 0; }} }}
  </style>
  <defs>
    <linearGradient id="gl" x1="0" y1="0" x2="1" y2="0"><stop stop-color="{VIOLET}" stop-opacity="0"/><stop offset="1" stop-color="{VIOLET}"/></linearGradient>
    <linearGradient id="gr" x1="0" y1="0" x2="1" y2="0"><stop stop-color="{PINK}"/><stop offset="1" stop-color="{PINK}" stop-opacity="0"/></linearGradient>
    <linearGradient id="gt" x1="0" y1="0" x2="1" y2="0"><stop stop-color="{VIOLET}"/><stop offset=".5" stop-color="{CYAN}"/><stop offset="1" stop-color="{PINK}"/></linearGradient>
  </defs>
  <path d="M40 36H{600 - len(label) * 9 - 30}" stroke="url(#gl)" stroke-width="2" class="d"/>
  <path d="M1160 36H{600 + len(label) * 9 + 30}" stroke="url(#gr)" stroke-width="2" class="d"/>
  <text x="600" y="45" text-anchor="middle" class="l" fill="url(#gt)">{escape(label)}</text>
</svg>"""


def footer() -> str:
    waves = "".join(
        f"""<path fill="{c}" fill-opacity="{o}" d="M0 60 C 200 {20 + i * 10}, 400 {100 - i * 10}, 600 60 S 1000 {20 + i * 10}, 1200 60 V160 H0Z">
      <animate attributeName="d" dur="{7 + i * 2}s" repeatCount="indefinite" values="
        M0 60 C 200 {20 + i * 10}, 400 {100 - i * 10}, 600 60 S 1000 {20 + i * 10}, 1200 60 V160 H0Z;
        M0 60 C 200 {100 - i * 10}, 400 {20 + i * 10}, 600 60 S 1000 {100 - i * 10}, 1200 60 V160 H0Z;
        M0 60 C 200 {20 + i * 10}, 400 {100 - i * 10}, 600 60 S 1000 {20 + i * 10}, 1200 60 V160 H0Z"/></path>"""
        for i, (c, o) in enumerate([(VIOLET, ".35"), (CYAN, ".35"), (PINK, ".5")])
    )
    return f"""
<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="160" viewBox="0 0 1200 160" fill="none">
  <defs><clipPath id="c"><rect width="1200" height="160" rx="24"/></clipPath></defs>
  <g clip-path="url(#c)"><rect width="1200" height="160" fill="#0b0f19"/>{waves}
    <text x="600" y="128" text-anchor="middle" font-family="{MONO}" font-size="17" font-weight="600" fill="#ffffff">Thanks for stopping by — see you in the commits ✦</text>
  </g>
</svg>"""


# --------------------------------------------------------------------------- skillicons-style rows
@cache
def fetch(url: str) -> str:
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})) as r:
        return r.read().decode()


def brand(slug: str, color: str, bg: str = "#242938", extra: str = "", scale: float = 6.2) -> str:
    """A 256x256 tile in skillicons style wrapping a simple-icons glyph."""
    path = re.search(r' d="([^"]+)"', fetch(f"https://cdn.jsdelivr.net/npm/simple-icons@latest/icons/{slug}.svg"))[1]
    off = 128 - 12 * scale
    return (f'<rect width="256" height="256" rx="60" fill="{bg}"/>'
            f'<path transform="translate({off} {off}) scale({scale})" fill="{color}" d="{path}"/>{extra}')


def skillicon(slug: str) -> str:
    return re.search(r"<g[^>]*>\s*(<svg.*</svg>)\s*</g>", fetch(f"https://skillicons.dev/icons?i={slug}"), re.S)[1]


def claude_code() -> str:
    path = re.search(r' d="([^"]+)"', fetch("https://cdn.jsdelivr.net/npm/simple-icons@latest/icons/claude.svg"))[1]
    return (f'<rect width="256" height="256" rx="60" fill="#242938"/>'
            f'<path transform="translate(68 40) scale(5)" fill="#D97757" d="{path}"/>'
            f'<text x="128" y="212" text-anchor="middle" font-family="{MONO}" font-weight="800" font-size="46" fill="#e6edf3">&gt;_</text>')


def skills_tile() -> str:
    return ('<rect width="256" height="256" rx="60" fill="#242938"/>'
            f'<path d="M78 52h72l36 36v116a12 12 0 0 1-12 12H78a12 12 0 0 1-12-12V64a12 12 0 0 1 12-12z" fill="{VIOLET}" fill-opacity=".18" stroke="{VIOLET}" stroke-width="10" stroke-linejoin="round"/>'
            f'<path d="M150 52v36h36" stroke="{VIOLET}" stroke-width="10" stroke-linejoin="round"/>'
            f'<text x="126" y="178" text-anchor="middle" font-family="{MONO}" font-weight="800" font-size="40" fill="#e6edf3">.md</text>'
            f'<path d="M190 150l8 18 18 8-18 8-8 18-8-18-18-8 18-8z" fill="{AMBER}"/>')


AI_ICONS = [
    lambda: brand("claude", "#D97757"),
    claude_code,
    lambda: brand("githubcopilot", "#ffffff"),
    lambda: brand("modelcontextprotocol", "#ffffff"),
    lambda: brand("ollama", "#ffffff"),
    lambda: brand("opencode", "#ffffff", scale=5),
    skills_tile,
]

FIVEM_ICONS = [
    lambda: brand("fivem", "#ffffff", bg="#F40552"),
    lambda: skillicon("lua"),
    lambda: skillicon("ts"),
    lambda: skillicon("js"),
    lambda: skillicon("cs"),
    lambda: skillicon("react"),
]


def icon_row(icons: list) -> str:
    n = len(icons)
    vw = 300 * n - 44
    tiles = "".join(f'<g transform="translate({i * 300} 0)">{_tile(make())}</g>' for i, make in enumerate(icons))
    return f'<svg xmlns="http://www.w3.org/2000/svg" width="{vw * 48 / 256:.2f}" height="48" viewBox="0 0 {vw} 256" fill="none">{tiles}</svg>'


def _tile(inner: str) -> str:
    return inner if inner.startswith("<svg") else f'<svg width="256" height="256" viewBox="0 0 256 256" fill="none">{inner}</svg>'


# --------------------------------------------------------------------------- FiveM showcase
def fivem() -> str:
    RED = "#f40552"
    logo = re.search(r' d="([^"]+)"', fetch("https://cdn.jsdelivr.net/npm/simple-icons@latest/icons/fivem.svg"))[1]
    cfg = PROFILE["fivem"]
    pills = [(str(github()["fivem"]), "public FiveM & RP repos, more private"), *cfg["pills"]]
    pill_svg = "".join(
        f'<g class="in" style="animation-delay:{0.2 + i * 0.1:.1f}s"><text x="{48 + i * 280}" y="196" class="pv">{escape(v)}</text>'
        f'<text x="{48 + i * 280}" y="222" class="pl">{escape(l)}</text></g>'
        for i, (v, l) in enumerate(pills)
    )
    server = cfg["server"]
    server_svg = f"""
  <g class="in" style="animation-delay:.45s">
    <rect x="40" y="250" width="1120" height="74" rx="14" fill="{RED}" fill-opacity=".08" stroke="{RED}" stroke-opacity=".35"/>
    <circle cx="68" cy="287" r="6" fill="{RED}"><animate attributeName="opacity" values="1;.2;1" dur="1.6s" repeatCount="indefinite"/></circle>
    <text x="88" y="279" class="tt">{escape(server["name"])}<tspan class="ts" dx="12">{escape(server["role"].upper())}</tspan></text>
    <text x="88" y="306" class="td">{escape(server["description"])}</text>
    <text x="1136" y="293" text-anchor="end" class="ts">{escape(urlparse(server["url"]).netloc)} ↗</text>
  </g>"""
    tiles = [(t["title"], t["subtitle"], t["description"], t["tech"], t.get("private", False)) for t in cfg["tiles"]]
    top = 346
    tile_svg = []
    for i, (t, sub, desc, tech, private) in enumerate(tiles):
        x, y = 40 + (i % 2) * 570, top + (i // 2) * 168
        tile_svg.append(
            f'<g class="in" style="animation-delay:{0.5 + i * 0.12:.2f}s">'
            f'<rect x="{x}" y="{y}" width="550" height="150" rx="14" fill="#ffffff" fill-opacity=".03" stroke="#ffffff" stroke-opacity=".08"/>'
            f'<rect x="{x}" y="{y + 22}" width="3" height="40" rx="1.5" fill="{RED}"/>'
            f'<text x="{x + 24}" y="{y + 40}" class="tt">{escape(t)}</text>'
            f'<text x="{x + 24}" y="{y + 62}" class="ts">{escape(sub)}</text>'
            + "".join(f'<text x="{x + 24}" y="{y + 94 + j * 22}" class="td">{escape(l)}</text>' for j, l in enumerate(desc))
            + f'<text x="{x + 526}" y="{y + 40}" text-anchor="end" class="tk">{escape(tech.format_map(github()["stars"]))}</text>'
            + (f'<rect x="{x + 440}" y="{y + 108}" width="86" height="24" rx="12" fill="#ffffff" fill-opacity=".06" stroke="#ffffff" stroke-opacity=".18"/>'
               f'<rect x="{x + 452}" y="{y + 120}" width="10" height="8" rx="1.5" fill="#8b949e"/>'
               f'<path d="M{x + 454} {y + 120}v-2.5a3 3 0 0 1 6 0v2.5" stroke="#8b949e" stroke-width="1.6"/>'
               f'<text x="{x + 468}" y="{y + 125}" class="tk" fill="#8b949e">private</text>' if private else "")
            + "</g>"
        )
    h = top + (len(tiles) + 1) // 2 * 168 + 22
    return f"""
<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="{h}" viewBox="0 0 1200 {h}" fill="none">
  <style>
    .h  {{ font: 800 40px {SANS}; fill: #f0f6fc; }}
    .s  {{ font: 400 17px {SANS}; fill: #9da7b3; }}
    .pv {{ font: 800 26px {SANS}; fill: {RED}; }}
    .pl {{ font: 500 14px {SANS}; fill: #8b949e; }}
    .tt {{ font: 800 21px {SANS}; fill: #f0f6fc; }}
    .ts {{ font: 600 13px {MONO}; fill: {RED}; letter-spacing: .5px; }}
    .td {{ font: 400 15.5px {SANS}; fill: #9da7b3; }}
    .tk {{ font: 600 12.5px {MONO}; fill: #6e7681; }}
    .in {{ opacity: 0; animation: in .6s ease-out forwards; }}
    @keyframes in {{ from {{ opacity: 0; transform: translateY(8px); }} to {{ opacity: 1; transform: none; }} }}
    .glow {{ animation: g 7s ease-in-out infinite; transform-box: fill-box; transform-origin: center; }}
    @keyframes g {{ 0%,100% {{ opacity: .35; transform: scale(1); }} 50% {{ opacity: .6; transform: scale(1.15); }} }}
    .scan {{ animation: scan 6s linear infinite; }}
    @keyframes scan {{ from {{ transform: translateY(-40px); }} to {{ transform: translateY({h + 40}px); }} }}
  </style>
  <defs>
    <radialGradient id="rg"><stop stop-color="{RED}"/><stop offset="1" stop-color="{RED}" stop-opacity="0"/></radialGradient>
    <linearGradient id="bd" x1="0" y1="0" x2="1200" y2="{h}" gradientUnits="userSpaceOnUse">
      <stop stop-color="{RED}" stop-opacity=".9"/><stop offset=".4" stop-color="#30363d"/><stop offset="1" stop-color="#30363d"/>
    </linearGradient>
    <linearGradient id="sc" x1="0" y1="0" x2="0" y2="1"><stop stop-color="{RED}" stop-opacity="0"/><stop offset="1" stop-color="{RED}" stop-opacity=".08"/></linearGradient>
    <clipPath id="cl"><rect x="1" y="1" width="1198" height="{h - 2}" rx="20"/></clipPath>
  </defs>
  <g clip-path="url(#cl)">
    <rect width="1200" height="{h}" fill="#0d1117"/>
    <circle cx="1100" cy="40" r="260" fill="url(#rg)" class="glow"/>
    <rect class="scan" x="0" y="0" width="1200" height="40" fill="url(#sc)"/>
    <path transform="translate(960 -30) scale(11)" fill="{RED}" fill-opacity=".07" d="{logo}"/>
  </g>
  <rect x="1" y="1" width="1198" height="{h - 2}" rx="20" stroke="url(#bd)" stroke-width="1.5"/>
  <rect x="40" y="40" width="64" height="64" rx="16" fill="{RED}"/>
  <path transform="translate(52 52) scale(1.667)" fill="#fff" d="{logo}"/>
  <text x="126" y="82" class="h">{escape(cfg["headline"])}</text>
  <text x="128" y="112" class="s">{escape(cfg["tagline"])}</text>
  <path d="M40 152H1160" stroke="#21262d"/>
  {pill_svg}
  {server_svg}
  {"".join(tile_svg)}
</svg>"""


def fivem_block() -> str:
    cfg = PROFILE["fivem"]
    server = cfg["server"]
    alt = f'{cfg["headline"]} — {server["role"]} on {server["name"]}, ' + ", ".join(t["title"] for t in cfg["tiles"])
    links = [f'  <a href="{escape(server["url"])}"><b>🎮 {escape(server["name"])}</b></a>']
    for link in cfg["links"]:
        label = f'<b>{escape(link["label"])}</b>'
        links.append(f'  <a href="{escape(link["url"])}">{label}</a>' if "url" in link else f"  {label} <sub>🔒 private</sub>")
    return (f'<a href="https://github.com/{USER}?tab=repositories&q=fivem"><img src="./assets/fivem.svg" width="100%" alt="{escape(alt)}" /></a>\n\n'
            f'<p align="center">\n' + " ·\n".join(links) + "\n</p>")


def sync_generated(prefix: str, names: set[str]) -> None:
    """Delete generated `prefix-*.svg` files that are no longer referenced (e.g. an unpinned repo)."""
    for path in OUT.glob(f"{prefix}-*.svg"):
        if path.name not in names:
            path.unlink()
            print(f"removed assets/{path.name}")


if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    gh = github()
    write("header.svg", header())
    write("about.svg", about())
    write("languages.svg", languages())

    cards = {f"project-{r['name'].lower()}.svg": card(r, PALETTE[i % len(PALETTE)]) for i, r in enumerate(gh["pinned"])}
    rows = {f"recent-{i + 1}.svg": recent_row(r, i) for i, r in enumerate(gh["recent"])}
    for name, svg in {**cards, **rows}.items():
        write(name, svg)
    sync_generated("project", set(cards))
    sync_generated("recent", set(rows))

    for key, label in [("about", "ABOUT ME"), ("stack", "TECH STACK"), ("fivem", "FIVEM DEVELOPMENT"), ("projects", "FEATURED PROJECTS"), ("stats", "BY THE NUMBERS"), ("activity", "ACTIVITY")]:
        write(f"title-{key}.svg", divider(label))
    write("footer.svg", footer())
    write("stack-ai.svg", icon_row(AI_ICONS))
    write("stack-fivem.svg", icon_row(FIVEM_ICONS))
    write("fivem.svg", fivem())

    readme = README.read_text(encoding="utf-8")
    readme = replace_block(readme, "fivem", fivem_block())
    readme = replace_block(readme, "featured", featured_block(gh["pinned"]))
    readme = replace_block(readme, "recent", recent_block(gh["recent"]))
    README.write_text(readme, encoding="utf-8")
    print("wrote README.md")
