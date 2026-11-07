"""Build the offline applied-changes and remaining-choices review."""
import json
from pathlib import Path
import struct

from fixture import ROOT
import header_options
import search_options
import agenda_options

HERE = Path(__file__).resolve().parent


def image(name):
    path = HERE / 'images' / (name + '.png')
    data = path.read_bytes()
    assert data[:8] == b'\x89PNG\r\n\x1a\n', path
    width, height = struct.unpack('>II', data[16:24])
    return dict(path='images/' + path.name, width=width, height=height)


HEADERS = {}
for number, key in enumerate([f'{layout}-{style}' for layout in header_options.LAYOUTS
                              for style in header_options.STYLES] + ['split-filled-tight'], 1):
    layout, style, *tight = key.split('-')
    HEADERS[key] = dict(id=f'H{number}', layout=layout, style=style, tight=bool(tight),
        name=('Tighter split layout' if tight else header_options.LAYOUTS[layout]['name']) + ' · ' + header_options.STYLES[style]['name'],
        description=header_options.LAYOUTS[layout]['detail'],
        treatment=header_options.STYLES[style]['detail'],
        density='20px title and 28px controls; same two-row alignment, 10px less height.' if tight else '22px title and 32px controls; explicit label and control rows.',
        images={size: image(f'header-{key}-{size}') for size in ('wide', 'narrow')},
        full={size: image(f'header-{key}-{size}-full') for size in ('wide', 'narrow')})

APPLIED = {
    'neutral-week': dict(name='Week · Neutral', description='Applied app: distinct source controls and event cards, visible section boundaries, muted weekend text, continuous columns, and no extra all-day creation strip.'),
    'neutral-month': dict(name='Month', description='The same source hues, selected-event markers, and theme roles in the month view.'),
    'neutral-year': dict(name='Year', description='The year overview follows the selected interface scheme and subtle weekend labels.'),
    'neutral-narrow': dict(name='Smaller window', description='The applied app at 1030 × 690. Navigation and command-group placement remain the existing baseline while the header alternatives below are reviewed.'),
    'neutral-search': dict(name='Search with a series expanded', description='Production source-colored result cards retain native selection, focus, and folding. No timeline is applied to search.'),
    'warm-week': dict(name='Week · Warm', description='A real saved Warm preference, with warmer source-panel and interface colors. Source calendar hues remain unchanged.'),
    'cool-week': dict(name='Week · Cool', description='A real saved Cool preference. The timed grid remains unshaded; calendar source colors remain unchanged.'),
    'preferences': dict(name='Appearance preferences', description='Color scheme is a persisted profile preference: Neutral, Warm, or Cool. Text size, language, and date/time formats remain independent.'),
    'warm-import': dict(name='Import preview · Warm', description='Interface scheme also reaches the import preview, source field, date inclusion markers, and controls.'),
}
for key, item in APPLIED.items():
    item['image'] = image('applied-' + key)

SEARCH = {}
for number, (key, item) in enumerate(search_options.OPTIONS.items(), 1):
    SEARCH[key] = dict(item, id=f'S{number}', images={f'{state}-{scale}': image(f'search-{key}-{state}-{scale}')
        for state in ('agenda', 'query') for scale in (100, 140)})
AGENDA = {}
for number, (key, item) in enumerate(agenda_options.OPTIONS.items(), 1):
    AGENDA[key] = dict(item, id=f'A{number}', images={state: image(f'agenda-{key}-{state}') for state in ('agenda', 'query')})

DATA = dict(applied=APPLIED, headers=HEADERS, search=SEARCH, agenda=AGENDA,
            metrics=json.loads((HERE / 'metrics.json').read_text())['headers'])

HTML = '''<!doctype html>
<html lang="en">
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Debacal — applied decisions and remaining choices</title>
<style>
  :root { color-scheme:light; --ink:#27313a; --muted:#596570; --line:#d2dce0; --accent:#475f52; --bg:#f5f7f8; }
  * { box-sizing:border-box; }
  body { margin:0; color:var(--ink); background:var(--bg); font:16px/1.5 system-ui,sans-serif; }
  header,main,footer { max-width:1560px; margin:auto; padding:24px 32px; }
  header { padding-bottom:8px; }
  h1 { margin:5px 0 14px; font-size:32px; line-height:1.2; letter-spacing:-.7px; }
  h2 { font-size:23px; margin:0 0 12px; letter-spacing:-.3px; }
  h3 { font-size:17px; margin:0 0 8px; }
  p { margin:0 0 12px; max-width:94ch; }
  a { color:var(--accent); text-underline-offset:3px; }
  .muted,small { color:var(--muted); }
  .eyebrow { color:var(--accent); font-weight:650; font-size:13px; letter-spacing:.03em; }
  nav { display:flex; flex-wrap:wrap; gap:10px 24px; padding:12px 0; }
  button,select,input { font:inherit; accent-color:var(--accent); }
  button,select { background:white; color:var(--ink); border:1px solid var(--line); border-radius:6px; padding:7px 12px; min-height:38px; }
  button { cursor:pointer; }
  button:hover { border-color:#7b9385; background:#eef3ef; }
  button[aria-pressed="true"] { background:#e2ece4; border-color:var(--accent); box-shadow:inset 0 0 0 1px var(--accent); }
  :focus-visible { outline:3px solid #426a9d; outline-offset:3px; }
  section { margin:28px 0 44px; scroll-margin-top:20px; }
  .tools { display:flex; flex-wrap:wrap; align-items:center; gap:12px 24px; margin:16px 0; }
  .tools label { display:flex; align-items:center; gap:8px; font-size:14px; }
  .tools .group { display:flex; align-items:center; gap:6px; }
  .tools .group > span { color:var(--muted); font-size:14px; margin-right:4px; }
  .card { border:1px solid var(--line); border-radius:8px; background:white; padding:18px; }
  .implemented { display:grid; grid-template-columns:1fr 1fr; gap:24px; margin:18px 0; }
  ul { margin:8px 0; padding-left:20px; }
  li { margin:6px 0; }
  figure { margin:0; min-width:0; background:white; border:1px solid var(--line); border-radius:8px; overflow:hidden; }
  figcaption { padding:10px 14px; border-bottom:1px solid var(--line); display:flex; align-items:center; justify-content:space-between; gap:12px; font-size:14px; }
  .scroll { overflow:auto; background:white; }
  .photo { position:relative; width:100%; }
  .photo img { display:block; max-width:100%; width:auto; height:auto; margin:auto; }
  .photo svg { position:absolute; inset:0; width:100%; height:100%; pointer-events:none; }
  .photo svg line { stroke:#2f6ea9; stroke-width:1.1; stroke-dasharray:6 4; }
  .actual .photo { width:max-content; }
  .actual .photo img { max-width:none; margin:0; }
  .actual .scroll { max-height:960px; }
  .caption { border-top:1px solid var(--line); padding:14px 16px; font-size:14px; }
  .caption p:last-child { margin-bottom:0; }
  .matrix { width:100%; border-collapse:collapse; margin:18px 0; }
  .matrix th,.matrix td { padding:10px 12px; text-align:left; vertical-align:top; border-bottom:1px solid var(--line); }
  .matrix th { color:var(--muted); font-size:14px; }
  .matrix td:first-child { width:38%; font-size:14px; }
  .matrix button { width:100%; text-align:left; font-size:14px; }
  #header-stage { display:grid; gap:20px; }
  .sidebars { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:16px; align-items:start; }
  .sidebars.agenda { grid-template-columns:repeat(3,minmax(0,1fr)); }
  .sidebars figcaption { display:block; min-height:86px; }
  .sidebars figcaption strong { display:block; margin-bottom:6px; }
  .decision-note { padding:16px 20px; background:#eaf0ec; border-left:3px solid #82998b; margin:18px 0; }
  .measure { color:#526b80; font-size:13px; }
  code { background:#e8eeea; border-radius:4px; padding:2px 5px; font-size:.9em; }
  footer { border-top:1px solid var(--line); font-size:14px; color:var(--muted); }
  [hidden] { display:none !important; }
  @media(max-width:1300px) { .sidebars { grid-template-columns:repeat(2,minmax(0,1fr)); } }
  @media(max-width:900px) { header,main,footer { padding:20px 16px; } .implemented { grid-template-columns:1fr; } .sidebars.agenda { grid-template-columns:1fr; } .matrix th,.matrix td { padding:8px 6px; } }
  @media(max-width:580px) { .sidebars { grid-template-columns:1fr; } h1 { font-size:28px; } .matrix td:first-child { width:30%; } }
</style>
<header>
  <div class="eyebrow">DEBACAL · DESIGN DECISIONS · 7 NOVEMBER 2026</div>
  <h1>Applied changes, then focused choices</h1>
  <p>The clear decisions are now in the app. Header/navigation, search placement, and possible day grouping remain separate experiments. The images below use the updated application with synthetic events.</p>
  <nav aria-label="Review sections"><a href="#applied">Applied app</a><a href="#headers">H1–H7 · Header</a><a href="#search">S1–S4 · Search</a><a href="#agenda">A1–A3 · Agenda</a></nav>
</header>
<main>
  <section id="applied">
    <h2>Applied to the app</h2>
    <div class="implemented">
      <article class="card"><h3>Sidebar and calendar</h3><ul><li>Larger source-color squares; the creation calendar is bold and underlined, without a selection box.</li><li>Source-tinted event cards, with readable text, distinct selection/focus, and no timeline.</li><li>Clear overview / calendars / agenda-search regions, using B’s pane and separators.</li><li>Muted weekend text with no shaded weekend columns.</li><li>Continuous week/day boundaries, including pinned all-day and timed lanes with either scrollbar.</li><li>No repeated + All day row or hover plus. Existing creation gestures remain.</li></ul></article>
      <article class="card"><h3>Preferences and scope</h3><ul><li>Preferences → Appearance → Color scheme: Neutral, Warm, or Cool.</li><li>The choice persists per profile and applies to calendar views, overview, editor/import previews, and date pickers.</li><li>Server calendar colors remain source data and are preserved.</li><li>Individual icon actions are borderless; selected segments no longer have a second inner outline.</li><li>Header placement retains the existing app baseline pending the choices below.</li><li>Search stays above the results heading until a placement is chosen.</li></ul><p class="muted">Full suite: 1,328 tests and 135 subtests passed, including the launch/import tests requiring local IPC sockets.</p></article>
    </div>
    <div class="tools"><label>Applied screenshot <select id="applied-choice"></select></label><div class="group"><button id="applied-fit" aria-pressed="true">Fit</button><button id="applied-actual" aria-pressed="false">100%</button></div></div>
    <div id="applied-stage"></div>
  </section>

  <section id="headers">
    <h2>Header choices · H1–H7</h2>
    <p>The first six cross three navigation layouts with two selected-state treatments. All keep the command groups together and use A/B-style vertical alignment. H7 separately tests tighter spacing while retaining those alignment rows.</p>
    <div id="header-matrix"></div>
    <div class="tools"><label>Window <select id="header-size"><option value="wide">1440 × 960</option><option value="narrow">1030 × 690</option></select></label><label>Comparison <select id="header-compare"><option value="style">Other control treatment</option><option value="none">Only selected option</option><option value="spacing">H3 versus H7 spacing</option></select></label><label><input id="guide" type="checkbox"> First-column guide</label><div class="group"><button id="header-fit" aria-pressed="true">Fit</button><button id="header-actual" aria-pressed="false">100%</button></div></div>
    <div id="header-stage"></div>
    <div class="decision-note"><strong>How I would compare these:</strong> H3/H4 answer the month-alignment and selected-style questions directly. H1/H2 preserve the familiar navigation cluster. H5/H6 keep that cluster together while aligning only the month; the range is indented. H7 tests whether a smaller title and tighter controls improve the vertical balance. Today’s placement is deliberately visible in every option; none is silently chosen for the app.</div>
  </section>

  <section id="search">
    <h2>Search placement · S1–S4</h2>
    <p>Compare both the resting agenda and an active query. These options preserve Ctrl+F, source visibility, filtering, series folding, and the main calendar’s width.</p>
    <div class="tools"><label>State <select id="search-state"><option value="agenda">Next events</option><option value="query">Searching “work”</option></select></label><label>Text size <select id="search-scale"><option value="100">100%</option><option value="140">140%</option></select></label></div>
    <div id="search-stage" class="sidebars"></div>
    <div class="decision-note">S1 is the applied baseline. S2 saves a row with shorter Agenda/Results captions. S3 gives the resting agenda a cleaner heading but requires a click or Ctrl+F to expose search. S4 makes search look global, at the cost of distancing the field from its results. My preference is to compare S2’s persistent field with S3’s quieter resting state.</div>
  </section>

  <section id="agenda">
    <h2>Day structure, with or without a timeline · A1–A3</h2>
    <p>The app currently uses A1. These additional experiments test the day-break issue before deciding whether any timeline belongs in Next events. A2/A3 deliberately revert to ordinary event cards during search.</p>
    <div class="tools"><label>State <select id="agenda-state"><option value="agenda">Next events</option><option value="query">Search: ordinary cards in every option</option></select></label></div>
    <div id="agenda-stage" class="sidebars agenda"></div>
    <div class="decision-note">Day headings can make dates easier to scan without a timeline. A3 demonstrates short rails that stop at day boundaries, rather than joining unrelated days. Both add structure and consume space; A1 remains a reasonable choice if consistency and compactness matter more.</div>
  </section>
</main>
<footer><p>All images are native Qt captures with synthetic events and an in-memory database. The fixture is frozen to 6 November 2026; no personal calendars or accounts are involved. Header/search/agenda prototypes are isolated from production code. <a href="README.txt">Reproduce the review</a> · <a href="metrics.json">Header measurements</a></p><p>The guide is a review annotation, not an app decoration. Dense week titles still truncate; prototype layouts are candidates for further testing before adoption. All palettes currently use light backgrounds.</p></footer>
<script>
const DATA=__DATA__;
const $=id=>document.getElementById(id);
const esc=value=>String(value).replace(/[&<>"']/g,char=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
function photo(img,extra=''){return `<div class="scroll"><div class="photo"><img src="${img.path}" width="${img.width}" height="${img.height}" alt="Synthetic Debacal design example">${extra}</div></div>`;}
function applied(){const item=DATA.applied[$('applied-choice').value];$('applied-stage').innerHTML=`<figure><figcaption><strong>${esc(item.name)} · applied app</strong><a href="${item.image.path}" target="_blank" rel="noopener">Full size ↗</a></figcaption>${photo(item.image)}<div class="caption">${esc(item.description)}</div></figure>`;}
for(const [key,item] of Object.entries(DATA.applied))$('applied-choice').add(new Option(item.name,key));
$('applied-choice').addEventListener('change',applied);
let selected='split-filled';
const parts=location.hash.slice(1).split('/');
if(DATA.headers[parts[0]])selected=parts[0];
const layouts=['compact','split','range'];
$('header-matrix').innerHTML='<table class="matrix"><thead><tr><th>Navigation</th><th>Filled choices, one outline</th><th>Underlines + header separator</th></tr></thead><tbody>'+layouts.map(layout=>'<tr><td>'+esc(DATA.headers[layout+'-filled'].name.split(' · ')[0])+'</td>'+['filled','underlined'].map(style=>{const key=layout+'-'+style,item=DATA.headers[key];return `<td><button data-header="${key}" aria-pressed="false">${item.id} · ${esc(style==='filled'?'Filled':'Underlined')}</button></td>`;}).join('')+'</tr>').join('')+'</tbody></table><button data-header="split-filled-tight" aria-pressed="false">H7 · H3 with tighter vertical spacing</button>';
function header(){
  const size=$('header-size').value,comparison=$('header-compare').value;
  let keys=[selected];
  if(comparison==='spacing')keys=['split-filled','split-filled-tight'];
  else if(comparison==='style'){const item=DATA.headers[selected];keys=item.tight?[selected,'split-underlined']:[item.layout+'-filled',item.layout+'-underlined'];}
  document.querySelectorAll('[data-header]').forEach(button=>button.setAttribute('aria-pressed',String(button.dataset.header===selected)));
  $('header-stage').innerHTML=keys.map(key=>{
    const item=DATA.headers[key],img=item.images[size],m=DATA.metrics[key][size];
    const guide=$('guide').checked?`<svg viewBox="0 0 ${img.width} ${img.height}" aria-hidden="true"><line x1="${m.first_column}" x2="${m.first_column}" y1="0" y2="${img.height}"/></svg>`:'';
    return `<figure><figcaption><strong>${item.id} · ${esc(item.name)}</strong><a href="${item.full[size].path}" target="_blank" rel="noopener">Whole window ↗</a></figcaption>${photo(img,guide)}<div class="caption"><p>${esc(item.description)}</p><p>${esc(item.treatment)} ${esc(item.density)}</p><p class="measure">Month offset from first column: ${m.month-m.first_column}px.</p></div></figure>`;
  }).join('');
}
document.querySelectorAll('[data-header]').forEach(button=>button.addEventListener('click',()=>{selected=button.dataset.header;if(DATA.headers[selected].tight)$('header-compare').value='spacing';else if($('header-compare').value==='spacing')$('header-compare').value='style';header();}));
['header-size','guide'].forEach(id=>$(id).addEventListener('change',header));
$('header-compare').addEventListener('change',()=>{if($('header-compare').value==='spacing')selected='split-filled-tight';header();});
function search(){const state=$('search-state').value+'-'+$('search-scale').value;$('search-stage').innerHTML=Object.values(DATA.search).map(item=>{const img=item.images[state];return `<figure><figcaption><strong>${item.id} · ${esc(item.name)}</strong><a href="${img.path}" target="_blank" rel="noopener">Full size ↗</a></figcaption>${photo(img)}<div class="caption"><p>${esc(item.thesis)}</p><p class="muted">${esc(item.tradeoff)}</p><p>${esc(item.title_meaning)}</p></div></figure>`;}).join('');}
['search-state','search-scale'].forEach(id=>$(id).addEventListener('change',search));
function agenda(){const state=$('agenda-state').value;$('agenda-stage').innerHTML=Object.values(DATA.agenda).map(item=>{const img=item.images[state];return `<figure><figcaption><strong>${item.id} · ${esc(item.name)}</strong><a href="${img.path}" target="_blank" rel="noopener">Full size ↗</a></figcaption>${photo(img)}<div class="caption"><p>${esc(item.thesis)}</p><p class="muted">${esc(item.tradeoff)}</p><p>${esc(item.search_behavior)}</p></div></figure>`;}).join('');}
$('agenda-state').addEventListener('change',agenda);
function scaling(prefix){for(const actual of [false,true])$(prefix+(actual?'-actual':'-fit')).addEventListener('click',()=>{$(prefix+'-stage').classList.toggle('actual',actual);$(prefix+'-fit').setAttribute('aria-pressed',String(!actual));$(prefix+'-actual').setAttribute('aria-pressed',String(actual));});}
scaling('applied');scaling('header');applied();header();search();agenda();
</script>
</html>'''

(HERE / 'index.html').write_text(HTML.replace('__DATA__', json.dumps(DATA).replace('</', '<\\/')))
print('Review built: applied screenshots, H1–H7, S1–S4, and A1–A3.')
