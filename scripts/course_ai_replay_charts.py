"""Render already time-truncated packet using Pillow; no price-source access."""
import json
import sys
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

path = Path(sys.argv[1])
packet = json.loads(path.read_text(encoding="utf-8"))
lookback = int(sys.argv[2]) if len(sys.argv) > 2 else 70
font = ImageFont.truetype("C:/Windows/Fonts/arial.ttf", 15)
small = ImageFont.truetype("C:/Windows/Fonts/arial.ttf", 12)
items = packet["items"]
for start in range(0, len(items), 6):
    group = items[start:start+6]
    img = Image.new("RGB", (1600, 1050), "#ffffff")
    draw = ImageDraw.Draw(img)
    for n, item in enumerate(group):
        x0, y0 = (n % 2)*800, (n//2)*350
        bars = item["bars"][-lookback:]
        lo = min(min(b["low"], b["MA55"]) for b in bars)
        hi = max(max(b["high"], b["MA55"]) for b in bars)
        span = max(.1, hi-lo)
        lo -= span*.06; hi += span*.06
        left, right, top, bottom = x0+55, x0+780, y0+38, y0+265
        px = lambda i: left+(i+.5)*(right-left)/len(bars)
        py = lambda p: bottom-(p-lo)/(hi-lo)*(bottom-top)
        title = f"{item['alias']}  {packet['as_of']}  C={bars[-1]['close']:.2f}  MA21 blue / MA55 orange"
        draw.text((x0+15,y0+9),title,fill="#111111",font=font)
        for k in range(5):
            p = lo+(hi-lo)*k/4; y=py(p)
            draw.line((left,y,right,y), fill="#e1e6eb")
            draw.text((x0+4,y-6),f"{p:.1f}",font=small,fill="#666666")
        for key,color in [("MA21","#2474d8"),("MA55","#c78317")]:
            draw.line([(px(i),py(b[key])) for i,b in enumerate(bars)],fill=color,width=2)
        vmax=max(1,max(b["volume"] for b in bars))
        for i,b in enumerate(bars):
            x=px(i); color="#c63a3a" if b["close"]>=b["open"] else "#208c68"
            draw.line((x,py(b["low"]),x,py(b["high"])), fill=color)
            w=max(1,(right-left)/len(bars)*.3)
            ys=sorted([py(b["open"]),py(b["close"])])
            draw.rectangle((x-w,ys[0],x+w,max(ys[0]+1,ys[1])), fill=color)
            draw.rectangle((x-w,y0+323-b["volume"]/vmax*42,x+w,y0+323),fill=color)
        for i in sorted(set([0,len(bars)//3,2*len(bars)//3,len(bars)-1])):
            draw.text((px(i)-22,y0+330),bars[i]["date"][5:],fill="#555555",font=small)
        if item["position"]:
            t=item["position"]
            draw.text((left,y0+267),f"entry {t['entry_price']:.2f}, defense {t['dynamic_defense']:.2f}, qty {t['remaining']}",fill="#222222",font=small)
    out=path.with_name(path.stem+f"_{lookback}_p{start//6+1}.png")
    img.save(out)
    print(out)
