"""Pull people, emails and phone numbers out of a firm's own web pages.

No filing carries an adviser's email or direct line. The firm's team page,
bio pages and vCards are where those live, so this reads them. Pure functions
over HTML or text, no network and no database: scripts/web_enrich fetches and
stores, this only reads.

What it extracts, and how:

  emails   mailto links, Cloudflare `data-cfemail` blobs, a raw sweep of the
           page source, and the bracket obfuscation people use to dodge
           scrapers (`jane [at] firm [dot] com`). Every candidate goes through
           `classify`, which throws out asset filenames (logo@2x.png), template
           placeholders (john.doe@example.com), platform boilerplate (Sentry,
           Wix, font CDNs) and no-reply boxes, then splits what is left into a
           personal mailbox or a shared role inbox.
  phones   tel: links and US numbers in the visible text, with extensions,
           labelled from the words next to them (direct, mobile, office, main,
           toll free). A number labelled fax is dropped outright: nobody
           prospects a fax machine.
  people   four ways, strongest first:
             jsonld      schema.org Person entries the site publishes itself
             vcard       a .vcf card, the most reliable thing on a site
             card        team-card layouts: repeated sibling blocks that each
                         hold a person-shaped name, usually a title line and
                         sometimes a mailto or tel. Repetition is the signal;
                         a lone name-shaped string proves nothing
             known_name  a roster name we already hold (Schedule A, the
                         individual feed) found in the page text
             email_name  a personal address whose local part fits a person's
                         name under a known pattern (jdoe@ for Jane Doe)

Titles are cleaned hard because the old crawler's were not: it took the 80
characters after a name, so "Partner" arrived as "Partner Jane joined the firm
in" and a CFP mark arrived as mojibake. Here a title must read like a title
(a title word, no digits, no sentence verbs, short) and anything after the
first sign of a biography sentence is cut. Names must look like names: two to
four capitalised words, none of them a page word such as Team, Contact or
Wealth.

Credits: `classify`, the junk, placeholder, asset and no-reply tables,
`decode_cfemail`, the bracket obfuscation pattern and the confidence rule are
ported from the user's own site_email_harvester.py (Sequencer). Changes made
in the port, each for a reason:
  - "john", "jane" and "mail" are no longer placeholder locals. On an adviser
    site john@ is very often John, and mail@ is a real shared inbox.
  - Domain fragments such as "cdn." and "media." now match only at the start
    of a domain label, and "font" became "fonts.", so a firm domain that
    merely contains those letters (fontainewealth.com) is not thrown away.
  - Role detection also consults prospect.contacts.is_role_email, which knows
    adviser-specific inboxes (compliance@, operations@, clientservices@).
"""

from __future__ import annotations

import html as _html
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from urllib.parse import unquote, urljoin, urlparse

from . import contacts
from .emailguess import _detect   # tries every pattern in emailguess.PATTERNS

try:
    from lxml import html as _lxml_html
except ImportError:  # pragma: no cover - lxml ships with Scrapling
    _lxml_html = None


# ---------------------------------------------------------------- ported tables

# Anything whose "TLD" is one of these is a filename, not an email.
FILE_EXTENSIONS = {
    "png", "jpg", "jpeg", "gif", "svg", "webp", "avif", "bmp", "tiff", "ico",
    "css", "js", "mjs", "cjs", "json", "jsonld", "xml", "map", "txt", "csv",
    "html", "htm", "php", "asp", "aspx", "jsp", "md", "yml", "yaml", "toml",
    "woff", "woff2", "ttf", "eot", "otf", "fnt",
    "mp3", "mp4", "webm", "ogg", "wav", "mov", "avi", "mkv",
    "pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "zip", "gz", "tar",
    "rar", "7z", "exe", "dll", "bin", "iso", "psd", "ai", "eps", "sketch",
    "py", "rb", "java", "class", "go", "rs", "sh", "bat", "sql", "lock",
    "min", "br", "webmanifest", "scss", "less", "vue", "jsx", "tsx", "ts",
}

# Domains that are never a real contact for the site being read.
JUNK_DOMAINS = {
    "example.com", "example.org", "example.net", "example.edu", "example",
    "domain.com", "domain.co", "yourdomain.com", "your-domain.com",
    "mydomain.com", "somedomain.com", "site.com", "yoursite.com",
    "website.com", "yourwebsite.com", "company.com", "yourcompany.com",
    "mycompany.com", "acme.com", "acmecorp.com", "test.com", "testing.com",
    "sample.com", "samples.com", "demo.com", "dummy.com", "placeholder.com",
    "email.com", "youremail.com", "myemail.com", "mailinator.com",
    "localhost", "localhost.com", "foo.com", "bar.com", "baz.com",
    "lorem.com", "ipsum.com", "xyz.com", "abc.com", "asdf.com",
    "yopmail.com", "tempmail.com", "guerrillamail.com", "10minutemail.com",
    "sentry.io", "sentry-next.wixpress.com", "wixpress.com", "wix.com",
    "parastorage.com", "squarespace.com", "sqsp.net", "webflow.com",
    "wordpress.org", "wordpress.com", "wp.com", "automattic.com",
    "godaddy.com", "weebly.com", "duda.co", "shopifycdn.com",
    "fonts.googleapis.com", "fonts.gstatic.com", "googleapis.com",
    "gstatic.com", "typekit.net", "use.typekit.net", "fontawesome.com",
    "use.fontawesome.com", "fonts.net", "myfonts.com", "fontsquirrel.com",
    "fontshare.com", "cloudflare.com", "cdnjs.cloudflare.com",
    "jsdelivr.net", "cdn.jsdelivr.net", "unpkg.com", "bootstrapcdn.com",
    "stackpath.bootstrapcdn.com", "jquery.com", "gravatar.com",
    "w3.org", "schema.org", "purl.org", "xmlns.com", "adobe.com",
    "googletagmanager.com", "google-analytics.com", "doubleclick.net",
    "facebook.net", "hotjar.com", "segment.com", "intercom.io",
    # regulators: their addresses sit in every adviser disclosure footer
    "sec.gov", "finra.org", "nasaa.org", "adviserinfo.sec.gov",
}

# Fragments that must start a domain label ("cdn.example.com", not "xcdn.").
JUNK_LABEL_PREFIXES = ("cdn.", "s3.", "static.", "assets.", "media.", "img.",
                       "ingest.", "fonts.")
# Fragments that may sit anywhere in the domain.
JUNK_DOMAIN_SUBSTRINGS = (
    "sentry", "cloudfront.net", "akamai", "fastly", "typekit", "gstatic",
    "googleapis", "parastorage", "wixpress", "cloudflareinsights",
    "amazonaws.com", "azureedge", "fontawesome", "fontshare", ".invalid",
    ".local", ".test", ".example",
)

# Social hosts. An address there is a profile handle, never a mailbox, and the
# @linkedin.com incident is why this is enforced here as well as downstream.
SOCIAL_EMAIL_HOSTS = ("linkedin.", "facebook.", "twitter.", "x.com", "instagram.",
                      "youtube.", "tiktok.", "medium.", "vimeo.", "spotify.",
                      "pinterest.", "yelp.", "threads.")

# Local parts that mark the address as a template value.
JUNK_LOCALS = {
    "johndoe", "john.doe", "john_doe", "janedoe", "jane.doe",
    "joedoe", "joe.doe", "joebloggs", "joe.bloggs", "mustermann",
    "name", "yourname", "your.name", "your_name", "myname",
    "firstname", "lastname", "firstname.lastname", "first.last", "fname",
    "email", "emailaddress", "youremail", "your.email", "your_email",
    "myemail", "my.email", "mailaddress", "e-mail",
    "user", "username", "yourusername", "someone", "somebody", "anyone",
    "test", "tests", "testing", "test1", "test123", "testuser", "testmail",
    "abc", "abcd", "abc123", "xyz", "aaa", "bbb", "asdf", "qwerty",
    "foo", "bar", "baz", "qux", "sample", "example", "demo", "dummy",
    "placeholder", "lorem", "ipsum", "nobody", "null", "none", "void",
    "changeme", "replaceme", "insert", "enteremail", "your-email",
}

# Mailboxes that exist but can never be written to.
NO_REPLY_PATTERNS = re.compile(
    r"^(no[-_.]?reply|do[-_.]?not[-_.]?reply|donotreply|noreply|bounce|"
    r"mailer[-_.]?daemon|postmaster|automated|notification[s]?|alerts?|"
    r"unsubscribe|abuse|spam|listserv|majordomo)([-_.].*)?$",
    re.IGNORECASE,
)

# Role mailboxes: real and reachable, but not a named person. Kept, labelled.
ROLE_PATTERNS = re.compile(
    r"^(info|contact|hello|hi|hey|enquir(y|ies)|inquir(y|ies)|sales|support|"
    r"help|admin|office|team|mail|press|media|marketing|careers|jobs|hr|"
    r"recruit(ing|ment)?|billing|accounts?|accounting|finance|invoices?|"
    r"legal|privacy|security|partners|partnerships|business|bd|general|"
    r"customerservice|customer[-_.]?care|service|booking|reservations|"
    r"orders?|shop|store|webmaster|hostmaster|sysadmin|it|desk|reception|"
    r"newsletter|subscribe|feedback|questions|ask|talk|connect)"
    r"s?([-_.].*)?$",
    re.IGNORECASE,
)

COMMON_TLDS = {
    "com", "org", "net", "edu", "gov", "mil", "int", "co", "io", "ai", "app",
    "dev", "me", "info", "biz", "us", "uk", "ca", "au", "de", "fr", "es", "it",
    "nl", "be", "ch", "at", "se", "no", "dk", "fi", "pl", "cz", "pt", "gr",
    "ie", "in", "cn", "jp", "kr", "sg", "hk", "tw", "my", "ph", "id", "th",
    "vn", "nz", "za", "ng", "ke", "eg", "ae", "sa", "il", "tr", "ru", "ua",
    "br", "mx", "ar", "cl", "eu", "asia", "cloud", "tech", "online", "site",
    "store", "agency", "digital", "studio", "group", "media", "law", "health",
    "finance", "capital", "consulting", "partners", "solutions", "services",
    "systems", "ventures", "fund", "global", "world", "life", "live",
    "network", "email", "md", "sh", "py", "rs", "so", "ml", "cd", "im", "ms",
    "cm", "cx", "gs", "bz", "cc", "tv", "ws", "fm", "am", "is", "la", "li",
    "lu", "mc", "mt", "mu", "gg", "je", "ag", "sc", "st", "tc", "vg", "ky",
    "bm", "gi", "mo", "lt", "lv", "ee", "sk", "si", "hr", "bg", "ro", "hu",
    "by", "kz", "pk", "bd", "lk", "np", "ir", "iq", "jo", "lb", "kw", "qa",
    "bh", "om", "ma", "dz", "tn", "gh", "tz", "ug", "zm", "zw", "bw", "mz",
    "sn", "ci", "pe", "ve", "ec", "uy", "bo", "cr", "pa", "gt", "do", "pr",
    "jm", "tt", "bs", "bb", "academy", "associates", "advisors", "attorney",
    "bank", "care", "clinic", "coach", "company", "computer", "construction",
    "dental", "design", "engineering", "estate", "events", "expert",
    "financial", "fitness", "foundation", "gallery", "insurance", "institute",
    "management", "marketing", "properties", "realty", "recipes", "software",
    "support", "team", "today", "tools", "training", "travel", "vision",
    "works", "wealth", "investments", "money", "tax", "cpa", "llc", "pro",
}

EMAIL_RE = re.compile(
    r"(?<![A-Za-z0-9._%+\-])"
    r"([A-Za-z0-9][A-Za-z0-9._%+\-]{0,63})"
    r"@"
    r"([A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?)+)"
)

# name [at] domain [dot] com  /  name (at) domain (dot) com  /  name {at} ...
OBFUSCATED_BRACKET_RE = re.compile(
    r"([A-Za-z0-9._%+\-]{1,64})"
    r"\s*[\[\(\{]\s*(?:@|at)\s*[\]\)\}]\s*"
    r"([A-Za-z0-9.\-]{1,200}?)"
    r"\s*(?:[\[\(\{]\s*(?:\.|dot)\s*[\]\)\}]|\.)\s*"
    r"([A-Za-z]{2,24})\b",
    re.IGNORECASE,
)

# name at domain dot com. High false-positive rate in prose ("meet at noon
# dot..."), so only accepted when the domain is the firm's own.
OBFUSCATED_SPACED_RE = re.compile(
    r"\b([A-Za-z0-9._%+\-]{2,64})\s+at\s+([A-Za-z0-9\-]{2,60})\s+dot\s+([A-Za-z]{2,12})\b",
    re.IGNORECASE,
)

HEX_BLOB_RE = re.compile(r"^[0-9a-f]{24,}$", re.IGNORECASE)
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
VERSIONISH_RE = re.compile(r"^v?\d+(\.\d+)*$")


# ---------------------------------------------------------------- domains

_TWO_LEVEL = {"co.uk", "org.uk", "ac.uk", "gov.uk", "com.au", "net.au", "org.au",
              "co.nz", "co.za", "com.br", "co.jp", "com.mx", "com.sg", "com.hk",
              "co.in", "co.il", "com.tr", "co.kr"}


def registrable(host: str) -> str:
    """firm.com for www.firm.com or mail.firm.com. A short suffix list is
    enough here: the firms are US advisers, nearly all on .com/.net/.org."""
    host = (host or "").lower().strip().strip(".").split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    parts = [p for p in host.split(".") if p]
    if len(parts) <= 2:
        return ".".join(parts)
    if ".".join(parts[-2:]) in _TWO_LEVEL:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def on_domain(email_or_domain: str, site_domain: str | None) -> bool:
    if not site_domain:
        return False
    dom = email_or_domain.rsplit("@", 1)[-1].lower()
    return registrable(dom) == registrable(site_domain)


# ---------------------------------------------------------------- email filter

def classify(email: str, site_domain: str = "", keep_noreply: bool = False):
    """Return (verdict, category, reason). verdict True keeps the address;
    category is 'personal' or 'role' for a kept one, else the drop bucket.
    Ported from site_email_harvester.py; see the module docstring for the
    changes."""
    if email.count("@") != 1:
        return False, "malformed", "not exactly one @"
    local, domain = email.split("@", 1)
    local_l, domain_l = local.lower(), domain.lower()

    if len(email) > 254 or len(local) > 64:
        return False, "malformed", "too long"
    if ".." in local or ".." in domain:
        return False, "malformed", "consecutive dots"
    if local.startswith(".") or local.endswith(".") or local.endswith("-"):
        return False, "malformed", "bad local boundary"
    if domain.startswith("-") or domain.startswith(".") or "." not in domain:
        return False, "malformed", "bad domain"
    if "%" in local or "%" in domain:
        return False, "malformed", "url-encoded fragment"

    labels = domain_l.split(".")
    tld = labels[-1]
    # A real TLD wins over the extension list: .ai, .md and .sh are both.
    if tld not in COMMON_TLDS and tld in FILE_EXTENSIONS:
        return False, "asset", f"'{tld}' is a file extension (logo@2x.png style)"
    if re.fullmatch(r"[2-9]x", labels[0]):
        return False, "asset", "retina image marker (@2x / @3x)"
    if not tld.isalpha():
        return False, "asset", "non-alphabetic TLD (package@1.2.3 style)"
    if len(tld) < 2 or len(tld) > 24:
        return False, "asset", "implausible TLD length"
    if VERSIONISH_RE.match(local_l):
        return False, "asset", "local part is a version number"

    if domain_l in JUNK_DOMAINS:
        return False, "junk_domain", "placeholder or platform domain"
    if len(labels) > 2 and ".".join(labels[-2:]) in JUNK_DOMAINS:
        return False, "junk_domain", "subdomain of a junk domain"
    if tld == "gov":
        return False, "junk_domain", "government address in a disclosure"
    for frag in JUNK_DOMAIN_SUBSTRINGS:
        if frag in domain_l:
            return False, "junk_domain", f"domain contains '{frag}'"
    for frag in JUNK_LABEL_PREFIXES:
        if domain_l.startswith(frag) or ("." + frag) in domain_l:
            return False, "junk_domain", f"domain label starts '{frag}'"
    if any(s in domain_l for s in SOCIAL_EMAIL_HOSTS):
        return False, "junk_domain", "social platform, not a mailbox"

    if local_l in JUNK_LOCALS:
        return False, "placeholder", "dummy local part"
    if re.sub(r"[._\-]", "", local_l) in JUNK_LOCALS:
        return False, "placeholder", "dummy local part (punctuation stripped)"
    if local_l.isdigit():
        return False, "placeholder", "all-numeric local part"
    if HEX_BLOB_RE.match(local_l) or UUID_RE.match(local_l):
        return False, "asset", "hash / key, not a mailbox"
    if len(set(local_l)) == 1 and len(local_l) > 2:
        return False, "placeholder", "repeated character local part"

    if NO_REPLY_PATTERNS.match(local_l) and not keep_noreply:
        return False, "noreply", "unmonitored mailbox"

    role = ROLE_PATTERNS.match(local_l) or contacts.is_role_email(email)
    return True, ("role" if role else "personal"), ""


def confidence(method: str, email: str, site_domain: str) -> int:
    """The harvester's high/medium/low rule, mapped onto contact_point's
    0 to 100 scale (85 is what a published website address is worth)."""
    domain_l = email.rsplit("@", 1)[-1].lower()
    tld = domain_l.rsplit(".", 1)[-1]
    score = 0
    if method in ("mailto", "cfemail"):
        score += 2
    if tld in COMMON_TLDS:
        score += 1
    if on_domain(domain_l, site_domain):
        score += 2
    if method == "obfuscated-spaced":
        score -= 2
    return 85 if score >= 3 else 70 if score >= 1 else 50


def decode_cfemail(hexstr: str) -> str | None:
    """Decode Cloudflare's data-cfemail obfuscation: the first byte is an XOR
    key for the rest."""
    try:
        data = bytes.fromhex((hexstr or "").strip())
        if len(data) < 2:
            return None
        key = data[0]
        return "".join(chr(b ^ key) for b in data[1:])
    except (ValueError, TypeError):
        return None


def _clean_addr(addr: str) -> str:
    addr = unquote(addr).strip().strip(".,;:\"'<>()[]{}")
    local, _, domain = addr.partition("@")
    return f"{local}@{domain.lower().rstrip('.')}"


# ---------------------------------------------------------------- text tidying

_MARKS = re.compile("[\u00ae\u2122\u00a9\u2120\u200b\u200c\u200d\u2060\ufeff\u00ad]"
                    r"|\((?:R|TM|C)\)", re.I)


def tidy(s: str | None) -> str:
    """Entities decoded, trademark marks and invisible characters removed,
    whitespace collapsed. CFP marks were the mojibake in the old titles."""
    s = _html.unescape(s or "")
    s = s.replace("\u00a0", " ").replace("\u2019", "'").replace("\u2018", "'")
    s = _MARKS.sub("", s)
    return " ".join(s.split())


# ---------------------------------------------------------------- names

# Credentials that trail a name ("Jane Doe, CFP, CPA") or lead a title.
CREDENTIALS = {
    "cfp", "cfa", "cpa", "chfc", "clu", "aif", "aifa", "crpc", "cima", "cepa",
    "cpwa", "ricp", "ea", "mba", "jd", "phd", "cdfa", "cltc", "aams", "apma",
    "crps", "cka", "bfa", "cap", "cfs", "rma", "cpfa", "pfs", "awma", "ctfa",
    "mst", "msfs", "llm", "esq", "cfe", "caia", "frm", "cmt", "cipm", "cebs",
    "chsnc", "clf", "cssc", "fsa", "asa", "cic", "ppc", "cmfc", "crc", "rfc",
    "cfep", "crpc", "rlp", "cpa/pfs", "ms", "ma", "bs", "ba", "cimc", "aep",
    "cwpp", "chfc/clu", "cdp", "cfci", "fbs", "cwm", "rp", "qpfc", "cpc",
    "ctep", "tep", "cebs", "cplc", "ricp", "cssd", "cpfp", "ccps", "ce",
    "lutcf", "fic", "lic", "cwa", "cps", "pmp", "mpas", "cpm", "cfs",
}

_HONORIFICS = {"mr", "mrs", "ms", "miss", "dr", "prof", "rev", "hon", "sir"}
_SUFFIXES = {"jr", "sr", "ii", "iii", "iv"}
_PARTICLES = {"de", "da", "del", "della", "der", "di", "du", "la", "le", "van",
              "von", "st", "dos", "das", "y", "bin", "al", "el", "ter", "ten", "den"}

# Words that appear in name-shaped page text ("Our Team", "Contact Us",
# "Wealth Management", "San Diego") and are not, in practice, part of a person's
# name. Real surnames that are also words (Rich, Young, Price, Banks, Park,
# West, King, Wells) are deliberately absent.
NON_NAME_WORDS = set("""
our the team teams meet contact contacts about us read more learn view bio bios
biography full profile profiles services service wealth management financial
finance finances planning investment investments invest investing advisors
advisor adviser advisers advisory group partners capital llc inc ltd lp llp
pllc corp corporation company co home news insights insight client clients login
portal privacy policy policies careers career jobs who we are what do how why
approach philosophy office offices location locations resources resource blog
blogs events event schedule call get started family families retirement tax
taxes estate insurance private firm leadership staff people board directors form
adv crs disclosure disclosures terms site sitemap map street suite avenue road
drive blvd boulevard floor strategies strategy solutions asset assets fund funds
equity income growth portfolio portfolios market markets economic economy outlook
quarterly monthly weekly annual letter letters update updates newsletter video
videos podcast podcasts webinar webinars faq faqs testimonials reviews review
copyright rights reserved all and of for with in to a an is by on at your you my
me plan plans planner planners benefits sign up subscribe download click here
menu search skip content main navigation close open toggle back top next previous
prev associates trust trusts bank banking securities brokerage holdings
fiduciary independent registered fee fees only certified chartered senior junior
vice president chief officer director manager analyst associate principal
founder founders partner managing executive operations compliance relationship
specialist coordinator assistant administrator consultant paraplanner story
mission values culture community giving foundation history welcome hello
thank thanks meeting appointment book today now free consultation guide
calculator calculators tools library articles article education seminars
speaking media difference process pricing sustainable esg impact
philanthropy business businesses owners owner executives physicians doctors
dentists women retirees individuals institutions institutional nonprofits
endowments endowment foundations corporate pension employer employers employee
employees roth social security medicare annuities annuity life health care
disability property casualty auto legacy generational generations wealthy net
worth high flow debt budget budgeting college savings saving divorce
widows transition transitions succession exit sale liquidity alternatives
alternative real hedge venture credit stocks options risk tolerance
questionnaire account accounts statements statement online access center
documents forms links partnership affiliates custodian custody schwab fidelity
pershing morningstar orion envestnet tamarac linkedin facebook twitter instagram
youtube email phone fax address directions hours monday tuesday wednesday
thursday friday saturday sunday january february september october november
december united states america american national international global world new
san santa los las des fort palm beach lake city springs valley county island bay
mount mountain river heights village town centre plaza square tower building
parkway highway way place circle terrace trail chicago boston atlanta seattle
miami philadelphia pittsburgh detroit minneapolis nashville texas florida
california colorado arizona ohio michigan illinois indiana iowa kansas kentucky
louisiana maine maryland massachusetts minnesota mississippi missouri montana
nebraska nevada hampshire jersey oklahoma oregon pennsylvania tennessee utah
vermont wisconsin wyoming alabama alaska arkansas connecticut delaware hawaii
idaho rhode mexico investor investors member members membership certified
award awards recognition ranked top best guide guides checklist ebook report
reports study studies cases research overview introduction summary
experience expertise team's firm's we're you're it's let's join apply
ourteam aboutus contactus stewardship navigator compass summit pinnacle
horizon heritage legacy keystone cornerstone bridge harbor anchor beacon
information info record records track proven website web third party
professional professionals credentials credential designation designations
latest commentary perspective perspectives personalized comprehensive holistic
tailored objective unbiased transparent trusted dedicated experienced
fiduciaries discretionary coverage protection preservation distribution
accumulation transfer transfers rollover rollovers secure simple simply
clear clarity peace mind confidence freedom future financially retire retired
retiring tomorrow forward ahead beyond together complete total boutique
useful helpful important notice notices regulatory brochure brochures
supplement supplements code ethics why choose working work works partnering
serving serve sustainable responsible approach approaches philosophy values
accounting administration administrative admin marketing sales support trading
research human technology billing relations development onboarding paraplanning
department division operational reception front desk desk office-manager
past recent posts releases release related popular featured upcoming archive archives older newer
processing deferred compensation lead head no or nor not any if as
""".split())

_NAME_PREFIX = re.compile(r"^(?:meet|about|introducing|contact|email|call|"
                          r"biography of|bio of|welcome)\s+", re.I)


def _title_case(s: str) -> str:
    return re.sub(r"[^\W\d_]+", lambda m: m.group(0)[0].upper() + m.group(0)[1:].lower(), s)


def _name_token(w: str) -> bool:
    if w.endswith("."):
        return False          # "Smith." ends a sentence, not a name
    if len(w) < 2 or not w[0].isalpha() or not w[0].isupper():
        return False
    if not all(c.isalpha() or c in "'-" for c in w):
        return False
    if not any(c.islower() for c in w):
        return False
    # A capital inside the word is fine after Mc, Mac, De, La, O' and the like
    # (McDonald, DeAngelo, O'Brien) or a hyphen; anywhere else it means two
    # words ran together ("BartschVice", "BlundellCompany").
    for m in re.finditer(r"(?<=[a-z])[A-Z]", w):
        if not re.fullmatch(r"(?:Mc|Mac|De|Di|Da|Du|La|Le|Van|Von|St|Fitz|Del)",
                            w[:m.start()].split("-")[-1].split("'")[-1]):
            return False
    return sum(c.isupper() for c in w) <= 3


def normalise_name(text: str | None) -> str | None:
    """A cleaned person name, or None when the text is not name-shaped.

    Two to four capitalised words; middle initials, particles (van, de) and a
    Jr./III suffix are allowed; honorifics and trailing credentials are
    dropped; an all-capitals name is re-cased."""
    t = tidy(text).strip(" ,.;:|-*")
    t = _NAME_PREFIX.sub("", t)
    # Robert "Bob" Smith: the quoted nickname is not part of the legal name.
    t = re.sub(r"\s*[\"\u201c(][A-Za-z]+[\"\u201d)]\s*", " ", t).strip()
    if not t or len(t) > 60:
        return None
    if re.search(r"[\d@&+/\\|#%$!?;:=_\[\]{}<>\"\u2022]", t):
        return None
    # "George M. Peaden, Jr." or "Christina L. Doss, AAMS": what follows the
    # comma is a suffix or credentials, not part of the name.
    if "," in t:
        head, _, tail = t.partition(",")
        tail_toks = [x.strip(" .") for x in re.split(r"[,\s]+", tail) if x.strip(" .")]
        if tail_toks and all(x.lower() in CREDENTIALS or x.lower() in _SUFFIXES
                             for x in tail_toks):
            sfx = [x for x in tail_toks if x.lower() in _SUFFIXES]
            t = f"{head} {sfx[0]}" if sfx else head
    toks = t.split()
    while toks and toks[-1].lower().strip(".,") in CREDENTIALS:
        toks.pop()
    if toks and toks[-1].endswith(","):
        toks[-1] = toks[-1].rstrip(",")
    if any("," in w for w in toks):
        return None
    if sum(c.isupper() for c in "".join(toks)) == sum(c.isalpha() for c in "".join(toks)):
        toks = _title_case(" ".join(toks)).split()
    while toks and toks[0].lower().rstrip(".") in _HONORIFICS:
        toks.pop(0)
    suffix = None
    if toks and toks[-1].lower().rstrip(".") in _SUFFIXES:
        suffix = toks.pop()
        if suffix.lower().rstrip(".") in ("jr", "sr"):
            suffix = suffix.rstrip(".").capitalize() + "."
        else:
            suffix = suffix.upper()
    if not 2 <= len(toks) <= 4:
        return None
    for i, w in enumerate(toks):
        lw = w.lower().rstrip(".")
        if re.fullmatch(r"[A-Z]\.?", w):
            if i == len(toks) - 1:
                return None   # a surname is never an initial
            continue
        if lw in NON_NAME_WORDS or lw in CREDENTIALS:
            return None
        if 0 < i < len(toks) - 1 and lw in _PARTICLES:
            continue
        if not _name_token(w):
            return None
    if re.fullmatch(r"[A-Z]\.?", toks[0]) and len(toks) == 2:
        return None           # "J. Smith" is too thin to call a person
    out = " ".join(toks)
    return f"{out} {suffix}" if suffix else out


def looks_like_name(text: str | None) -> bool:
    return normalise_name(text) is not None


# ---------------------------------------------------------------- titles

# A title has to name a role. Words like Wealth, Investment or Operations are
# in most titles but also in every marketing line, so on their own they prove
# nothing; the role noun ("Advisor", "Director", "Associate") is what counts.
TITLE_WORDS = re.compile(
    r"\b(president|chief|ceo|cio|cco|cfo|coo|cto|cmo|founder|co-?founder|cofounder"
    r"|principal|partner|owner|director|advis[oe]r|officer|planner|paraplanner"
    r"|analyst|associate|chair(?:man|woman|person)?|manager|counsel|specialist"
    r"|consultant|administrator|assistant|coordinator|representative|strategist"
    r"|trader|head of|team lead|lead advisor|executive|controller|treasurer|secretary"
    r"|accountant|attorney|economist|vp|svp|evp|avp|receptionist|intern|concierge"
    r"|agent|broker|banker|paralegal|member|actuary|cpa|enrolled agent|steward"
    r"|liaison|generalist)\b",
    re.I)
_CONNECTORS = {"of", "and", "&", "the", "for", "to", "in", "at", "de", "with", "on", "a"}

# Where a title stops and a biography sentence starts.
_BLEED = re.compile(
    r"\b(?:joined|joins|manages|leads|brings|has|have|had|is|was|were|works|worked"
    r"|began|begins|spent|holds|held|earned|received|graduated|serves|served"
    r"|specializes|specialises|focuses|helps|enjoys|lives|grew|started|founded"
    r"|oversees|meet|read|learn|view|back|contact|schedule|email|e-mail|phone"
    r"|call|linkedin|bio|biography|more|about|with over|since|prior|before"
    r"|after|during|when|where|who|which|that|currently|originally|born"
    r"|i|i'm|he|she|they|his|her|their"
    # news headlines: "Saltmarsh CEO Named to Forbes Best-In-State"
    r"|named|earns|earned|receives|recognized|recognised|awarded|announces"
    r"|announced|wins|won|selected|honored|honoured|ranked|featured|elected"
    r"|celebrates|welcomes|promoted|appointed|speaks|discusses|shares)\b", re.I)

_TITLE_SEPS = " ,|-:/\u2013\u2014\u2022\u00b7*"


def clean_title(raw: str | None, name: str | None = None) -> str | None:
    """A job title, or None. Cuts at the first sign of a bio sentence, drops
    credentials and a trailing nickname, and insists on a title word."""
    t = tidy(raw)
    if not t:
        return None
    # Field labels: "Title: Partner" keeps the value, "Company: CAIS Advisors"
    # or "Location: Dallas" is not a title at all.
    t = re.sub(r"^(?:title|position|role|job title)\s*:\s*", "", t, flags=re.I)
    if re.match(r"(?i)(?:company|firm|organi[sz]ation|location|office|phone|email|"
                r"e-mail|address|website|department|team)\s*:", t):
        return None
    if name:
        t = re.sub(re.escape(name), " ", t, flags=re.I)
    t = _BLEED.split(t, maxsplit=1)[0]
    t = re.split(r"(?<=[a-z])\.\s+(?=[A-Z])|\s{2,}|\s+\|\s+|\n", t, maxsplit=1)[0]
    t = t.strip(_TITLE_SEPS + ".")
    words = t.split()
    # credentials lead ("CFP, Senior Advisor") or trail ("Advisor, CFA")
    while words and words[0].strip(",;.").lower() in CREDENTIALS:
        words.pop(0)
        while words and words[0] in ("|", ",", "-", "&", "and"):
            words.pop(0)
    while words and words[-1].strip(",;.").lower() in CREDENTIALS:
        words.pop()
    # Pages follow a title with a nickname ("Chief Investment Officer Jeff");
    # a trailing word that looks like the person's first name, or any word of
    # the name, is that bleed.
    if name:
        parts = [p.lower() for p in re.findall(r"[A-Za-z']+", name)]
        first3 = parts[0][:3] if parts else ""
        while words and (words[-1].lower().strip(",.") in parts
                         or (first3 and words[-1].lower().startswith(first3)
                             and not TITLE_WORDS.fullmatch(words[-1].strip(",.")))):
            words.pop()
    while words and words[-1].lower().strip(",.") in ("and", "&", "of", "the", "for",
                                                       "to", "in", "at", "|", "-"):
        words.pop()
    t = " ".join(words).strip(_TITLE_SEPS + ".")
    if not t or len(t) > 80 or len(t.split()) > 10:
        return None
    if re.search(r"\d|@|https?:|www\.|\.com\b", t, re.I):
        return None
    if not TITLE_WORDS.search(t):
        return None
    if re.match(r"(?i)(?:the|a|an|our|your|my|this|these|latest)\b", t):
        return None           # "The CFA designation", "Our Advisors": headings, not titles
    lower_words = [w for w in t.split() if w[:1].islower() and w.lower() not in _CONNECTORS]
    if len(lower_words) >= 2 and not t.islower():
        return None           # sentence-case prose, not a title
    if t.isupper():
        from .names import nice_name
        t = nice_name(t)
        # "Director Of Operations" reads wrong; small words stay small.
        t = re.sub(r"(?<=\s)(Of|And|The|For|To|In|At|With|On)(?=\s)",
                   lambda m: m.group(1).lower(), t)
    return t


def split_name_title(text: str | None) -> tuple[str | None, str | None]:
    """'Jane Doe, CFP | Partner' -> ('Jane Doe', 'Partner'). The name must come
    first; whatever follows a separator is offered to clean_title."""
    t = tidy(text)
    if not t:
        return None, None
    name = normalise_name(t)
    if name:
        return name, None
    m = re.match(r"^(.*?)(?:\s*[,|:\u2013\u2014\u2022\u00b7]\s*|\s+-\s+|\s*\(\s*)(.*)$", t)
    if not m:
        # "Emily Cobel Processing Specialist", "Holly Adams Accounting
        # Administration": a title or department run straight on with no
        # separator. Split at the shortest name whose remainder starts with a
        # word that is never part of a name and reads as a title (or is all
        # department words). The shortest name first, because the remainder
        # check is what keeps "Mary Ann Smith Partner" whole: "Smith" does
        # not start a title.
        toks = t.split()
        for k in range(2, min(4, len(toks) - 1) + 1):
            rest = toks[k:]
            head = rest[0].strip(",.&")
            if not (head.lower() in NON_NAME_WORDS or head.lower() in CREDENTIALS
                    or TITLE_WORDS.fullmatch(head)):
                continue
            name = normalise_name(" ".join(toks[:k]))
            if not name:
                continue
            title = clean_title(" ".join(rest), name)
            if title or (len(rest) <= 6 and all(
                    w.lower().strip(",.&") in NON_NAME_WORDS or w in ("&", "|", "-")
                    or TITLE_WORDS.fullmatch(w) for w in rest)):
                return name, title
        return None, None
    head, rest = m.group(1), m.group(2)
    name = normalise_name(head)
    if not name:
        # "Jane Doe CFP, CPA - Partner": credentials before the separator
        return None, None
    rest = rest.rstrip(")")
    return name, clean_title(rest, name)


# ---------------------------------------------------------------- nicknames

_NICK_GROUPS = """
james jim jimmy jamie|robert rob bob bobby robbie bert|william will bill billy willy liam
michael mike mikey mick|thomas tom tommy|david dave davey|christopher chris kit topher
daniel dan danny|joseph joe joey|steven steve stevie stephen|richard rick rich richie dick ricky
anthony tony|matthew matt|andrew andy drew|edward ed eddie ted ned|gregory greg
jeffrey jeff geoffrey geoff|jonathan jon jonny|john jack johnny jon|kenneth ken kenny
lawrence larry laurence|patrick pat|peter pete|ronald ron ronnie|samuel sam sammy
timothy tim timmy|nicholas nick nicky|benjamin ben benny|alexander alex al xander
charles charlie chuck chas|frederick fred freddy|gerald gerry jerry|harold hal harry
henry hank harry|joshua josh|leonard leo len lenny|louis lou|nathaniel nate nathan nat
philip phil phillip|raymond ray|russell russ|stanley stan|theodore ted teddy theo
vincent vince vinny|walter walt|zachary zach zack|douglas doug|donald don donnie
eugene gene|francis frank fran|franklin frank|jacob jake|jerome jerry|maxwell max
mitchell mitch|randall randy|rodney rod|terrence terry terence|bradley brad
bryan brian|cameron cam|clifford cliff|curtis curt|dennis denny|dominic dom
elizabeth liz beth betsy betty eliza lizzie libby|katherine kate katie kathy cathy kat catherine kathryn kathleen kay
jennifer jen jenny|margaret maggie meg peggy marge|susan sue susie suzanne
patricia pat patty trish tricia|deborah deb debbie debra|rebecca becky becca
victoria vicky tori|christine chris christy kristine tina|christina tina chris
jessica jess jessie|kimberly kim|pamela pam|barbara barb|cynthia cindy
jacqueline jackie|melissa missy mel|stephanie steph|abigail abby|alexandra alex alexa lexi
allison ally allie alison|amanda mandy|eleanor ellie nora|jillian jill|judith judy
lauren laurie|madeline maddie madeleine|nicole nikki|samantha sam|sandra sandy
theresa terry tess tessa teresa|valerie val|virginia ginny|gerard gerry
albert al bert|allen al allan alan|arthur art|bernard bernie|calvin cal
clarence clare|dale|derek|edwin ed|ernest ernie|everett|herbert herb|howard howie
jeremiah jeremy|kristopher kris chris|marcus marc mark|martin marty|melvin mel
mortimer mort|nelson|norman norm|oliver ollie|oscar|quentin|reginald reg reggie
roland|roger|ross|scott scotty|spencer|stuart stu stewart|sylvester sly
wesley wes|wilbur|winston|wyatt|tobias toby|emily em|eric rick|frederic fred
""".replace("\n", "|")

NICKNAMES: dict[str, set[str]] = defaultdict(set)
for _grp in _NICK_GROUPS.split("|"):
    _names = _grp.split()
    for _n in _names:
        NICKNAMES[_n].update(_names)


def _given_ok(page_first: str, roster_given: list[str]) -> bool:
    """Does the first name printed on the page fit the filed given names?
    Exact, nickname, a shared prefix of three letters (Chris/Christopher), or
    the person going by their middle name."""
    pf = page_first.lower().strip(".")
    if not pf:
        return False
    for g in roster_given:
        g = g.lower().strip(".")
        if not g:
            continue
        if pf == g or pf in NICKNAMES.get(g, ()) or g in NICKNAMES.get(pf, ()):
            return True
        if len(pf) >= 3 and len(g) >= 3 and (g.startswith(pf) or pf.startswith(g)):
            return True
    return False


def _parts(name: str) -> tuple[str, list[str], str] | None:
    """(first, middles, last) in lowercase letters, suffixes dropped."""
    toks = [re.sub(r"[^a-z'\-]", "", t.lower()) for t in tidy(name).split()]
    toks = [t for t in toks if t and t not in _SUFFIXES and t not in CREDENTIALS
            and t not in _HONORIFICS]
    if len(toks) < 2:
        return None
    return toks[0], toks[1:-1], toks[-1]


def _same_last(a: str, b: str) -> bool:
    a, b = a.replace("'", ""), b.replace("'", "")
    if a == b:
        return True
    pa, pb = set(a.split("-")), set(b.split("-"))
    return bool(pa & pb) and (len(pa) > 1 or len(pb) > 1) or a.replace("-", "") == b.replace("-", "")


def match_known(name: str, known_names: list[str]) -> str | None:
    """The roster name this page name refers to, when exactly one fits."""
    p = _parts(name)
    if not p:
        return None
    hits = []
    # "W. Kirk Dunk" goes by Kirk: an initial first means the next name counts.
    givens = [p[0]] + [m for m in p[1] if len(m) > 1] if len(p[0]) == 1 else [p[0]]
    for k in known_names:
        q = _parts(k)
        if not q or not _same_last(p[2], q[2]):
            continue
        if any(_given_ok(g, [q[0]] + q[1]) for g in givens if len(g) > 1):
            hits.append(k)
    uniq = {contacts.name_key(h) for h in hits}
    return hits[0] if len(uniq) == 1 else None


# ---------------------------------------------------------------- phones

PHONE_RE = re.compile(
    r"(?<![\w/=.\-])"
    r"(?:\+?1[\s.\-\u2010-\u2015]*)?"
    r"(?:\(\s*\d{3}\s*\)|\d{3})"
    r"[\s.\-\u2010-\u2015]{0,3}\d{3}[\s.\-\u2010-\u2015]{1,3}\d{4}"
    r"(?:\s*,?\s*(?:ext\.?|extension|x|\#)\s*\d{1,6})?"
    r"(?![\w\-])", re.I)

_LABELS = [
    ("fax", re.compile(r"(?:\bfax\b|\bfacsimile\b|(?:^|[\s|,;(\[])f\s*[:.)\]])", re.I)),
    ("mobile", re.compile(r"(?:\bmobile\b|\bcell(?:ular)?\b|\bmob\b|(?:^|[\s|,;(\[])[mc]\s*[:.)\]])", re.I)),
    ("direct", re.compile(r"(?:\bdirect(?:\s+line)?\b|\bdir\b|\bdesk\b|(?:^|[\s|,;(\[])d\s*[:.)\]])", re.I)),
    ("toll_free", re.compile(r"\btoll[\s\-]?free\b", re.I)),
    ("main", re.compile(r"\b(?:main|switchboard|headquarters)\b", re.I)),
    ("office", re.compile(r"(?:\boffice\b|\btel(?:ephone)?\b|\bphone\b|\bph\b|\bwork\b"
                          r"|\bcall\b|(?:^|[\s|,;(\[])[pto]\s*[:.)\]])", re.I)),
]


def _label_from(before: str, after: str) -> str | None:
    """The label word closest to the number: searched in the text just before
    it, then in a short parenthetical or word just after it."""
    best, best_end = None, -1
    for lab, rx in _LABELS:
        for m in rx.finditer(before):
            if m.end() > best_end:
                best, best_end = lab, m.end()
    if best:
        return best
    m = re.match(r"^\s*[(\[]?\s*(fax|cell|mobile|direct|office|main|toll[\s\-]?free)\b",
                 after, re.I)
    if m:
        w = m.group(1).lower()
        return {"cell": "mobile"}.get(w, "toll_free" if w.startswith("toll") else w)
    return None


def _norm_phone(raw: str) -> str | None:
    raw = re.sub(r"[\u2010-\u2015]", "-", raw)
    raw = re.sub(r"\s*,?\s*(?:extension|ext\.?|\#)\s*", " x", raw, flags=re.I)
    return contacts.norm_phone(raw)


def _phones_from(lines: list[str], tels: list[tuple[str, str]]) -> list[dict]:
    """Phones from visible lines and tel: anchors. Fax-labelled numbers are
    dropped; a number seen several times keeps its most specific label."""
    found: dict[str, str | None] = {}
    order: list[str] = []
    fax: set[str] = set()
    rank = {"direct": 0, "mobile": 1, "toll_free": 2, "office": 3, "main": 4, None: 5}

    def add(p: str | None, label: str | None) -> None:
        if not p:
            return
        if label == "fax":
            fax.add(p)
            return
        d10 = re.sub(r"\D", "", p)[:10]
        if contacts.phone_label(d10) == "toll_free":
            label = "toll_free"
        if p not in found:
            found[p] = label
            order.append(p)
        elif rank[label] < rank[found[p]]:
            found[p] = label

    for line in lines:
        prev_end = 0
        matches = list(PHONE_RE.finditer(line))
        for i, m in enumerate(matches):
            before = line[max(prev_end, m.start() - 30):m.start()]
            nxt = matches[i + 1].start() if i + 1 < len(matches) else len(line)
            after = line[m.end():min(nxt, m.end() + 20)]
            add(_norm_phone(m.group(0)), _label_from(before, after))
            prev_end = m.end()
    for href, text in tels:
        num = unquote(href.split(":", 1)[-1]).split("?")[0]
        p = _norm_phone(num)
        if p and p not in found and p not in fax:
            add(p, _label_from(text, ""))
    # A bare number printed next to the same number with an extension is the
    # switchboard behind that extension; the extension is the person's line.
    with_ext = {p.split(" x")[0] for p in order if " x" in p}
    return [{"phone": p, "label": found[p]} for p in order
            if p not in fax and not (" x" not in p and p in with_ext)]


# ---------------------------------------------------------------- document model

BLOCK_TAGS = {"address", "article", "aside", "blockquote", "body", "dd", "div", "dl",
              "dt", "fieldset", "figcaption", "figure", "footer", "form", "h1", "h2",
              "h3", "h4", "h5", "h6", "header", "hr", "li", "main", "nav", "ol", "p",
              "pre", "section", "table", "tbody", "thead", "tfoot", "td", "th", "tr",
              "ul", "br", "button", "label", "summary", "details", "caption",
              "center", "html", "dialog", "picture"}
SKIP_TAGS = {"script", "style", "noscript", "template", "svg", "iframe", "head",
             "title", "meta", "link", "object", "embed", "canvas", "video", "audio",
             "map", "select", "option", "input", "textarea", "img"}
# A span or link styled as its own line usually says so in its class.
_BLOCKISH = re.compile(r"name|title|position|role|job|designation|subtitle|heading"
                       r"|credentials|phone|email|contact", re.I)
NAME_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6", "strong", "b", "p", "span", "div",
             "a", "li", "td", "dt", "dd", "figcaption", "em", "label", "button",
             "small", "header"}
# Site-wide chrome: menus, the masthead, the footer. Names there are menu
# entries and copyright lines, not cards. Only whole class or id tokens count,
# so a card's own "team-card-header" is not mistaken for the site header.
CHROME_TOKENS = {"header", "footer", "site-header", "site-footer", "main-header",
                 "main-footer", "masthead", "colophon", "navbar", "navigation", "nav",
                 "menu", "main-menu", "primary-menu", "mega-menu", "top-bar", "topbar",
                 "footer-widgets", "header-wrapper", "footer-wrapper", "site-nav",
                 "main-navigation", "breadcrumbs", "breadcrumb"}
_CONTENT_TAGS = {"article", "section", "main", "aside", "li", "dialog"}


def _is_chrome(el, tag: str) -> bool:
    role = (el.get("role") or "").lower()
    if tag == "nav" or role in ("navigation", "banner", "contentinfo"):
        return True
    if (el.get("data-elementor-type") or "").lower() in ("header", "footer"):
        return True
    if tag in ("header", "footer"):
        # an <article>'s own header is part of the card, not the site masthead
        a = el.getparent()
        while a is not None:
            if isinstance(a.tag, str) and a.tag.lower() in _CONTENT_TAGS:
                return False
            a = a.getparent()
        return True
    if tag == "div":
        toks = set(f"{el.get('class', '')} {el.get('id', '')}".lower().split())
        return bool(toks & CHROME_TOKENS)
    return False


def _parse(html_text: str):
    if _lxml_html is None or not html_text or not html_text.strip():
        return None
    parser = _lxml_html.HTMLParser(remove_comments=True, remove_pis=True,
                                   recover=True, huge_tree=True)
    try:
        return _lxml_html.document_fromstring(html_text.replace("\x00", ""),
                                              parser=parser)
    except Exception:
        return None


class _Doc:
    """One page as ordered lines of visible text, each tied to the document
    position of the element where it starts, plus the anchors and Cloudflare
    blobs by position. Positions are preorder indices, so an element's whole
    subtree is the contiguous range [pos, end]."""

    def __init__(self, html_text: str, url: str = ""):
        self.url = url
        self.root = _parse(html_text)
        self.pos: dict = {}
        self.end: dict = {}
        self.els: list = []
        self.lines: list[tuple[int, str]] = []
        self.anchors: list[tuple[int, object, str, str]] = []
        self.cfemails: list[tuple[int, str]] = []
        self.chrome: set[int] = set()
        self._buf: list[str] = []
        self._buf_pos: int | None = None
        if self.root is None:
            return
        body = self.root.find("body")
        self._walk(body if body is not None else self.root, False)
        self._flush()
        self.line_pos = [p for p, _ in self.lines]

    # -- building
    def _emit(self, text: str | None, p: int) -> None:
        if not text:
            return
        if self._buf_pos is None and text.strip():
            self._buf_pos = p
        self._buf.append(text)

    def _flush(self) -> None:
        if self._buf:
            t = tidy("".join(self._buf))
            if t and self._buf_pos is not None:
                self.lines.append((self._buf_pos, t))
        self._buf, self._buf_pos = [], None

    def _walk(self, el, in_chrome: bool) -> None:
        tag = el.tag if isinstance(el.tag, str) else ""
        tag = tag.lower()
        p = len(self.els)
        self.els.append(el)
        self.pos[el] = p
        if not tag or tag in SKIP_TAGS:
            self.end[el] = p
            return
        if not in_chrome and _is_chrome(el, tag):
            in_chrome = True
        if in_chrome:
            self.chrome.add(p)
        block = tag in BLOCK_TAGS or (tag in ("span", "a", "strong", "b", "em", "small", "i")
                                      and _BLOCKISH.search(f"{el.get('class', '')} "
                                                           f"{el.get('itemprop', '')}"))
        if block:
            self._flush()
        if tag == "a":
            self.anchors.append((p, el, el.get("href", "") or "", ""))
        cf = el.get("data-cfemail")
        if cf:
            self.cfemails.append((p, cf))
        self._emit(el.text, p)
        for child in el:
            self._walk(child, in_chrome)
            self._emit(child.tail, self.pos.get(child, p))
        if block:
            self._flush()
        self.end[el] = len(self.els) - 1

    # -- reading
    def text(self) -> str:
        return "\n".join(t for _, t in self.lines)

    def lines_in(self, start: int, end: int, *, skip_chrome: bool = False) -> list[tuple[int, str]]:
        import bisect
        i = bisect.bisect_left(self.line_pos, start)
        out = []
        while i < len(self.lines) and self.lines[i][0] <= end:
            if not skip_chrome or self.lines[i][0] not in self.chrome:
                out.append(self.lines[i])
            i += 1
        return out

    def text_len(self, el) -> int:
        return sum(len(t) for _, t in self.lines_in(self.pos[el], self.end[el]))

    def anchors_in(self, start: int, end: int):
        return [a for a in self.anchors if start <= a[0] <= end]

    def in_chrome(self, el) -> bool:
        return self.pos.get(el, -1) in self.chrome


# ---------------------------------------------------------------- emails

def _visible_text_cheap(html_text: str) -> str:
    t = re.sub(r"(?is)<(script|style|noscript|template)\b.*?</\1>", " ", html_text)
    t = re.sub(r"<[^>]+>", " ", t)
    return tidy(t)


def _emails_raw(html_text: str, site_domain: str, text: str | None = None) -> list[tuple[str, str]]:
    """(email, method) candidates before filtering."""
    found: list[tuple[str, str]] = []
    unesc = _html.unescape(html_text)
    for m in re.finditer(r"""href\s*=\s*["']?\s*mailto:([^"'>\s]+)""", unesc, re.I):
        payload = unquote(m.group(1)).split("?", 1)[0]
        for part in payload.split(","):
            if "@" in part:
                found.append((_clean_addr(part), "mailto"))
    for m in re.finditer(r"""data-cfemail\s*=\s*["']([0-9a-fA-F]+)["']""", html_text):
        d = decode_cfemail(m.group(1))
        if d and "@" in d:
            found.append((_clean_addr(d), "cfemail"))
    for m in re.finditer(r"/cdn-cgi/l/email-protection#([0-9a-fA-F]+)", html_text):
        d = decode_cfemail(m.group(1))
        if d and "@" in d:
            found.append((_clean_addr(d), "cfemail"))
    sweep = unesc.replace("%40", "@").replace("&#64;", "@")
    for m in EMAIL_RE.finditer(sweep):
        found.append((_clean_addr(f"{m.group(1)}@{m.group(2)}"), "html"))
    text = text if text is not None else _visible_text_cheap(html_text)
    for m in OBFUSCATED_BRACKET_RE.finditer(text):
        if "[" in m.group(0) or "(" in m.group(0) or "{" in m.group(0):
            found.append((_clean_addr(f"{m.group(1)}@{m.group(2)}.{m.group(3)}"), "obfuscated"))
    for m in OBFUSCATED_SPACED_RE.finditer(text):
        dom = f"{m.group(2)}.{m.group(3)}".lower()
        if site_domain and on_domain(dom, site_domain):
            found.append((_clean_addr(f"{m.group(1)}@{dom}"), "obfuscated-spaced"))
    return found


_METHOD_RANK = {"mailto": 0, "cfemail": 1, "obfuscated": 2, "html": 3, "obfuscated-spaced": 4}


def extract_emails(html: str, site_domain: str = "", *, _text: str | None = None) -> list[dict]:
    """Every usable address on one page:
    [{email, method, category ('personal' | 'role'), confidence}].

    An address on another domain is kept only if a visitor could see it
    (a mailto, a decoded Cloudflare blob, or the visible text). Off-domain
    addresses that live only in scripts and attributes are almost always a
    vendor's, an embed's or a theme author's."""
    if not html:
        return []
    text = _text if _text is not None else _visible_text_cheap(html)
    text_l = text.lower()
    best: dict[str, str] = {}
    for email, method in _emails_raw(html, site_domain, text):
        key = email.lower()
        if key not in best or _METHOD_RANK[method] < _METHOD_RANK[best[key]]:
            best[key] = method
    out = []
    for email, method in best.items():
        ok, cat, _ = classify(email, site_domain)
        if not ok:
            continue
        if len(email.split("@", 1)[0]) < 2 and method not in ("mailto", "cfemail"):
            continue          # "orserc</a><a>m@yahoo.com": the tail of a split address
        visible = method != "html" or email in text_l
        if not visible and not on_domain(email, site_domain):
            continue
        out.append({"email": email, "method": method, "category": cat,
                    "confidence": confidence(method, email, site_domain)})
    out.sort(key=lambda r: (r["category"] != "personal", -r["confidence"], r["email"]))
    return out


def extract_phones(html_or_text: str) -> list[dict]:
    """[{phone, label}] from tel: links and visible text. US numbers only,
    normalised to (555) 555-5555 with any extension; fax numbers dropped."""
    s = html_or_text or ""
    if "<" in s and ">" in s:
        doc = _Doc(s)
        if doc.root is not None:
            tels = [(href, _anchor_label(doc, el)) for _, el, href, _ in doc.anchors
                    if href.lower().startswith("tel:")]
            return _phones_from([t for _, t in doc.lines], tels)
    return _phones_from(s.splitlines(), [])


def chrome_phones(html: str) -> set[str]:
    """Numbers printed in the site's header, footer or menus. Those are the
    firm's own lines, repeated on every page, never one person's direct dial."""
    doc = _Doc(html)
    if doc.root is None:
        return set()
    lines = [t for p, t in doc.lines if p in doc.chrome]
    tels = [(href, _anchor_label(doc, el)) for p, el, href, _ in doc.anchors
            if p in doc.chrome and href.lower().startswith("tel:")]
    return {x["phone"] for x in _phones_from(lines, tels)}


def _anchor_label(doc: _Doc, el) -> str:
    parts = [el.text_content() or "", el.get("title", "") or "", el.get("aria-label", "") or "",
             el.get("class", "") or ""]
    prev = el.getprevious()
    if prev is not None and isinstance(prev.tag, str):
        parts.insert(0, (prev.text_content() or "")[-30:])
    par = el.getparent()
    if par is not None and par.text:
        parts.insert(0, par.text[-30:])
    return " ".join(tidy(p) for p in parts if p)


# ---------------------------------------------------------------- vCards

def parse_vcard(text: str) -> dict:
    """The first card in a .vcf file: {name, title, org, emails, phones}.
    phones are [{phone, label}] with label mobile, office, main or None; a
    fax line is dropped."""
    out: dict = {"name": None, "title": None, "org": None, "emails": [], "phones": []}
    if not text or "BEGIN:VCARD" not in text.upper():
        return out
    raw = re.sub(r"\r?\n[ \t]", "", text.replace("\r\n", "\n"))
    n_name = None
    started = False
    for line in raw.split("\n"):
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        params = key.split(";")
        prop = params[0].split(".")[-1].strip().upper()
        ptypes = ";".join(params[1:]).upper()
        if prop == "BEGIN":
            started = True
            continue
        if prop == "END":
            if started:
                break
            continue
        if "QUOTED-PRINTABLE" in ptypes:
            import quopri
            try:
                val = quopri.decodestring(val.encode()).decode("utf-8", "replace")
            except Exception:
                pass
        val = val.replace("\\,", ",").replace("\\;", ";").replace("\\n", " ").strip()
        if prop == "FN" and val:
            out["name"] = tidy(val)
        elif prop == "N" and val:
            bits = (val.split(";") + [""] * 5)[:5]
            last, first, middle = bits[0].strip(), bits[1].strip(), bits[2].strip()
            n_name = " ".join(x for x in (first, middle, last) if x)
        elif prop == "TITLE" and val:
            out["title"] = tidy(val)
        elif prop == "ROLE" and val and not out["title"]:
            out["title"] = tidy(val)
        elif prop == "ORG" and val:
            out["org"] = tidy(val.replace(";", " "))
        elif prop == "EMAIL" and val:
            e = _clean_addr(val.split(":", 1)[-1] if val.lower().startswith("mailto:") else val)
            if "@" in e and e.lower() not in out["emails"]:
                out["emails"].append(e.lower())
        elif prop == "TEL" and val:
            if "FAX" in ptypes:
                continue
            num = val.split(":", 1)[-1] if val.lower().startswith("tel:") else val
            p = _norm_phone(num)
            if not p:
                continue
            label = ("mobile" if ("CELL" in ptypes or "MOBILE" in ptypes)
                     else "main" if "MAIN" in ptypes
                     else "office" if "WORK" in ptypes else None)
            if p not in [x["phone"] for x in out["phones"]]:
                out["phones"].append({"phone": p, "label": label})
    if not out["name"] and n_name:
        out["name"] = tidy(n_name)
    return out


# ---------------------------------------------------------------- people

@dataclass
class PersonHit:
    name: str
    title: str | None = None
    email: str | None = None
    phone: str | None = None
    method: str = "card"          # jsonld | vcard | card | known_name | email_name
    source_url: str = ""
    phone_label: str | None = None
    other_phones: list = field(default_factory=list)   # [{phone, label}]
    known: str | None = None      # the roster name this matched, if any
    bio_url: str | None = None

    def has_data(self) -> bool:
        return bool(self.title or self.email or self.phone)


_METHOD_ORDER = {"vcard": 0, "jsonld": 1, "card": 2, "known_name": 3, "email_name": 4}
_PHONE_RANK = {"direct": 0, "mobile": 1, None: 2, "office": 3, "main": 4, "toll_free": 5}


def _pick_phone(phones: list[dict]) -> tuple[str | None, str | None, list]:
    if not phones:
        return None, None, []
    ordered = sorted(phones, key=lambda r: _PHONE_RANK.get(r["label"], 2))
    return ordered[0]["phone"], ordered[0]["label"], ordered[1:]


def email_fits(email: str, name: str) -> bool:
    """Public name for the pattern test below, for the job's cross-page pass."""
    return _email_fits(email, name)


def _email_fits(email: str, name: str) -> bool:
    """Does the local part fit this person's name under a known pattern? The
    page name, its nicknames and middle names are all tried, so jim.smith@
    fits James Smith."""
    local = email.split("@", 1)[0].lower()
    p = _parts(name)
    if not p:
        return False
    first, middles, last = p
    lasts = {last.replace("'", ""), last.replace("-", "").replace("'", "")}
    lasts |= set(last.split("-"))
    givens = {first} | set(middles) | NICKNAMES.get(first, set())
    for g in givens:
        g = re.sub(r"[^a-z]", "", g)
        for la in lasts:
            la = re.sub(r"[^a-z]", "", la)
            if g and la and _detect(g, la, local):
                return True
    return False


def _jsonld_people(html_text: str, url: str) -> list[PersonHit]:
    out = []
    for m in re.finditer(r"""<script[^>]+type\s*=\s*["']application/ld\+json["'][^>]*>(.*?)</script>""",
                         html_text, re.I | re.S):
        try:
            data = json.loads(m.group(1).strip(), strict=False)
        except (ValueError, TypeError):
            continue
        stack = [data]
        seen = 0
        while stack and seen < 500:
            seen += 1
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(node)
                continue
            if not isinstance(node, dict):
                continue
            typ = node.get("@type")
            types = typ if isinstance(typ, list) else [typ]
            if "Person" in types:
                name = normalise_name(node.get("name") if isinstance(node.get("name"), str) else
                                      " ".join(str(node.get(k, "")) for k in ("givenName", "familyName")))
                jt = node.get("jobTitle")
                if isinstance(jt, list):
                    jt = jt[0] if jt else None
                title = clean_title(jt, name) if isinstance(jt, str) else None
                em = node.get("email")
                em = em[0] if isinstance(em, list) and em else em
                email = None
                if isinstance(em, str) and "@" in em:
                    email = _clean_addr(em.replace("mailto:", "")).lower()
                tel = node.get("telephone")
                tel = tel[0] if isinstance(tel, list) and tel else tel
                phone = _norm_phone(tel) if isinstance(tel, str) else None
                if name:
                    out.append(PersonHit(name=name, title=title, email=email, phone=phone,
                                         method="jsonld", source_url=url))
            for k in ("@graph", "employee", "employees", "member", "members", "founder",
                      "founders", "author", "mainEntity", "itemListElement", "item",
                      "worksFor", "about"):
                v = node.get(k)
                if isinstance(v, (list, dict)):
                    stack.append(v)
    return out


def _depth(el) -> int:
    d = 0
    p = el.getparent()
    while p is not None:
        d += 1
        p = p.getparent()
    return d


def _alike(a, b) -> bool:
    """Scrapling's find_similar rule, applied between two candidates: same
    depth, same tag, same parent and grandparent tags (checked by the caller's
    bucketing), and attributes at least 20% alike, ignoring href and src. The
    rule is reimplemented rather than called because here it runs pairwise
    over a few name candidates instead of over the whole document per element."""
    aa = {k: v for k, v in a.attrib.items() if k not in ("href", "src")}
    ba = {k: v for k, v in b.attrib.items() if k not in ("href", "src")}
    if not aa and not ba:
        return True
    if not aa or not ba:
        return False
    score = sum(SequenceMatcher(None, v, ba.get(k, "")).ratio() for k, v in aa.items())
    return round(score / max(len(aa), len(ba)), 2) >= 0.2


@dataclass
class _Cand:
    el: object
    name: str
    inline_title: str | None


def _candidates(doc: _Doc) -> list[_Cand]:
    """Elements whose whole text is a person-shaped name (optionally followed
    by a title), deepest element only, page chrome (nav, header, footer)
    excluded."""
    cands: dict = {}
    for el in doc.els:
        tag = el.tag.lower() if isinstance(el.tag, str) else ""
        if tag not in NAME_TAGS or doc.in_chrome(el):
            continue
        p, e = doc.pos[el], doc.end[el]
        ls = doc.lines_in(p, e)
        if not ls or sum(len(t) for _, t in ls) > 160:
            continue
        own = tidy(" ".join(el.itertext()))   # adjacent spans are separate words
        if not own or len(own) > 120:
            continue
        name, title = split_name_title(own)
        if not name:
            continue
        cands[el] = _Cand(el, name, title)
    # keep the deepest element carrying the name
    for el in list(cands):
        par = el.getparent()
        while par is not None:
            if par in cands and cands[par].name == cands[el].name:
                del cands[par]
            par = par.getparent()
    return sorted(cands.values(), key=lambda c: doc.pos[c.el])


def _groups(doc: _Doc, cands: list[_Cand]) -> list[list[_Cand]]:
    buckets: dict = defaultdict(list)
    for c in cands:
        par = c.el.getparent()
        gp = par.getparent() if par is not None else None
        key = (_depth(c.el), c.el.tag, par.tag if par is not None else None,
               gp.tag if gp is not None else None)
        buckets[key].append(c)
    groups = []
    for items in buckets.values():
        clusters: list[list[_Cand]] = []
        for c in items:
            for cl in clusters:
                if _alike(cl[0].el, c.el):
                    cl.append(c)
                    break
            else:
                clusters.append([c])
        for cl in clusters:
            names = {x.name for x in cl}
            if len(names) >= 2:
                groups.append(cl)
    return groups


def _card(doc: _Doc, el, member_count: Counter, max_chars: int = 1500):
    """The largest ancestor of a name element that holds no other member of
    its group: that is the person's card."""
    best = el
    cur = el.getparent()
    while cur is not None and isinstance(cur.tag, str) and cur.tag.lower() not in ("body", "html"):
        if member_count[cur] > 1 or doc.text_len(cur) > max_chars:
            break
        best = cur
        cur = cur.getparent()
    return best


def _region_contacts(doc: _Doc, start: int, end: int, site_domain: str):
    """Emails, phones and links inside one card region. The site's header,
    footer and menus are left out unless the card itself sits there: on a
    short bio page the container can reach the footer, and the founder's
    address printed there is not this person's."""
    skip = start not in doc.chrome
    lines = [t for _, t in doc.lines_in(start, end, skip_chrome=skip)]
    emails: list[str] = []
    tels: list[tuple[str, str]] = []
    links: list[tuple[str, str]] = []
    for p, el, href, _ in doc.anchors_in(start, end):
        if skip and p in doc.chrome:
            continue
        h = href.strip()
        hl = h.lower()
        if hl.startswith("mailto:"):
            for part in unquote(h[7:]).split("?", 1)[0].split(","):
                if "@" in part:
                    emails.append(_clean_addr(part).lower())
        elif hl.startswith("tel:"):
            tels.append((h, _anchor_label(doc, el)))
        elif "/cdn-cgi/l/email-protection#" in hl:
            d = decode_cfemail(h.split("#", 1)[1])
            if d and "@" in d:
                emails.append(_clean_addr(d).lower())
        elif hl and not hl.startswith(("#", "javascript:")):
            links.append((urljoin(doc.url, h), tidy(el.text_content())))
    for p, cf in doc.cfemails:
        if start <= p <= end and not (skip and p in doc.chrome):
            d = decode_cfemail(cf)
            if d and "@" in d:
                emails.append(_clean_addr(d).lower())
    blob = "\n".join(lines)
    for m in EMAIL_RE.finditer(blob):
        emails.append(_clean_addr(f"{m.group(1)}@{m.group(2)}").lower())
    for m in OBFUSCATED_BRACKET_RE.finditer(blob):
        if any(c in m.group(0) for c in "[({"):
            emails.append(_clean_addr(f"{m.group(1)}@{m.group(2)}.{m.group(3)}").lower())
    personal = []
    for e in dict.fromkeys(emails):
        ok, cat, _ = classify(e, site_domain)
        if ok and cat == "personal":
            personal.append(e)
    return lines, personal, _phones_from(lines, tels), links


def _title_near(lines: list[str], name: str, inline: str | None,
                before: list[str]) -> str | None:
    if inline:
        return inline
    idx = None
    for i, t in enumerate(lines):
        if name.lower() in t.lower() or normalise_name(t) == name:
            idx = i
            break
        n2, _ = split_name_title(t)
        if n2 == name:
            idx = i
            break
    if idx is not None:
        n2, t2 = split_name_title(lines[idx])
        if t2:
            return t2
        rest = re.sub(re.escape(name), " ", lines[idx], flags=re.I)
        if rest.strip(_TITLE_SEPS) and len(rest) < 90:
            t3 = clean_title(rest, name)
            if t3:
                return t3
        for t in lines[idx + 1: idx + 4]:
            if normalise_name(t):
                break       # the next person's name: this card had no title
            got = clean_title(t, name) if len(t) <= 120 else None
            if got:
                return got
    for t in reversed(before[-2:]):
        if len(t) <= 60:
            got = clean_title(t, name)
            if got:
                return got
    return None


def _best_email(personal: list[str], name: str, others=()) -> str | None:
    """The card's own address: one that fits the name, else the only personal
    address in the card, unless that one fits somebody else's name (a deal
    page naming its partner contact, an assistant's card)."""
    if not personal:
        return None
    for e in personal:
        if _email_fits(e, name):
            return e
    if len(personal) != 1:
        return None
    me = contacts.name_key(name)
    if any(_email_fits(personal[0], o) for o in others if contacts.name_key(o) != me):
        return None
    return personal[0]


def _brand_name(name: str, site_domain: str | None) -> bool:
    """A name that starts with the firm's own brand word is a product, fund or
    portfolio company: "Alterna Aviation" on alternacapital.com. A person
    whose whole name is the domain (jackkeeter.com) is kept."""
    label = (site_domain or "").split(".")[0].lower()
    p = _parts(name)
    if not label or not p:
        return False
    return len(p[0]) >= 4 and p[0] in label and p[2] not in label


_BIO_LINK_TEXT = re.compile(r"\b(bio|biography|profile|read more|learn more|view|meet"
                            r"|more about|full bio|about)\b", re.I)


def _person_from_region(doc: _Doc, el, name: str, inline: str | None, start: int,
                        end: int, site_domain: str, url: str, method: str,
                        others=()) -> PersonHit:
    """One person read from the region [start, end] around their name element:
    title from the lines after the name (or just before it), the address that
    fits them, their best phone, and a link to their bio page."""
    _lines, personal, phones, links = _region_contacts(doc, start, end, site_domain)
    name_pos = doc.pos[el]
    after = [t for p, t in doc.lines_in(name_pos, end)]
    before = [t for p, t in doc.lines_in(start, name_pos - 1)] if name_pos > start else []
    title = _title_near(after, name, inline, before)
    email = _best_email(personal, name, others)
    phone, label, more_phones = _pick_phone(phones)
    bio = None
    page = urlparse(url)
    for href, text in links:
        pu = urlparse(href)
        if pu.scheme not in ("http", "https"):
            continue
        if registrable(pu.netloc) != registrable(page.netloc):
            continue
        if pu.path.rstrip("/") == page.path.rstrip("/"):
            continue
        if normalise_name(text) or _BIO_LINK_TEXT.search(text or "") or not text:
            bio = href
            break
    return PersonHit(name=name, title=title, email=email, phone=phone, method=method,
                     source_url=url, phone_label=label, other_phones=more_phones, bio_url=bio)


def _card_people(doc: _Doc, site_domain: str, url: str, known_names: list[str]) -> tuple[list[PersonHit], set]:
    cands = _candidates(doc)
    groups = _groups(doc, cands)
    out: list[PersonHit] = []
    used: set = set()
    for grp in groups:
        members = [c.el for c in grp]
        count: Counter = Counter()
        for m in members:
            a = m.getparent()
            while a is not None:
                count[a] += 1
                a = a.getparent()
        hits = []
        ordered = sorted(grp, key=lambda c: doc.pos[c.el])
        peers = [c.name for c in ordered]
        for i, c in enumerate(ordered):
            card = _card(doc, c.el, count)
            if card is c.el:
                # Members share every ancestor (one paragraph, <br> between
                # people): the region runs to the next member, at most a few lines.
                par = c.el.getparent()
                start = doc.pos[c.el]
                stop = doc.pos[ordered[i + 1].el] - 1 if i + 1 < len(ordered) else (
                    doc.end[par] if par is not None else doc.end[c.el])
                ls = doc.lines_in(start, stop)
                if len(ls) > 6:
                    stop = ls[6][0] - 1
                end = stop
            else:
                start, end = doc.pos[card], doc.end[card]
            hits.append(_person_from_region(doc, c.el, c.name, c.inline_title,
                                            start, end, site_domain, url, "card",
                                            peers + known_names))
        # A group counts only if it looks like a team: some members carry a
        # title, a personal email, or a name we already know.
        good = sum(1 for h in hits if h.title or h.email or match_known(h.name, known_names))
        if good == 0 or good * 3 < len(hits):
            continue
        # Then each member needs evidence of its own: the same heading style
        # is often reused for "Proven Track Record" right next to the people.
        for h, c in zip(hits, ordered):
            if not (h.title or h.email or h.phone or match_known(h.name, known_names)
                    or _bio_slug_fits(h.bio_url, h.name)):
                continue
            out.append(h)
            used.add(c.el)
    return out, used


def _bio_slug_fits(url: str | None, name: str) -> bool:
    """A card's link goes to a page named after the person (/team/jane-doe)."""
    if not url:
        return False
    p = _parts(name)
    if not p:
        return False
    slug = urlparse(url).path.lower()
    last = p[2].replace("'", "")
    return last in slug and (p[0] in slug or p[0][:1] + last in slug)


def _bio_person(doc: _Doc, site_domain: str, url: str, known_names: list[str],
                used: set) -> PersonHit | None:
    """A single-person page: its first main heading is a name."""
    heads = [el for el in doc.els if isinstance(el.tag, str)
             and el.tag.lower() in ("h1", "h2") and not doc.in_chrome(el)]
    h1s = [h for h in heads if h.tag.lower() == "h1"]
    for el in (h1s[:2] or heads[:2]):
        if el in used:
            continue
        name, inline = split_name_title(el.text_content())
        if not name:
            continue
        # the bio container: climb while it stays a modest block of text
        card = el
        cur = el.getparent()
        while cur is not None and isinstance(cur.tag, str) and cur.tag.lower() not in ("body", "html"):
            if doc.text_len(cur) > 4000:
                break
            card = cur
            cur = cur.getparent()
        start = doc.pos[card]
        end = doc.end[card]
        h = _person_from_region(doc, el, name, inline, start, end, site_domain, url,
                                "card", known_names)
        if h.title or h.email or match_known(name, known_names):
            return h
    return None


def _variant_regex(full: str) -> re.Pattern | None:
    p = _parts(full)
    if not p:
        return None
    first, middles, last = p
    givens = {first} | NICKNAMES.get(first, set()) | set(m for m in middles if len(m) > 1)
    giv = "|".join(sorted((re.escape(g) for g in givens if g), key=len, reverse=True))
    la = re.escape(last).replace("\\-", "[\\-\\s]?")
    mid = r"(?:\s+[A-Za-z]\.?|\s+[A-Za-z][a-z]+\.?|\s+[\"'(][A-Za-z]+[\"')]){0,2}"
    return re.compile(rf"(?<![A-Za-z])(?:{giv}){mid}\s+{la}(?![a-z])"
                      rf"|(?<![A-Za-z]){la},\s*(?:{giv})(?![a-z])", re.I)


def _known_people(doc: _Doc, site_domain: str, url: str, known_names: list[str],
                  have: set[str]) -> list[PersonHit]:
    out = []
    rxs = {k: _variant_regex(k) for k in known_names}
    for full in known_names:
        if contacts.name_key(full) in have:
            continue
        rx = rxs.get(full)
        if rx is None:
            continue
        best = None
        for i, (p, t) in enumerate(doc.lines):
            m = rx.search(t)
            if not m:
                continue
            heading = len(t) <= len(m.group(0)) + 60
            if best is None or (heading and not best[2]):
                best = (i, p, heading, m.group(0))
            if heading:
                break
        if best is None:
            continue
        i, p, heading, shown = best
        shown_name = normalise_name(shown) or full
        if not heading:
            out.append(PersonHit(name=shown_name, method="known_name", source_url=url, known=full))
            continue
        el = doc.els[p]
        # card: climb while the block stays small and names nobody else we know
        card = el
        cur = el.getparent()
        while cur is not None and isinstance(cur.tag, str) and cur.tag.lower() not in ("body", "html"):
            if doc.text_len(cur) > 900:
                break
            blob = " ".join(t for _, t in doc.lines_in(doc.pos[cur], doc.end[cur]))
            if any(k != full and r is not None and r.search(blob) for k, r in rxs.items()):
                break
            card = cur
            cur = cur.getparent()
        if doc.in_chrome(el):
            start, end = p, p
        else:
            start, end = doc.pos[card], doc.end[card]
        h = _person_from_region(doc, el, shown_name, None, start, end, site_domain,
                                url, "known_name", known_names)
        h.known = full
        out.append(h)
    return out


def _vcard_hit(text: str, url: str, known_names: list[str]) -> list[PersonHit]:
    v = parse_vcard(text)
    name = normalise_name(v["name"]) if v["name"] else None
    if not name:
        return []
    email = None
    for e in v["emails"]:
        ok, cat, _ = classify(e)
        if ok and cat == "personal":
            email = e
            break
    phone, label, others = _pick_phone(v["phones"])
    return [PersonHit(name=name, title=clean_title(v["title"], name) if v["title"] else None,
                      email=email, phone=phone, phone_label=label, other_phones=others,
                      method="vcard", source_url=url, known=match_known(name, known_names))]


def extract_people(html: str, url: str, known_names: list[str] | None = None,
                   site_domain: str | None = None) -> list[PersonHit]:
    """People on one page, merged so each person appears once.

    `known_names` are roster display names ("Jane A. Doe"); a page name that
    fits one is tagged with it in `known`. A vCard body is recognised and read
    as one."""
    known_names = known_names or []
    if not html:
        return []
    site_domain = site_domain or registrable(urlparse(url).netloc)
    if html.lstrip()[:20].upper().startswith("BEGIN:VCARD"):
        return _vcard_hit(html, url, known_names)

    hits: list[PersonHit] = _jsonld_people(html, url)
    hits = [h for h in hits if h.has_data() or match_known(h.name, known_names)]
    doc = _Doc(html, url)
    if doc.root is not None:
        cards, used = _card_people(doc, site_domain, url, known_names)
        hits += cards
        bio = _bio_person(doc, site_domain, url, known_names, used)
        if bio:
            hits.append(bio)
        have = {contacts.name_key(match_known(h.name, known_names) or h.name) for h in hits}
        hits += _known_people(doc, site_domain, url, known_names, have)

    merged: dict[str, PersonHit] = {}
    for h in hits:
        h.known = h.known or match_known(h.name, known_names)
        if not h.known and h.method != "vcard" and _brand_name(h.name, site_domain):
            continue
        key = contacts.name_key(h.known or h.name)
        if not key:
            continue
        cur = merged.get(key)
        if cur is None:
            merged[key] = h
            continue
        if _METHOD_ORDER[h.method] < _METHOD_ORDER[cur.method]:
            h, cur = cur, h
            merged[key] = cur
        cur.title = cur.title or h.title
        cur.email = cur.email or h.email
        if not cur.phone and h.phone:
            cur.phone, cur.phone_label = h.phone, h.phone_label
        elif h.phone and h.phone != cur.phone and h.phone not in [o["phone"] for o in cur.other_phones]:
            cur.other_phones.append({"phone": h.phone, "label": h.phone_label})
        cur.bio_url = cur.bio_url or h.bio_url

    # Personal addresses nobody has claimed: attribute by name pattern.
    emails = extract_emails(html, site_domain, _text=doc.text() if doc.root is not None else None)
    claimed = {h.email for h in merged.values() if h.email}
    people = list(merged.values())
    candidates = [(h.known or h.name, h) for h in people]
    candidates += [(k, None) for k in known_names
                   if contacts.name_key(k) not in merged]
    for e in emails:
        if e["category"] != "personal" or e["email"] in claimed:
            continue
        fits = [(n, h) for n, h in candidates if _email_fits(e["email"], n)
                or (h is not None and _email_fits(e["email"], h.name))]
        keys = {contacts.name_key(n) for n, _ in fits}
        if len(keys) != 1:
            continue
        n, h = fits[0]
        if h is not None:
            if not h.email:
                h.email = e["email"]
                claimed.add(e["email"])
        else:
            hit = PersonHit(name=n, email=e["email"], method="email_name", source_url=url,
                            known=n)
            merged[contacts.name_key(n)] = hit
            candidates = [(x, y) for x, y in candidates if contacts.name_key(x) != contacts.name_key(n)]
            candidates.append((n, hit))
            claimed.add(e["email"])
    return list(merged.values())


def visible_text(html: str) -> str:
    """The page's visible text, one block per line. Used by the crawler to
    decide whether a page is an empty JavaScript shell."""
    doc = _Doc(html)
    if doc.root is None:
        return _visible_text_cheap(html or "")
    return doc.text()
