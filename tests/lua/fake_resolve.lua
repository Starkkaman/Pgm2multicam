-- Fake Resolve objects (Lua tables with colon methods) for tests/test_resolve_lua.py.
local F = {}

local function copy(list)
    local out = {}
    for i, v in ipairs(list) do
        out[i] = v
    end
    return out
end

local function file_exists(p)
    local f = io.open(p, "rb")
    if f then
        f:close()
        return true
    end
    return false
end

function F.clip(name, path, ctype)
    local c = {name = name, props = {["File Path"] = path, Type = ctype or "Video + Audio"}}
    function c:GetName() return self.name end
    function c:GetClipProperty(key)
        if key == nil then
            return self.props
        end
        return self.props[key] or ""
    end
    return c
end

function F.timeline(name)
    local t = {name = name}
    function t:GetName() return self.name end
    return t
end

function F.folder(name, clips)
    local f = {name = name, clips = copy(clips or {}), subs = {}}
    function f:GetName() return self.name end
    function f:GetClipList() return copy(self.clips) end
    function f:GetSubFolderList() return copy(self.subs) end
    return f
end

-- opts: fail = {suffix, ...}, mc_type, no_selection_api, make_timeline(name)
function F.media_pool(selected, project, opts)
    opts = opts or {}
    local mp = {selected = selected, project = project, fail = opts.fail or {}, mc_type = opts.mc_type or "Multicam",
                imports = {}, media_imports = {}, make_timeline = opts.make_timeline or F.timeline}
    mp.root = F.folder("Master", selected)
    mp.current = mp.root
    if not opts.no_selection_api then
        function mp:GetSelectedClips() return copy(self.selected) end
    end
    function mp:GetRootFolder() return self.root end
    function mp:GetCurrentFolder() return self.current end
    function mp:AddSubFolder(parent, name)
        local f = F.folder(name)
        parent.subs[#parent.subs + 1] = f
        return f
    end
    function mp:SetCurrentFolder(f)
        self.current = f
        return true
    end
    function mp:ImportMedia(paths)
        self.media_imports[#self.media_imports + 1] = {paths = copy(paths), bin = self.current.name}
        local clips = {}
        for i, p in ipairs(paths) do
            clips[i] = F.clip(p:match("[^/]*$"), p, "Still")
            self.current.clips[#self.current.clips + 1] = clips[i]
        end
        return clips
    end
    function mp:ImportTimelineFromFile(path, o)
        self.imports[#self.imports + 1] = {path = path, timelineName = o.timelineName,
                                           importSourceClips = o.importSourceClips, bin = self.current.name}
        for _, suf in ipairs(self.fail) do
            if path:sub(-#suf) == suf then
                return nil
            end
        end
        assert(file_exists(path), "fcpxml missing: " .. path)
        if path:find("multicam", 1, true) then
            self.current.clips[#self.current.clips + 1] = F.clip("PGM Multicam", "", self.mc_type)
        end
        local tl = self.make_timeline(o.timelineName)
        self.project.timelines[#self.project.timelines + 1] = tl
        return tl
    end
    return mp
end

function F.project(selected, opts)
    local p = {timelines = {F.timeline("PGM Multicam Cut")}, current_tl = nil}
    p.mp = F.media_pool(selected, p, opts)
    function p:GetMediaPool() return self.mp end
    function p:GetTimelineCount() return #self.timelines end
    function p:GetTimelineByIndex(i) return self.timelines[i] end
    function p:SetCurrentTimeline(tl)
        self.current_tl = tl
        return true
    end
    return p
end

function F.resolve(project)
    local r = {project = project}
    function r:GetProjectManager() return self end
    function r:GetCurrentProject() return self.project end
    return r
end

-- timeline items and track timelines for the SuperSource fix-up
function F.item(start, path, name, refuse)
    local it = {start = start, name = name or "", refuse = refuse or {}, set = {}}
    if path ~= nil then
        it.mpi = F.clip(path ~= "" and path:match("[^/]*$") or (name or ""), path)
    end
    function it:GetStart() return self.start end
    function it:GetName() return self.name end
    function it:GetMediaPoolItem() return self.mpi end
    function it:SetProperty(key, value)
        self.set[#self.set + 1] = {key, value}
        for _, r in ipairs(self.refuse) do
            if r == key then
                return false
            end
        end
        return true
    end
    return it
end

function F.track_timeline(tracks, start, name, broken)
    local t = {tracks = tracks, start = start or 90000, name = name or "T", broken = broken}
    function t:GetName() return self.name end
    function t:GetStartFrame() return self.start end
    function t:GetTrackCount(kind)
        if kind ~= "video" then
            return 1
        end
        local n = 0
        for k in pairs(self.tracks) do
            n = math.max(n, k)
        end
        return n
    end
    function t:GetItemListInTrack(kind, idx)
        if self.broken then
            error("boom")
        end
        if kind ~= "video" then
            return {}
        end
        return copy(self.tracks[idx] or {})
    end
    return t
end

return F
