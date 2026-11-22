-- aeweb.lua: push AE2 network status and inventory to the ae-web backend.
-- Needs: an internet card, and an adapter next to an ME controller or ME interface.
-- Usage: `aeweb [config]` (default config /home/aeweb.cfg; Ctrl+Alt+C stops it).
-- Settings live in the config file, so replacing this script keeps them.

local component = require("component")
local computer = require("computer")
local event = require("event")
local filesystem = require("filesystem")

------------------------------------------------------------------ settings

local CONFIG_PATH = (...) or "/home/aeweb.cfg"

local DEFAULTS = {
  BASE_URL = "https://ae-web.example.com",
  TOKEN = "CHANGE-ME-oc-token",
  STATUS_INTERVAL = 5,
  ITEMS_INTERVAL = 60,
  CHUNK_LINES = 500,
  CPU_ITEM_LIMIT = 100,
  HTTP_TIMEOUT = 20,
}

local TEMPLATE = [[
-- aeweb settings. Lua syntax: strings in quotes, numbers without.

-- Backend address, no trailing slash.
BASE_URL = "https://ae-web.example.com"
-- Same value as oc_token in the backend's config.toml.
TOKEN = "CHANGE-ME-oc-token"

-- Optional, defaults shown.
-- STATUS_INTERVAL = 5    -- seconds between power/CPU reports
-- ITEMS_INTERVAL = 60    -- seconds between full inventory snapshots
-- CHUNK_LINES = 500      -- inventory lines per upload request
-- CPU_ITEM_LIMIT = 100   -- max active/pending entries reported per CPU
-- HTTP_TIMEOUT = 20      -- seconds
]]

-- Returns the settings table, or nil and a message for the user.
local function loadConfig(path)
  if not filesystem.exists(path) then
    local file = assert(io.open(path, "w"))
    file:write(TEMPLATE)
    file:close()
    return nil, "created " .. path .. ", fill in BASE_URL and TOKEN (edit " .. path .. ") and run again"
  end
  local file = assert(io.open(path, "r"))
  local text = file:read("*a")
  file:close()
  local values = {}
  local chunk, err = load(text, "=" .. path, "t", values)
  if not chunk then return nil, "config error: " .. tostring(err) end
  local ok, runErr = pcall(chunk)
  if not ok then return nil, "config error: " .. tostring(runErr) end

  local settings = {}
  for key, default in pairs(DEFAULTS) do
    local value = values[key]
    if value == nil then value = default end
    if type(value) ~= type(default) then
      return nil, string.format("config error: %s must be a %s", key, type(default))
    end
    settings[key] = value
  end
  for key in pairs(values) do
    if DEFAULTS[key] == nil then io.write("warning: unknown setting ", key, " in ", path, "\n") end
  end
  settings.BASE_URL = settings.BASE_URL:gsub("/+$", "")
  if settings.BASE_URL == DEFAULTS.BASE_URL or settings.TOKEN == DEFAULTS.TOKEN then
    return nil, "set BASE_URL and TOKEN in " .. path .. " first (edit " .. path .. ")"
  end
  return settings
end

local settings, configMessage = loadConfig(CONFIG_PATH)
if not settings then
  io.write(configMessage, "\n")
  return
end
local BASE_URL = settings.BASE_URL
local TOKEN = settings.TOKEN
local STATUS_INTERVAL = settings.STATUS_INTERVAL
local ITEMS_INTERVAL = settings.ITEMS_INTERVAL
local CHUNK_LINES = settings.CHUNK_LINES
local CPU_ITEM_LIMIT = settings.CPU_ITEM_LIMIT
local HTTP_TIMEOUT = settings.HTTP_TIMEOUT

------------------------------------------------------------------ components

local inet = component.isAvailable("internet") and component.internet
-- The flat (cable part) ME interface is also called "me_interface" but has no
-- network API, so pick the first component that actually offers it.
local me
for _, kind in ipairs({ "me_controller", "me_interface" }) do
  for address in component.list(kind, true) do
    -- values are the "direct" flags (false for these), so test for presence
    local methods = component.methods(address)
    if methods.allItems ~= nil and methods.getCpus ~= nil and methods.getStoredPower ~= nil then
      me = component.proxy(address)
      break
    end
  end
  if me then break end
end
assert(inet, "no internet card")
assert(me, "no usable ME component: put the adapter next to an ME controller or a"
  .. " full-block ME interface (the flat cable-part interface has no network API)")

------------------------------------------------------------------ helpers

-- Prefixed with real seconds since boot; os.date in OC shows in-game time.
local function log(fmt, ...)
  io.write(string.format("[%7.1fs] ", computer.uptime()), string.format(fmt, ...), "\n")
end

-- Integers as plain digits (no 1e+15), everything else with full precision.
local function num(x)
  x = tonumber(x) or 0
  if x == math.floor(x) and math.abs(x) < 2 ^ 53 then
    return string.format("%.0f", x)
  end
  return string.format("%.17g", x)
end

local escapes = { ['"'] = '\\"', ["\\"] = "\\\\", ["\b"] = "\\b", ["\f"] = "\\f",
  ["\n"] = "\\n", ["\r"] = "\\r", ["\t"] = "\\t" }

local function jsonString(s)
  return '"' .. tostring(s):gsub('[%c"\\]', function(c)
    return escapes[c] or string.format("\\u%04x", c:byte())
  end) .. '"'
end

local function toJson(v)
  local t = type(v)
  if t == "table" then
    local out = {}
    if #v > 0 or next(v) == nil and getmetatable(v) == "array" then
      for i = 1, #v do out[i] = toJson(v[i]) end
      return "[" .. table.concat(out, ",") .. "]"
    end
    for k, val in pairs(v) do
      out[#out + 1] = jsonString(k) .. ":" .. toJson(val)
    end
    return "{" .. table.concat(out, ",") .. "}"
  elseif t == "string" then
    return jsonString(v)
  elseif t == "number" then
    return num(v)
  elseif t == "boolean" then
    return tostring(v)
  end
  return "null"
end

local function emptyArray()
  return setmetatable({}, { __metatable = "array" })
end

local function clean(s)
  return (tostring(s or ""):gsub("[\t\r\n]", " "))
end

local function post(path, body, contentType)
  local ok, handle = pcall(inet.request, BASE_URL .. path, body, {
    ["Authorization"] = "Bearer " .. TOKEN,
    ["Content-Type"] = contentType,
  })
  if not ok or not handle then
    return nil, "request failed: " .. tostring(handle)
  end
  local deadline = computer.uptime() + HTTP_TIMEOUT
  local function timedOut()
    return computer.uptime() > deadline
  end

  while true do
    local okc, connected = pcall(handle.finishConnect)
    if not okc then handle.close() return nil, tostring(connected) end
    if connected then break end
    if timedOut() then handle.close() return nil, "connect timeout" end
    os.sleep(0.05)
  end

  local code
  repeat
    code = handle.response()
    if not code then
      if timedOut() then handle.close() return nil, "response timeout" end
      os.sleep(0.05)
    end
  until code

  local parts = {}
  while true do
    local okr, chunk = pcall(handle.read, 8192)
    if not okr then handle.close() return nil, tostring(chunk) end
    if chunk == nil then break end
    if chunk == "" then
      if timedOut() then handle.close() return nil, "read timeout" end
      os.sleep(0.05)
    else
      parts[#parts + 1] = chunk
    end
  end
  handle.close()
  local text = table.concat(parts)
  if code < 200 or code >= 300 then
    return nil, "HTTP " .. code .. " " .. text:sub(1, 120)
  end
  return text
end

------------------------------------------------------------------ status

local function shortItem(it)
  if type(it) ~= "table" then return nil end
  return { n = it.name or "?", d = it.damage or 0, l = it.label or it.name or "?",
           s = it.size or it.amount or 0 }
end

local function itemList(fn)
  local out = emptyArray()
  local ok, list = pcall(fn)
  if not ok or type(list) ~= "table" then return out, false end
  for i = 1, math.min(#list, CPU_ITEM_LIMIT) do
    out[#out + 1] = shortItem(list[i])
  end
  return out, #list > CPU_ITEM_LIMIT
end

local function collectStatus()
  local status = {
    power = {
      stored = me.getStoredPower(),
      max = me.getMaxStoredPower(),
      avgUse = me.getAvgPowerUsage(),
      avgInject = me.getAvgPowerInjection(),
      idle = me.getIdlePowerUsage(),
    },
    cpus = emptyArray(),
    uptime = computer.uptime(),
  }
  for i, c in ipairs(me.getCpus()) do
    local entry = {
      name = (c.name and c.name ~= "") and c.name or ("CPU #" .. i),
      storage = c.storage or 0,
      coprocessors = c.coprocessors or 0,
      busy = c.busy and true or false,
      active = emptyArray(),
      pending = emptyArray(),
    }
    if entry.busy and c.cpu then
      -- needs a Crafting Monitor in the CPU, otherwise returns nil + reason
      local okf, final, reason = pcall(c.cpu.finalOutput)
      entry.final = okf and shortItem(final) or nil
      if not entry.final then entry.finalError = tostring(okf and reason or final) end
      local t1, t2
      entry.active, t1 = itemList(c.cpu.activeItems)
      entry.pending, t2 = itemList(c.cpu.pendingItems)
      entry.truncated = t1 or t2
    end
    status.cpus[#status.cpus + 1] = entry
  end
  return status
end

local function sendStatus()
  local _, err = post("/api/oc/status", toJson(collectStatus()), "application/json")
  if err then log("status upload failed: %s", err) end
  return err == nil
end

------------------------------------------------------------------ inventory

local function sendItems()
  local started = computer.uptime()
  local snap = string.format("%d-%d", math.floor(started * 1000), math.random(1, 1e9))
  local seq, total, buf = 0, 0, {}

  local function flush(final)
    local path = string.format("/api/oc/items?snap=%s&seq=%d&final=%d&count=%d&scan=%s",
      snap, seq, final and 1 or 0, total, num(computer.uptime() - started))
    local body = table.concat(buf, "\n")
    local _, err = post(path, body, "text/plain; charset=utf-8")
    if err then -- one retry; the backend ignores an exact duplicate
      os.sleep(1)
      _, err = post(path, body, "text/plain; charset=utf-8")
    end
    if err then error("inventory upload failed: " .. err, 0) end
    seq, buf = seq + 1, {}
  end

  local function add(line)
    buf[#buf + 1] = line
    total = total + 1
    if #buf >= CHUNK_LINES then flush(false) end
  end

  -- With allowItemStackNBTTags=true every entry carries its NBT, and a single
  -- item with huge NBT can run the computer out of memory. The iterator has
  -- already advanced when that happens, so skip the item and keep going.
  local nextItem, skipped = me.allItems(), 0
  while true do
    local ok, it = pcall(nextItem)
    if not ok then
      if not tostring(it):find("not enough memory", 1, true) or skipped >= 100 then
        error(it, 0)
      end
      skipped = skipped + 1
      collectgarbage()
    elseif it == nil then
      break
    else
      add(table.concat({ "i", clean(it.name), num(it.damage), num(it.size),
        it.isCraftable and "1" or "0", clean(it.label) }, "\t"))
      it = nil
    end
  end
  nextItem = nil
  local okf, fluids = pcall(me.getFluidsInNetwork)
  if okf and type(fluids) == "table" then
    for _, f in ipairs(fluids) do
      add(table.concat({ "f", clean(f.name), "0", num(f.amount), "0", clean(f.label) }, "\t"))
    end
  end
  fluids = nil
  flush(true)
  log("inventory: %d entries in %.1fs%s", total, computer.uptime() - started,
    skipped > 0 and string.format(", %d skipped (out of memory)", skipped) or "")
end

------------------------------------------------------------------ main loop

-- Runs fn, logging any error; returns true when it succeeded.
local function attempt(what, fn)
  local ok, result = pcall(fn)
  if not ok then
    log("%s error: %s", what, tostring(result))
    return false
  end
  return result ~= false
end

math.randomseed(math.floor(computer.uptime() * 1000 + os.time()))
log("ae-web uploader started, target %s", BASE_URL)
log("memory: %d KB free of %d KB", math.floor(computer.freeMemory() / 1024), math.floor(computer.totalMemory() / 1024))
local nextStatus, nextItems = 0, 0
local failures = 0

while true do
  local ok = true
  if computer.uptime() >= nextStatus then
    ok = attempt("status", sendStatus)
    nextStatus = computer.uptime() + STATUS_INTERVAL
  end
  if ok and computer.uptime() >= nextItems then
    ok = attempt("inventory", sendItems)
    nextItems = computer.uptime() + ITEMS_INTERVAL
  end

  -- back off 2, 4, ... 64 s while the backend is unreachable
  failures = ok and 0 or math.min(failures + 1, 6)
  local wait = math.min(nextStatus, nextItems) - computer.uptime()
  if failures > 0 then wait = math.max(wait, 2 ^ failures) end
  if event.pull(math.max(wait, 0.05), "interrupted") then
    log("stopped")
    break
  end
end
