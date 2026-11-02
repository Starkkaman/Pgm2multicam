-- PGM to Multicam: DaVinci Resolve script (Workspace > Scripts > Utility > PGM to Multicam).
--
-- Lua port of "PGM to Multicam.py" (Resolve embeds Lua, so no Python is needed). Pick the live-cut program clip
-- and the ISO clips in the Media Pool; the script runs the self-contained pgm2mc engine in the background,
-- follows its output and imports the resulting FCPXML timelines. Lua 5.1 / LuaJIT, no external modules.

local M = {}
local unpack = unpack or table.unpack

M.TITLE = "PGM to Multicam"
M.BIN_NAME = "PGM to Multicam"
M.PROGRAM_PATTERNS = {"pgm", "prog", "pvw[ _%-]?out"} -- matched against the lower-cased name
M.SYNC_CHOICES = {"Auto", "Timecode", "Audio"}
M.AUDIO_CHOICES = {"Program audio", "Follow camera"}
M.STAGE_LABELS = {probe = "Probing", sync = "Syncing", frames = "Reading frames", match = "Matching",
                  write = "Writing"}
M.TIMELINE_SUFFIX = {multicam = " Multicam Cut", conform = " Conform", stacked = " Stacked"}
M.IMPORT_ORDER = {"multicam", "conform", "stacked"}
M.COMPOUND_HINT = "Tip: if the multicam arrives as a compound clip, right-click it in the Media Pool > "
    .. "\"Convert Compound Clips to Multicam Clips\" (or use the Conform timeline)."
M.NON_VIDEO_TYPES = {"timeline", "multicam", "compound", "fusion", "generator", "still", "subtitle"}
M.DEFAULT_REBUILD = {dissolves = true, stills = true, stills_min_repeat = 2, supersource = true}
M.BOX_PROP_ORDER = {"Scaling", "ZoomGang", "ZoomX", "ZoomY", "Pan", "Tilt", "CropRetain",
                    "CropLeft", "CropRight", "CropTop", "CropBottom"}
M.FIXUP_KINDS = {"multicam", "conform"}
M.INSTALL_HINT = "Run Install.command (from the PGM to Multicam download) and restart Resolve."

local ELLIPSIS = "\226\128\166"
local PLUSMINUS = "\194\177"

-- ---------------------------------------------------------------- small utilities

local function trim(s)
    return (tostring(s or ""):gsub("^%s+", ""):gsub("%s+$", ""))
end

local function starts_with(s, prefix)
    return s:sub(1, #prefix) == prefix
end

local function toint(v)
    v = tonumber(v)
    if v == nil or v ~= v then
        return nil
    end
    if v >= 0 then
        return math.floor(v)
    end
    return math.ceil(v)
end

local function num_str(v)
    if type(v) == "number" and v == math.floor(v) and math.abs(v) < 1e15 then
        return string.format("%d", v)
    end
    return tostring(v)
end

local function contains(list, value)
    for _, v in ipairs(list) do
        if v == value then
            return true
        end
    end
    return false
end

local function sorted_keys(t)
    local keys = {}
    for k in pairs(t) do
        keys[#keys + 1] = k
    end
    table.sort(keys, function(a, b) return tostring(a) < tostring(b) end)
    return keys
end

function M.as_list(x)
    if type(x) ~= "table" then
        return {}
    end
    local nums, others = {}, {}
    for k in pairs(x) do
        if type(k) == "number" then
            nums[#nums + 1] = k
        else
            others[#others + 1] = k
        end
    end
    table.sort(nums)
    table.sort(others, function(a, b) return tostring(a) < tostring(b) end)
    local out = {}
    for _, k in ipairs(nums) do
        out[#out + 1] = x[k]
    end
    for _, k in ipairs(others) do
        out[#out + 1] = x[k]
    end
    return out
end

-- obj:name(...) that never raises; nil on any failure
local function call(obj, name, ...)
    if obj == nil then
        return nil
    end
    local n, args = select("#", ...), {...}
    local ok, res = pcall(function()
        local f = obj[name]
        if f == nil then
            return nil
        end
        return f(obj, unpack(args, 1, n))
    end)
    if ok then
        return res
    end
    return nil
end
M.call = call

function M.has_api(obj, name)
    local ok, res = pcall(function() return obj[name] ~= nil end)
    return ok and res == true
end

-- ---------------------------------------------------------------- JSON decoder

local json = {}
M.json = json
json.null = setmetatable({}, {__tostring = function() return "null" end}) -- only used inside arrays

local function utf8_char(cp)
    if cp < 0x80 then
        return string.char(cp)
    elseif cp < 0x800 then
        return string.char(0xC0 + math.floor(cp / 0x40), 0x80 + cp % 0x40)
    elseif cp < 0x10000 then
        return string.char(0xE0 + math.floor(cp / 0x1000), 0x80 + math.floor(cp / 0x40) % 0x40, 0x80 + cp % 0x40)
    end
    return string.char(0xF0 + math.floor(cp / 0x40000), 0x80 + math.floor(cp / 0x1000) % 0x40,
        0x80 + math.floor(cp / 0x40) % 0x40, 0x80 + cp % 0x40)
end

local ESCAPES = {['"'] = '"', ["\\"] = "\\", ["/"] = "/", b = "\b", f = "\f", n = "\n", r = "\r", t = "\t"}

-- Decode a JSON text. null becomes nil in objects (key absent) and json.null in arrays (keeps indices).
function json.decode(s)
    if type(s) ~= "string" then
        error("JSON error: not a string", 0)
    end
    local pos = 1
    local value

    local function fail(msg)
        error(string.format("JSON error at position %d: %s", pos, msg), 0)
    end

    local function skip_ws()
        pos = s:find("[^ \t\r\n]", pos) or (#s + 1)
    end

    local function str()
        pos = pos + 1
        local buf = {}
        while true do
            local a = s:find('["\\]', pos)
            if not a then
                fail("unterminated string")
            end
            buf[#buf + 1] = s:sub(pos, a - 1)
            if s:sub(a, a) == '"' then
                pos = a + 1
                return table.concat(buf)
            end
            local c = s:sub(a + 1, a + 1)
            if c == "u" then
                local hex = s:sub(a + 2, a + 5)
                if not hex:match("^%x%x%x%x$") then
                    pos = a
                    fail("bad \\u escape")
                end
                local cp = tonumber(hex, 16)
                pos = a + 6
                if cp >= 0xD800 and cp <= 0xDBFF then
                    local lo = s:match("^\\u(%x%x%x%x)", pos)
                    local l = lo and tonumber(lo, 16)
                    if l and l >= 0xDC00 and l <= 0xDFFF then
                        cp = 0x10000 + (cp - 0xD800) * 0x400 + (l - 0xDC00)
                        pos = pos + 6
                    end
                end
                buf[#buf + 1] = utf8_char(cp)
            else
                local e = ESCAPES[c]
                if not e then
                    pos = a
                    fail("bad escape")
                end
                buf[#buf + 1] = e
                pos = a + 2
            end
        end
    end

    local function number()
        local start = pos
        local m = s:match("^-?%d+", pos)
        if not m then
            fail("unexpected character '" .. s:sub(pos, pos) .. "'")
        end
        pos = pos + #m
        local frac = s:match("^%.%d+", pos)
        if frac then
            pos = pos + #frac
        end
        local exp = s:match("^[eE][-+]?%d+", pos)
        if exp then
            pos = pos + #exp
        end
        return tonumber(s:sub(start, pos - 1))
    end

    local function array()
        pos = pos + 1
        local out, n = {}, 0
        skip_ws()
        if s:sub(pos, pos) == "]" then
            pos = pos + 1
            return out
        end
        while true do
            local v = value()
            n = n + 1
            if v == nil then
                out[n] = json.null
            else
                out[n] = v
            end
            skip_ws()
            local c = s:sub(pos, pos)
            pos = pos + 1
            if c == "]" then
                return out
            elseif c ~= "," then
                pos = pos - 1
                fail("expected ',' or ']'")
            end
        end
    end

    local function object()
        pos = pos + 1
        local out = {}
        skip_ws()
        if s:sub(pos, pos) == "}" then
            pos = pos + 1
            return out
        end
        while true do
            skip_ws()
            if s:sub(pos, pos) ~= '"' then
                fail("expected a string key")
            end
            local k = str()
            skip_ws()
            if s:sub(pos, pos) ~= ":" then
                fail("expected ':'")
            end
            pos = pos + 1
            out[k] = value()
            skip_ws()
            local c = s:sub(pos, pos)
            pos = pos + 1
            if c == "}" then
                return out
            elseif c ~= "," then
                pos = pos - 1
                fail("expected ',' or '}'")
            end
        end
    end

    value = function()
        skip_ws()
        local c = s:sub(pos, pos)
        if c == "{" then
            return object()
        elseif c == "[" then
            return array()
        elseif c == '"' then
            return str()
        elseif s:sub(pos, pos + 3) == "true" then
            pos = pos + 4
            return true
        elseif s:sub(pos, pos + 4) == "false" then
            pos = pos + 5
            return false
        elseif s:sub(pos, pos + 3) == "null" then
            pos = pos + 4
            return nil
        elseif c == "" then
            fail("unexpected end of input")
        end
        return number()
    end

    local result = value()
    skip_ws()
    if pos <= #s then
        fail("trailing characters")
    end
    return result
end

-- ---------------------------------------------------------------- files and shell

function M.shell_quote(s)
    return "'" .. tostring(s):gsub("'", "'\\''") .. "'"
end

function M.shell_join(argv)
    local parts = {}
    for i, a in ipairs(argv) do
        parts[i] = M.shell_quote(a)
    end
    return table.concat(parts, " ")
end

function M.exec_ok(cmd)
    local a, _, c = os.execute(cmd)
    if type(a) == "number" then
        return a == 0
    end
    return a == true and (c == nil or c == 0)
end

function M.read_file(path)
    local f = io.open(path, "rb")
    if not f then
        return nil
    end
    local data = f:read("*a")
    f:close()
    return data
end

function M.is_dir(path)
    if not path or path == "" then
        return false
    end
    local f = io.open(path .. "/.", "r")
    if f then
        f:close()
        return true
    end
    return false
end

function M.is_file(path)
    if not path or path == "" or M.is_dir(path) then
        return false
    end
    local f = io.open(path, "rb")
    if f then
        f:close()
        return true
    end
    return false
end

function M.is_writable(path)
    return M.exec_ok("test -w " .. M.shell_quote(path))
end

function M.make_dirs(path)
    if M.is_dir(path) then
        return true
    end
    M.exec_ok("mkdir -p " .. M.shell_quote(path) .. " >/dev/null 2>&1")
    return M.is_dir(path)
end

-- Output lines of a shell command (io.popen, or a temp file where popen is unavailable).
function M.command_lines(cmd)
    local text
    local ok, p = pcall(io.popen, cmd)
    if ok and p then
        text = p:read("*a")
        p:close()
    else
        local tmp = os.tmpname()
        M.exec_ok(cmd .. " > " .. M.shell_quote(tmp))
        text = M.read_file(tmp)
        os.remove(tmp)
    end
    local lines = {}
    for line in (text or ""):gmatch("[^\n]+") do
        lines[#lines + 1] = line
    end
    return lines
end

function M.list_dir(path)
    return M.command_lines("ls -1 " .. M.shell_quote(path) .. " 2>/dev/null")
end

function M.sleep(sec)
    local b = rawget(_G, "bmd")
    if b ~= nil and pcall(function() b.wait(sec) end) then
        return
    end
    os.execute("sleep " .. tostring(sec))
end

function M.basename(path)
    return (tostring(path or ""):match("([^/]*)/*$"))
end

function M.dirname(path)
    local d = tostring(path or ""):match("^(.*)/[^/]*$")
    if d == nil then
        return ""
    end
    if d == "" then
        return "/"
    end
    return d
end

function M.join(a, b)
    if a == "" then
        return b
    end
    if a:sub(-1) == "/" then
        return a .. b
    end
    return a .. "/" .. b
end

function M.file_stem(path)
    local base = M.basename(path)
    return base:match("^(.+)%.[^.]*$") or base
end

function M.norm_path(p)
    if not p or p == "" then
        return ""
    end
    local absolute = p:sub(1, 1) == "/"
    local parts = {}
    for seg in p:gmatch("[^/]+") do
        if seg == ".." then
            if #parts > 0 and parts[#parts] ~= ".." then
                parts[#parts] = nil
            elseif not absolute then
                parts[#parts + 1] = seg
            end
        elseif seg ~= "." then
            parts[#parts + 1] = seg
        end
    end
    local s = table.concat(parts, "/")
    if absolute then
        return "/" .. s
    end
    return s ~= "" and s or "."
end

-- ---------------------------------------------------------------- pure helpers (tested without Resolve)

function M.is_program_name(name)
    local n = tostring(name or ""):lower()
    for _, pat in ipairs(M.PROGRAM_PATTERNS) do
        if n:find(pat) then
            return true
        end
    end
    return false
end

-- 1-based indices of names that look like the program recording
function M.program_candidates(names)
    local out = {}
    for i, n in ipairs(names) do
        if M.is_program_name(n) then
            out[#out + 1] = i
        end
    end
    return out
end

function M.guess_program_index(names)
    return M.program_candidates(names)[1] or 1
end

function M.is_video_type(clip_type)
    local t = trim(clip_type):lower()
    if t == "" or not t:find("video", 1, true) then
        return false
    end
    for _, bad in ipairs(M.NON_VIDEO_TYPES) do
        if t:find(bad, 1, true) then
            return false
        end
    end
    return true
end

-- A 'pgm2multicam' folder next to the PGM file, else ~/Movies/pgm2multicam.
function M.default_output_dir(pgm_path, home, writable)
    writable = writable or function(d) return M.is_dir(d) and M.is_writable(d) end
    home = home or os.getenv("HOME") or ""
    if pgm_path and pgm_path ~= "" then
        local parent = M.dirname(pgm_path)
        local target = M.join(parent, "pgm2multicam")
        if (M.is_dir(target) and writable(target)) or writable(parent) then
            return target
        end
    end
    return M.join(M.join(home, "Movies"), "pgm2multicam")
end

function M.platform()
    local j = rawget(_G, "jit")
    if type(j) == "table" and j.os then
        if j.os == "OSX" then
            return "darwin"
        elseif j.os == "Windows" then
            return "win32"
        end
        return "linux"
    end
    if package.config:sub(1, 1) == "\\" then
        return "win32"
    end
    return M.command_lines("uname -s 2>/dev/null")[1] == "Darwin" and "darwin" or "linux"
end

function M.app_dir(platform, env, home)
    platform = platform or M.platform()
    env = env or {}
    home = home or os.getenv("HOME") or ""
    if platform == "darwin" then
        return home .. "/Library/Application Support/pgm2multicam"
    end
    local base = env.XDG_DATA_HOME or os.getenv("XDG_DATA_HOME")
    if not base or base == "" then
        base = home .. "/.local/share"
    end
    return base .. "/pgm2multicam"
end

function M.engine_from_bundle(dir)
    dir = dir:gsub("/+$", "")
    return {kind = "bundle", dir = dir, exe = dir .. "/pgm2mc", cmd = {dir .. "/pgm2mc"},
            ffmpeg = dir .. "/bin/ffmpeg", ffprobe = dir .. "/bin/ffprobe", env = {}}
end

function M.engine_from_python(python, engine_dir, cfg)
    return {kind = "python", dir = engine_dir, exe = python, cmd = {python, "-u", "-m", "pgm2mc"},
            ffmpeg = cfg.ffmpeg, ffprobe = cfg.ffprobe,
            env = {PYTHONPATH = engine_dir, PYTHONUNBUFFERED = "1", PYTHONIOENCODING = "utf-8"},
            path_prefix = "/opt/homebrew/bin:/usr/local/bin"}
end

-- Engine to run: config.json ("engine" = bundle folder, or legacy "python"/"engine_dir"), else the default
-- bundle in the app folder. Returns engine, or nil and a readable message.
function M.load_config(appdir, platform)
    appdir = appdir or M.app_dir(platform)
    local path = appdir .. "/config.json"
    local default_dir = appdir .. "/engine/pgm2mc"
    local cfg, problem = {}, nil
    local text = M.read_file(path)
    if text then
        text = text:gsub("^\239\187\191", "")
        local ok, data = pcall(json.decode, text)
        if not ok or type(data) ~= "table" then
            return nil, string.format("Cannot read %s: %s. %s", path, tostring(ok and "not a JSON object" or data),
                M.INSTALL_HINT)
        end
        cfg = data
    end
    if type(cfg.engine) == "string" and cfg.engine ~= "" then
        local e = M.engine_from_bundle(cfg.engine)
        if M.is_file(e.exe) then
            return e
        end
        problem = string.format("config.json points to %s, but there is no pgm2mc engine in it.", cfg.engine)
    elseif cfg.python or cfg.engine_dir then
        local py = cfg.python or (appdir .. "/venv/bin/python")
        local edir = cfg.engine_dir or (appdir .. "/engine")
        if M.is_file(py) and M.is_file(edir .. "/pgm2mc/__init__.py") then
            return M.engine_from_python(py, edir, cfg)
        end
        problem = string.format("config.json names Python %s and engine folder %s, but they are incomplete.",
            tostring(py), tostring(edir))
    end
    local e = M.engine_from_bundle(default_dir)
    if M.is_file(e.exe) then
        return e
    end
    local msg = "pgm2multicam engine is not installed: no engine at " .. default_dir .. "."
    if problem then
        msg = msg .. " " .. problem
    end
    return nil, msg .. " " .. M.INSTALL_HINT
end

function M.audio_flag(choice)
    local c = trim(choice or "pgm"):lower()
    if c == "" or c == "pgm" or c == "program audio" or c == "program" then
        return "pgm"
    end
    if c == "follow" or c == "follow camera" then
        return "follow"
    end
    error("audio must be pgm or follow", 0)
end

-- CLI flags for the v2 rebuild options; defaults add nothing (works with older engines).
function M.rebuild_flags(rebuild)
    local opts = {}
    for k, v in pairs(M.DEFAULT_REBUILD) do
        opts[k] = v
    end
    for k, v in pairs(rebuild or {}) do
        opts[k] = v
    end
    local flags = {}
    if not opts.dissolves then
        flags[#flags + 1] = "--no-dissolves"
    end
    if not opts.stills then
        flags[#flags + 1] = "--no-stills"
    else
        local n = toint(opts.stills_min_repeat)
        if not n or n == 0 then
            n = 2
        end
        n = math.max(1, math.min(20, n))
        if n ~= M.DEFAULT_REBUILD.stills_min_repeat then
            flags[#flags + 1] = "--stills-min-repeat"
            flags[#flags + 1] = num_str(n)
        end
    end
    if not opts.supersource then
        flags[#flags + 1] = "--no-supersource"
    end
    return flags
end

-- cmd = engine command prefix ({exe} or {python, "-u", "-m", "pgm2mc"}); o = name, sync, audio, min_shot,
-- refine, ffmpeg, ffprobe, rebuild
function M.build_argv(cmd, pgm, isos, outdir, o)
    o = o or {}
    local sync = (o.sync or "auto"):lower()
    if sync ~= "auto" and sync ~= "timecode" and sync ~= "audio" then
        error("sync must be auto, timecode or audio", 0)
    end
    local audio = M.audio_flag(o.audio)
    local argv = {}
    local function add(...)
        for _, v in ipairs({...}) do
            argv[#argv + 1] = v
        end
    end
    for _, c in ipairs(cmd) do
        add(c)
    end
    add(pgm)
    for _, iso in ipairs(isos) do
        add(iso)
    end
    add("-o", outdir)
    if o.name and o.name ~= "" then
        add("--name", o.name)
    end
    add("--sync", sync, "--audio", audio, "--min-shot", num_str(toint(o.min_shot or 6) or 6))
    if o.refine then
        add("--refine")
    end
    if o.ffmpeg and o.ffmpeg ~= "" then
        add("--ffmpeg", o.ffmpeg)
    end
    if o.ffprobe and o.ffprobe ~= "" then
        add("--ffprobe", o.ffprobe)
    end
    for _, f in ipairs(M.rebuild_flags(o.rebuild)) do
        add(f)
    end
    return argv
end

function M.plural(n, word, many)
    return string.format("%s %s", num_str(n), n == 1 and word or (many or word .. "s"))
end

-- '12 transitions, 3 stills, 2 SuperSource layouts' (only the counts the engine reported)
function M.result_summary(result)
    local parts = {}
    for _, kw in ipairs({{"transitions", "transition"}, {"stills", "still"},
                         {"supersources", "SuperSource layout"}}) do
        local v = result[kw[1]]
        if v ~= nil and v ~= json.null then
            local n = toint(v)
            if n then
                parts[#parts + 1] = M.plural(n, kw[2])
            end
        end
    end
    return table.concat(parts, ", ")
end

-- One engine output line -> kind, payload. kinds: progress ({stage, pct}), warn, error, result (table), log.
function M.parse_line(line)
    line = tostring(line or ""):gsub("[\r\n]+$", "")
    local s = trim(line)
    if starts_with(s, "PROGRESS ") then
        local parts = {}
        for w in s:gmatch("%S+") do
            parts[#parts + 1] = w
        end
        if #parts >= 3 then
            local v = toint(tonumber(parts[3]))
            if v and v == v and math.abs(v) ~= math.huge then
                return "progress", {parts[2], math.max(0, math.min(100, v))}
            end
        end
    elseif starts_with(s, "WARN ") then
        return "warn", trim(s:sub(6))
    elseif starts_with(s, "ERROR ") then
        return "error", trim(s:sub(7))
    elseif starts_with(s, "RESULT ") then
        local ok, data = pcall(json.decode, s:sub(8))
        if ok and type(data) == "table" and data[1] == nil then
            return "result", data
        end
    end
    return "log", line
end

function M.progress_text(stage, pct)
    local label = M.STAGE_LABELS[stage]
    if not label then
        if stage and stage ~= "" then
            label = stage:sub(1, 1):upper() .. stage:sub(2):lower()
        else
            label = "Working"
        end
    end
    return string.format("%s%s %d%%", label, ELLIPSIS, pct)
end

function M.describe_event(kind, payload)
    if kind == "progress" then
        return nil
    elseif kind == "warn" then
        return "Warning: " .. payload
    elseif kind == "error" then
        return "Error: " .. payload
    elseif kind == "result" then
        local extra = M.result_summary(payload)
        local seg = payload.segments
        return string.format("Engine finished: %s segments%s.", seg == nil and "?" or num_str(seg),
            extra ~= "" and ("; " .. extra) or "")
    end
    return payload
end

-- Base name so that none of the generated timeline names collides with an existing timeline.
function M.unique_base_name(base, existing_timeline_names)
    local existing = {}
    for _, n in ipairs(existing_timeline_names or {}) do
        existing[n] = true
    end
    local function clashes(c)
        for _, suf in pairs(M.TIMELINE_SUFFIX) do
            if existing[c .. suf] then
                return true
            end
        end
        return false
    end
    local candidate, n = base, 1
    while clashes(candidate) do
        n = n + 1
        candidate = string.format("%s %d", base, n)
    end
    return candidate
end

-- {{kind, fcpxml path, timeline name}, ...} in import order, for the kinds the user ticked.
function M.planned_imports(result, base_name, wanted)
    local out = {}
    for _, kind in ipairs(M.IMPORT_ORDER) do
        local path = result[kind]
        if wanted[kind] and type(path) == "string" and path ~= "" then
            out[#out + 1] = {kind, path, base_name .. M.TIMELINE_SUFFIX[kind]}
        end
    end
    return out
end

function M.missing_stills(still_files, pool_paths)
    local have = {}
    for _, p in ipairs(pool_paths) do
        if p and p ~= "" then
            have[M.norm_path(p)] = true
        end
    end
    local out = {}
    for _, f in ipairs(still_files) do
        if not have[M.norm_path(f)] then
            out[#out + 1] = f
        end
    end
    return out
end

-- ---------------------------------------------------------------- SuperSource box fix-up (RESEARCH2 §5.2/§7)

local function clamp_pair(a, b)
    a, b = math.max(0.0, tonumber(a)), math.max(0.0, tonumber(b))
    if a + b >= 0.99 then
        local k = 0.99 / (a + b)
        a, b = a * k, b * k
    end
    return a, b
end

-- TimelineItem:SetProperty values for one box (spatial conform = fit, so Zoom 1 = fitted ISO).
-- Pan/Tilt: timeline px from centre (+right / +up); Crop*: px of the fitted image.
function M.box_props(box, iso_w, iso_h, seq_w, seq_h, uniform_tol)
    uniform_tol = uniform_tol or 0.02
    local rect = box.rect
    local x0, y0, x1, y1 = tonumber(rect[1]), tonumber(rect[2]), tonumber(rect[3]), tonumber(rect[4])
    local crop = box.crop
    if type(crop) ~= "table" or crop[1] == nil then
        crop = {0, 0, 0, 0}
    end
    local l, t, r, b = tonumber(crop[1]), tonumber(crop[2]), tonumber(crop[3]), tonumber(crop[4])
    l, r = clamp_pair(l, r)
    t, b = clamp_pair(t, b)
    local fit = math.min(seq_w / iso_w, seq_h / iso_h)
    local cw, ch = iso_w * fit, iso_h * fit
    local sx = (x1 - x0) * seq_w / (cw * (1 - l - r))
    local sy = (y1 - y0) * seq_h / (ch * (1 - t - b))
    if math.abs(sx - sy) <= uniform_tol * math.max(sx, sy) then
        sx = (sx + sy) / 2
        sy = sx
    end
    local dcx, dcy = cw * (l - r) / 2, ch * (t - b) / 2
    local tcx, tcy = ((x0 + x1) / 2 - 0.5) * seq_w, ((y0 + y1) / 2 - 0.5) * seq_h
    local px = tcx - sx * dcx
    local py = -(tcy - sy * dcy)
    return {Scaling = 2, ZoomGang = (sx == sy), ZoomX = sx, ZoomY = sy, Pan = px + 0.0, Tilt = py + 0.0,
            CropRetain = true, CropLeft = l * cw, CropRight = r * cw, CropTop = t * ch, CropBottom = b * ch}
end

-- One entry per (SuperSource occurrence, box), in PGM frames. track = Resolve video track (boxes V3..).
function M.box_targets(analysis)
    local prog = analysis.program or {}
    local seq_w, seq_h = tonumber(prog.width), tonumber(prog.height)
    local isos = {}
    for _, iso in ipairs(analysis.isos or {}) do
        if type(iso) == "table" and iso.id ~= nil then
            isos[iso.id] = iso
        end
    end
    local tol = 0
    for _, tr in ipairs(analysis.transitions or {}) do
        if type(tr) == "table" then
            tol = math.max(tol, toint(tr.duration or 0) or 0)
        end
    end
    tol = tol + 2
    local out = {}
    if not seq_w or seq_w == 0 or not seq_h or seq_h == 0 then
        return out
    end
    for _, ss in ipairs(analysis.supersources or {}) do
        local boxes = {}
        for i, bx in ipairs(ss.boxes or {}) do
            boxes[#boxes + 1] = {i = i, box = bx, layer = tonumber(bx.layer) or 0}
        end
        table.sort(boxes, function(a, b)
            if a.layer ~= b.layer then
                return a.layer < b.layer
            end
            return a.i < b.i
        end)
        for rank, entry in ipairs(boxes) do
            local box = entry.box
            local iso = isos[box.angle]
            if iso and tonumber(iso.width) and tonumber(iso.width) ~= 0 and tonumber(iso.height)
                    and tonumber(iso.height) ~= 0 then
                local props = M.box_props(box, tonumber(iso.width), tonumber(iso.height), seq_w, seq_h)
                for _, occ in ipairs(ss.occurrences or {}) do
                    out[#out + 1] = {ss = ss.id, track = rank + 2, lo = toint(occ[1]) - tol, hi = toint(occ[2]) + tol,
                                     path = iso.path or "", name = iso.name or "", props = props}
                end
            end
        end
    end
    return out
end

function M.item_matches(item, path, name)
    local mpi = call(item, "GetMediaPoolItem")
    if mpi ~= nil then
        local fp = call(mpi, "GetClipProperty", "File Path")
        if type(fp) == "table" then
            fp = fp["File Path"]
        end
        if type(fp) == "string" and fp ~= "" and path and path ~= "" then
            return M.norm_path(fp) == M.norm_path(path)
        end
    end
    if not name or name == "" then
        return false
    end
    local names = {call(item, "GetName"), mpi ~= nil and call(mpi, "GetName") or nil}
    for i = 1, 2 do
        local n = names[i]
        if n ~= nil and n ~= "" and starts_with(tostring(n), name) then
            return true
        end
    end
    return false
end

-- Timeline item for a box target: its track first, then the other tracks above V2.
function M.find_box_item(timeline, target, start_frame, used)
    used = used or {}
    local ntracks
    local ok, n = pcall(function() return timeline:GetTrackCount("video") end)
    if ok then
        ntracks = toint(n or 0) or 0
    else
        ntracks = target.track
    end
    local order = {target.track}
    for k = 3, ntracks do
        if k ~= target.track then
            order[#order + 1] = k
        end
    end
    local lo, hi = start_frame + target.lo, start_frame + target.hi
    for _, track in ipairs(order) do
        for _, item in ipairs(M.as_list(call(timeline, "GetItemListInTrack", "video", track))) do
            if not used[item] then
                local st = toint(call(item, "GetStart"))
                if st and lo <= st and st < hi and M.item_matches(item, target.path, target.name) then
                    used[item] = true
                    return item
                end
            end
        end
    end
    return nil
end

-- SetProperty on every box found; returns applied, not_found, failed_keys (sorted list).
function M.apply_box_fixup(timeline, analysis)
    local ok, start = pcall(function() return toint(timeline:GetStartFrame() or 0) or 0 end)
    if not ok then
        start = 0
    end
    local applied, missing, failed, used = 0, 0, {}, {}
    for _, target in ipairs(M.box_targets(analysis)) do
        local item = M.find_box_item(timeline, target, start, used)
        if item == nil then
            missing = missing + 1
        else
            local good = true
            for _, key in ipairs(M.BOX_PROP_ORDER) do
                local pok, res = pcall(function() return item:SetProperty(key, target.props[key]) end)
                if not pok then
                    res = false
                end
                if res == false then
                    failed[key] = true
                    good = good and not contains({"ZoomX", "ZoomY", "Pan", "Tilt"}, key)
                end
            end
            if good then
                applied = applied + 1
            else
                missing = missing + 1
            end
        end
    end
    return applied, missing, sorted_keys(failed)
end

-- Re-apply box geometry on the imported Multicam Cut / Conform timelines. Never raises.
function M.fixup_supersources(created, result, log)
    local ok, err = pcall(function()
        if (toint(result.supersources) or 0) == 0 then
            return
        end
        local targets = {}
        for _, k in ipairs(M.FIXUP_KINDS) do
            if created[k] ~= nil then
                targets[#targets + 1] = created[k]
            end
        end
        if #targets == 0 then
            return
        end
        local text = M.read_file(tostring(result.analysis))
        if not text then
            error("cannot read " .. tostring(result.analysis), 0)
        end
        local analysis = json.decode(text)
        for _, tl in ipairs(targets) do
            local name = call(tl, "GetName") or "timeline"
            local fok, applied, missing, failed = pcall(M.apply_box_fixup, tl, analysis)
            if not fok then
                log(string.format("SuperSource box fix-up failed on %s: %s", name, tostring(applied)))
            else
                local msg = string.format("SuperSource boxes on %s: %d applied, %d not found.", name, applied, missing)
                if #failed > 0 then
                    msg = msg .. " Resolve refused: " .. table.concat(failed, ", ") .. "."
                end
                log(msg)
            end
        end
    end)
    if not ok then
        log("SuperSource box fix-up skipped: " .. tostring(err))
    end
end

-- ---------------------------------------------------------------- engine process (background + log polling)

-- Shell command that starts the engine in the background; stdout/stderr go to files, the exit code is written
-- to files.rc (via a rename, so it appears complete) once the engine has finished.
function M.engine_shell_command(engine, argv, files)
    local q = M.shell_quote
    local prefix = {}
    for _, k in ipairs(sorted_keys(engine.env or {})) do
        prefix[#prefix + 1] = k .. "=" .. q(engine.env[k]) .. " "
    end
    if engine.path_prefix then
        prefix[#prefix + 1] = "PATH=" .. q(engine.path_prefix) .. "\":$PATH\" "
    end
    local tmp_rc = files.rc .. ".tmp"
    return "( unset PYTHONHOME PYTHONPATH; " .. table.concat(prefix) .. M.shell_join(argv)
        .. " > " .. q(files.out) .. " 2> " .. q(files.err) .. " < /dev/null & echo $! > " .. q(files.pid)
        .. "; wait $!; echo $? > " .. q(tmp_rc) .. "; mv -f " .. q(tmp_rc) .. " " .. q(files.rc)
        .. " ) > /dev/null 2>&1 &"
end

local Run = {}
Run.__index = Run

function M.start_engine(engine, argv, outdir, base)
    local stem = M.join(outdir, (base or "pgm2mc") .. "_engine")
    local files = {out = stem .. "_stdout.log", err = stem .. "_stderr.log", rc = stem .. "_exit.txt",
                   pid = stem .. "_pid.txt"}
    for _, p in pairs(files) do
        os.remove(p)
    end
    os.remove(files.rc .. ".tmp")
    local cmd = M.engine_shell_command(engine, argv, files)
    if not M.exec_ok(cmd) then
        error("the shell refused to start " .. tostring(argv[1]), 0)
    end
    local self = setmetatable({}, Run)
    self.argv, self.files, self.cmd = argv, files, cmd
    self.streams = {{path = files.err, offset = 0, buf = ""}, {path = files.out, offset = 0, buf = ""}}
    self.result, self.errors, self.warnings, self.returncode = nil, {}, {}, nil
    return self
end

function Run:_lines(stream, final)
    local f = io.open(stream.path, "rb")
    if f then
        f:seek("set", stream.offset)
        local data = f:read("*a") or ""
        f:close()
        stream.offset = stream.offset + #data
        stream.buf = stream.buf .. data
    end
    local lines = {}
    while true do
        local i = stream.buf:find("\n", 1, true)
        if not i then
            break
        end
        lines[#lines + 1] = stream.buf:sub(1, i - 1)
        stream.buf = stream.buf:sub(i + 1)
    end
    if final and stream.buf ~= "" then
        lines[#lines + 1] = stream.buf
        stream.buf = ""
    end
    return lines
end

-- New events since the last call, as {kind, payload} pairs; never blocks.
function Run:poll()
    local out = {}
    if self.returncode ~= nil then
        return out
    end
    local rc = (M.read_file(self.files.rc) or ""):match("%-?%d+")
    for _, stream in ipairs(self.streams) do
        for _, line in ipairs(self:_lines(stream, rc ~= nil)) do
            local kind, payload = M.parse_line(line)
            if not (kind == "log" and trim(payload) == "") then
                if kind == "result" then
                    self.result = payload
                elseif kind == "error" then
                    self.errors[#self.errors + 1] = payload
                elseif kind == "warn" then
                    self.warnings[#self.warnings + 1] = payload
                end
                out[#out + 1] = {kind, payload}
            end
        end
    end
    if rc then
        self.returncode = tonumber(rc)
    end
    return out
end

function Run:finished()
    return self.returncode ~= nil
end

function Run:cancel()
    if self.returncode ~= nil then
        return
    end
    local pid = (M.read_file(self.files.pid) or ""):match("^%s*(%d+)")
    if pid then
        os.execute("kill " .. pid .. " >/dev/null 2>&1")
    end
end

-- ---------------------------------------------------------------- Resolve access

function M.get_resolve(ns)
    local r = ns.resolve
    if r then
        return r
    end
    if type(ns.Resolve) == "function" then
        local ok, res = pcall(ns.Resolve)
        if ok and res then
            return res
        end
    end
    local b = ns.bmd
    if b ~= nil then
        local ok, res = pcall(function() return b.scriptapp("Resolve") end)
        if ok and res then
            return res
        end
    end
    return nil
end

function M.get_fusion(resolve, ns)
    local f = ns.fu or ns.fusion
    if f then
        return f
    end
    return call(resolve, "Fusion")
end

-- {name, path} for a usable video clip, else nil
function M.clip_info(clip)
    local ok, ctype, path = pcall(function()
        return clip:GetClipProperty("Type"), clip:GetClipProperty("File Path")
    end)
    if not ok then
        return nil
    end
    if type(ctype) == "table" then
        ctype = ctype.Type
    end
    if type(path) == "table" then
        path = path["File Path"]
    end
    if type(path) ~= "string" or path == "" or not M.is_video_type(ctype) then
        return nil
    end
    local name = call(clip, "GetName")
    if type(name) ~= "string" or name == "" then
        name = M.basename(path)
    end
    return {name = name, path = path}
end

-- infos, source where source is 'selection' or 'bin'
function M.candidate_clips(media_pool)
    local clips, source = {}, "selection"
    if M.has_api(media_pool, "GetSelectedClips") then
        clips = M.as_list(call(media_pool, "GetSelectedClips"))
    end
    if #clips == 0 then
        source = "bin"
        clips = M.as_list(call(call(media_pool, "GetCurrentFolder"), "GetClipList"))
    end
    local infos, seen = {}, {}
    for _, c in ipairs(clips) do
        local info = M.clip_info(c)
        if info and not seen[info.path] then
            seen[info.path] = true
            infos[#infos + 1] = info
        end
    end
    return infos, source
end

function M.timeline_names(project)
    local names = {}
    local n = toint(call(project, "GetTimelineCount") or 0) or 0
    for i = 1, n do
        local tl = call(project, "GetTimelineByIndex", i)
        if tl then
            local name = call(tl, "GetName")
            if name then
                names[#names + 1] = name
            end
        end
    end
    return names
end

function M.ensure_bin(media_pool, name)
    name = name or M.BIN_NAME
    local root = call(media_pool, "GetRootFolder")
    if root == nil then
        return nil
    end
    local target
    for _, f in ipairs(M.as_list(call(root, "GetSubFolderList"))) do
        if call(f, "GetName") == name then
            target = f
            break
        end
    end
    if target == nil then
        target = call(media_pool, "AddSubFolder", root, name)
    end
    if target ~= nil then
        call(media_pool, "SetCurrentFolder", target)
    end
    return target
end

function M.pool_file_paths(folder, depth)
    depth = depth or 0
    local paths = {}
    if folder == nil or depth > 8 then
        return paths
    end
    for _, c in ipairs(M.as_list(call(folder, "GetClipList"))) do
        local fp = call(c, "GetClipProperty", "File Path")
        if type(fp) == "table" then
            fp = fp["File Path"]
        end
        if type(fp) == "string" and fp ~= "" then
            paths[#paths + 1] = fp
        end
    end
    for _, sub in ipairs(M.as_list(call(folder, "GetSubFolderList"))) do
        for _, p in ipairs(M.pool_file_paths(sub, depth + 1)) do
            paths[#paths + 1] = p
        end
    end
    return paths
end

-- Best effort: ImportMedia the still PNGs the FCPXML import did not bring in. Never raises.
function M.import_missing_stills(media_pool, folder, result, log)
    local ok, err = pcall(function()
        local sdir = result.stills_dir
        if type(sdir) ~= "string" or not M.is_dir(sdir) then
            return
        end
        local files = {}
        for _, f in ipairs(M.list_dir(sdir)) do
            if f:lower():sub(-4) == ".png" then
                files[#files + 1] = M.join(sdir, f)
            end
        end
        table.sort(files)
        if #files == 0 then
            return
        end
        local missing = M.missing_stills(files, M.pool_file_paths(call(media_pool, "GetRootFolder")))
        if #missing == 0 then
            return
        end
        if folder ~= nil then
            call(media_pool, "SetCurrentFolder", folder)
        end
        local got = M.as_list(media_pool:ImportMedia(missing))
        log(string.format("Imported %d of %d still image(s) missing from the Media Pool.", #got, #missing))
    end)
    if not ok then
        log(string.format("Could not import the still images (%s); import %s by hand if needed.", tostring(err),
            tostring(result.stills_dir)))
    end
end

local function clip_type_text(c)
    local t = call(c, "GetClipProperty", "Type")
    if type(t) == "table" then
        t = t.Type
    end
    return tostring(t or "")
end

-- Import the chosen FCPXML files; returns {kind = timeline}, count.
function M.import_timelines(project, media_pool, result, base_name, wanted, log)
    local plan = M.planned_imports(result, base_name, wanted)
    if #plan == 0 then
        log("Nothing to import.")
        return {}, 0
    end
    local folder = M.ensure_bin(media_pool)
    if folder == nil then
        log(string.format("Could not create the Media Pool bin \"%s\"; importing into the current bin.", M.BIN_NAME))
    end
    local created, count = {}, 0
    for _, step in ipairs(plan) do
        local kind, path, tl_name = step[1], step[2], step[3]
        local ok, tl = pcall(function()
            return media_pool:ImportTimelineFromFile(path, {timelineName = tl_name, importSourceClips = true})
        end)
        if not ok then
            log(string.format("Import error for %s: %s", path, tostring(tl)))
            tl = nil
        end
        if tl then
            created[kind] = tl
            count = count + 1
            log("Created timeline: " .. tostring(call(tl, "GetName") or tl_name))
        else
            log(string.format("Import failed: %s\n  Import it manually via File > Import > Timeline.", path))
        end
    end
    local main_tl = created.multicam or created.conform or created.stacked
    if main_tl ~= nil then
        call(project, "SetCurrentTimeline", main_tl)
    end
    if created.multicam ~= nil then
        local types = {}
        if folder ~= nil then
            for _, c in ipairs(M.as_list(call(folder, "GetClipList"))) do
                types[#types + 1] = clip_type_text(c):lower()
            end
        end
        local any_mc, any_compound = false, false
        for _, t in ipairs(types) do
            any_mc = any_mc or t:find("multicam", 1, true) ~= nil
            any_compound = any_compound or t:find("compound", 1, true) ~= nil
        end
        if #types == 0 or not any_mc or any_compound then
            log(M.COMPOUND_HINT)
        end
    end
    if count > 0 then
        M.import_missing_stills(media_pool, folder, result, log)
        M.fixup_supersources(created, result, log)
    end
    return created, count
end

-- ---------------------------------------------------------------- run orchestration

-- Creates the output folder and returns the engine argv; raises a readable error.
function M.prepare_run(engine, pgm, isos, outdir, name, sync, audio, min_shot, refine, rebuild)
    if not M.make_dirs(outdir) then
        error("Cannot create output folder " .. outdir, 0)
    end
    return M.build_argv(engine.cmd, pgm, isos, outdir, {name = name, sync = sync, audio = audio,
        min_shot = min_shot, refine = refine, ffmpeg = engine.ffmpeg, ffprobe = engine.ffprobe, rebuild = rebuild})
end

local function names_of(list)
    local out = {}
    for i, info in ipairs(list) do
        out[i] = info.name
    end
    return out
end

local function paths_of(list)
    local out = {}
    for i, info in ipairs(list) do
        out[i] = info.path
    end
    return out
end

-- Non-interactive path (no UIManager): selection + defaults, progress to the console.
function M.run_headless(resolve, engine, sleep, out)
    sleep = sleep or M.sleep
    out = out or print
    local T = M.TITLE
    local project = call(call(resolve, "GetProjectManager"), "GetCurrentProject")
    if project == nil then
        out(T .. ": open a project first.")
        return false
    end
    local mp = call(project, "GetMediaPool")
    local infos, source = M.candidate_clips(mp)
    local names = names_of(infos)
    local progs = M.program_candidates(names)
    local help_txt = "Select the program clip (its name must contain PGM, Program, Prog or 'PVW out') and the ISO "
        .. "clips in the Media Pool, then run Workspace > Scripts > " .. T .. " again."
    if #progs ~= 1 then
        if #progs == 0 then
            out(string.format("%s: no program clip found among %d video clip(s) (%s). %s", T, #infos, source,
                help_txt))
        else
            local pn = {}
            for _, i in ipairs(progs) do
                pn[#pn + 1] = names[i]
            end
            out(string.format("%s: more than one clip looks like the program: %s. %s", T, table.concat(pn, ", "),
                help_txt))
        end
        return false
    end
    local pgm = infos[progs[1]]
    local isos = {}
    for k, info in ipairs(infos) do
        if k ~= progs[1] then
            isos[#isos + 1] = info
        end
    end
    if #isos == 0 then
        out(string.format("%s: no ISO clips selected. %s", T, help_txt))
        return false
    end
    if not engine then
        local msg
        engine, msg = M.load_config()
        if not engine then
            out(T .. ": " .. msg)
            return false
        end
    end
    local outdir = M.default_output_dir(pgm.path)
    local base = M.unique_base_name(M.file_stem(pgm.path), M.timeline_names(project))
    out(string.format("%s: program = %s; ISOs = %s; output = %s", T, pgm.name,
        table.concat(names_of(isos), ", "), outdir))
    local ok, run = pcall(function()
        local argv = M.prepare_run(engine, pgm.path, paths_of(isos), outdir, base, "auto", "pgm", 6, false,
            M.DEFAULT_REBUILD)
        return M.start_engine(engine, argv, outdir, base)
    end)
    if not ok then
        out(string.format("%s: could not start the engine: %s", T, tostring(run)))
        return false
    end
    local last
    while true do
        for _, ev in ipairs(run:poll()) do
            if ev[1] == "progress" then
                local txt = M.progress_text(ev[2][1], ev[2][2])
                if txt ~= last then
                    out(txt)
                    last = txt
                end
            else
                out(M.describe_event(ev[1], ev[2]))
            end
        end
        if run:finished() then
            break
        end
        sleep(0.2)
    end
    if run.returncode ~= 0 or run.result == nil then
        out(string.format("%s: engine failed (exit code %s).", T, tostring(run.returncode)))
        return false
    end
    local _, count = M.import_timelines(project, mp, run.result, base,
        {multicam = true, conform = true, stacked = false}, out)
    return count > 0
end

-- UIManager dialog (Resolve Studio, or free <= 19.0).
function M.Dialog(resolve, fusion, bmd_mod, out)
    out = out or print
    local self = {}
    self.project = resolve:GetProjectManager():GetCurrentProject()
    self.mp = self.project:GetMediaPool()
    self.infos, self.source = M.candidate_clips(self.mp)
    local ui = fusion.UIManager
    local disp = bmd_mod.UIDispatcher(ui)
    self.run, self.base, self.timer, self.last_default_out = nil, nil, nil, ""

    local function row(label, widget)
        return ui:HGroup{Weight = 0, ui:Label{Text = label, Weight = 0.25}, widget}
    end

    local iso_group = {Weight = 0, Spacing = 2}
    for k, info in ipairs(self.infos) do
        iso_group[#iso_group + 1] = ui:CheckBox{ID = "Iso" .. k, Text = info.name .. "   (" .. info.path .. ")",
                                                Checked = true, Weight = 0}
    end
    if #self.infos == 0 then
        iso_group[1] = ui:Label{Text = "No video clips selected.", Weight = 0}
    end

    local win = disp:AddWindow({ID = "PGM2MC", WindowTitle = M.TITLE, Geometry = {200, 120, 720, 720},
        ui:VGroup{Spacing = 6,
            row("Program clip", ui:ComboBox{ID = "Pgm", Weight = 0.75}),
            ui:Label{Text = "ISO clips", Weight = 0},
            ui:VGroup(iso_group),
            row("Sync", ui:ComboBox{ID = "Sync", Weight = 0.75}),
            row("Audio in cut", ui:ComboBox{ID = "Audio", Weight = 0.75}),
            row("Min shot (frames)", ui:SpinBox{ID = "MinShot", Minimum = 1, Maximum = 250, Value = 6,
                                                Weight = 0.75}),
            ui:CheckBox{ID = "Refine", Text = "Refine sync " .. PLUSMINUS .. "3 frames", Checked = false, Weight = 0},
            ui:HGroup{Weight = 0,
                ui:Label{Text = "Rebuild", Weight = 0.25},
                ui:CheckBox{ID = "RbDissolves", Text = "Dissolves & dips", Checked = true},
                ui:CheckBox{ID = "RbStills", Text = "Repeating stills", Checked = true},
                ui:Label{Text = "min repeats", Weight = 0},
                ui:SpinBox{ID = "RbMinRepeat", Minimum = 1, Maximum = 20, Value = 2, Weight = 0},
                ui:CheckBox{ID = "RbSuperSource", Text = "SuperSource", Checked = true},
            },
            ui:HGroup{Weight = 0,
                ui:Label{Text = "Import", Weight = 0.25},
                ui:CheckBox{ID = "ImpMulticam", Text = "Multicam cut", Checked = true},
                ui:CheckBox{ID = "ImpConform", Text = "Conform", Checked = true},
                ui:CheckBox{ID = "ImpStacked", Text = "Stacked", Checked = false},
            },
            row("Output folder", ui:LineEdit{ID = "OutDir", Weight = 0.75}),
            ui:Label{ID = "Progress", Text = "", Weight = 0},
            ui:TextEdit{ID = "Log", ReadOnly = true, Weight = 1},
            ui:HGroup{Weight = 0, ui:Button{ID = "Run", Text = "Analyze & Build"},
                                  ui:Button{ID = "Close", Text = "Close"}},
        },
    })
    if win == nil then
        error("AddWindow returned nothing", 0)
    end
    local itm = win:GetItems()
    self.win, self.itm, self.disp = win, itm, disp

    function self.log(msg)
        if msg == nil then
            return
        end
        out(M.TITLE .. ": " .. msg)
        if not pcall(function() itm.Log:Append(msg) end) then
            pcall(function() itm.Log.PlainText = (itm.Log.PlainText or "") .. msg .. "\n" end)
        end
    end

    local function program_index()
        local ok, v = pcall(function() return toint(itm.Pgm.CurrentIndex) end)
        return (ok and v or 0) + 1
    end

    local function program_changed()
        if #self.infos == 0 then
            return
        end
        local p = program_index()
        for k = 1, #self.infos do
            local box = itm["Iso" .. k]
            if box ~= nil then
                box.Checked = (k ~= p)
                box.Enabled = (k ~= p)
            end
        end
        local new_default = M.default_output_dir(self.infos[p].path)
        local current = itm.OutDir.Text or ""
        if current == "" or current == self.last_default_out then
            itm.OutDir.Text = new_default
        end
        self.last_default_out = new_default
    end

    local function rebuild_options()
        return {dissolves = itm.RbDissolves.Checked and true or false,
                stills = itm.RbStills.Checked and true or false,
                stills_min_repeat = toint(itm.RbMinRepeat.Value) or 2,
                supersource = itm.RbSuperSource.Checked and true or false}
    end

    local function stop_timer()
        if self.timer ~= nil then
            pcall(function() self.timer:Stop() end)
        end
    end

    function self.on_poll(ev)
        if self.run == nil then
            return true
        end
        for _, e in ipairs(self.run:poll()) do
            if e[1] == "progress" then
                itm.Progress.Text = M.progress_text(e[2][1], e[2][2])
            else
                self.log(M.describe_event(e[1], e[2]))
            end
        end
        if not self.run:finished() then
            return false
        end
        stop_timer()
        local run = self.run
        self.run = nil
        if run.returncode == 0 and run.result ~= nil then
            itm.Progress.Text = "Importing" .. ELLIPSIS
            local _, count = M.import_timelines(self.project, self.mp, run.result, self.base, self.wanted, self.log)
            itm.Progress.Text = string.format("Done: %d timeline(s) imported.", count)
        else
            itm.Progress.Text = string.format("Failed (exit code %s).", tostring(run.returncode))
        end
        itm.Run.Enabled = true
        return true
    end

    function self.on_run(ev)
        if self.run ~= nil or #self.infos == 0 then
            return
        end
        local p = program_index()
        local pgm = self.infos[p]
        local isos = {}
        for k, info in ipairs(self.infos) do
            if k ~= p and itm["Iso" .. k] ~= nil and itm["Iso" .. k].Checked then
                isos[#isos + 1] = info
            end
        end
        if #isos == 0 then
            self.log("Tick at least one ISO clip.")
            return
        end
        local engine, msg = M.load_config()
        if not engine then
            self.log(msg)
            return
        end
        local outdir = trim(itm.OutDir.Text or "")
        if outdir == "" then
            outdir = M.default_output_dir(pgm.path)
        end
        self.wanted = {multicam = itm.ImpMulticam.Checked and true or false,
                       conform = itm.ImpConform.Checked and true or false,
                       stacked = itm.ImpStacked.Checked and true or false}
        self.base = M.unique_base_name(M.file_stem(pgm.path), M.timeline_names(self.project))
        local ok, err = pcall(function()
            local argv = M.prepare_run(engine, pgm.path, paths_of(isos), outdir, self.base,
                itm.Sync.CurrentText or "Auto", itm.Audio.CurrentText or "Program audio",
                toint(itm.MinShot.Value) or 6, itm.Refine.Checked and true or false, rebuild_options())
            self.log(string.format("Running engine: program %s, %d ISO(s), output %s", pgm.name, #isos, outdir))
            self.run = M.start_engine(engine, argv, outdir, self.base)
        end)
        if not ok then
            self.log("Could not start the engine: " .. tostring(err))
            self.run = nil
            return
        end
        itm.Run.Enabled = false
        itm.Progress.Text = "Starting" .. ELLIPSIS
        if self.timer ~= nil and pcall(function() self.timer:Start() end) then
            return
        end
        while not self.on_poll(nil) do -- no timer: block, UI updates when done
            M.sleep(0.2)
        end
    end

    function self.on_close(ev)
        if self.run ~= nil then
            self.run:cancel()
        end
        disp:ExitLoop()
    end

    for _, info in ipairs(self.infos) do
        itm.Pgm:AddItem(info.name)
    end
    for _, s in ipairs(M.SYNC_CHOICES) do
        itm.Sync:AddItem(s)
    end
    for _, a in ipairs(M.AUDIO_CHOICES) do
        itm.Audio:AddItem(a)
    end
    if #self.infos > 0 then
        itm.Pgm.CurrentIndex = M.guess_program_index(names_of(self.infos)) - 1
    end
    program_changed()
    win.On.Run.Clicked = self.on_run
    win.On.Close.Clicked = self.on_close
    win.On.PGM2MC.Close = self.on_close
    win.On.Pgm.CurrentIndexChanged = function(ev) program_changed() end
    win.On.RbStills.Clicked = function(ev) itm.RbMinRepeat.Enabled = itm.RbStills.Checked and true or false end
    local tok, timer = pcall(function() return ui:Timer{ID = "PollTimer", Interval = 200} end)
    if tok and timer ~= nil then
        self.timer = timer
        for _, target in ipairs({disp, win}) do
            pcall(function() target.On.PollTimer.Timeout = self.on_poll end)
        end
        pcall(function() disp.On.Timeout = function(ev) self.on_poll(ev) end end)
    end
    local src = self.source == "selection" and "Media Pool selection" or "clips in the current bin"
    self.log(string.format("%d video clip(s) from the %s.", #self.infos, src))

    function self.show()
        win:Show()
        disp:RunLoop()
        win:Hide()
    end
    return self
end

function M.main(ns, out)
    ns = ns or _G
    out = out or print
    local resolve = M.get_resolve(ns)
    if resolve == nil then
        out(M.TITLE .. ": run this from DaVinci Resolve (Workspace > Scripts).")
        return false
    end
    local project = call(call(resolve, "GetProjectManager"), "GetCurrentProject")
    if project == nil then
        out(M.TITLE .. ": open a project first.")
        return false
    end
    local fusion = M.get_fusion(resolve, ns)
    local bmd_mod = ns.bmd
    local has_ui = false
    if fusion ~= nil and bmd_mod ~= nil then
        local ok, mgr = pcall(function() return fusion.UIManager end)
        has_ui = ok and mgr ~= nil
    end
    if has_ui then
        local ok, dlg = pcall(M.Dialog, resolve, fusion, bmd_mod, out)
        if not ok then
            out(string.format("%s: dialog unavailable (%s); using the Media Pool selection with defaults.", M.TITLE,
                tostring(dlg)))
        else
            dlg.show()
            return true
        end
    end
    return M.run_headless(resolve, nil, nil, out)
end

local function in_resolve()
    for _, k in ipairs({"resolve", "Resolve", "fu", "fusion"}) do
        if rawget(_G, k) ~= nil then
            return true
        end
    end
    return false
end

if not rawget(_G, "PGM2MC_TEST") and in_resolve() then
    M.main(_G)
end

return M
