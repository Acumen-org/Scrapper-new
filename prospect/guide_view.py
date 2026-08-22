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
    ("who", "Choosing who to call"),
    ("call", "Preparing for a call"),
    ("dinner", "Planning a dinner"),
    ("review", "Clearing reviews"),
    ("weekly", "The weekly pull"),
    ("autopilot", "Autopilot"),
    ("numbers", "Reading the numbers"),
    ("keys", "Keyboard"),
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

<section id="who">
<h2>Choosing who to call</h2>
<p>Four lists, in descending order of how much they have already proven.</p>
<h3>The intersection</h3>
<p><a href="/lists?tab=intersection">Twenty one firms</a> whose clients already
buy private funds <em>and</em> already hold real estate income stocks. Both hard
objections, illiquidity and the asset class itself, are pre cleared. Sorted by
position size, because a six million dollar position and a four thousand dollar
one are not the same evidence. Rows marked <strong>possible legacy</strong> are a
single small holding that may just have been inherited.</p>
<h3>Tier A</h3>
<p>The <a href="/lists?tab=working">Prairie Hill top hundred</a>: a real high net
worth book, proven appetite for illiquid funds, and no competing real estate
product of their own. Every score shows the four components that produced it, so
you can disagree with a rank and see exactly why it landed there.</p>
<h3>Tier C</h3>
<p>The <a href="/lists?tab=tierc">AcuBooth list</a>, ranked by how many accounts a
single yes could produce. Read the banner: this ranks who is worth the free
holdings analysis, and it can never show sellable accounts, because those accounts
are held away and appear in no filing anywhere.</p>
<h3>Sponsors</h3>
<p><a href="/lists?tab=sponsors">Competitors</a>, with their fundraising progress
where a Form D exists. Intelligence, not pipeline.</p>
</section>

<section id="call">
<h2>Preparing for a call</h2>
<p>Open any firm by clicking its name, or press <kbd>Ctrl</kbd><kbd>K</kbd> and
type a few letters of it from anywhere in the app.</p>
<ol>
<li>Read down the page. Scale and client mix, then the real estate verdict
<strong>with the four numbers that produced it</strong>, then funds, holdings,
the firm's own brochure sentences, its people, and its history.</li>
<li>Press <strong>Copy to clipboard</strong>. You get a prep sheet with the
caveats written in, ready to paste anywhere.</li>
<li>Star the firm to watch it, set its status and owner so nobody duplicates
your work, and write what happened into the notes.</li>
</ol>
<p>Three things on that page are worth knowing about. <strong>How to reach
them</strong> shows only details the firm itself filed: its main office phone
from Form ADV, and the emails and numbers printed on its own brochure, each
marked <em>filed</em>. The chart is the firm's regulatory AUM going back to
2011 with real axes, so a growth claim on a call can be specific; hover any
point for the filing behind it. And the people are real registered
representatives from the SEC individual feed. Pattern-guessed emails still
exist as a fallback, but they are labelled as guesses and kept visually apart
from anything filed.</p>
</section>

<section id="dinner">
<h2>Planning a dinner</h2>
<p>The format needs ten to twelve qualified firms within driving distance of one
room, which a nationally ranked list cannot answer.</p>
<ol>
<li>Open <a href="/lists?tab=geo">Geography</a> and pick a state. The columns
show how many tier A, intersection, and tier C firms sit there.</li>
<li>Pick a city. Firms cluster by metro, and the zip prefix tells you how tight
that cluster really is.</li>
<li>Work the list, intersection firms first. Export it as CSV for the invitations
when the shape looks right.</li>
</ol>
</section>

<section id="review">
<h2>Clearing reviews</h2>
<p>The <a href="/review">review queue</a> asks for judgement the system will not
make on its own. Two kinds, both two buttons, and both safe to skip.</p>
<p><strong>Is this the same firm?</strong> An SEC filer matched an adviser by
name, but not confidently enough to merge. Confirm only when you are sure: a
wrong link puts holdings a firm does not own into a call opener, which is the one
error that costs credibility rather than time.</p>
<p><strong>Is this really a negation?</strong> A brochure sentence pairs one of
our phrases with words like "do not". Read it. If the firm truly says it does not
do this, the tag flips to an explicit negative signal, which is useful in its own
right.</p>
<p class="note">An unreviewed row never deletes a signal. It only holds its
confidence lower until somebody looks. The queue can sit for months without
breaking anything downstream.</p>
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

<section id="autopilot">
<h2>Autopilot</h2>
<p>Some work takes days rather than minutes: reading eight thousand brochures,
extracting the contact details printed in them, reading every firm's website
for its team, refetching current filings for the flagged firms. Those run as
background jobs on <a href="/health">Pipeline health</a>, each with a sentence
on why it exists, a progress bar, and its own <strong>Start</strong> and
<strong>Pause</strong>.</p>
<p>Jobs run in small slices and check their state between each one, so pausing
takes effect within seconds and never requires killing anything, and a job that
finishes pauses itself. Nothing here costs money: every job talks only to the
SEC, to advisers' own websites, or to a DNS resolver.</p>
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

<section id="keys">
<h2>Keyboard</h2>
<div class="keys">
<div><span class="k"><kbd>Ctrl</kbd><kbd>K</kbd></span>
<span>Jump to any firm by name or CRD, from any screen</span></div>
<div><span class="k"><kbd>j</kbd> <kbd>k</kbd></span>
<span>Move down and up the inbox</span></div>
<div><span class="k"><kbd>d</kbd></span><span>Mark the selected row done</span></div>
<div><span class="k"><kbd>s</kbd></span><span>Snooze it</span></div>
<div><span class="k"><kbd>x</kbd></span><span>Dismiss it</span></div>
<div><span class="k"><kbd>Enter</kbd></span>
<span>Open the selected row's firm</span></div>
</div>
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
