local addonName, ns = ...

--- Collects the current party or raid and renders it as WowUtils "Import Roster JSON".
local roster = {}
ns.roster = roster

--- WowUtils validates rank against its own list, and "Raider" is the one every roster has.
--- Change it for a whole export with /wugx rank <name>.
local fallbackRank = "Raider"

local getSpecializationInfo = _G.GetSpecializationInfo
	or (C_SpecializationInfo and C_SpecializationInfo.GetSpecializationInfo)
local getSpecialization = _G.GetSpecialization or (C_SpecializationInfo and C_SpecializationInfo.GetSpecialization)

--- Every unit token in the group, including the player.
function roster.units()
	local units = {}

	if IsInRaid() then
		for index = 1, GetNumGroupMembers() do
			units[#units + 1] = "raid" .. index
		end
	else
		units[#units + 1] = "player"
		for index = 1, GetNumSubgroupMembers() do
			units[#units + 1] = "party" .. index
		end
	end

	return units
end

--- The rank every exported member gets. The export stands on its own, nothing is read from
--- the guild roster.
function roster.rank()
	return (ns.db and ns.db.rank) or fallbackRank
end

--- The player's own battletag, used for metadata.exportedBy.
local function ownBattletag()
	if not BNGetInfo then
		return nil
	end
	local _, battleTag = BNGetInfo()
	return battleTag
end

--- Battletag per character id, covering the player and every online Battle.net friend.
--- WowUtils uses the battletag to fold a player's characters into a single member on import.
local function battletags()
	local tags = {}

	local name, realm = UnitFullName("player")
	if name then
		tags[ns.text.characterId(name, realm ~= "" and realm or GetRealmName())] = ownBattletag()
	end

	if not C_BattleNet or not BNGetNumFriends then
		return tags
	end

	for index = 1, BNGetNumFriends() do
		local account = C_BattleNet.GetFriendAccountInfo(index)
		if account and account.battleTag then
			for accountIndex = 1, (C_BattleNet.GetFriendNumGameAccounts(index) or 0) do
				local game = C_BattleNet.GetFriendGameAccountInfo(index, accountIndex)
				if game and game.clientProgram == "WoW" and game.characterName and game.realmName then
					tags[ns.text.characterId(game.characterName, game.realmName)] = account.battleTag
				end
			end
		end
	end

	return tags
end

--- Specialisation id for a unit, from the live API for the player and from the inspect cache
--- for everyone else.
local function specForUnit(unit)
	if UnitIsUnit(unit, "player") then
		local index = getSpecialization and getSpecialization()
		return index and getSpecializationInfo and getSpecializationInfo(index) or nil
	end
	return ns.inspect.cachedSpec(UnitGUID(unit))
end

--- Falls back to the role the group has assigned when the spec is unknown, and to a class
--- default to decide between melee and ranged.
local function fallbackRole(unit, classToken)
	local assigned = UnitGroupRolesAssigned(unit)
	return ns.data.assignedRoles[assigned] or ns.data.classRoles[classToken] or "melee"
end

local function describeUnit(unit, tags)
	if not UnitExists(unit) or not UnitIsPlayer(unit) then
		return nil
	end

	local name, realm = UnitFullName(unit)
	if not name or name == "" or name == UNKNOWNOBJECT then
		return nil
	end

	local _, classToken = UnitClass(unit)
	local id = ns.text.characterId(name, realm ~= "" and realm or GetRealmName())

	-- The real spec when it could be read, otherwise a guess from the assigned role.
	local spec = ns.data.specInfo(specForUnit(unit), classToken)
		or ns.data.guessSpec(classToken, UnitGroupRolesAssigned(unit))

	return {
		name = name,
		realm = ns.text.realmName(realm),
		id = id,
		className = ns.data.classNames[classToken] or classToken,
		specName = spec and spec.name or nil,
		specGuessed = spec ~= nil and spec.guessed == true,
		mainRole = spec and spec.role or fallbackRole(unit, classToken),
		rank = roster.rank(),
		battletag = tags[id],
	}
end

--- Builds the member list, sorted by role and then name so the export stays stable.
function roster.collect()
	local tags = battletags()
	local members = {}
	local seen = {}

	for _, unit in ipairs(roster.units()) do
		local member = describeUnit(unit, tags)
		if member and not seen[member.id] then
			seen[member.id] = true
			members[#members + 1] = member
		end
	end

	table.sort(members, function(left, right)
		local leftOrder = ns.data.roleOrder[left.mainRole] or 9
		local rightOrder = ns.data.roleOrder[right.mainRole] or 9
		if leftOrder ~= rightOrder then
			return leftOrder < rightOrder
		end
		return left.name < right.name
	end)

	return members
end

--- Where the roster came from, the guild name when there is one.
local function exportSource()
	local guild = GetGuildInfo("player")
	if guild and guild ~= "" then
		return guild
	end
	return (UnitName("player") or "Unknown") .. "'s group"
end

--- Renders the member list in the format the WowUtils "Import Roster JSON" dialog accepts.
function roster.buildJson(members)
	local writer = ns.json.writer()

	writer:openObject()
	writer:field("version", "1.0")

	writer:openObject("metadata")
	writer:field("exportedAt", date("!%Y-%m-%dT%H:%M:%S", GetServerTime()) .. ".000Z")
	writer:field("exportedFrom", exportSource())
	writer:optional("exportedBy", ownBattletag())
	writer:field("characterCount", #members)
	writer:field("memberCount", #members)
	writer:closeObject()

	writer:openArray("members")
	for _, member in ipairs(members) do
		writer:openObject()
		writer:field("displayName", member.name)
		writer:optional("battletag", member.battletag)
		writer:field("rank", member.rank)
		writer:field("mainRole", member.mainRole)

		writer:openArray("characters")
		writer:openObject()
		writer:field("name", member.name)
		writer:field("realm", member.realm)
		writer:field("playerClass", member.className)
		writer:optional("playerSpec", member.specName)
		writer:field("order", "a0")
		writer:closeObject()
		writer:closeArray()

		writer:openObject("characterStatuses")
		writer:field(member.id, "main")
		writer:closeObject()

		writer:field("mainCharacterId", member.id)
		writer:closeObject()
	end
	writer:closeArray()

	writer:closeObject()

	return writer:build()
end

--- Names of the members carrying a guessed specialisation rather than one read off the
--- character, in the order they appear in the export.
function roster.guessedNames(members)
	local names = {}
	for _, member in ipairs(members) do
		if member.specGuessed then
			names[#names + 1] = member.name
		end
	end
	return names
end
