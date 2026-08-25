"""How to use: one continuous document.

Design rules, after two rejected attempts:

  - It reads top to bottom. No cards, no boxes, no grid of tiles. Sections are
    separated by space and a hairline, the way a well set document is.
  - One narrow reading column (about 68 characters) because that is what prose
    wants, with the right hand space earning its keep as a contents list that
    tracks the current section rather than sitting empty.
  - Type carries the hierarchy: large serif section headings, comfortable body,
    quiet labels. Colour appears only where it means something.
  - Prose with inline links, not a wall of buttons.
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from .webapp import PAGE_CSS, conn, nav

router = APIRouter()

GUIDE_CSS = """
/* The document and the page title share one centred measure, so the heading,
   the prose and the contents list all hang off the same two edges however wide
   the window gets. Left anchoring a fixed column just parks a dead gutter on
   the right. */
header{max-width:1020px;margin:0 auto;padding:44px 30px 0}
header h1{font-size:38px;letter-spacing:-.028em}
.doc{display:grid;grid-template-columns:minmax(0,700px) 200px;gap:60px;
max-width:1020px;margin:0 auto;padding:6px 30px 160px;align-items:start}
@media (max-width:1180px){.doc{grid-template-columns:minmax(0,1fr);gap:0}
 .toc{display:none}}
.doc .lede{font-size:19.5px;line-height:1.6;color:var(--soft);
letter-spacing:-.008em;margin:18px 0 2px}
.doc section{padding:40px 0 0;margin-top:40px;scroll-margin-top:22px;
border-top:1px solid var(--rule)}
.doc h2{font:600 27px/1.25 Georgia,"Times New Roman",serif;letter-spacing:-.02em;
margin:0 0 14px;color:var(--ink)}
.doc h3{font:600 15px/1.4 "Segoe UI",system-ui,sans-serif;margin:26px 0 8px;
color:var(--ink)}
.doc p{font-size:15.5px;line-height:1.72;color:var(--soft);margin:0 0 14px}
.doc p strong,.doc li strong{color:var(--ink);font-weight:600}
.doc ol,.doc ul{margin:0 0 14px;padding:0;list-style:none;
counter-reset:step}
.doc ol li{counter-increment:step;position:relative;padding:0 0 12px 34px;
font-size:15.5px;line-height:1.7;color:var(--soft)}
.doc ol li::before{content:counter(step);position:absolute;left:0;top:1px;
width:22px;height:22px;border-radius:99px;background:var(--red-bg);
color:var(--red-hi);font:600 11.5px/22px "Segoe UI",sans-serif;
text-align:center}
.doc ul li{position:relative;padding:0 0 11px 34px;font-size:15.5px;
line-height:1.7;color:var(--soft)}
.doc ul li::before{content:"";position:absolute;left:9px;top:11px;width:5px;
height:5px;border-radius:99px;background:var(--faint)}
.doc a{color:var(--ink);text-decoration:none;
box-shadow:inset 0 -1px 0 0 var(--rule2)}
.doc a:hover{box-shadow:inset 0 -1px 0 0 var(--red-hi);color:var(--red-hi)}
.doc .note{border-left:2px solid var(--rule2);padding:2px 0 2px 18px;
margin:0 0 14px;font-size:14.5px;line-height:1.65;color:var(--faint)}
.doc .eyebrow{font:600 10.5px/1 "Segoe UI",sans-serif;letter-spacing:.14em;
text-transform:uppercase;color:var(--red-hi);margin:0 0 10px}
kbd{display:inline-block;min-width:21px;padding:1px 6px;border-radius:5px;
border:1px solid var(--rule2);border-bottom-width:2px;background:var(--card);
font:12px/1.5 Consolas,monospace;color:var(--ink);text-align:center}
.keys{margin:0 0 14px}
.keys div{display:flex;gap:14px;align-items:baseline;padding:7px 0;
border-bottom:1px solid var(--rule);font-size:14.5px;color:var(--soft)}
.keys div:last-child{border-bottom:0}
.keys span.k{flex:none;min-width:132px}
.swatch{display:inline-block;width:9px;height:9px;border-radius:99px;
vertical-align:baseline;margin-right:7px}
.toc{position:sticky;top:26px}
.toc .t{font:600 10.5px/1 "Segoe UI",sans-serif;letter-spacing:.14em;
text-transform:uppercase;color:var(--faint);margin:0 0 12px;padding-left:13px}
.toc a{display:block;padding:5px 0 5px 13px;font-size:13px;color:var(--faint);
text-decoration:none;border-left:2px solid transparent;line-height:1.4}
.toc a:hover{color:var(--soft)}
.toc a.on{color:var(--ink);border-left-color:var(--red)}
"""

TOC_JS = """
<script>
(function(){
  var secs = Array.prototype.slice.call(document.querySelectorAll('.doc section[id]'));
  var links = {};
  Array.prototype.forEach.call(document.querySelectorAll('.toc a'), function(a){
    links[a.getAttribute('href').slice(1)] = a;
  });
  function spy(){
    var best = secs[0], line = 130;
    for (var i = 0; i < secs.length; i++) {
      if (secs[i].getBoundingClientRect().top <= line) best = secs[i];
    }
    for (var id in links) links[id].className = '';
    if (best && links[best.id]) links[best.id].className = 'on';
  }
  window.addEventListener('scroll', spy, {passive:true});
  window.addEventListener('resize', spy);
  spy();
})();
</script>
"""

SECTIONS = [
    ("what", "What this is"),
    ("daily", "Your morning"),
    ("inbox", "Reading the inbox"),
    ("firms", "Finding firms"),
    ("outreach", "Emails and export"),
    ("lists", "Your lists"),
    ("call", "Preparing for a call"),
    ("system", "Keeping it healthy"),
    ("weekly", "The weekly pull"),
    ("numbers", "Reading the numbers"),
    ("ops", "Starting and stopping"),
]


@router.get("/guide", response_class=HTMLResponse)
def guide():
    c = conn()
    snaps = c.execute("SELECT COUNT(*) n FROM snapshot WHERE source_key='adv_feed'"
                      ).fetchone()["n"]
    c.close()

    live = ("Live weekly triggers are on: the inbox now mixes fresh events with "
            "older archive ones." if snaps >= 2 else
            "Live weekly triggers switch on with the second feed capture. Until "
            "then most events are archive derived and rank low on purpose.")

    toc = "".join(f'<a href="#{sid}">{title}</a>' for sid, title in SECTIONS)

    return HTMLResponse(f"""<!doctype html><meta charset="utf-8">
<title>How to use</title><style>{PAGE_CSS}{GUIDE_CSS}</style>
{nav("guide")}
<header><h1>How to use</h1></header>
<div class="doc">
<div>

<p class="lede">Bellwether reads what every US registered investment adviser files
with the SEC and turns it into two ranked call lists: one for Prairie Hill, one
for AcuBooth. Nothing here was bought. Every number traces back to a filing, and
every number that needs a caveat carries one.</p>

<section id="what">
<h2>What this is</h2>
<p>Over 40,000 investment advisers are registered in the United States: about
17,000 with the SEC and another 20,000 with their states, who dominate the 25
to 100 million dollar range. Bellwether reads both weekly feeds, narrows the
whole universe to the few hundred firms worth your time, explains why each one
made the list, and tells you when something changes at a firm that makes today
the right day to call.</p>
<p>Three things it will never do. It will not show you a number without its
caveat. It will not treat missing data as bad news. And it will not quietly
change a list without saying so on
<a href="/health">Pipeline health</a>.</p>
</section>

<section id="daily">
<p class="eyebrow">Ten minutes, every morning</p>
<h2>Your morning</h2>
<p>Open the <a href="/">Trigger inbox</a> and work down from the top. The list is
already sorted so the strongest signal is first, and each row is written to be
acted on without clicking anything.</p>
<ol>
<li>Read the row. If it deserves a call, open the firm and press
<strong>Copy to clipboard</strong> for the prep sheet.</li>
<li>Press <kbd>d</kbd> when it is handled, <kbd>s</kbd> to see it again later,
<kbd>x</kbd> if it is noise.</li>
<li>Stop when the top of the list stops being interesting. The tail is ordered,
not urgent.</li>
</ol>
<p class="note">{live}</p>
</section>

<section id="inbox">
<h2>Reading the inbox</h2>
<p>The bar on the left of each row is its priority, and its colour is the whole
point.</p>
<p><span class="swatch" style="background:var(--ok)"></span>
<strong>Green</strong> means a lead. The longer the bar, the stronger the signal
and the more recent the event.</p>
<p><span class="swatch" style="background:var(--red-hi)"></span>
<strong>Red</strong> means a disqualifier. A firm that left Schwab cannot take
AcuBooth, so the correct action is removing it from your list, not calling it.</p>
<p>Split the queue between people with the product filter, either
<a href="/?product=PHH">Prairie Hill only</a> or
<a href="/?product=ACUBOOTH">AcuBooth only</a>. Once a filter combination earns
its keep, save it and it appears in the sidebar for good.</p>
<p>Firms you have starred float to a separate strip above everything else, so a
watched firm never gets lost behind a filter.</p>
</section>

<section id="firms">
<h2>Finding firms</h2>
<p>The <a href="/firms">Firms</a> section is where you find, filter, and export.
Pick a <strong>preset</strong> from the dropdown, then narrow with the filters or
the search box.</p>
<ul>
<li><strong>PHH, Tier A</strong>: the Prairie Hill top hundred, a real high net
worth book and proven appetite for illiquid funds, with no competing product of
their own. Each row shows its score.</li>
<li><strong>PHH, Intersection</strong>: firms whose clients already buy private
funds <em>and</em> already hold real estate income stocks, both objections
pre cleared.</li>
<li><strong>AcuBooth, Tier C</strong>: ranked by how many accounts a single yes
could produce. It ranks who is worth the free holdings analysis, never sellable
accounts, which are held away and appear in no filing.</li>
<li><strong>Competitors and sponsors</strong>: firms running their own real
estate funds. Intelligence, not pipeline.</li>
<li><strong>All firms</strong>, or one of your own lists.</li>
</ul>
<p>Two tabs show that set two ways. <strong>Firms</strong> is one row per firm;
<strong>Contacts</strong> is one row per person, the mail merge list. Search by
name or CRD from the box, or press <kbd>Ctrl</kbd><kbd>K</kbd> from anywhere.
Every view exports, and any firm can be dropped into one of your lists with the
control on its row.</p>
</section>

<section id="outreach">
<h2>Emails and export</h2>
<p>The <a href="/firms?view=contacts">Contacts</a> tab is one row per decision
maker: a person, their best email, and a phone, built for a mail merge. It draws
from three places and labels every one, so a real address is never confused with
a guess.</p>
<ul>
<li><strong>Their website</strong> and <strong>filed at firm</strong> are real:
scraped from the firm's own team page, or printed in its brochure. A firm inbox
like info@ is labelled as such, never pinned to a person's name.</li>
<li><strong>Inferred</strong> is a guess, one per person. Bellwether learns the
pattern each firm uses for its own people (if jsmith@ is real, the CEO follows
the same shape) and applies that single most likely pattern, not a menu of three
that mostly bounce.</li>
</ul>
<p>Every address carries a domain check: <strong>domain ok</strong> means the
domain can receive mail, <strong>dead domain</strong> means it cannot and you
should skip it. None of it proves a specific mailbox exists, and there is no free
way to confirm one (the paid tools connect to mail servers on a port cloud hosts
block), so a clean send and a watch for bounces is the method.</p>
<p>Filter however you like, then <strong>Export contacts</strong>. The sheet is
one row per person, everyone at a firm grouped together, with their title, email,
status, and phone, kept as text so Excel does not mangle them.</p>
</section>

<section id="lists">
<h2>Your lists</h2>
<p>The presets are built in. <a href="/lists">Lists</a> are the ones you build
yourself, like playlists: a hand-picked bucket of firms. Add a firm to a list
from the control on its row in Firms, or create an empty one first. Open a list
to work it as a filtered Firms view, and export its firms or contacts like any
other set. Deleting a list never touches the firms in it.</p>
</section>

<section id="call">
<h2>Preparing for a call</h2>
<p>Open any firm by clicking its name, or press <kbd>Ctrl</kbd><kbd>K</kbd> and
type a few letters of it from anywhere.</p>
<ol>
<li>Read down the page. Scale and client mix, then the real estate verdict
<strong>with the four numbers that produced it</strong>, then funds, holdings,
the firm's own brochure sentences, its people, and its history.</li>
<li>Press <strong>Copy to clipboard</strong> for a prep sheet with the caveats
written in, ready to paste anywhere.</li>
<li>Star the firm to watch it, set its status and owner so nobody duplicates your
work, and write what happened into the notes.</li>
</ol>
<p><strong>How to reach them</strong> at the top shows the filed and scraped
contact details; the chart is the firm's regulatory AUM back to 2011 with real
axes, hover a point for the filing behind it; and the people are the officers and
reps we hold, with a button to guess and check one email each.</p>
</section>

<section id="system">
<h2>Keeping it healthy</h2>
<p>The <a href="/health">System</a> section has two tabs. <strong>Pipeline
health</strong> answers whether the data is current and whether anything broke:
red is failed or stale, amber is flagged or due, no colour is healthy. The
background jobs live here too, each with why it exists and its own Start and
Pause. Nothing here costs money to run.</p>
<p><strong>Review queue</strong> asks for the occasional judgement the system
will not make on its own: whether an uncertain firm match is really the same
firm, and whether a brochure sentence is really a negation. Both are two buttons
and both are safe to skip. An unreviewed row never deletes a signal; it only
holds its confidence lower until somebody looks, so the queue can sit for
months.</p>
</section>

<section id="weekly">
<p class="eyebrow">One glance, once a week</p>
<h2>The weekly pull runs itself</h2>
<p>The SEC publishes a fresh adviser file every week and keeps no archive of it.
Bellwether pulls it automatically: a scheduler checks every half hour whether a
new week is due and runs the whole cycle when it is, including catching up the
moment your PC comes back on after a missed week. You do not run anything.</p>
<p>Your only job is a weekly glance at <a href="/health">Pipeline health</a>:
the top panel says when the pull last ran, and the page shows red for failed or
stale, amber for flagged or due. A flagged row means the SEC changed something
upstream, and the message on the row says what. The manual button still exists
for the rare day you want a pull immediately.</p>
</section>

<section id="numbers">
<h2>Reading the numbers</h2>
<p>Anything with a dotted underline has a caveat attached. Hover it. Five are
worth knowing by heart.</p>
<ul>
<li><strong>Filed versus guessed contacts.</strong> Anything marked
<em>filed</em> came from the firm's own Form ADV or brochure and is real.
Pattern-guessed emails are labelled guesses and never dressed up as more.</li>
<li><strong>Schwab share</strong> counts only custodians holding ten percent or
more of a firm's managed accounts, so it is an upper bound. It flags the late
2026 institutional opportunity and never means accounts you can sell today.</li>
<li><strong>Estimated client size</strong> divides by clients, not accounts. A
household with four accounts counts once, so real account size is likely two to
four times smaller.</li>
<li><strong>Archive derived</strong> means the figure comes from historical
filings ending December 2024. Every firm has amended since. Treat it as a
baseline to compare against, never as today.</li>
<li><strong>No 13F holdings</strong> is not bad news. The reporting threshold is
a hundred million in listed equities, so most firms this size never have to
file.</li>
</ul>
</section>

<section id="ops">
<h2>Starting and stopping</h2>
<p>There is nothing to start. Bellwether comes up by itself when you sign in to
Windows, with no window and no browser tab, so it is simply there when you open
it. The <strong>Bellwether</strong> shortcut on your desktop opens it, and starts
it first if it somehow is not running.</p>
<p>To stop it, use <strong>Quit Bellwether</strong> at the bottom of the sidebar.
That is the only way to stop it, and it is almost never necessary. Quitting stops
background jobs too, and they resume from where they stopped.</p>
<p>To turn off starting at sign in, open Task Manager, go to Startup apps, and
switch Bellwether off. To back everything up, copy this folder: the database is
<strong>prospect.db</strong> and the raw SEC captures live in
<strong>data/snapshots</strong>.</p>
<p>Nothing here phones home. Nothing leaves your machine except requests to the
SEC, and you can confirm the state of every stage on
<a href="/health">Pipeline health</a>.</p>
</section>

</div>
<nav class="toc"><div class="t">On this page</div>{toc}</nav>
</div>
{TOC_JS}""")
