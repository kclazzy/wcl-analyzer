local addonName, ns = ...

--- There is no API that hands you the specialisation of another player, so specs are read by
--- inspecting the group one member at a time. Inspection only works while the target is
--- visible and in range, so results are cached in saved variables and reused as a fallback
--- for anyone who cannot be reached right now.
local inspect = {}
ns.inspect = inspect

local requestTimeout = 1.5
local requestDelay = 0.3
local cacheLifetime = 600

local getInspectSpecialization = _G.GetInspectSpecialization
	or (C_SpecializationInfo and C_SpecializationInfo.GetInspectSpecialization)

local listener = CreateFrame("Frame")
local queue = {}
local active
local progressHandler, completeHandler
local total, done = 0, 0

local completeActive

local function cache()
	local db = ns.db
	if not db then
		return {}
	end
	db.specs = db.specs or {}
	return db.specs
end

--- The last specialisation seen for a character, regardless of age.
function inspect.cachedSpec(guid)
	local entry = guid and cache()[guid]
	return entry and entry.spec
end

--- True while a cached spec is recent enough to skip a fresh inspect.
local function isFresh(guid)
	local entry = guid and cache()[guid]
	return entry ~= nil and entry.time ~= nil and (GetServerTime() - entry.time) < cacheLifetime
end

function inspect.remember(guid, specId, name, realm)
	if not guid or not specId or specId == 0 then
		return
	end
	cache()[guid] = { spec = specId, time = GetServerTime(), name = name, realm = realm }
end

local function finish()
	local handler = completeHandler
	progressHandler, completeHandler = nil, nil
	active = nil
	inspect.isScanning = false
	listener:UnregisterEvent("INSPECT_READY")
	if handler then
		handler()
	end
end

local function report()
	done = done + 1
	if progressHandler then
		progressHandler(done, total)
	end
end

local function processQueue()
	if active then
		return
	end

	local unit = table.remove(queue, 1)
	if not unit then
		finish()
		return
	end

	local reachable = UnitExists(unit)
		and UnitIsConnected(unit)
		and UnitIsVisible(unit)
		and CanInspect(unit, false)

	if not reachable then
		report()
		C_Timer.After(0, processQueue)
		return
	end

	local pending = { unit = unit, guid = UnitGUID(unit) }
	active = pending
	ClearInspectPlayer()
	NotifyInspect(unit)

	C_Timer.After(requestTimeout, function()
		if active == pending then
			completeActive(nil)
		end
	end)
end

--- Finishes the in-flight request, stores the result and moves on after a short delay so the
--- server side inspect throttle is not tripped.
function completeActive(specId)
	local entry = active
	active = nil

	if entry then
		local name, realm = UnitName(entry.unit)
		inspect.remember(entry.guid, specId, name, realm)
		ClearInspectPlayer()
	end

	report()
	C_Timer.After(requestDelay, processQueue)
end

listener:SetScript("OnEvent", function(_, event, guid)
	if event ~= "INSPECT_READY" or not active then
		return
	end
	if guid and active.guid and guid ~= active.guid then
		return
	end

	local specId = getInspectSpecialization and getInspectSpecialization(active.unit)
	completeActive(specId)
end)

--- Walks the given units, inspecting everyone whose spec is unknown or stale.
--- `onProgress` receives (done, total), `onComplete` fires once the queue drains.
function inspect.scan(units, onProgress, onComplete)
	if inspect.isScanning then
		return false
	end

	queue = {}
	for _, unit in ipairs(units) do
		if not UnitIsUnit(unit, "player") and not isFresh(UnitGUID(unit)) then
			queue[#queue + 1] = unit
		end
	end

	total, done = #queue, 0
	progressHandler, completeHandler = onProgress, onComplete
	inspect.isScanning = true
	listener:RegisterEvent("INSPECT_READY")

	if total == 0 then
		C_Timer.After(0, finish)
	else
		processQueue()
	end

	return true
end

function inspect.pending()
	return total - done
end
