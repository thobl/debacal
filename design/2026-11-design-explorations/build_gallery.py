"""Build the offline review from the native captures and explicit tradeoffs."""
import json
from pathlib import Path
import struct

HERE = Path(__file__).resolve().parent

VARIANTS = {
    'sources': {
        'name': 'Sources & agenda',
        'idea': 'Different things get different visual forms.',
        'description': 'Calendar sources are compact controls with square swatches and a current-target check. Events are dated cards arranged on a timeline.',
        'cost': 'More component styles and a 312px sidebar. Cards spend more height on each result; the time column takes width from long titles.',
        'weekend': 'Warm wash + muted umber labels',
        'color': '#8b6254', 'tint': '#f8f1eb',
        'header': 'Two explicit alignment rows; labels above controls',
        'grid': 'Open date header; grid begins with event-bearing rows',
        'all_day': 'An integrated + appears on hover/focus in an empty all-day cell',
        'selection': 'Creation target: check + outlined control. Event: source-colored card outline.',
    },
    'shared': {
        'name': 'Shared list language',
        'idea': 'Treat both lists as one navigation system.',
        'description': 'Calendars and events share a slate navigation pane, aligned source bars, flat rows, and a common selected-row treatment. Dividers separate roles.',
        'cost': 'Fast, regular scanning, but similar selected rows can suggest that choosing a source and choosing an event mean the same thing.',
        'weekend': 'Cool wash + slate-blue labels',
        'color': '#546c83', 'tint': '#eef3f8',
        'header': 'Two explicit alignment rows; lightly outlined actions',
        'grid': 'Continuous columns through the date header and event rows',
        'all_day': 'Persistent + All day labels: more discoverable, at a 20px height cost',
        'selection': 'Shared slate fill + selection edge; source hue remains in each row.',
    },
    'type': {
        'name': 'Typographic',
        'idea': 'Use hierarchy and small marks rather than repeated boxes.',
        'description': 'Source dots and an underlined current calendar; open agenda text with a selected-event rule. Inline group captions center the date block against a shorter control band.',
        'cost': 'The least framing and more calendar height. Selected options and icon actions are less conspicuous; small source dots are slower to scan than large tints.',
        'weekend': 'Muted rose labels; no column wash',
        'color': '#925866', 'tint': '#ffffff',
        'header': 'Optically centered date block; captions beside flat controls',
        'grid': 'Quiet floating labels above the matrix',
        'all_day': 'Lane and familiar creation gestures; no persistent extra label',
        'selection': 'Creation target: underline + weight. Event: neutral vertical rule + weight.',
    },
}

for key, variant in VARIANTS.items():
    variant['images'] = {}
    for state in ('week', 'month', 'narrow', 'search', 'yellow', 'sidebar', 'header', 'all-day'):
        path = HERE / 'images' / f'{key}-{state}.png'
        raw = path.read_bytes()
        assert raw[:8] == b'\x89PNG\r\n\x1a\n', path
        width, height = struct.unpack('>II', raw[16:24])
        variant['images'][state] = dict(path='images/' + path.name, width=width, height=height)

DATA = dict(variants=VARIANTS, questions=json.loads((HERE / 'questions.json').read_text()))

HTML = '''<!doctype html>
<html lang="en">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Debacal — questions and alternatives</title>
<style>
  :root { color-scheme:light; --ink:#27313a; --muted:#596570; --line:#d5dde1; --accent:#475f52; --bg:#f5f7f8; }
  * { box-sizing:border-box; }
  body { margin:0; color:var(--ink); background:var(--bg); font:16px/1.5 system-ui,sans-serif; }
  header,main,footer { max-width:1560px; margin:auto; padding:24px 32px; }
  header { padding-bottom:8px; }
  h1 { font-size:32px; letter-spacing:-.7px; margin:5px 0 12px; line-height:1.2; }
  h2 { margin:0 0 12px; font-size:23px; letter-spacing:-.3px; }
  h3 { margin:0 0 8px; font-size:17px; }
  p { margin:0 0 12px; max-width:92ch; }
  a { color:var(--accent); text-underline-offset:3px; }
  .muted,small { color:var(--muted); }
  .eyebrow { font-size:13px; color:var(--accent); font-weight:650; letter-spacing:.03em; }
  button,select,input { font:inherit; accent-color:var(--accent); }
  button,select { background:white; color:var(--ink); border:1px solid var(--line); border-radius:6px; padding:7px 12px; min-height:38px; }
  button { cursor:pointer; }
  button:hover { background:#eef3ef; border-color:#7d9386; }
  button[aria-pressed="true"] { background:#e7eee8; border-color:var(--accent); box-shadow:inset 0 0 0 1px var(--accent); }
  :focus-visible { outline:3px solid #426a9d; outline-offset:3px; }
  .directions { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:16px; margin:12px 0 20px; }
  .direction { text-align:left; padding:16px; }
  .direction strong { display:block; font-size:18px; margin-bottom:6px; }
  .direction span { display:block; font-size:14px; color:var(--muted); line-height:1.5; }
  .weekend { display:flex !important; align-items:center; gap:8px; margin-top:12px; }
  .weekend i { width:24px; height:24px; border:1px solid currentColor; border-radius:4px; display:inline-flex; align-items:center; justify-content:center; font-style:normal; font-size:11px; font-weight:650; }
  .tools { display:flex; flex-wrap:wrap; gap:12px 24px; align-items:center; margin-bottom:16px; }
  .tools label { display:flex; gap:8px; align-items:center; font-size:14px; }
  .tools .group { display:flex; gap:6px; align-items:center; }
  .tools .group > span { font-size:14px; color:var(--muted); margin-right:4px; }
  .stage { display:grid; grid-template-columns:minmax(0,1fr); gap:20px; align-items:start; }
  .stage.compare.sidebars { grid-template-columns:repeat(3,minmax(0,1fr)); }
  figure { margin:0; min-width:0; background:white; border:1px solid var(--line); border-radius:8px; overflow:hidden; }
  figcaption { display:flex; align-items:center; justify-content:space-between; gap:12px; padding:10px 14px; border-bottom:1px solid var(--line); font-size:14px; }
  .scroll { overflow:auto; background:white; }
  .scroll img { display:block; max-width:100%; width:auto; height:auto; margin:auto; }
  .stage.actual .scroll { max-height:960px; }
  .stage.actual .scroll img { max-width:none; }
  .explanation { padding:14px 16px; border-top:1px solid var(--line); font-size:14px; }
  .explanation p { margin-bottom:6px; }
  .context { margin:18px 0 28px; color:var(--muted); font-size:14px; }
  .principles { display:grid; grid-template-columns:1fr 1fr; gap:24px; margin:24px 0; }
  .card { background:white; padding:20px; border:1px solid var(--line); border-radius:8px; }
  ul { padding-left:20px; margin:8px 0; }
  li { margin:6px 0; }
  .choices { margin:28px 0; }
  table { border-collapse:collapse; width:100%; font-size:14px; }
  th,td { padding:12px; border-bottom:1px solid var(--line); text-align:left; vertical-align:top; }
  th { color:var(--muted); font-weight:600; }
  td:first-child { font-weight:600; width:19%; }
  .question { background:white; border:1px solid var(--line); border-radius:8px; margin:12px 0; padding:16px 18px; }
  summary { cursor:pointer; font-weight:600; }
  .confidence { display:inline-block; margin:0 0 10px; color:var(--muted); font-size:12px; font-weight:600; border-bottom:2px solid #b9c8bf; }
  .question-body { padding-top:16px; }
  .question h4 { margin:18px 0 8px; font-size:15px; }
  .sources { display:flex; flex-wrap:wrap; gap:8px 16px; font-size:13px; }
  .questions-heading { display:flex; flex-wrap:wrap; justify-content:space-between; align-items:center; gap:12px; margin:32px 0 12px; }
  .questions-heading h2 { margin:0; }
  .recommendation { background:#edf3ee; border-left:3px solid #7e9986; padding:20px; margin:28px 0; }
  footer { color:var(--muted); font-size:14px; border-top:1px solid var(--line); margin-top:24px; }
  code { font-size:.9em; background:#e9eeee; padding:2px 5px; border-radius:4px; }
  @media(max-width:980px) { header,main,footer { padding:20px 16px; } .directions,.stage.compare.sidebars { gap:8px; } .direction { padding:12px; } .principles { grid-template-columns:1fr; } }
  @media(max-width:700px) { .directions,.stage.compare.sidebars { grid-template-columns:1fr; } table,tbody,tr,td { display:block; } thead { display:none; } td,td:first-child { width:auto; border:0; padding:6px 0; } tr { padding:10px 0; border-bottom:1px solid var(--line); } h1 { font-size:28px; } .explanation { padding:12px; } }
</style>
<header>
  <div class="eyebrow">DEBACAL · DESIGN EXPLORATION</div>
  <h1>Three hypotheses for option B</h1>
  <p>The questions do not imply that every difference is a defect. These directions compare different ways of explaining the app: sources versus event objects, a shared navigation list, and a flatter typographic interface.</p>
  <p class="muted">All retain weekend emphasis and keep the controls grouped. The alternatives below can be mixed; none is presented as the one correct appearance. The app is unchanged.</p>
</header>
<main>
  <div id="directions" class="directions" role="group" aria-label="Design hypothesis"></div>
  <div class="tools">
    <label>Compare <select id="state"><option value="sidebar">Sidebar roles</option><option value="all-day">Weekends, grid & all-day affordance</option><option value="header">Header alignment & controls</option><option value="week">Full week</option><option value="month">Month</option><option value="narrow">Smaller window</option><option value="search">Expanded series in search</option><option value="yellow">Bright yellow source</option></select></label>
    <label><input id="compare" type="checkbox" checked> Show all three</label>
    <div class="group" role="group" aria-label="Image scale"><span>Size</span><button id="fit" aria-pressed="true">Fit</button><button id="actual" aria-pressed="false">100%</button></div>
    <a href="#questions">Answers and tradeoffs ↓</a>
  </div>
  <div id="stage" class="stage compare sidebars"></div>
  <p id="context" class="context"></p>

  <section class="principles">
    <article class="card"><h3>What I would defend strongly</h3><ul><li>Readable event text while preserving the server’s source colors.</li><li>Predictable group placement, with navigation beside the date.</li><li>Clear current-source, selected-event, visibility, and keyboard-focus states.</li><li>Accurate column geometry, including scrollbar space.</li></ul><p class="muted">These constrain usability and the app’s intended behavior. They do not dictate a particular tint, radius, or container style.</p></article>
    <article class="card"><h3>What remains a design choice</h3><ul><li>Calendars and events can share a grammar or intentionally look different.</li><li>Weekends can use warm/cool shading or a quieter red text cue.</li><li>Whitespace, dividers, and surfaces are different ways to group content.</li><li>A header line break or modest type-size difference can have a purpose.</li></ul><p class="muted">Consistency is a coherent relationship between appearance and meaning; it does not require making different things look identical.</p></article>
  </section>

  <section class="choices"><h2>The choices being tested</h2><div id="matrix"></div><p class="muted" style="margin-top:12px">Bundling choices makes the full app visible, but does not prove which individual choice caused a preference. The component views above let us compare the questions separately.</p></section>
  <section class="recommendation"><h3>My provisional reading</h3><p>I lean toward distinguishing the creation-calendar controls from event objects, because they change different kinds of state. That does not require adopting the card density of Sources & agenda: its time column and tinted cards are an experiment, not a prerequisite.</p><p>I favor a restrained weekend cue and an integrated all-day creation affordance. I do not see a universal winner between warm shading, cool shading, and muted red labels, or between a continuous grid and an open date header. The flatter direction is intentionally useful as a counterpoint: less framing can also mean weaker discoverability.</p></section>
  <section id="questions"><div class="questions-heading"><h2>Revisiting the actual questions</h2><button id="expand">Expand all</button></div><p>Each answer gives alternatives, a recommendation, and its confidence. “Strong preference” is a reasoned judgment for this app, not proof that another treatment is wrong.</p><div id="answers"></div></section>
</main>
<footer><p>24 native Qt captures · synthetic events and in-memory database · no production changes. <a href="README.txt">Reproduce the study</a> · <a href="questions.json">Structured reasoning and sources</a> · <a href="../2026-11-ui-refinement/index.html">Previous refinement</a></p><p>The screenshots show an extra six-week synthetic recurring event so series hierarchy can be judged in search. The integrated + is demonstrated on an empty day; an occupied-day creation target still needs interaction review. No complete accessibility or user-testing claim is made from static images.</p></footer>
<script>
const DATA=__DATA__;
const $=id=>document.getElementById(id);
const escape=value=>String(value).replace(/[&<>"']/g,char=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
let selected='sources';
const parts=location.hash.slice(1).split('/');
let keepQuestionAnchor=parts[0]==='questions';
if(DATA.variants[parts[0]]) selected=parts[0];
if(DATA.variants[selected].images[parts[1]]) $('state').value=parts[1];
if(parts[2]==='one') $('compare').checked=false;
$('directions').innerHTML=Object.entries(DATA.variants).map(([key,item])=>`<button class="direction" data-direction="${key}" aria-pressed="${key===selected}"><strong>${escape(item.name)}</strong><span>${escape(item.idea)}</span><span class="weekend"><i style="color:${item.color};background:${item.tint}">Sa</i>${escape(item.weekend)}</span></button>`).join('');
const notes={sidebar:'Native sidebar captures at their actual widths: 312px, 292px, and 280px. The different source and event markers are intentional, not an alignment error. Scroll the page to compare the complete lists.',
  'all-day':'Compare the same date/header area: warm versus cool shading versus muted red labels; grid lines through the header versus beginning below it; integrated hover + versus persistent creation strip versus lane-only gestures. These are three different compromises.',
  header:'All keep View → Selection → Events together. The first two align text baselines and control rows. Typographic places captions beside the controls and centers the two-line date block against a shorter band.',
  week:'Same events and selected agenda object. B’s source-tinted event cards remain in the main grid in every direction. Weekend emphasis survives in both the overview and main calendar.',
  month:'The first two keep the same weekday size across views. Typographic deliberately uses a slightly smaller month-label role; this is a density tradeoff, not a requirement.',
  narrow:'At 1030 × 690, the complete control pack wraps together. Source rows stay visible; the agenda scrolls. Existing truncation of long week titles remains visible.',
  search:'The same real query “work”, with a six-week recurring resource expanded and its first occurrence selected. Compare hierarchy, indentation, density, and whether the current creation source still reads differently from event selection.',
  yellow:'The Work source hue is #ffff00. Text stays neutral, main-event selection retains the source hue, and the source square/bar/dot is still recognizable. This is a stress case, not a proposed palette.'};
function update(){
  const state=$('state').value, compare=$('compare').checked;
  document.querySelectorAll('[data-direction]').forEach(button=>button.setAttribute('aria-pressed',String(button.dataset.direction===selected)));
  $('stage').classList.toggle('compare',compare);$('stage').classList.toggle('sidebars',state==='sidebar');
  const keys=compare?Object.keys(DATA.variants):[selected];
  $('stage').innerHTML=keys.map(key=>{
    const item=DATA.variants[key],img=item.images[state];
    return `<figure><figcaption><strong>${escape(item.name)}</strong><a href="${img.path}" target="_blank" rel="noopener">Full size ↗</a></figcaption><div class="scroll"><img src="${img.path}" width="${img.width}" height="${img.height}" alt="${escape(item.name)}: ${escape(state)}, synthetic events"></div><div class="explanation"><p>${escape(item.description)}</p><p class="muted"><strong>Tradeoff:</strong> ${escape(item.cost)}</p></div></figure>`;
  }).join('');
  $('context').textContent=notes[state];
  if(!keepQuestionAnchor)location.hash=selected+'/'+state+(compare?'/all':'/one');
  keepQuestionAnchor=false;
}
document.querySelectorAll('[data-direction]').forEach(button=>button.addEventListener('click',()=>{selected=button.dataset.direction;$('compare').checked=false;update();}));
['state','compare'].forEach(id=>$(id).addEventListener('change',update));
function scale(actual){$('stage').classList.toggle('actual',actual);$('fit').setAttribute('aria-pressed',String(!actual));$('actual').setAttribute('aria-pressed',String(actual));}
$('fit').addEventListener('click',()=>scale(false));$('actual').addEventListener('click',()=>scale(true));
const rows=[['weekend','Weekend cue'],['selection','State and selection'],['header','Header/control rhythm'],['grid','Line work'],['all_day','All-day creation']];
$('matrix').innerHTML='<table><thead><tr><th>Question</th>'+Object.values(DATA.variants).map(item=>'<th>'+escape(item.name)+'</th>').join('')+'</tr></thead><tbody>'+rows.map(([key,label])=>'<tr><td>'+label+'</td>'+Object.values(DATA.variants).map(item=>'<td>'+escape(item[key])+'</td>').join('')+'</tr>').join('')+'</tbody></table>';
function sourceLabel(url){if(url.includes('nngroup'))return 'NNGroup · '+url.split('/').filter(Boolean).at(-1).replaceAll('-',' ');if(url.includes('gnome'))return 'GNOME · '+url.split('/').at(-1).replace('.html','').replaceAll('-',' ');return 'W3C · '+url.split('/').at(-1).replace('.html','').replaceAll('-',' ');}
$('answers').innerHTML=DATA.questions.map(question=>`<details class="question" id="${question.id}" ${['weekend_color','sources_versus_events'].includes(question.id)?'open':''}><summary>${escape(question.question)}</summary><div class="question-body"><span class="confidence">${escape(question.confidence)}</span><table><thead><tr><th>Alternative</th><th>Benefit</th><th>Cost</th></tr></thead><tbody>${question.options.map(option=>`<tr><td>${escape(option.choice)}</td><td>${escape(option.benefit)}</td><td>${escape(option.cost)}</td></tr>`).join('')}</tbody></table><h4>My recommendation</h4><p>${escape(question.recommendation)}</p><h4>Why</h4><p>${escape(question.reason)}</p><div class="sources">${question.sources.map(url=>`<a href="${escape(url)}">${escape(sourceLabel(url))}</a>`).join('')}</div></div></details>`).join('');
$('expand').addEventListener('click',()=>{const details=Array.from(document.querySelectorAll('.question')),open=details.some(item=>!item.open);details.forEach(item=>item.open=open);$('expand').textContent=open?'Collapse all':'Expand all';});
update();
</script>
</html>'''

(HERE / 'index.html').write_text(HTML.replace('__DATA__', json.dumps(DATA).replace('</', '<\\/')))
print('Gallery built: three hypotheses, 24 native captures, 11 questions with alternatives.')
