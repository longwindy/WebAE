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
  CPU_ITEM_LIMIT = 300,
  CPU_CALL_BUDGET = 60,
  CPU_FINAL_REFRESH = 60,
  HTTP_TIMEOUT = 20,
  MAX_ITEM_ID = 32767,
  ID_BATCH = 512,
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
-- CPU_ITEM_LIMIT = 300   -- max stored/active/pending entries reported per CPU list
-- CPU_CALL_BUDGET = 60   -- AE calls (about one tick each) spent per report on busy CPU details
-- CPU_FINAL_REFRESH = 60 -- seconds between re-reading a busy CPU's crafting target
-- HTTP_TIMEOUT = 20      -- seconds
-- MAX_ITEM_ID = 32767    -- highest numeric item ID scanned
-- ID_BATCH = 512         -- item IDs read per call (split automatically if too big)
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
local CPU_CALL_BUDGET = settings.CPU_CALL_BUDGET
local CPU_FINAL_REFRESH = settings.CPU_FINAL_REFRESH
local HTTP_TIMEOUT = settings.HTTP_TIMEOUT
local MAX_ITEM_ID = settings.MAX_ITEM_ID
local ID_BATCH = settings.ID_BATCH

------------------------------------------------------------------ components

local inet = component.isAvailable("internet") and component.internet
-- The flat (cable part) ME interface is also called "me_interface" but has no
-- network API, so pick the first component that actually offers it.
local me
for _, kind in ipairs({ "me_controller", "me_interface" }) do
  for address in component.list(kind, true) do
    -- values are the "direct" flags (false for these), so test for presence
    local methods = component.methods(address)
    if methods.getItemsInNetworkById ~= nil and methods.getCpus ~= nil and methods.getStoredPower ~= nil then
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

-- Every AE call below waits for a server tick, so busy CPUs cannot all be read
-- in full each report once there are dozens of them. Each report reads the CPU
-- list, then spends at most CPU_CALL_BUDGET calls refreshing the busy CPUs whose
-- details are oldest. Only CPUs refreshed in this report carry "detail"; the
-- backend keeps the last one for the others. The target (finalOutput) rarely
-- changes during a job, so it is re-read only every CPU_FINAL_REFRESH seconds.
local cpuCache = {} -- per busy CPU: { detailAt, finalAt }

local function readFinal(entry, cpu)
  -- needs a Crafting Monitor in the CPU, otherwise returns nil + reason
  local ok, final, reason = pcall(cpu.finalOutput)
  entry.final = ok and shortItem(final) or nil
  entry.finalError = entry.final and "" or tostring(ok and reason or final)
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
  local now, busy, seen = computer.uptime(), {}, {}
  for i, c in ipairs(me.getCpus()) do
    local entry = {
      index = i,
      rawName = c.name or "",
      name = (c.name and c.name ~= "") and c.name or ("CPU #" .. i),
      storage = c.storage or 0,
      coprocessors = c.coprocessors or 0,
      busy = c.busy and true or false,
    }
    if entry.busy and c.cpu then
      local key = table.concat({ i, entry.rawName, entry.storage }, "|")
      cpuCache[key] = cpuCache[key] or {}
      seen[key] = true
      busy[#busy + 1] = { entry = entry, cpu = c.cpu, cache = cpuCache[key] }
    end
    status.cpus[#status.cpus + 1] = entry
  end
  for key in pairs(cpuCache) do
    if not seen[key] then cpuCache[key] = nil end
  end

  table.sort(busy, function(a, b) return (a.cache.detailAt or -1) < (b.cache.detailAt or -1) end)
  local calls = 0
  for _, b in ipairs(busy) do
    local needFinal = not b.cache.finalAt or now - b.cache.finalAt >= CPU_FINAL_REFRESH
    local cost = needFinal and 4 or 3
    if calls > 0 and calls + cost > CPU_CALL_BUDGET then break end
    calls = calls + cost
    if needFinal then
      readFinal(b.entry, b.cpu)
      b.cache.finalAt = now
    end
    local stored, t1 = itemList(b.cpu.storedItems)
    local active, t2 = itemList(b.cpu.activeItems)
    local pending, t3 = itemList(b.cpu.pendingItems)
    b.entry.detail = { stored = stored, active = active, pending = pending, truncated = t1 or t2 or t3 }
    b.cache.detailAt = now
  end
  return status
end

------------------------------------------------------------------ commands
-- Commands arrive as tab-separated lines in the status response. Each id is
-- written to HANDLED_PATH before it is acted on, so a command the backend
-- re-sends (lost reply, reboot) never runs twice. Job progress goes back to
-- the backend with the next status upload.

local HANDLED_PATH = CONFIG_PATH:gsub("%.cfg$", "") .. ".handled" -- /home/aeweb.handled
local HANDLED_KEEP = 200
local handled, handledOrder = {}, {}
local trackers = {}   -- { id, status, state } for crafting jobs being watched
local updates = {}    -- reports not yet accepted by the backend

local function loadHandled()
  local file = io.open(HANDLED_PATH, "r")
  if not file then return end
  for line in file:lines() do
    if line ~= "" and not handled[line] then
      handled[line] = true
      handledOrder[#handledOrder + 1] = line
    end
  end
  file:close()
  if #handledOrder > HANDLED_KEEP * 2 then -- compact the file now and then
    local keep = {}
    for i = #handledOrder - HANDLED_KEEP + 1, #handledOrder do keep[#keep + 1] = handledOrder[i] end
    handled, handledOrder = {}, keep
    for _, id in ipairs(keep) do handled[id] = true end
    file = assert(io.open(HANDLED_PATH, "w"))
    file:write(table.concat(keep, "\n"), "\n")
    file:close()
  end
end

local function markHandled(id)
  handled[id] = true
  handledOrder[#handledOrder + 1] = id
  local file = assert(io.open(HANDLED_PATH, "a"))
  file:write(id, "\n")
  file:close()
end

local function report(id, state, msg)
  updates[#updates + 1] = { id = id, state = state, msg = msg and tostring(msg) or "" }
  log("order %s: %s %s", id, state, msg and tostring(msg) or "")
end

local function isTracked(id)
  for _, t in ipairs(trackers) do
    if t.id == id then return true end
  end
  return false
end

local function split(line)
  local out, start = {}, 1
  while true do
    local i = line:find("\t", start, true)
    if not i then
      out[#out + 1] = line:sub(start)
      return out
    end
    out[#out + 1] = line:sub(start, i - 1)
    start = i + 1
  end
end

-- Finds the pattern for name/damage, using the label to pick among NBT variants.
local function findCraftable(name, damage, label)
  local ok, list = pcall(me.getCraftables, { name = name, damage = damage })
  if not ok or type(list) ~= "table" or #list == 0 then
    ok, list = pcall(me.getCraftables, { label = label })
  end
  if not ok or type(list) ~= "table" or #list == 0 then return nil end
  if #list == 1 then return list[1] end
  for _, craftable in ipairs(list) do
    local okStack, stack = pcall(craftable.getStack)
    if okStack and type(stack) == "table" and stack.label == label then return craftable end
  end
  return list[1]
end

-- Fluid patterns: ask AE for the "fluid" stack type directly, else filter by name/label.
local function findFluidCraftable(name, label)
  local ok, craftable = pcall(me.getCraftable, { name = name, amount = 1 }, "fluid")
  if ok and craftable and craftable.request then return craftable end
  for _, filter in ipairs({ { name = name }, { label = label } }) do
    local okList, list = pcall(me.getCraftables, filter)
    if okList and type(list) == "table" then
      for _, c in ipairs(list) do
        local okStack, stack = pcall(c.getStack)
        if okStack and type(stack) == "table" and stack.name == name then return c end
      end
    end
  end
  return nil
end

local function runCraft(id, amount, cpu, craftable)
  if not craftable then return report(id, "failed", "no pattern") end
  local ok, status = pcall(craftable.request, amount, false, cpu ~= "" and cpu or nil)
  if not ok or not status then return report(id, "failed", tostring(status)) end
  trackers[#trackers + 1] = { id = id, status = status, state = "computing" }
  report(id, "computing")
end

local function runCancelCpu(id, index, name)
  local ok, cpus = pcall(me.getCpus)
  local entry = ok and cpus[index]
  if not entry or (entry.name or "") ~= name then
    return report(id, "failed", "cpu changed")
  end
  local okc, err = pcall(entry.cpu.cancel)
  if okc then report(id, "done") else report(id, "failed", tostring(err)) end
end

local function runCommands(text)
  for line in text:gmatch("[^\n]+") do
    local f = split(line)
    local kind, id = f[1], f[2]
    if id and handled[id] then
      if not isTracked(id) then report(id, "lost", "already handled") end
    elseif kind == "craft" and #f >= 7 then -- craft id amount damage name cpu label
      markHandled(id)
      runCraft(id, tonumber(f[3]), f[6], findCraftable(f[5], tonumber(f[4]), f[7]))
    elseif kind == "craft_fluid" and #f >= 6 then -- craft_fluid id amount(L) name cpu label
      markHandled(id)
      runCraft(id, tonumber(f[3]), f[5], findFluidCraftable(f[4], f[6]))
    elseif kind == "cancel_cpu" and #f >= 4 then
      markHandled(id)
      runCancelCpu(id, tonumber(f[3]), f[4])
    elseif id then
      markHandled(id)
      report(id, "error", "unknown command " .. tostring(kind))
    end
  end
end

-- isComputing/hasFailed/isCanceled/isDone each cost a tick, so ask only what is needed.
local function pollTrackers()
  for i = #trackers, 1, -1 do
    local t, final = trackers[i], nil
    local ok, computing = pcall(t.status.isComputing)
    if not ok then
      final = { "lost", tostring(computing) }
    elseif not computing then
      local failed, reason = t.status.hasFailed()
      if failed then
        final = { "failed", reason }
      elseif t.status.isCanceled() then
        final = { "canceled" }
      elseif t.status.isDone() then
        final = { "done" }
      elseif t.state ~= "crafting" then
        t.state = "crafting"
        report(t.id, "crafting")
      end
    end
    if final then
      report(t.id, final[1], final[2])
      table.remove(trackers, i)
    end
  end
end

local function sendStatus()
  pcall(pollTrackers)
  local status = collectStatus()
  local sending = #updates
  status.updates = emptyArray()
  for i = 1, sending do status.updates[i] = updates[i] end
  status.tracking = emptyArray()
  for _, t in ipairs(trackers) do status.tracking[#status.tracking + 1] = t.id end

  local reply, err = post("/api/oc/status", toJson(status), "application/json")
  if err then
    log("status upload failed: %s", err)
    return false
  end
  for _ = 1, sending do table.remove(updates, 1) end -- delivered; keep newer ones
  runCommands(reply)
  return true
end

loadHandled()

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

  -- Items are read in batches of numeric item IDs with getItemsInNetworkById.
  -- Not allItems(): its iterator registers as an AE listener and rebuilds the
  -- whole storage list from inside AE's change notification, which corrupted
  -- AE's item list and crashed the server. A batch too big for memory is split
  -- in half and retried; a single ID that still does not fit is skipped.
  local skipped = {}

  local function scanIds(lo, hi)
    local ids = {}
    for id = lo, hi do ids[#ids + 1] = id end
    local ok, list = pcall(me.getItemsInNetworkById, ids)
    ids = nil
    if not ok then
      if not tostring(list):find("not enough memory", 1, true) then error(list, 0) end
      list = nil
      collectgarbage()
      if lo == hi then
        skipped[#skipped + 1] = lo
        return
      end
      local mid = math.floor((lo + hi) / 2)
      scanIds(lo, mid)
      return scanIds(mid + 1, hi)
    end
    for i = 1, #list do
      local it = list[i]
      add(table.concat({ "i", clean(it.name), num(it.damage), num(it.size),
        it.isCraftable and "1" or "0", clean(it.label) }, "\t"))
      list[i] = nil
    end
  end

  for lo = 0, MAX_ITEM_ID, ID_BATCH do
    scanIds(lo, math.min(lo + ID_BATCH - 1, MAX_ITEM_ID))
  end
  local okf, fluids = pcall(me.getFluidsInNetwork)
  if okf and type(fluids) == "table" then
    for _, f in ipairs(fluids) do
      add(table.concat({ "f", clean(f.name), "0", num(f.amount),
        f.isCraftable and "1" or "0", clean(f.label) }, "\t"))
    end
  end
  fluids = nil
  flush(true)
  log("inventory: %d entries in %.1fs%s", total, computer.uptime() - started,
    #skipped > 0 and (", skipped item IDs (out of memory): " .. table.concat(skipped, ",")) or "")
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
