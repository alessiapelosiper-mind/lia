# Banco di montaggio di LIA (gira su GitHub Actions).
# mode "montaggio": grezzo -> Whisper -> fotogrammi -> Claude (via LIA) decide i tagli -> ffmpeg 9:16 + sottotitoli -> OneDrive
# mode "video": storyboard Iuris -> voce (edge-tts) -> testi animati -> ffmpeg -> OneDrive
# mode "carosello": storyboard Iuris -> slide 1080x1350 PNG -> OneDrive
import json, os, re, subprocess, base64, urllib.request, urllib.parse, asyncio, glob

P = json.loads(os.environ["PAYLOAD"])
LIA, ID, TOK, MODE = P["lia"].rstrip("/"), P["id"], P["token"], P.get("mode", "montaggio")
W = "work"; os.makedirs(W, exist_ok=True)

def http(url, data=None, method=None, headers=None, raw=False):
    h = {"user-agent": "lia-banco", **(headers or {})}
    if data is not None and not isinstance(data, (bytes, bytearray)):
        data = json.dumps(data).encode(); h["content-type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method or ("POST" if data is not None else "GET"), headers=h)
    with urllib.request.urlopen(req, timeout=600) as r:
        b = r.read()
        return b if raw else (json.loads(b) if b else {})

def q(path): return f"{LIA}{path}?id={ID}&token={TOK}"
def lia(path, body=None): return http(q(path), {**(body or {}), "id": ID, "token": TOK} if body is not None else None)
def stage(s):
    print("::", s, flush=True)
    try: lia("/montaggio/progress", {"stage": s})
    except Exception as e: print("progress", e)
def sh(*a): print("$", " ".join(map(str, a))[:300], flush=True); subprocess.run(list(map(str, a)), check=True)

def probe(path):
    j = json.loads(subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", path], capture_output=True, text=True, check=True).stdout)
    v = next((s for s in j["streams"] if s["codec_type"] == "video"), {})
    w, h = int(v.get("width", 1080)), int(v.get("height", 1920))
    rot = abs(int((v.get("tags", {}) or {}).get("rotate", 0) or next((sd.get("rotation", 0) for sd in v.get("side_data_list", []) if "rotation" in sd), 0) or 0))
    if rot in (90, 270): w, h = h, w
    return float(j["format"]["duration"]), w, h, any(s["codec_type"] == "audio" for s in j["streams"])

def upload(path, name):
    url = lia("/montaggio/upload", {"name": name})["uploadUrl"]
    size, CH, res = os.path.getsize(path), 10 * 1024 * 1024, {}
    with open(path, "rb") as f:
        off = 0
        while off < size:
            chunk = f.read(CH)
            res = http(url, chunk, "PUT", {"Content-Length": str(len(chunk)), "Content-Range": f"bytes {off}-{off + len(chunk) - 1}/{size}"})
            off += len(chunk)
    return res

def ass_color(hexc, alpha="00"):
    h = hexc.lstrip("#"); return f"&H{alpha}{h[4:6]}{h[2:4]}{h[0:2]}".upper()
def t_ass(s):
    s = max(0, s); return f"{int(s // 3600)}:{int(s % 3600 // 60):02d}:{s % 60:05.2f}"
def t_srt(s):
    ms = int(round(max(0, s) * 1000)); return f"{ms // 3600000:02d}:{ms % 3600000 // 60000:02d}:{ms % 60000 // 1000:02d},{ms % 1000:03d}"
def esc_ass(t): return str(t or "").replace("\\", "").replace("{", "(").replace("}", ")").replace("\n", "\\N")

def ass_file(path, w, h, styles, events):
    head = f"[Script Info]\nScriptType: v4.00+\nPlayResX: {w}\nPlayResY: {h}\nWrapStyle: 0\nScaledBorderAndShadow: yes\n\n[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
    head += "".join(f"Style: {s}\n" for s in styles)
    head += "\n[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    with open(path, "w", encoding="utf-8") as f:
        f.write(head + "".join(f"Dialogue: {l},{t_ass(a)},{t_ass(b)},{st},,0,0,0,,{tx}\n" for l, a, b, st, tx in events))

def chunks_from_words(words, max_w=4, max_d=1.8):
    out, cur = [], []
    for wd in words:
        cur.append(wd)
        if len(cur) >= max_w or cur[-1]["end"] - cur[0]["start"] >= max_d or re.search(r"[.,!?;:]$", wd["word"].strip()):
            out.append(cur); cur = []
    if cur: out.append(cur)
    return [(c[0]["start"], c[-1]["end"], " ".join(x["word"].strip() for x in c)) for c in out]

def done(**kw): lia("/montaggio/done", kw)

# ---------------------------------------------------------------- MONTAGGIO
def transcribe(audio):
    if os.environ.get("FAKE_TRANSCRIPT"): return json.load(open(os.environ["FAKE_TRANSCRIPT"]))
    from faster_whisper import WhisperModel
    m = WhisperModel(os.environ.get("WHISPER_MODEL", "small"), device="cpu", compute_type="int8")
    segs, _ = m.transcribe(audio, language="it", word_timestamps=True, vad_filter=True)
    return [{"start": s.start, "end": s.end, "text": s.text.strip(), "words": [{"start": w.start, "end": w.end, "word": w.word} for w in (s.words or [])]} for s in segs]

def frame_filter(w, h):
    if w / h < 0.7: return "scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1"
    return "split[a][b];[a]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=20:2[bg];[b]scale=1080:-2[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1"

def montaggio():
    raw = f"{W}/raw.mp4"
    stage("scarico il grezzo")
    with urllib.request.urlopen(P["video"], timeout=600) as r, open(raw, "wb") as f:
        while True:
            b = r.read(1 << 20)
            if not b: break
            f.write(b)
    dur, w, h, has_audio = probe(raw)
    segs = []
    if has_audio:
        stage("trascrivo (Whisper)")
        sh("ffmpeg", "-y", "-loglevel", "error", "-i", raw, "-vn", "-ac", "1", "-ar", "16000", f"{W}/audio.wav")
        segs = transcribe(f"{W}/audio.wav")
    words = [wd for s in segs for wd in s.get("words", [])]
    stage("estraggo i fotogrammi")
    n = int(min(30, max(8, dur / 3)))
    frames = []
    for i in range(n):
        t = (i + 0.5) * dur / n
        fp = f"{W}/f{i:02d}.jpg"
        sh("ffmpeg", "-y", "-loglevel", "error", "-ss", f"{t:.2f}", "-i", raw, "-frames:v", "1", "-vf", "scale=360:-2", "-q:v", "7", fp)
        if os.path.exists(fp): frames.append((t, base64.b64encode(open(fp, "rb").read()).decode()))
    brief = http(q("/montaggio/brief"))
    if brief.get("blocked") or brief.get("error"): raise Exception(brief.get("error", "bloccato"))
    stage("Claude decide il montaggio")
    tr = "\n".join(f"[{s['start']:.1f}-{s['end']:.1f}] {s['text']}" for s in segs) or "(nessun parlato)"
    content = [{"type": "text", "text": f"Durata del grezzo: {dur:.1f} s. Formato sorgente {w}x{h}.\n\nTRASCRIZIONE (secondi del grezzo):\n{tr}\n\nFOTOGRAMMI:"}]
    for t, b64 in frames:
        content += [{"type": "text", "text": f"Fotogramma a {t:.1f} s"}, {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}}]
    content.append({"type": "text", "text": "Ora restituisci SOLO il JSON del montaggio."})
    body = json.dumps({"system": brief["system"], "messages": [{"role": "user", "content": content}]}).encode()
    txt = http(q("/montaggio/claude"), body, "POST", {"content-type": "application/json"}).get("text", "")
    plan = json.loads(txt[txt.find("{"): txt.rfind("}") + 1])
    clips = []
    for c in plan.get("clip", []):
        a, b = max(0.0, float(c.get("da", 0))), min(dur, float(c.get("a", 0)))
        for wd in words:  # non tagliare le parole a metà
            if wd["start"] <= a < wd["end"]: a = max(0, wd["start"] - 0.05)
            if wd["start"] < b <= wd["end"]: b = min(dur, wd["end"] + 0.08)
        if b - a >= 0.3: clips.append({**c, "da": a, "a": b})
    if not clips: clips = [{"da": 0, "a": dur}]
    plan["clip"] = clips
    stage(f"monto {len(clips)} clip")
    subs, top, off = [], [], 0.0
    for c in clips:
        cw = [{**wd, "start": off + wd["start"] - c["da"], "end": off + min(wd["end"], c["a"]) - c["da"]} for wd in words if wd["start"] >= c["da"] - 0.01 and wd["end"] <= c["a"] + 0.15]
        subs += chunks_from_words(cw)
        if (c.get("testo_schermo") or "").strip(): top.append((off, off + c["a"] - c["da"], c["testo_schermo"].strip()))
        off += c["a"] - c["da"]
    styles = ["Sub,DejaVu Sans,64,&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,5,0,2,80,80,480,1",
              "Top,DejaVu Sans,70,&H00000000,&H00000000,&H00FFFFFF,&H00FFFFFF,-1,0,0,0,100,100,0,0,3,18,0,8,90,90,260,1"]
    ev = [(0, a, b, "Sub", esc_ass(t)) for a, b, t in subs] if plan.get("sottotitoli", True) else []
    ev += [(1, a, b, "Top", "{\\fad(150,0)}" + esc_ass(t)) for a, b, t in top]
    ass_file(f"{W}/subs.ass", 1080, 1920, styles, ev)
    with open(f"{W}/sottotitoli.srt", "w", encoding="utf-8") as f:
        f.write("".join(f"{i + 1}\n{t_srt(a)} --> {t_srt(b)}\n{t}\n\n" for i, (a, b, t) in enumerate(subs)))
    parts, ins = [], ""
    for i, c in enumerate(clips):
        parts.append(f"[0:v]trim=start={c['da']:.3f}:end={c['a']:.3f},setpts=PTS-STARTPTS[v{i}]")
        if has_audio: parts.append(f"[0:a]atrim=start={c['da']:.3f}:end={c['a']:.3f},asetpts=PTS-STARTPTS[a{i}]")
        ins += f"[v{i}]" + (f"[a{i}]" if has_audio else "")
    parts.append(f"{ins}concat=n={len(clips)}:v=1:a={1 if has_audio else 0}[cv]" + ("[ca]" if has_audio else ""))
    ff = frame_filter(w, h)
    parts.append(f"[cv]{ff.replace('split[a][b];[a]', 'split[ga][gb];[ga]').replace('[b]scale', '[gb]scale')},ass={W}/subs.ass,fps=30[vout]")
    if has_audio: parts.append("[ca]loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000[aout]")
    out = f"{W}/montato.mp4"
    sh("ffmpeg", "-y", "-loglevel", "error", "-i", raw, "-filter_complex", ";".join(parts), "-map", "[vout]", *(["-map", "[aout]", "-c:a", "aac", "-b:a", "160k"] if has_audio else ["-an"]),
       "-c:v", "libx264", "-preset", "medium", "-crf", "21", "-pix_fmt", "yuv420p", "-movflags", "+faststart", out)
    cov = plan.get("copertina") or {}
    ass_file(f"{W}/cover.ass", 1080, 1920, ["Cov,DejaVu Sans,110,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,8,0,5,80,80,0,1"], [(0, 0, 5, "Cov", esc_ass(cov.get("testo", "")))])
    sh("ffmpeg", "-y", "-loglevel", "error", "-ss", f"{min(dur - 0.1, float(cov.get('secondo', 1) or 1)):.2f}", "-i", raw, "-frames:v", "1", "-vf", f"{ff.replace('split[a][b];[a]', 'split[ga][gb];[ga]').replace('[b]scale', '[gb]scale')},ass={W}/cover.ass", f"{W}/copertina.jpg")
    stage("carico su OneDrive")
    base = re.sub(r"[^\w\- àèéìòù]", "", P.get("title") or brief.get("title") or "video").strip() or "video"
    base = f"{ID} {base}"
    up = upload(out, f"{base} · montato.mp4")
    upload(f"{W}/sottotitoli.srt", f"{base} · sottotitoli.srt")
    upload(f"{W}/copertina.jpg", f"{base} · copertina.jpg")
    done(plan=plan, transcript=tr[:50000], duration=dur, out_duration=probe(out)[0], out_item=up.get("id"), out_url=up.get("webUrl"), out_size=os.path.getsize(out))

# ---------------------------------------------------------------- IURIS: VIDEO E CAROSELLI DA ZERO
def brand_styles(B, w):
    f, ft = B.get("font") or "Montserrat", B.get("font_titoli") or B.get("font") or "Lora"
    tx, ac = ass_color(B.get("testo", "#F5F1E8")), ass_color(B.get("accento", "#C9A227"))
    big = 92 if w == 1080 and MODE == "video" else 78
    return [f"Tit,{ft},{big},{tx},{tx},&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,0,0,5,90,90,0,1",
            f"Txt,{f},{50 if MODE == 'video' else 44},{tx},{tx},&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,0,0,8,100,100,0,1",
            f"Acc,{f},40,{ac},{ac},&H00000000,&H00000000,-1,0,0,0,100,100,2,0,1,0,0,2,90,90,90,1",
            f"Sub,{f},52,{tx},{tx},&H00000000,&HA0000000,-1,0,0,0,100,100,0,0,3,14,0,2,110,110,300,1",
            f"Bar,{f},20,{ac},{ac},&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1"]

async def tts(text, voice, path):
    if os.environ.get("FAKE_TTS"):
        d = max(1.5, len(text) / 15)
        sh("ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"sine=f=220:d={d:.2f}", path); return
    import edge_tts
    await edge_tts.Communicate(text, voice, rate="+4%").save(path)

def genera_video(spec, B):
    scenes = spec.get("scene") or []
    voice = B.get("voce") or "it-IT-IsabellaNeural"
    ev, t, aud = [], 0.0, []
    for i, s in enumerate(scenes):
        mp = f"{W}/v{i}.mp3"
        asyncio.run(tts(s.get("voce") or s.get("titolo") or "", voice, mp))
        d = max(float(s.get("durata_s") or 0), probe(mp)[0] + 0.5)
        aud.append((mp, d))
        a, b = t, t + d
        ev.append((1, a, b, "Tit", "{\\fad(250,150)\\move(540,1000,540,900,0,300)}" + esc_ass(s.get("titolo"))))
        if s.get("testo"): ev.append((1, a + 0.4, b, "Txt", "{\\fad(300,150)\\pos(540,1060)}" + esc_ass(s["testo"])))
        ev.append((0, a, b, "Bar", f"{{\\p1\\pos(0,0)\\t(0,{int(d * 1000)},\\fscx100)}}m 0 0 l {int(1080 * (i + 1) / len(scenes))} 0 {int(1080 * (i + 1) / len(scenes))} 10 0 10"))
        words = (s.get("voce") or "").split()
        if words:
            k, per = 0, (d - 0.5) / max(1, len(words))
            while k < len(words):
                part = words[k:k + 4]
                ev.append((2, a + k * per, a + (k + len(part)) * per, "Sub", esc_ass(" ".join(part))))
                k += 4
        t = b
    ev.append((1, 0, t, "Acc", esc_ass(B.get("firma") or "Iuris Studio")))
    ass_file(f"{W}/video.ass", 1080, 1920, brand_styles(B, 1080), ev)
    # voce: ogni scena con il suo silenzio di coda
    lst = open(f"{W}/voce.txt", "w")
    for i, (mp, d) in enumerate(aud):
        sh("ffmpeg", "-y", "-loglevel", "error", "-i", mp, "-af", f"apad=whole_dur={d:.3f},aresample=48000", "-ac", "2", f"{W}/p{i}.wav")
        lst.write(f"file 'p{i}.wav'\n")
    lst.close()
    sh("ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", f"{W}/voce.txt", f"{W}/voce.wav")
    out = f"{W}/video.mp4"
    bg = B.get("sfondo", "#0F1B2D").lstrip("#")
    sh("ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"color=c=0x{bg}:s=1080x1920:r=30:d={t:.2f}", "-i", f"{W}/voce.wav",
       "-vf", f"ass={W}/video.ass", "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k", "-af", "loudnorm=I=-14:TP=-1.5", "-shortest", "-movflags", "+faststart", out)
    sh("ffmpeg", "-y", "-loglevel", "error", "-ss", "0.6", "-i", out, "-frames:v", "1", f"{W}/copertina.jpg")
    return out

def genera_carosello(spec, B):
    slides = spec.get("slide") or []
    bg = B.get("sfondo", "#0F1B2D").lstrip("#")
    files = []
    for i, s in enumerate(slides):
        body = esc_ass(s.get("testo"))
        if s.get("punti"): body += ("\\N\\N" if body else "") + "\\N".join("• " + esc_ass(p) for p in s["punti"])
        ev = [(1, 0, 5, "Tit", ("{\\pos(540,420)}" if body else "{\\pos(540,620)}") + esc_ass(s.get("titolo"))),
              (1, 0, 5, "Txt", "{\\pos(540,620)}" + body) if body else None,
              (1, 0, 5, "Acc", esc_ass(f"{B.get('firma') or 'Iuris Studio'}   {i + 1}/{len(slides)}")),
              (0, 0, 5, "Bar", f"{{\\p1\\pos(0,0)}}m 0 0 l 1080 0 1080 12 0 12")]
        ass_file(f"{W}/s{i}.ass", 1080, 1350, brand_styles(B, 1350), [e for e in ev if e])
        fp = f"{W}/slide{i + 1:02d}.png"
        sh("ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"color=c=0x{bg}:s=1080x1350:d=1", "-vf", f"ass={W}/s{i}.ass", "-frames:v", "1", fp)
        files.append(fp)
    return files

def iuris():
    br = http(q("/montaggio/brief"))
    spec, B = br.get("spec") or {}, br.get("brand") or {}
    base = re.sub(r"[^\w\- àèéìòù]", "", br.get("title") or spec.get("titolo") or "iuris").strip()[:80]
    if MODE == "carosello":
        stage("disegno le slide")
        files = genera_carosello(spec, B)
        stage("carico su OneDrive")
        first = None
        for fp in files:
            r = upload(fp, f"{base} · {os.path.basename(fp)}")
            first = first or r
        done(plan={"caption": spec.get("caption")}, slides=len(files), out_item=first.get("id"), out_url=first.get("webUrl"), out_size=os.path.getsize(files[0]))
    else:
        stage("voce fuori campo e grafica")
        out = genera_video(spec, B)
        stage("carico su OneDrive")
        up = upload(out, f"{base}.mp4")
        upload(f"{W}/copertina.jpg", f"{base} · copertina.jpg")
        done(plan={"caption": spec.get("caption")}, out_duration=probe(out)[0], out_item=up.get("id"), out_url=up.get("webUrl"), out_size=os.path.getsize(out))

if __name__ == "__main__":
    try:
        montaggio() if MODE == "montaggio" else iuris()
    except Exception as e:
        import traceback; traceback.print_exc()
        try: done(error=f"{type(e).__name__}: {e}"[:400])
        except Exception as e2: print("done", e2)
        raise
