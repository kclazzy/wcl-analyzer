local addonName, ns = ...

--- Entry point. Opens the export window straight away with whatever is already known, then
--- keeps refreshing it as specialisations are read off the group.
local retryDelay = 5
local namesShown = 8

local lastSignature

local events = CreateFrame("Frame")
events:RegisterEvent("ADDON_LOADED")
events:SetScript("OnEvent", function(_, event, loadedAddon)
	if event ~= "ADDON_LOADED" or loadedAddon ~= addonName then
		return
	end

	WowUtilsGroupExportDB = WowUtilsGroupExportDB or {}
	WowUtilsGroupExportDB.specs = WowUtilsGroupExportDB.specs or {}
	ns.db = WowUtilsGroupExportDB
end)

--- Identifies the state of an export, so a background refresh only replaces the document when
--- something actually changed. Replacing it re-selects the text, which would fight anyone in
--- the middle of copying.
local function signature(members)
	local parts = {}
	for index, member in ipairs(members) do
		parts[index] = table.concat({
			member.id,
			member.specName or "?",
			member.specGuessed and "guess" or "read",
			member.mainRole,
			member.rank,
		}, "|")
	end
	return table.concat(parts, ";")
end

local function render(force)
	local members = ns.roster.collect()
	local current = signature(members)

	if force or current ~= lastSignature then
		lastSignature = current
		ns.ui.setText(ns.roster.buildJson(members))
	end

	return members
end

local function describeResult(members, guessed)
	if #guessed == 0 then
		return string.format("%d |4member:members; exported, every specialisation read.", #members)
	end
	return string.format(
		"%d |4member:members; exported, %d |4specialisation:specialisations; guessed.",
		#members,
		#guessed
	)
end

--- Lists the members whose spec had to be guessed, trimmed so a full raid stays readable.
local function listNames(names)
	local shown = {}
	for index = 1, math.min(#names, namesShown) do
		shown[index] = names[index]
	end

	local list = table.concat(shown, ", ")
	if #names > namesShown then
		list = list .. string.format(" and %d more", #names - namesShown)
	end
	return list
end

--- The banner above the document. Empty once every spec was read off a real character.
local function describeWarning(members, guessed)
	if #guessed == 0 then
		return nil
	end

	return string.format(
		"Incomplete export, %d of %d |4specialisation:specialisations; had to be guessed from role.\n"
			.. "A specialisation can only be read while that player is in range and visible. Bring the group "
			.. "together and leave this window open, guesses are replaced as people come into range.\n"
			.. "Guessed: %s",
		#guessed,
		#members,
		listNames(guessed)
	)
end

--- Pushes the result of a collection to the window, banner and status line alike.
local function reportResult(members)
	local guessed = ns.roster.guessedNames(members)
	ns.ui.setStatus(describeResult(members, guessed))
	ns.ui.setWarning(describeWarning(members, guessed))
	return guessed
end

local runScan
local retryPending = false

--- Keeps retrying while the window is open and anything is still guessed. Members who are out
--- of range are skipped without touching the server, so this is close to free until someone
--- actually comes into view. One retry is in flight at a time, so hitting Refresh cannot pile
--- up parallel loops.
local function scheduleRetry(guessed)
	if retryPending or not ns.ui.isShown() or #guessed == 0 then
		return
	end

	retryPending = true
	C_Timer.After(retryDelay, function()
		retryPending = false
		if ns.ui.isShown() then
			runScan(true)
		end
	end)
end

--- Reads specialisations off the group and refreshes the window when it learns something.
--- A quiet scan leaves the status line alone until it has a result to report.
function runScan(quiet)
	if not quiet then
		ns.ui.setStatus("Reading specialisations...")
	end

	local onProgress = not quiet
		and function(done, total)
			ns.ui.setStatus(string.format("Reading specialisations, %d of %d...", done, total))
		end

	local started = ns.inspect.scan(ns.roster.units(), onProgress or nil, function()
		scheduleRetry(reportResult(render(false)))
	end)

	if not started then
		scheduleRetry(ns.roster.guessedNames(ns.roster.collect()))
	end
end

--- Builds the export and shows it. Called by the slash command and the Refresh button.
function ns.export()
	ns.ui.show()
	reportResult(render(true))
	runScan(false)
end

local function setRank(value)
	if value == "" then
		print(string.format('|cff33ff99%s|r: exporting everyone as rank "%s".', addonName, ns.roster.rank()))
		return
	end

	ns.db.rank = value
	print(string.format('|cff33ff99%s|r: rank set to "%s".', addonName, value))
end

SLASH_WOWUTILSGROUPEXPORT1 = "/wugexport"
SLASH_WOWUTILSGROUPEXPORT2 = "/groupexport"
SLASH_WOWUTILSGROUPEXPORT3 = "/wugx"
SlashCmdList.WOWUTILSGROUPEXPORT = function(input)
	local command, value = (input or ""):match("^(%S*)%s*(.-)%s*$")

	if command:lower() == "rank" then
		setRank(value)
		return
	end

	ns.export()
end
