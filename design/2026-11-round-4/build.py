"""Build a self-contained offline gallery of native Qt design comparisons."""
import json
from pathlib import Path
import struct
import sidebar_options
from render import OPTIONS

HERE = Path(__file__).resolve().parent

def image(name):
    path = HERE / 'images' / (name + '.png')
    blob = path.read_bytes()
    assert blob[:8] == b'\x89PNG\r\n\x1a\n'
    width, height = struct.unpack('>II', blob[16:24])
    return dict(src='images/' + path.name, width=width, height=height)

NAV = {}
for key, (layout, style, tight) in OPTIONS.items():
    NAV[key] = dict(name=key.upper() + ' · ' + ('Arrows beside month' if layout == 'split' else 'Range, then buttons')
        + ' · ' + style + (' · compact' if tight else ''),
        description=('Same placement as H3/H4/H7: Previous beside the month, Today beside the range, Next fixed after the reserved label column.'
                     if layout == 'split' else 'H5/H6 with the requested swap: range first, then Previous/Today/Next on the lower line. Month and range share the first gridline.'),
        style=style, tight=tight, layout=layout,
        images={f'{locale}-{size}-{month}': image(f'{key}-{locale}-{size}-{month}')
                for locale in ('en_GB', 'de_DE') for size in ('wide', 'narrow') for month in (5, 9)},
        full={size: image(f'{key}-en_GB-{size}-9-full') for size in ('wide', 'narrow')})
SIDEBARS = {key: dict(item, images={f'{state}-{scale}': image(f'{key}-{state}-{scale}')
    for state in ('agenda', 'query') for scale in (100, 140)}) for key, item in sidebar_options.OPTIONS.items()}
APPLIED = {key: dict(name=label, image=image('applied-' + key)) for key, label in {
    'week': 'App · week view', 'search': 'App · search with expanded series',
    'resting': 'S3 · resting agenda', 'search-empty': 'S3 · first click, ready to type',
    'query': 'S3 · query and occurrence focus', 'reordered': 'Calendars · reordered, with read-only lock',
    'preferences': 'Preferences → Appearance', 'cards': 'A1 · event cards',
    'grouped': 'A2 · grouped by day', 'timeline': 'A3 · per-day timeline (default)',
}.items()}
DATA = dict(nav=NAV, sidebar=SIDEBARS, applied=APPLIED, metrics=json.loads((HERE / 'metrics.json').read_text()))
HTML = r'''<!doctype html>
<html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Debacal · design round 4</title>
<style>
:root { color-scheme:light; --ink:#27313a; --muted:#596570; --edge:#cbd3da; --accent:#475f52; }
* { box-sizing:border-box; } body { margin:0; color:var(--ink); background:#f5f7f8; font:16px/1.5 system-ui,sans-serif; }
header,main,footer { max-width:1600px; margin:auto; padding:24px 32px; } header { padding-bottom:0; }
h1 { font-size:32px; line-height:1.2; letter-spacing:-.6px; margin:6px 0 14px; } h2 { font-size:24px; margin:0 0 12px; } h3 { margin:0 0 8px; font-size:17px; }
p { margin:0 0 12px; max-width:96ch; } .muted,small { color:var(--muted); } a { color:var(--accent); text-underline-offset:3px; }
nav { display:flex; flex-wrap:wrap; gap:24px; padding:8px 0; } section { margin:12px 0 48px; scroll-margin-top:20px; }
button,select,input { font:inherit; accent-color:var(--accent); } button,select { border:1px solid var(--edge); border-radius:5px; color:var(--ink); background:white; padding:7px 10px; min-height:38px; max-width:100%; }
button { cursor:pointer; } button[aria-pressed=true] { background:#e7eee8; border-color:var(--accent); } :focus-visible { outline:3px solid #426a9d; outline-offset:3px; }
.tools { display:flex; flex-wrap:wrap; gap:12px 22px; align-items:center; margin:16px 0; } label { display:flex; flex-wrap:wrap; align-items:center; gap:8px; font-size:14px; }
.two { display:grid; grid-template-columns:1fr 1fr; gap:18px; } article { min-width:0; } .card { padding:18px; background:white; border:1px solid var(--edge); border-radius:7px; }
ul { padding-left:20px; margin:8px 0; } li { margin:5px 0; } .note { padding:14px 18px; margin:16px 0; border-left:3px solid #82998b; background:#eaf0ec; }
figure { margin:0; min-width:0; border:1px solid var(--edge); background:white; border-radius:6px; overflow:hidden; }
figcaption { padding:9px 12px; border-bottom:1px solid var(--edge); display:flex; justify-content:space-between; gap:14px; font-size:14px; }
.scroll { overflow:auto; background:white; } .photo { position:relative; width:100%; } .photo img { display:block; max-width:100%; width:auto; height:auto; margin:auto; }
.photo svg { position:absolute; inset:0; width:100%; height:100%; pointer-events:none; } .photo line { stroke:#2f6ea9; stroke-width:1; stroke-dasharray:5 4; }
.actual .photo { width:max-content; } .actual .photo img { max-width:none; margin:0; } .caption { padding:12px; border-top:1px solid #e0e5e8; font-size:14px; } .caption p:last-child { margin-bottom:0; }
#nav-stage { display:grid; gap:20px; } .pair { display:grid; grid-template-columns:1fr 1fr; gap:14px; }
.sidebar-stage { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:18px; align-items:start; }
.sidebar-stage .photo { width:300px; max-width:100%; margin:auto; } .sidebar-stage figcaption { display:block; min-height:76px; }
.sidebar-stage strong { display:block; } .measure { font:12px/1.5 ui-monospace,monospace; color:#526b80; }
#applied-stage figure { max-width:1442px; } .agenda-previews { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:18px; }
.agenda-previews img { display:block; width:300px; max-width:100%; height:auto; margin:auto; }
footer { border-top:1px solid var(--edge); font-size:14px; color:var(--muted); } code { background:#e8eeea; padding:2px 5px; border-radius:3px; }
[hidden] { display:none!important; } @media(max-width:1000px) { .pair,.two { grid-template-columns:1fr; } .sidebar-stage { grid-template-columns:repeat(2,minmax(0,1fr)); } }
@media(max-width:620px) { header,main,footer { padding:18px 14px; } .sidebar-stage,.agenda-previews { grid-template-columns:1fr; } h1 { font-size:27px; } }
</style>
<header><small>DEBACAL · DESIGN ROUND 4 · 7 NOVEMBER 2026</small>
<h1>Fixed navigation, cleaner choices, aligned sidebar</h1>
<p>Decided behavior is applied to the app. The navigation geometry, control treatment and sidebar edges below remain mockups for comparison. Every screenshot comes from Qt widgets with synthetic calendar data.</p>
<nav aria-label="Review sections"><a href="#applied">Applied changes</a><a href="#navigation">N1–N6 · Navigation & controls</a><a href="#alignment">L1–L3 · Sidebar alignment</a></nav></header>
<main><section id="applied"><h2>Applied now</h2><div class="two">
<article class="card"><h3>Search and agenda</h3><ul><li>S3 on-demand search: click the search icon or press Ctrl+F. The field immediately receives focus.</li><li>A query stays visible while navigating results. An empty search returns to “Next events” after leaving its controls.</li><li>Preferences → Appearance → Upcoming events: A1 event cards, A2 day groups, or A3 per-day timeline. A3 is the default.</li><li>Search retains source-colored cards and native series folding in every agenda style.</li><li>Day headings align with the outer edge; timeline rails stop at day boundaries.</li></ul></article>
<article class="card"><h3>Calendar list</h3><ul><li>Read-only calendars show a small lock and an explanatory tooltip.</li><li>Drag a calendar’s name or color square to reorder. A line previews the insertion position.</li><li>The order is saved per profile and survives restarting. Reordering preserves the calendar used for creating events.</li><li>The chosen calendar retains its bold underline, without an extra transient focus border.</li><li>The applied header keeps its existing layout while the alternatives below are reviewed.</li></ul></article></div>
<div class="tools"><label>Screenshot <select id="applied-choice"></select></label><label><input id="applied-actual" type="checkbox">100% image size</label></div><div id="applied-stage"></div>
<details><summary>Compare the three saved agenda preferences</summary><div id="agenda-previews" class="agenda-previews"></div></details>
</section>
<section id="navigation"><h2>Navigation & controls · N1–N6</h2>
<p>May and September are shown together using the <strong>same reserved label width</strong>. The Next button never moves when the month changes. Compare the visible empty space after May with the alternative that puts all navigation after the range.</p>
<div class="tools"><label>Option <select id="nav-choice"></select></label><label>Window <select id="nav-size"><option value="wide">1440 × 960</option><option value="narrow">1030 × 690</option></select></label><label>Month language <select id="nav-locale"><option value="en_GB">English</option><option value="de_DE">German</option></select></label></div>
<div class="tools"><label><input id="nav-compare" type="checkbox" checked>Show the other button style below</label><label><input id="nav-guides" type="checkbox">Show first-gridline / Next guides</label><label><input id="nav-actual" type="checkbox">100% image size</label></div>
<div id="nav-stage"></div>
<div class="note"><strong>How I would choose:</strong> N1/N3 retain the preferred H3/H4/H7 geometry; N3 reduces the title and controls together. Their short-month gap is the visible cost of keeping Next fixed. N5 removes that gap and keeps navigation together, with range-before-buttons order. The underlined styles are quieter, but need the rule below the toolbar for separation. N2/N4/N6 give a fairer filled comparison: selection reaches the group outline, with no white moat, inner border or unequal inset.</div>
</section>
<section id="alignment"><h2>Sidebar alignment · L1–L3</h2>
<p>These alternatives answer whether source controls, headings, search and event frames need one shared edge. None adds hierarchy through arbitrary heading indentation. Compare the resting A3 agenda and an expanded search result, since a timeline cannot define search structure.</p>
<div class="tools"><label>State <select id="sidebar-state"><option value="agenda">Next events · A3</option><option value="query">Search · expanded series</option></select></label><label>Text size <select id="sidebar-scale"><option value="100">100%</option><option value="140">140%</option></select></label><label><input id="sidebar-guides" type="checkbox">Show measured edges</label></div>
<div id="sidebar-stage" class="sidebar-stage"></div>
<div class="note"><strong>Tradeoff:</strong> L1 is closest to the applied A3. Its date headings and source squares align, while event frames start after the clock column. L2 gives every frame a common outer edge but puts more line work inside cards. L3 treats the inset as a real utility lane, with the cost of narrower search and title space. I would compare L1 and L2 first; L3 is useful if a common text edge matters more than horizontal room.</div>
</section></main>
<footer>Offline review; no external resources. Samples use an in-memory store, fixed dates and disabled sync. Mockup code is isolated from the application. Open a screenshot in a new tab to inspect it at native size.</footer>
<script>
const data = DATA_JSON;
const $ = id => document.getElementById(id);
function photo(item, title, guides=[], full=null) {
 const lines=guides.map(x=>`<line x1="${x}" x2="${x}" y1="0" y2="${item.height}"/>`).join('');
 return `<figure><figcaption><strong>${title}</strong><a href="${full?full.src:item.src}" target="_blank" rel="noopener">${full?'Full app':'Open image'}</a></figcaption><div class="scroll"><div class="photo"><img src="${item.src}" width="${item.width}" height="${item.height}" alt="${title}">${lines?`<svg viewBox="0 0 ${item.width} ${item.height}" aria-hidden="true">${lines}</svg>`:''}</div></div></figure>`;
}
function options(id, entries) { for(const [key,label] of entries) { const o=document.createElement('option');o.value=key;o.textContent=label;$(id).append(o); } }
function applied(){const item=data.applied[$('applied-choice').value];$('applied-stage').classList.toggle('actual',$('applied-actual').checked);$('applied-stage').innerHTML=photo(item.image,item.name);}
function navigation(){
 const key=$('nav-choice').value, selected=data.nav[key],size=$('nav-size').value,locale=$('nav-locale').value;
 const keys=[key];if($('nav-compare').checked)keys.push('n'+(Number(key.slice(1))+(selected.style==='underlined'?1:-1)));
 $('nav-stage').classList.toggle('actual',$('nav-actual').checked);
 $('nav-stage').innerHTML=keys.map(k=>{const n=data.nav[k];return `<article><h3>${n.name}</h3><p>${n.description}</p><div class="pair">`+[5,9].map(month=>{
 const suffix=`${locale}-${size}-${month}`,m=data.metrics[k][suffix],img=n.images[suffix];
 const guides=$('nav-guides').checked?[m.first_column,m.next+14]:[];
 return `<div>${photo(img,(month===5?(locale==='de_DE'?'Mai':'May'):'September')+' 2026',guides,month===9&&locale==='en_GB'?n.full[size]:null)}<div class="caption measure">Next x=${m.next}px · reserved label=${m.title_width}px · month text=${m.text_width}px · controls ${m.inline?'on the same row':'below the period'}</div></div>`;
 }).join('')+'</div></article>';}).join('');
}
function sidebars(){const state=$('sidebar-state').value,scale=$('sidebar-scale').value; $('sidebar-stage').innerHTML=Object.entries(data.sidebar).map(([key,item])=>{
 const m=data.metrics[key][`${state}-${scale}`], guides=$('sidebar-guides').checked?[m.source_square,m.event_frame]:[];
 return `<article>${photo(item.images[`${state}-${scale}`],item.name,guides)}<div class="caption"><p>${item.thesis}</p><p class="muted">${item.tradeoff}</p><p class="measure">Source ${m.source_square}px · search ${m.search_field}px · frame ${m.event_frame}px${m.day_heading===undefined?'':` · day heading ${m.day_heading}px`}</p></div></article>`;
 }).join('');}
options('applied-choice',Object.entries(data.applied).map(([key,x])=>[key,x.name]));options('nav-choice',Object.entries(data.nav).map(([key,x])=>[key,x.name]));
for(const id of ['applied-choice','applied-actual'])$(id).addEventListener('change',applied);
for(const id of ['nav-choice','nav-size','nav-locale','nav-compare','nav-guides','nav-actual'])$(id).addEventListener('change',navigation);
for(const id of ['sidebar-state','sidebar-scale','sidebar-guides'])$(id).addEventListener('change',sidebars);
$('agenda-previews').innerHTML=['cards','grouped','timeline'].map(k=>photo(data.applied[k].image,data.applied[k].name)).join('');
applied();navigation();sidebars();
</script></html>'''
(HERE / 'index.html').write_text(HTML.replace('DATA_JSON', json.dumps(DATA)), encoding='utf-8')
print('Built', HERE / 'index.html')
