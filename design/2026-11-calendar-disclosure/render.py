from pathlib import Path
from html import escape
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtCore import QRectF
from PySide6.QtGui import QGuiApplication, QImage, QPainter
from PySide6.QtSvg import QSvgRenderer

HERE = Path(__file__).resolve().parent
SIDEBAR='#f0f4f7'; INK='#29343d'; MUTED='#62717d'; LINE='#d5dfe4'; ACCENT='#4f7865'
parts=[]
def add(s): parts.append(s)
def text(x,y,s,size=13,weight=400,color=INK):
    add(f'<text x="{x}" y="{y}" font-size="{size}" font-weight="{weight}" fill="{color}">{escape(s)}</text>')
def line(x,y,xx,yy,color=LINE,width=1):
    add(f'<path d="M{x},{y} L{xx},{yy}" stroke="{color}" stroke-width="{width}" fill="none"/>')
def rect(x,y,w,h,fill,stroke='none',radius=0):
    add(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{radius}" fill="{fill}" stroke="{stroke}"/>')
def caret(x,y,opened=False):
    dy=-6 if opened else 6
    add(f'<path d="M{x-5},{y-dy/2} L{x},{y+dy/2} L{x+5},{y-dy/2}" fill="none" stroke="{MUTED}" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>')
def eye(x,y):
    add(f'<path d="M{x-7},{y} Q{x},{y-8} {x+7},{y} Q{x},{y+8} {x-7},{y} Z" fill="none" stroke="{MUTED}" stroke-width="1.2"/>')
    add(f'<circle cx="{x}" cy="{y}" r="2" fill="{MUTED}"/>')
def row(y,name,color,selected=False):
    rect(18,y-6,11,11,color,'#52636a',1)
    text(43,y+4,name,13,700 if selected else 400)
    if selected: line(43,y+7,96,y+7,INK)
    eye(250,y)
def plus(x,y): line(x-6,y,x+6,y,ACCENT,1.5);line(x,y-6,x,y+6,ACCENT,1.5)
def transfer(x,y,up):
    line(x-6,y+3,x-6,y+8,ACCENT,1.2);line(x-6,y+8,x+6,y+8,ACCENT,1.2);line(x+6,y+8,x+6,y+3,ACCENT,1.2)
    line(x,y+3,x,y-7,ACCENT,1.2)
    if up: line(x-3,y-4,x,y-7,ACCENT,1.2);line(x,y-7,x+3,y-4,ACCENT,1.2)
    else: line(x-3,y,x,y+3,ACCENT,1.2);line(x,y+3,x+3,y,ACCENT,1.2)
def agenda(y, separator=True):
    if separator: line(0,y-20,280,y-20)
    text(18,y+10,'Next events',13,700)
    rect(18,y+27,244,45,'#e1e9e7',radius=3)
    text(26,y+45,'Meeting',13,600)
    text(26,y+62,'Tomorrow · 10:00',12,400,MUTED)
def panel(kind,opened):
    rect(0,0,280,390 if opened else 365,SIDEBAR,LINE,4)
    text(18,33,'Calendars',13,700)
    transfer(182,27,False);transfer(216,27,True);plus(250,30)
    if kind=='B': caret(110,29,opened)
    if kind=='D':
        for xx in (106,111,116): add(f'<circle cx="{xx}" cy="29" r="1.3" fill="{MUTED}"/>')
    row(70,'Personal','#527a68',True);row(106,'Work','#7b78a2')
    if kind=='A':
        line(18,140,125,140);line(155,140,262,140);caret(140,140,opened)
        if opened:
            for yy,name,col in ((173,'Birthdays','#b6855d'),(209,'Holidays','#b7a04d'),(245,'Archive','#6f8b9c')):row(yy,name,col)
        agenda(300 if opened else 163, separator=opened)
    elif kind=='B':
        if opened:
            line(18,132,262,132)
            for yy,name,col in ((159,'Birthdays','#b6855d'),(195,'Holidays','#b7a04d'),(231,'Archive','#6f8b9c')):row(yy,name,col)
        agenda(286 if opened else 165)
    elif kind=='C':
        caret(140,142,opened)
        if opened:
            for yy,name,col in ((181,'Birthdays','#b6855d'),(217,'Holidays','#b7a04d'),(253,'Archive','#6f8b9c')):row(yy,name,col)
        agenda(308 if opened else 196)
    else:
        agenda(165)
        if opened:
            rect(25,54,246,130,'#cbd5da',radius=6)
            rect(22,50,246,130,'#ffffff',LINE,5)
            add('<path d="M103,50 L111,42 L119,50 Z" fill="#ffffff" stroke="#d5dfe4"/>')
            for yy,name,col in ((77,'Birthdays','#b6855d'),(113,'Holidays','#b7a04d'),(149,'Archive','#6f8b9c')):
                rect(35,yy-6,11,11,col,'#52636a',1);text(60,yy+4,name);eye(248,yy)

names=[('A','Divider disclosure'),('B','Header disclosure'),('C','Icon-only row'),('D','Header popover')]
for i,(key,name) in enumerate(names):
    x=22+i*302
    text(x,29,f'{key}  {name}',15,700)
    text(x,52,'Collapsed',12,400,MUTED)
    add(f'<g transform="translate({x},64)">');panel(key,False);add('</g>')
    # Crop the empty bottom of collapsed examples, keeping equal panel dimensions.
    text(x,461,'Expanded',12,400,MUTED)
    add(f'<g transform="translate({x},474)">');panel(key,True);add('</g>')
svg='<svg xmlns="http://www.w3.org/2000/svg" width="1230" height="885" viewBox="0 0 1230 885"><rect width="1230" height="885" fill="#ffffff"/><g font-family="DejaVu Sans">'+''.join(parts)+'</g></svg>'
(HERE / 'comparison.svg').write_text(svg)
app=QGuiApplication([])
renderer=QSvgRenderer(svg.encode())
image=QImage(2460,1770,QImage.Format.Format_ARGB32)
image.fill('#ffffff')
painter=QPainter(image)
renderer.render(painter,QRectF(0,0,2460,1770));painter.end()
assert image.save(str(HERE / 'comparison.png'))
print('Created SVG and PNG comparison; application files unchanged.')
