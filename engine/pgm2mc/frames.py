"""Stream small grayscale thumbnails out of ffmpeg (rawvideo pipe), aligned to PGM frames."""
import subprocess
import tempfile

import numpy as np

from .probe import EngineError

DEFAULT_SIZE = (64, 36)


class ThumbReader:
    """Sequential reader of WxH gray thumbnails starting at file-local frame `start_frame`.

    Negative `start_frame` yields "no media" frames until frame 0; frames past the end of the file are
    also "no media". read(n) always returns exactly n frames plus a validity mask.
    """

    def __init__(self, ffmpeg, path, fps, start_frame=0, size=DEFAULT_SIZE, hwaccel=None):
        self.w, self.h = size
        self.frame_bytes = self.w * self.h
        self.pad = max(0, -start_frame)
        self.eof = False
        self.decoded = 0
        self.path = path
        self._err = tempfile.TemporaryFile()
        cmd = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error"]
        if hwaccel:
            cmd += ["-hwaccel", hwaccel]
        if start_frame > 0:
            # seek half a frame early: the first frame with pts >= ss is exactly start_frame
            cmd += ["-ss", "%.6f" % ((start_frame - 0.5) / float(fps))]
        cmd += ["-i", path, "-map", "0:v:0", "-an", "-sn", "-dn",
                # setpts: after a seek the first frame is not at t=0 and cfr output would duplicate it
                "-vf", "setpts=PTS-STARTPTS,scale=%d:%d:flags=area,format=gray" % (self.w, self.h),
                "-f", "rawvideo", "-pix_fmt", "gray", "-"]
        self.proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=self._err,
                                     bufsize=self.frame_bytes * 64)

    def read(self, n):
        out = np.zeros((n, self.h, self.w), dtype=np.uint8)
        valid = np.zeros(n, dtype=bool)
        i = min(self.pad, n)
        self.pad -= i
        if i < n and not self.eof:
            want = n - i
            buf = self.proc.stdout.read(want * self.frame_bytes)
            got = len(buf) // self.frame_bytes
            if got:
                out[i:i + got] = np.frombuffer(buf[:got * self.frame_bytes], dtype=np.uint8).reshape(
                    got, self.h, self.w)
                valid[i:i + got] = True
                self.decoded += got
            if got < want:
                self._finish()
        return out, valid

    def _finish(self):
        self.eof = True
        self.proc.stdout.close()
        rc = self.proc.wait()
        if rc != 0:
            self._err.seek(0)
            msg = self._err.read().decode("utf-8", "replace").strip()
            raise EngineError("ffmpeg failed decoding %s: %s" % (self.path, msg[-800:]))

    def close(self):
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait()
        if self.proc.stdout and not self.proc.stdout.closed:
            self.proc.stdout.close()
        self._err.close()


def normalise(thumbs):
    """(n, h, w) uint8 -> (n, h*w) float32, zero mean and unit L2 norm per frame (flat frames -> zeros)."""
    x = thumbs.reshape(thumbs.shape[0], thumbs.shape[1] * thumbs.shape[2]).astype(np.float32)
    x -= x.mean(axis=1, keepdims=True)
    norm = np.sqrt(np.einsum("ij,ij->i", x, x))
    flat = norm < np.sqrt(x.shape[1])  # pixel std < 1 grey level
    norm[flat] = 1.0
    x /= norm[:, None]
    x[flat] = 0.0
    return x
