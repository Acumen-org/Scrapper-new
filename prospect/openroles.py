"""Open roles: what a firm says it is hiring for, on its own site and on job boards.

For each firm with a website (scripts/find_open_roles.py, the "open_roles"
job): read the home page, find the careers page (a link such as Careers or
Join our team, else the usual addresses), follow it to the applicant-tracking
board it embeds or links (Greenhouse, Lever, BambooHR, Paylocity and the
rest), and read the job titles there. Boards with a public feed (Greenhouse,
Lever, BambooHR, Workable, Ashby, Breezy, Recruitee) are read from the feed,
which lists every opening exactly. A page that says it has no openings is
recorded as such. For firms whose site lists nothing, a web search (the
"job_boards" job) looks for the firm's postings on the job boards, and keeps
only results that name the firm.

Each title is sorted into a function (advisory, operations and client service,
compliance, finance, marketing, technology, people, leadership) so the Growth
tab can say where the firm is hiring. A posting seen in the latest check is
active; one that has gone is kept as history.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urljoin, urlparse

SCHEMA = """
CREATE TABLE IF NOT EXISTS firm_careers (
    crd           TEXT PRIMARY KEY,
    checked_at    TEXT NOT NULL,
    status        TEXT NOT NULL,    -- roles | no_openings | page | no_page | unreachable | no_website
    careers_url   TEXT,
    ats           TEXT,             -- the applicant-tracking system, when one is used
    roles         INTEGER NOT NULL DEFAULT 0,
    searched_at   TEXT              -- last job-board search
);
CREATE TABLE IF NOT EXISTS firm_job_posting (
    crd         TEXT NOT NULL,
    title       TEXT NOT NULL,
    function    TEXT,
    url         TEXT NOT NULL,
    source      TEXT NOT NULL,      -- website | ats | job_board
    host        TEXT,
    first_seen  TEXT NOT NULL,
    last_seen   TEXT NOT NULL,
    active      INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (crd, title, url)
);
CREATE INDEX IF NOT EXISTS ix_job_posting_active ON firm_job_posting (crd, active);
"""

CAREERS_TEXT_RE = re.compile(
    r"\b(careers?|jobs|job openings|join (our|the) team|join us|employment|"
    r"career opportunities|we(?:'re| are) hiring|work (with|for|at) us|open (positions|roles)|"
    r"current openings)\b", re.I)
CAREERS_PATH_RE = re.compile(
    r"/(careers?|jobs|join-?(our-?)?team|join-us|employment|career-opportunities|work-with-us|"
    r"open-positions|current-openings)(/|$|\.html?|\.aspx|\?)", re.I)
PROBE_PATHS = ("/careers", "/join-our-team")

# Applicant-tracking hosts. Several of these also serve employee log-ins
# (payroll, HR), so those count only on a careers path.
ATS = {
    "greenhouse.io": "Greenhouse", "lever.co": "Lever", "workable.com": "Workable",
    "bamboohr.com": "BambooHR", "applytojob.com": "JazzHR", "jazzhr.com": "JazzHR",
    "recruiting.paylocity.com": "Paylocity", "workforcenow.adp.com": "ADP",
    "recruiting.adp.com": "ADP", "myworkdayjobs.com": "Workday", "breezy.hr": "Breezy",
    "recruitee.com": "Recruitee", "smartrecruiters.com": "SmartRecruiters",
    "ats.rippling.com": "Rippling", "icims.com": "iCIMS", "recruiting.ultipro.com": "UKG",
    "recruiting2.ultipro.com": "UKG", "paycomonline.net": "Paycom",
    "jobs.dayforcehcm.com": "Dayforce", "teamtailor.com": "Teamtailor", "ashbyhq.com": "Ashby",
    "jobs.gusto.com": "Gusto", "jobvite.com": "Jobvite", "hrmdirect.com": "HRM Direct",
    "isolvedhire.com": "iSolved", "hire.trakstar.com": "Trakstar", "recruiterbox.com": "Trakstar",
    "applicantpro.com": "ApplicantPro", "hireology.com": "Hireology",
    "theapplicantmanager.com": "The Applicant Manager", "pinpointhq.com": "Pinpoint",
}
ATS_PATH_NEEDED = {"BambooHR": re.compile(r"/(careers|jobs)", re.I),
                   "Paycom": re.compile(r"/ats/", re.I)}
JOB_BOARDS = {
    "linkedin.com": "LinkedIn", "indeed.com": "Indeed", "glassdoor.com": "Glassdoor",
    "ziprecruiter.com": "ZipRecruiter", "simplyhired.com": "SimplyHired", "monster.com": "Monster",
    "careerbuilder.com": "CareerBuilder", "builtin.com": "Built In", "wellfound.com": "Wellfound",
    "efinancialcareers.com": "eFinancialCareers", "kitces.com": "Kitces",
    "fpajobs.org": "FPA", "cfainstitute.org": "CFA Institute",
}
JOB_BOARD_PATH_RE = re.compile(r"/(jobs?|job-listing|viewjob|career|careers|positions?)\b", re.I)

# A job title names a role: "Client Service Associate", "Lead Advisor",
# "Chief Compliance Officer". A topic on its own ("Tax Planning", "Compliance")
# is a menu item or a heading, not an opening.
ROLE_RE = re.compile(
    r"\b(advis[eo]rs?|planners?|paraplanners?|analysts?|associates?|assistants?|administrators?|"
    r"managers?|traders?|accountants?|bookkeepers?|preparers?|directors?|specialists?|"
    r"coordinators?|interns?|internships?|officers?|engineers?|developers?|receptionists?|"
    r"consultants?|representatives?|controllers?|strategists?|cfo|coo|cco|cio|cto|head of|"
    r"vice president|recruiters?|paralegals?|attorneys?|counsel|csa|liaisons?|generalists?|"
    r"architects?|team lead|lead)\b", re.I)
NOT_A_ROLE_RE = re.compile(
    r"\b(our team|meet|about us|learn more|apply now|apply today|benefits|culture|why join|"
    r"contact|privacy|cookie|login|log in|sign in|subscribe|newsletter|blog|news|insights|"
    r"services|how we|our story|testimonials|disclosures?|form adv|read more|view all|see all|"
    r"back to|board of|leadership team|management team|our advisors|find an? advis|"
    r"become an? (advis|client)|for advisors|lead generation|registered representatives? of|"
    r"securities offered|advisory services|financial professionals?|our (people|staff)|"
    r"referral (bonus|bonuses|program)|bonus(es)?|assistance|reimbursement|tuition|401\(?k|"
    r"paid time off|\bpto\b|holidays?|parental leave|perks|wellness program|opportunit(y|ies)|"
    r"^(internships|careers|jobs|positions|roles|openings))\b", re.I)
# Job titles are singular ("Wealth Advisor"); a heading ending in a plural
# role ("Independent Advisors", "Internships") names a kind of people.
PLURAL_END_RE = re.compile(
    r"\b(advis[eo]rs|planners|paraplanners|analysts|associates|assistants|administrators|"
    r"managers|traders|accountants|specialists|coordinators|interns|internships|officers|"
    r"engineers|developers|consultants|representatives|strategists|recruiters|directors)\s*$",
    re.I)
# "Gregg JohnsonNational Sales Director": text of two elements run together.
RUN_TOGETHER_RE = re.compile(r"[a-z]{3,}[A-Z][a-z]")
PREFIX_RE = re.compile(r"^(spotlight|now hiring|hiring|new|featured|urgent|open role|opening)\s*[:|-]\s*",
                       re.I)
# "Jane Smith, Client Service Associate": a person on a team or testimonial
# block, not an opening.
PERSON_FIRST_RE = re.compile(r"^[A-Z][a-z]+(?: [A-Z]\.?)? [A-Z][a-zA-Z'-]+(?:,| [-|] )")
NO_OPENINGS_RE = re.compile(
    r"\b(no (current |open )?(openings|positions|vacancies|opportunities)|"
    r"no (positions|roles|jobs) (are )?(currently )?(available|open)|not (currently )?hiring|"
    r"there are (currently )?no (open|available|current)|check back (later|soon))\b", re.I)

# First match wins, so the narrower functions come first.
FUNCTIONS = (
    ("Compliance", re.compile(r"complian|\bcco\b|regulator|\brisk\b|legal|paralegal|counsel|"
                              r"attorney", re.I)),
    ("Leadership", re.compile(r"\bcoo\b|\bceo\b|\bcio\b|chief|head of|director|vice president|"
                              r"\bpresident\b|managing partner|\bprincipal\b", re.I)),
    ("Technology", re.compile(r"engineer|developer|technolog|(?-i:\bIT\b)|\bdata\b|systems|"
                              r"architect|\bcto\b|software", re.I)),
    ("Finance and tax", re.compile(r"\btax\b|accountant|accounting|bookkeep|controller|\bcfo\b|"
                                   r"finance|payroll|preparer|\baudit|treasury|fund account", re.I)),
    ("Marketing and growth", re.compile(r"marketing|business development|\bsales\b|growth|"
                                        r"communications|events|brand|account executive|"
                                        r"content|digital|social media", re.I)),
    ("People and HR", re.compile(r"recruit|talent|human resources|\bhr\b|people operations", re.I)),
    ("Operations and client service", re.compile(
        r"operations|client service|client success|client experience|client associate|"
        r"service associate|administrat|assistant|coordinator|receptionist|onboarding|billing|"
        r"office manager|\bcsa\b|support|service|\baccount (manager|coordinator|associate)|"
        r"vendor|contract|project|platform|workplace|facilit|\boffice\b|licensing|registration|"
        r"trading|implementation|transition|data entry|reconcil|settlement|\bclerk", re.I)),
    ("Advisory", re.compile(r"advis[eo]r|planner|paraplanner|planning|wealth manag|"
                            r"relationship manag|portfolio manag|investment|analyst|trader|"
                            r"\bcfp\b|research|strategist|estate", re.I)),
)
FUNCTION_ORDER = [name for name, _ in FUNCTIONS] + ["Other"]

TITLE_MIN, TITLE_MAX, ROLES_MAX = 6, 90, 30
FEED_MAX = 60                 # openings kept per firm from a tracking system's feed
# A heading that opens with one of these is a call to action or a sentence
# ("Become a Financial Advisor", "Search All Open Internships"), not a title.
LEAD_WORDS = {"we", "our", "you", "your", "join", "this", "the", "a", "an", "interested", "click",
              "email", "send", "to", "become", "search", "explore", "view", "find", "see", "browse",
              "learn", "discover", "apply", "meet", "work", "start", "build", "grow", "why", "how",
              "what", "who", "where", "when", "careers", "career", "current", "open", "all", "more",
              "about", "submit", "upload", "read", "get", "contact", "featured", "back", "return"}
_DASHES = "-" + chr(0x2013) + chr(0x2014)
# "Paraplanner - Acme Wealth | Indeed": the title is the part before the firm.
_TITLE_CUT = re.compile(r"\s[" + _DASHES + r"|]\s|\sat\s|\sjobs? in\s", re.I)
# "Acme Wealth hiring Paraplanner in Boston, MA | LinkedIn"
_HIRING = re.compile(r"\bhiring\s+(?:an?\s+)?(.+?)(?:\s+in\s+[A-Z][^|]*)?(?:\s*\||\s+["
                     + _DASHES + r"]\s|$)")
_CHROME = re.compile(r"(^|[\s_-])(nav|navbar|navigation|menu|header|footer|breadcrumbs?|sidebar|"
                     r"cookie|modal|social)([\s_-]|$)", re.I)
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0 Safari/537.36")


def init(conn) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def function_of(title: str) -> str:
    for name, rx in FUNCTIONS:
        if rx.search(title or ""):
            return name
    return "Other"


def _host(url: str) -> str:
    h = (urlparse(url).hostname or "").lower()
    return h[4:] if h.startswith("www.") else h


def ats_of(url: str) -> str | None:
    h = _host(url)
    name = next((n for dom, n in ATS.items() if h == dom or h.endswith("." + dom)), None)
    if name and name in ATS_PATH_NEEDED and not ATS_PATH_NEEDED[name].search(urlparse(url).path or ""):
        return None
    return name


def board_of(url: str) -> str | None:
    h = _host(url)
    return next((n for dom, n in JOB_BOARDS.items() if h == dom or h.endswith("." + dom)), None)


def _tree(html: str):
    from lxml import html as lh
    try:
        return lh.fromstring(html)
    except Exception:
        return None


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip(" \t\r\n-|:" + chr(0x2022) + chr(0xB7))


def careers_links(html: str, base: str) -> list[str]:
    """Links on a page that lead to careers or to a job board, best first."""
    t = _tree(html)
    if t is None:
        return []
    scored = []
    for a in t.iter("a"):
        href = (a.get("href") or "").strip()
        if not href or href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        url = urljoin(base, href).split("#")[0]
        text = _clean(a.text_content())[:80]
        score = 0
        if ats_of(url):
            score = 3
        elif CAREERS_TEXT_RE.search(text) and len(text) <= 40:
            score = 2
        elif CAREERS_PATH_RE.search(urlparse(url).path or ""):
            score = 1
        if score:
            scored.append((score, url))
    for fr in t.iter("iframe", "script"):
        # A board embedded in the page: an iframe, or the script that draws
        # one (Greenhouse's job_board/js?for=..., Lever's postings widget).
        src = urljoin(base, (fr.get("src") or "").strip())
        if fr.get("src") and ats_of(src):
            scored.append((4, src))
    seen, out = set(), []
    for _, url in sorted(scored, key=lambda x: -x[0]):
        if url not in seen and url.rstrip("/") != base.split("#")[0].rstrip("/"):
            seen.add(url)
            out.append(url)
    return out[:6]


def is_title(text: str) -> bool:
    """True when a short piece of text reads as a job title."""
    if not (TITLE_MIN <= len(text) <= TITLE_MAX):
        return False
    if not ROLE_RE.search(text) or NOT_A_ROLE_RE.search(text):
        return False
    who = PERSON_FIRST_RE.search(text)
    if who and not ROLE_RE.search(who.group(0)):
        return False
    if text.count(" ") > 10 or text.endswith((".", "?", "!", ":")):
        return False
    if "@" in text or "http" in text.lower() or "www." in text.lower():
        return False
    if PLURAL_END_RE.search(text) or RUN_TOGETHER_RE.search(text):
        return False
    # A sentence, not a title: three or more lower-case words that are not
    # the small joining ones ("Guide and inspire clients and planners").
    small = {"and", "of", "for", "to", "the", "in", "at", "on", "with", "a", "an", "or", "&"}
    lower = [w for w in text.split() if w[:1].islower() and w not in small]
    if len(lower) >= 3:
        return False
    first = text.lower().split()[0]
    return first not in LEAD_WORDS and not text.lower().startswith(("if you", "are you", "life at"))


def _in_chrome(el) -> bool:
    """Inside the page's navigation, header or footer, where menu items live.
    The page body's own classes ("mobile-header", "sticky-menu") describe the
    theme, not the element, so the search stops at main, article and body."""
    for a in el.iterancestors():
        if a.tag in ("main", "article", "body", "html"):
            return False
        if a.tag in ("nav", "header", "footer", "aside"):
            return True
        if _CHROME.search(f"{a.get('class') or ''} {a.get('id') or ''} {a.get('role') or ''}"):
            return True
    return False


def role_titles(html: str, base: str) -> list[tuple[str, str]]:
    """(title, url) for every job title a careers or board page lists."""
    t = _tree(html)
    if t is None:
        return []
    for bad in t.xpath("//script|//style|//noscript|//template"):
        bad.drop_tree()
    out, seen = [], set()
    for el in t.iter("h1", "h2", "h3", "h4", "h5", "h6", "a", "li", "strong", "b", "td", "dt",
                     "span", "p", "div"):
        # A heading or link holding the whole title; long blocks are prose,
        # and a block with children is a container, read through its children.
        if el.tag in ("li", "td", "span") and len(el) > 3:
            continue
        if el.tag in ("div", "p") and len(el):
            continue
        text = PREFIX_RE.sub("", _clean(el.text_content()))
        if not is_title(text):
            continue
        key = text.lower()
        if key in seen or _in_chrome(el):
            continue
        seen.add(key)
        href = el.get("href") if el.tag == "a" else None
        if href is None:
            link = next((x for x in el.iterancestors("a")), None)
            if link is None:
                link = next(iter(el.iter("a")), None)
            href = link.get("href") if link is not None else None
        if href and href.strip().startswith(("mailto:", "tel:", "javascript:", "#")):
            href = None
        out.append((text, urljoin(base, href) if href else base))
        if len(out) >= ROLES_MAX:
            break
    return out


def says_no_openings(text: str) -> bool:
    return bool(NO_OPENINGS_RE.search(text or ""))


# ---------------------------------------------------------- tracking systems

def _get_json(url: str, timeout: int = 15):
    import requests
    try:
        r = requests.get(url, timeout=timeout, headers={"User-Agent": UA,
                                                        "Accept": "application/json"})
        if r.status_code != 200:
            return None
        return r.json()
    except Exception:
        return None


def _seg(url: str, n: int = 0) -> str:
    parts = [x for x in (urlparse(url).path or "").split("/") if x]
    return parts[n] if len(parts) > n else ""


def ats_api(url: str) -> list[tuple[str, str]] | None:
    """The openings on a tracking system's board, read from the public feed
    the board itself loads, or None when this system has no such feed (the
    board's page is then read as HTML)."""
    name, host = ats_of(url), _host(url)
    sub = host.split(".")[0]
    if name == "Greenhouse":
        token = (parse_qs(urlparse(url).query).get("for") or [""])[0] or _seg(url)
        if token in ("", "embed"):
            return None
        js = _get_json(f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs")
        if not isinstance(js, dict):
            return None
        return [(j.get("title") or "", j.get("absolute_url") or url) for j in js.get("jobs") or []]
    if name == "Lever":
        company = _seg(url)
        if not company:
            return None
        js = _get_json(f"https://api.lever.co/v0/postings/{company}?mode=json")
        if not isinstance(js, list):
            return None
        return [(j.get("text") or "", j.get("hostedUrl") or url) for j in js]
    if name == "BambooHR" and sub not in ("www", "bamboohr"):
        js = _get_json(f"https://{sub}.bamboohr.com/careers/list")
        if not isinstance(js, dict):
            return None
        return [(j.get("jobOpeningName") or "", f"https://{sub}.bamboohr.com/careers/{j.get('id')}")
                for j in js.get("result") or []]
    if name == "Workable":
        account = _seg(url) if host in ("apply.workable.com", "jobs.workable.com") else sub
        if not account or account in ("www", "apply", "jobs", "j"):
            return None
        js = _get_json(f"https://apply.workable.com/api/v1/widget/accounts/{account}")
        if not isinstance(js, dict):
            return None
        return [(j.get("title") or "", j.get("url") or j.get("shortlink") or url)
                for j in js.get("jobs") or []]
    if name == "Ashby":
        org = _seg(url)
        if not org:
            return None
        js = _get_json(f"https://api.ashbyhq.com/posting-api/job-board/{org}")
        if not isinstance(js, dict):
            return None
        return [(j.get("title") or "", j.get("jobUrl") or url) for j in js.get("jobs") or []]
    if name == "Breezy" and sub not in ("www", "breezy"):
        js = _get_json(f"https://{sub}.breezy.hr/json")
        if not isinstance(js, list):
            return None
        return [(j.get("name") or "", j.get("url") or url) for j in js]
    if name == "Recruitee" and sub not in ("www", "recruitee"):
        js = _get_json(f"https://{sub}.recruitee.com/api/offers/")
        if not isinstance(js, dict):
            return None
        return [(j.get("title") or "", j.get("careers_url") or url) for j in js.get("offers") or []]
    return None


def embedded_titles(html: str, base: str) -> list[tuple[str, str]]:
    """Titles a board page carries as data rather than as text (Paylocity and
    some others put the whole list in a script)."""
    out, seen = [], set()
    for m in re.finditer(r'"(?:JobTitle|jobTitle|jobOpeningName|PostingTitle|postingTitle)"'
                         r'\s*:\s*"([^"]{6,90})"', html or ""):
        t = _clean(m.group(1).replace("\\u0026", "&").replace("\\/", "/"))
        if t.lower() not in seen and is_title(t):
            seen.add(t.lower())
            out.append((t, base))
    return out[:ROLES_MAX]


# ---------------------------------------------------------------- job boards

def board_title(title: str) -> str:
    """The job title in a job board's result title."""
    t = (title or "").strip()
    m = _HIRING.search(t)
    t = m.group(1) if m else _TITLE_CUT.split(t)[0]
    return re.sub(r"\s+(job|jobs|position|opening)$", "", _clean(t), flags=re.I)


def board_query(name: str) -> str:
    """One search per firm. Restricted to LinkedIn's single postings, which
    is where advisory firms post and which read "Firm hiring Title in City";
    a plain "<firm> jobs" search returns the boards' own search pages."""
    return f'site:linkedin.com/jobs/view "{name}"'


US_STATES = set("AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT "
                "NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY PR".split())
_PLACE_STATE = re.compile(r", ([A-Z]{2})\b")


def posting_states(text: str) -> set[str]:
    """US states a posting's title or snippet places it in ("in Boston, MA")."""
    return {m for m in _PLACE_STATE.findall(text or "") if m in US_STATES}


def board_postings(results: list[dict], firm_names: list[str],
                   states: set[str] | None = None) -> list[tuple[str, str, str]]:
    """(title, url, board) from web search results that are job-board postings
    naming the firm. With `states` (where the firm has offices or people), a
    posting placed in other states only is another firm of the same name,
    unless it says it is remote."""
    from .websearch import _core, _flat
    cores = [c for c in (_core(n) for n in firm_names if n) if c and len(c) >= 4]
    out, seen = [], set()
    for r in results or []:
        url = r.get("href") or r.get("url") or ""
        board = board_of(url) or ats_of(url)
        if not board or not JOB_BOARD_PATH_RE.search(urlparse(url).path or ""):
            continue
        raw = f"{r.get('title') or ''} {r.get('body') or ''}"
        blob = f" {_flat(raw)} "
        if not any(f" {c} " in blob for c in cores):
            continue
        where = posting_states(str(r.get("title") or "")) or posting_states(str(r.get("body") or ""))
        if states and where and not (where & states) and "remote" not in raw.lower():
            continue
        title = board_title(r.get("title") or "")
        # A title that is the firm's own name is a company page, not a posting.
        if is_title(title) and title.lower() not in seen and not any(
                f" {c} " in f" {_flat(title)} " for c in cores):
            seen.add(title.lower())
            out.append((title, url, board))
    return out
