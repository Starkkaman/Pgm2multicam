-- Minimal fake of Fusion's UIManager + bmd.UIDispatcher for tests/test_resolve_lua.py.
-- U.new(with_timer, script) -> fusion, bmd; script(disp, win, items, ui) runs inside disp:RunLoop().
local U = {}

local function auto()
    return setmetatable({}, {__index = function(t, k)
        local v = auto()
        rawset(t, k, v)
        return v
    end})
end

function U.new(with_timer, script)
    local ui = {items = {}}
    local function widget(kind)
        return function(self, props)
            local w = {kind = kind}
            for k, v in pairs(props or {}) do
                w[k] = v
            end
            if kind == "ComboBox" then
                w.entries, w.CurrentIndex = {}, 0
                function w:AddItem(s) self.entries[#self.entries + 1] = s end
                setmetatable(w, {__index = function(t, k)
                    if k == "CurrentText" then
                        return t.entries[t.CurrentIndex + 1]
                    end
                end})
            elseif kind == "TextEdit" then
                w.PlainText = ""
                function w:Append(s) self.PlainText = self.PlainText .. s .. "\n" end
            elseif kind == "LineEdit" then
                w.Text = w.Text or ""
            end
            if w.ID then
                ui.items[w.ID] = w
            end
            return w
        end
    end
    for _, k in ipairs({"HGroup", "VGroup", "Label", "CheckBox", "ComboBox", "SpinBox", "LineEdit", "TextEdit",
                        "Button"}) do
        ui[k] = widget(k)
    end
    if with_timer then
        function ui:Timer(props)
            local t = {ID = props.ID, Interval = props.Interval, running = false}
            function t:Start() self.running = true end
            function t:Stop() self.running = false end
            ui.timer = t
            return t
        end
    end
    local bmd = {}
    function bmd.UIDispatcher(mgr)
        local disp = {On = auto(), exited = false}
        function disp:AddWindow(props, root)
            local win = {On = auto(), props = props, root = root or props[1], shown = false}
            function win:GetItems() return ui.items end
            function win:Show() self.shown = true end
            function win:Hide() self.shown = false end
            disp.win = win
            return win
        end
        function disp:RunLoop() script(disp, disp.win, ui.items, ui) end
        function disp:ExitLoop() self.exited = true end
        ui.disp = disp
        return disp
    end
    return {UIManager = ui}, bmd
end

return U
