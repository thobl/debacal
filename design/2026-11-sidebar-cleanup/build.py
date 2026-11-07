"""Build a local comparison gallery; no web assets or application changes."""
import json
from pathlib import Path
import struct

from options import OPTIONS
from render import STATES

HERE = Path(__file__).resolve().parent


def photo(name):
    path = HERE / 'images' / (name + '.png')
    raw = path.read_bytes()
    assert raw[:8] == b'\x89PNG\r\n\x1a\n'
    width, height = struct.unpack('>II', raw[16:24])
    return dict(src='images/' + path.name, width=width, height=height)


DATA = {key: dict(item, images={f'{mode}-{state}-{crop}': photo(f'{key}-{mode}-{state}-{crop}')
    for mode in ('light', 'dark') for state in STATES for crop in ('sidebar', 'footer', 'full')})
    for key, item in OPTIONS.items()}

HTML = r'''<!doctype html>
<html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Debacal · sidebar background review</title>
<style>
:root {color-scheme:light;--page:#f5f7f8;--panel:#fff;--ink:#27313a;--muted:#596570;--line:#cbd3da;--accent:#475f52;--selected:#e7eee8;}
body.dark {color-scheme:dark;--page:#1a1e20;--panel:#24292b;--ink:#edf1f5;--muted:#b3bfc9;--line:#48545c;--accent:#a9c6b5;--selected:#33443b;}
*{box-sizing:border-box}body{margin:0;background:var(--page);color:var(--ink);font:16px/1.5 system-ui,sans-serif}
header,main,footer{max-width:1480px;margin:auto;padding:24px 32px}header{padding-bottom:0}h1{font-size:32px;line-height:1.2;letter-spacing:-.5px;margin:5px 0 16px}h2{font-size:23px;margin:0 0 10px}h3{font-size:17px;margin:0 0 6px}p{max-width:94ch;margin:0 0 12px}small,.muted{color:var(--muted)}a{color:var(--accent);text-underline-offset:3px}section{margin:18px 0 36px;scroll-margin-top:16px}
nav,.tools{display:flex;align-items:center;flex-wrap:wrap;gap:12px 22px;margin:14px 0}button,select,input{font:inherit;accent-color:var(--accent)}button,select{border:1px solid var(--line);border-radius:5px;color:var(--ink);background:var(--panel);padding:7px 11px;min-height:38px;max-width:100%}button{cursor:pointer}button[aria-pressed=true]{background:var(--selected);border-color:var(--accent)}label{display:flex;align-items:center;flex-wrap:wrap;gap:8px;font-size:14px}:focus-visible{outline:3px solid #558fbe;outline-offset:3px}
.pair{display:grid;grid-template-columns:1fr 1fr;gap:24px}.card{border:1px solid var(--line);background:var(--panel);border-radius:7px;padding:16px;min-width:0}.note{padding:14px 18px;border-left:3px solid var(--accent);background:var(--selected);margin:18px 0}.note p:last-child{margin-bottom:0}figure{margin:0;border:1px solid var(--line);border-radius:6px;background:var(--panel);overflow:hidden;min-width:0}figcaption{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:10px 14px;border-bottom:1px solid var(--line);font-size:14px}.scroll{overflow:auto}.photo{display:block;width:100%;height:auto;max-width:100%;margin:auto}.sidebar{width:300px;max-width:100%;margin:auto}.native .photo{width:auto;max-width:none;margin:0}.full{max-height:1000px}.native .full{max-height:960px}.description{padding:12px 16px;border-top:1px solid var(--line);font-size:14px}.description p:last-child{margin-bottom:0}.footer-pair img{width:300px;max-width:100%;margin:auto}footer{border-top:1px solid var(--line);font-size:14px;color:var(--muted)}ul{padding-left:20px;margin:8px 0}li{margin:4px 0}
@media(max-width:760px){header,main,footer{padding:20px 16px}.pair{gap:14px}.pair.descriptions{grid-template-columns:1fr}h1{font-size:27px}}@media(max-width:540px){.pair{grid-template-columns:1fr}}
</style>
<header><small>DEBACAL · SIDEBAR CLEANUP · BACKGROUND STUDIES</small><h1>One sidebar, or separate surfaces?</h1>
<p>The functional cleanup is applied: no right-hand message bar, routine action notices, G-selection mode or testing-profile note. The footer carries synchronization status and a warning indicator for errors. These two background treatments are still proposals.</p>
<nav aria-label="Review sections"><a href="#footer">Footer detail</a><a href="#sidebar">Whole sidebar</a><a href="#context">Full app</a></nav>
<div class="tools"><label>Appearance <select id="mode"><option value="light">Light</option><option value="dark">Dark</option></select></label><label>State <select id="state"><option value="ready">Normal footer</option><option value="warning" selected>Warning in the footer</option><option value="compact">Title hidden, with warning</option></select></label></div>
</header><main>
<div class="pair descriptions"><article class="card"><h3>A · Current surfaces</h3><p>The overview and footer use the canvas background; calendars and agenda use the source-panel background.</p><p class="muted">Clear separation between the overview and sources, with an extra background transition at the footer.</p></article><article class="card"><h3>B · One sidebar surface</h3><p>The whole left column uses the existing calendars-and-agenda background, including the overview and footer.</p><p class="muted">A continuous column. Spacing, headings and thin rules still separate its sections.</p></article></div>
<section id="footer"><h2>Footer detail</h2><p>The status controls and warning are identical in both options; only the background changes.</p><div class="pair footer-pair" id="footer-stage"></div></section>
<section id="sidebar"><h2>Whole sidebar</h2><p>Compare the overview, list sections and footer together. The title-hidden state also shows the Preferences control in the footer.</p><div class="pair" id="sidebar-stage"></div></section>
<section id="context"><h2>In context</h2><div class="tools"><button data-option="segmented" aria-pressed="false">A · Current</button><button data-option="uniform" aria-pressed="true">B · Uniform</button><label><input type="checkbox" id="native">100% image size</label></div><div id="full-stage"></div></section>
<div class="note"><p><strong>I would choose B.</strong> It follows the continuous-column direction while retaining the existing section hierarchy. The warning already has its own icon and color, so the footer does not need a separate background to make it discoverable.</p><p>A remains useful if the month overview should read as a distinct block.</p></div>
</main><footer>Native Qt screenshots; synthetic calendars in an in-memory database, fixed date, disabled network sync. Background B is applied only to these disposable preview windows. No production color setting has changed.</footer>
<script>
const data = DATA_JSON;
const $ = id => document.getElementById(id);
let current='uniform';
function figure(item,title,kind){return `<figure><figcaption><strong>${title}</strong><a href="${item.src}" target="_blank" rel="noopener">Open image</a></figcaption><div class="scroll ${kind==='full'?'full':''}"><img class="photo ${kind==='sidebar'?'sidebar':''}" src="${item.src}" width="${item.width}" height="${item.height}" alt="${title}"></div></figure>`;}
function render(){const mode=$('mode').value,state=$('state').value;document.body.classList.toggle('dark',mode==='dark');for(const crop of ['footer','sidebar'])$(crop+'-stage').innerHTML=Object.entries(data).map(([key,item])=>figure(item.images[`${mode}-${state}-${crop}`],item.id+' · '+item.name,crop)).join('');$('full-stage').classList.toggle('native',$('native').checked);const selected=data[current];$('full-stage').innerHTML=figure(selected.images[`${mode}-${state}-full`],selected.id+' · '+selected.name,'full');document.querySelectorAll('[data-option]').forEach(button=>button.setAttribute('aria-pressed',String(button.dataset.option===current)));}
for(const id of ['mode','state','native'])$(id).addEventListener('change',render);document.querySelectorAll('[data-option]').forEach(button=>button.addEventListener('click',()=>{current=button.dataset.option;render();}));render();
</script></html>'''
(HERE / 'index.html').write_text(HTML.replace('DATA_JSON', json.dumps(DATA)), encoding='utf-8')
print('Built', HERE / 'index.html')
