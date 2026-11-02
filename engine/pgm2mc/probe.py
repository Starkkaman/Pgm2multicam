"""Media probing: ffprobe JSON when available, else parse `ffmpeg -i` stderr."""
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from fractions import Fraction
from typing import List, Optional


class EngineError(Exception):
    pass


EXTRA_TOOL_DIRS = ["/opt/homebrew/bin", "/usr/local/bin"]


def find_tool(name, explicit=None):
    """Locate ffmpeg/ffprobe: explicit path, $PGM2MC_<NAME>, PATH, then common macOS dirs. None if absent."""
    candidates = []
    if explicit:
        candidates.append(explicit)
    env = os.environ.get("PGM2MC_" + name.upper())
    if env:
        candidates.append(env)
    for c in candidates:
        if os.path.isfile(c) and os.access(c, os.X_OK):
            return c
        found = shutil.which(c)
        if found:
            return found
    found = shutil.which(name)
    if found:
        return found
    for d in EXTRA_TOOL_DIRS:
        for exe in (name, name + ".exe"):
            p = os.path.join(d, exe)
            if os.path.isfile(p) and os.access(p, os.X_OK):
                return p
    return None


STANDARD_RATES = [Fraction(24000, 1001), Fraction(24), Fraction(25), Fraction(30000, 1001), Fraction(30),
                  Fraction(48000, 1001), Fraction(48), Fraction(50), Fraction(60000, 1001), Fraction(60),
                  Fraction(100), Fraction(120000, 1001), Fraction(120)]


def snap_rate(rate):
    """Snap a measured/rounded rate to the nearest standard broadcast rate (within 0.1%)."""
    rate = Fraction(rate)
    if rate <= 0:
        return rate
    for std in STANDARD_RATES:
        if abs(rate - std) / std < Fraction(1, 1000):
            return std
    if abs(rate - round(rate)) < Fraction(1, 1000):
        return Fraction(round(rate))
    return rate.limit_denominator(1001)


def parse_rate(text):
    """'30000/1001' / '25' / '29.97' -> Fraction, None when invalid (0/0)."""
    if not text:
        return None
    try:
        if "/" in text:
            n, d = text.split("/", 1)
            n, d = int(n), int(d)
            if n <= 0 or d <= 0:
                return None
            return Fraction(n, d)
        v = Fraction(text)
        return v if v > 0 else None
    except (ValueError, ZeroDivisionError):
        return None


def nominal_fps(fps):
    return int(round(float(fps)))


TC_RE = re.compile(r"^(\d{1,2})[:](\d{2})[:](\d{2})([:;,.])(\d{2,3})$")


def is_drop_frame_tc(tc):
    return bool(tc) and (";" in tc or "," in tc)


def tc_to_frames(tc, fps):
    """SMPTE timecode string -> frame count. ';' or ',' before the frames field means drop-frame."""
    m = TC_RE.match(tc.strip())
    if not m:
        raise EngineError("unparseable timecode %r" % tc)
    h, mi, s, sep, f = int(m.group(1)), int(m.group(2)), int(m.group(3)), m.group(4), int(m.group(5))
    nom = nominal_fps(fps)
    frames = ((h * 60 + mi) * 60 + s) * nom + f
    if sep in ";," and nom % 30 == 0:
        drop = nom // 15
        total_min = h * 60 + mi
        frames -= drop * (total_min - total_min // 10)
    return frames


def frames_per_day(fps, drop_frame):
    return tc_to_frames("24:00:00;00" if drop_frame else "24:00:00:00", fps)


@dataclass
class MediaInfo:
    path: str
    fps: Fraction
    duration_frames: int
    duration_seconds: float = 0.0
    start_tc: Optional[str] = None
    start_tc_frames: int = 0
    drop_frame: bool = False
    width: int = 0
    height: int = 0
    has_audio: bool = False
    audio_rate: int = 0
    audio_channels: int = 0
    warnings: List[str] = field(default_factory=list)

    @property
    def has_timecode(self):
        return self.start_tc is not None

    @property
    def name(self):
        return os.path.splitext(os.path.basename(self.path))[0]


def _finish(info):
    if info.start_tc:
        try:
            info.start_tc_frames = tc_to_frames(info.start_tc, info.fps)
            info.drop_frame = is_drop_frame_tc(info.start_tc)
        except EngineError as e:
            info.warnings.append("%s: %s (ignored)" % (os.path.basename(info.path), e))
            info.start_tc = None
            info.start_tc_frames = 0
    return info


def parse_ffprobe_json(data, path):
    """Build MediaInfo from `ffprobe -print_format json -show_streams -show_format` output (dict or str)."""
    if isinstance(data, (str, bytes)):
        data = json.loads(data)
    streams = data.get("streams") or []
    fmt = data.get("format") or {}
    video = next((s for s in streams if s.get("codec_type") == "video"
                  and not (s.get("disposition") or {}).get("attached_pic")), None)
    if video is None:
        raise EngineError("%s: no video stream" % path)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)

    r = parse_rate(video.get("r_frame_rate"))
    avg = parse_rate(video.get("avg_frame_rate"))
    fps = r or avg
    if r and avg and abs(r - avg) / avg > Fraction(1, 100):
        fps = avg
    if not fps:
        raise EngineError("%s: unknown frame rate" % path)
    fps = snap_rate(fps)

    def _float(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    dur_s = _float(video.get("duration")) or _float(fmt.get("duration")) or 0.0
    nb = video.get("nb_frames")
    try:
        nb = int(nb)
    except (TypeError, ValueError):
        nb = 0
    duration_frames = nb if nb > 0 else int(round(dur_s * float(fps)))

    tc = None
    for s in [video] + [s for s in streams if s is not video]:
        t = (s.get("tags") or {}).get("timecode")
        if t:
            tc = t
            break
    if tc is None:
        tc = (fmt.get("tags") or {}).get("timecode")

    info = MediaInfo(path=path, fps=fps, duration_frames=duration_frames, duration_seconds=dur_s,
                     start_tc=tc, width=int(video.get("width") or 0), height=int(video.get("height") or 0))
    if audio is not None:
        info.has_audio = True
        info.audio_rate = int(_float(audio.get("sample_rate")) or 0)
        info.audio_channels = int(audio.get("channels") or 0)
    return _finish(info)


CHANNEL_LAYOUTS = {"mono": 1, "stereo": 2, "2.1": 3, "3.0": 3, "quad": 4, "4.0": 4, "5.0": 5, "5.1": 6,
                   "6.1": 7, "7.1": 8, "downmix": 2}


def _fps_from_float_text(text):
    v = float(text)
    table = {"23.98": Fraction(24000, 1001), "23.976": Fraction(24000, 1001), "29.97": Fraction(30000, 1001),
             "59.94": Fraction(60000, 1001), "47.95": Fraction(48000, 1001), "119.88": Fraction(120000, 1001)}
    if text in table:
        return table[text]
    return snap_rate(Fraction(v).limit_denominator(1001))


def parse_ffmpeg_stderr(text, path):
    """Fallback: parse the banner `ffmpeg -hide_banner -i FILE` prints on stderr."""
    vline = None
    aline = None
    for line in text.splitlines():
        if vline is None and re.search(r"Stream #\d+:\d+.*?: Video:", line) and "attached pic" not in line:
            vline = line
        elif aline is None and re.search(r"Stream #\d+:\d+.*?: Audio:", line):
            aline = line
    if vline is None:
        raise EngineError("%s: no video stream (ffmpeg says: %s)" % (path, text.strip().splitlines()[-1:]))

    m = re.search(r"([\d.]+)(k?) fps", vline) or re.search(r"([\d.]+)(k?) tbr", vline)
    if not m:
        raise EngineError("%s: unknown frame rate" % path)
    fps = _fps_from_float_text(m.group(1))
    if m.group(2) == "k":
        fps = snap_rate(fps * 1000)

    width = height = 0
    m = re.search(r"[ ,](\d{2,5})x(\d{2,5})[ ,\[]", vline + " ")
    if m:
        width, height = int(m.group(1)), int(m.group(2))

    dur_s = 0.0
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", text)
    if m:
        dur_s = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))

    tc = None
    m = re.search(r"^\s*timecode\s*:\s*(\S+)\s*$", text, re.M)
    if m:
        tc = m.group(1)

    info = MediaInfo(path=path, fps=fps, duration_frames=int(round(dur_s * float(fps))), duration_seconds=dur_s,
                     start_tc=tc, width=width, height=height)
    if aline:
        info.has_audio = True
        m = re.search(r"(\d+) Hz,\s*([^,]+)", aline)
        if m:
            info.audio_rate = int(m.group(1))
            layout = m.group(2).strip()
            mc = re.match(r"(\d+) channels", layout)
            if mc:
                info.audio_channels = int(mc.group(1))
            else:
                info.audio_channels = CHANNEL_LAYOUTS.get(layout.split("(")[0], 2)
    return _finish(info)


def probe(path, ffprobe=None, ffmpeg=None):
    """Probe one file. Uses ffprobe when found, else ffmpeg's stderr."""
    if not os.path.isfile(path):
        raise EngineError("file not found: %s" % path)
    path = os.path.abspath(path)
    if ffprobe:
        cmd = [ffprobe, "-v", "error", "-print_format", "json", "-show_streams", "-show_format", path]
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if p.returncode != 0:
            raise EngineError("ffprobe failed on %s: %s" % (path, p.stderr.decode("utf-8", "replace").strip()))
        return parse_ffprobe_json(p.stdout.decode("utf-8", "replace"), path)
    if not ffmpeg:
        raise EngineError("neither ffprobe nor ffmpeg found (set PGM2MC_FFMPEG or install ffmpeg)")
    p = subprocess.run([ffmpeg, "-nostdin", "-hide_banner", "-i", path], stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE)
    return parse_ffmpeg_stderr(p.stderr.decode("utf-8", "replace"), path)
