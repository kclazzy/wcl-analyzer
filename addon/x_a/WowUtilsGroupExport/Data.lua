local addonName, ns = ...

--- Static lookups. Everything is kept in English on purpose, the export has to read the same
--- way on a German or French client as it does on an English one.
local data = {}
ns.data = data

--- Class file token to the English class name WowUtils expects in playerClass.
data.classNames = {
	DEATHKNIGHT = "Death Knight",
	DEMONHUNTER = "Demon Hunter",
	DRUID = "Druid",
	EVOKER = "Evoker",
	HUNTER = "Hunter",
	MAGE = "Mage",
	MONK = "Monk",
	PALADIN = "Paladin",
	PRIEST = "Priest",
	ROGUE = "Rogue",
	SHAMAN = "Shaman",
	WARLOCK = "Warlock",
	WARRIOR = "Warrior",
}

--- Specialisation id to English name and the mainRole it maps to.
data.specs = {
	-- Death Knight
	[250] = { name = "Blood", role = "tank" },
	[251] = { name = "Frost", role = "melee" },
	[252] = { name = "Unholy", role = "melee" },
	-- Demon Hunter
	[577] = { name = "Havoc", role = "melee" },
	[581] = { name = "Vengeance", role = "tank" },
	-- Druid
	[102] = { name = "Balance", role = "ranged" },
	[103] = { name = "Feral", role = "melee" },
	[104] = { name = "Guardian", role = "tank" },
	[105] = { name = "Restoration", role = "healer" },
	-- Evoker
	[1467] = { name = "Devastation", role = "ranged" },
	[1468] = { name = "Preservation", role = "healer" },
	[1473] = { name = "Augmentation", role = "ranged" },
	-- Hunter
	[253] = { name = "Beast Mastery", role = "ranged" },
	[254] = { name = "Marksmanship", role = "ranged" },
	[255] = { name = "Survival", role = "melee" },
	-- Mage
	[62] = { name = "Arcane", role = "ranged" },
	[63] = { name = "Fire", role = "ranged" },
	[64] = { name = "Frost", role = "ranged" },
	-- Monk
	[268] = { name = "Brewmaster", role = "tank" },
	[269] = { name = "Windwalker", role = "melee" },
	[270] = { name = "Mistweaver", role = "healer" },
	-- Paladin
	[65] = { name = "Holy", role = "healer" },
	[66] = { name = "Protection", role = "tank" },
	[70] = { name = "Retribution", role = "melee" },
	-- Priest
	[256] = { name = "Discipline", role = "healer" },
	[257] = { name = "Holy", role = "healer" },
	[258] = { name = "Shadow", role = "ranged" },
	-- Rogue
	[259] = { name = "Assassination", role = "melee" },
	[260] = { name = "Outlaw", role = "melee" },
	[261] = { name = "Subtlety", role = "melee" },
	-- Shaman
	[262] = { name = "Elemental", role = "ranged" },
	[263] = { name = "Enhancement", role = "melee" },
	[264] = { name = "Restoration", role = "healer" },
	-- Warlock
	[265] = { name = "Affliction", role = "ranged" },
	[266] = { name = "Demonology", role = "ranged" },
	[267] = { name = "Destruction", role = "ranged" },
	-- Warrior
	[71] = { name = "Arms", role = "melee" },
	[72] = { name = "Fury", role = "melee" },
	[73] = { name = "Protection", role = "tank" },
}

--- Roles for specs that are not in the table above yet, matched on the name the client
--- reports. Keeps newly added specialisations landing on the right side of melee/ranged.
data.rolesBySpecName = {
	Devourer = "ranged",
}

--- Where a damage dealer of this class most likely stands when the spec is unknown.
data.classRoles = {
	DEATHKNIGHT = "melee",
	DEMONHUNTER = "melee",
	DRUID = "ranged",
	EVOKER = "ranged",
	HUNTER = "ranged",
	MAGE = "ranged",
	MONK = "melee",
	PALADIN = "melee",
	PRIEST = "ranged",
	ROGUE = "melee",
	SHAMAN = "ranged",
	WARLOCK = "ranged",
	WARRIOR = "melee",
}

data.assignedRoles = {
	TANK = "tank",
	HEALER = "healer",
}

--- Sort order of the exported member list.
data.roleOrder = {
	tank = 1,
	healer = 2,
	melee = 3,
	ranged = 4,
}

--- Best guess at a specialisation for a class in a given role, used when the real spec cannot
--- be read. Tanks and healers are exact for every class that has one spec for the job, the
--- damage entries are only the most common pick and will be wrong often enough to be flagged
--- in the export window.
local guesses = {
	DEATHKNIGHT = { tank = 250, damage = 252 },
	DEMONHUNTER = { tank = 581, damage = 577 },
	DRUID = { tank = 104, healer = 105, damage = 102 },
	EVOKER = { healer = 1468, damage = 1467 },
	HUNTER = { damage = 253 },
	MAGE = { damage = 64 },
	MONK = { tank = 268, healer = 270, damage = 269 },
	PALADIN = { tank = 66, healer = 65, damage = 70 },
	PRIEST = { healer = 257, damage = 258 },
	ROGUE = { damage = 259 },
	SHAMAN = { healer = 264, damage = 262 },
	WARLOCK = { damage = 265 },
	WARRIOR = { tank = 73, damage = 72 },
}

local getSpecializationInfoByID = _G.GetSpecializationInfoByID
	or (C_SpecializationInfo and C_SpecializationInfo.GetSpecializationInfoByID)

--- Resolves a spec id to `{ name, role }`, falling back to whatever the client knows about
--- specs that are missing from {@link data.specs}.
function data.specInfo(specId, classToken)
	if not specId or specId == 0 then
		return nil
	end

	local known = data.specs[specId]
	if known then
		return known
	end

	if not getSpecializationInfoByID then
		return nil
	end

	local _, name, _, _, role = getSpecializationInfoByID(specId)
	if not name then
		return nil
	end

	return {
		name = name,
		role = data.assignedRoles[role]
			or data.rolesBySpecName[name]
			or data.classRoles[classToken]
			or "melee",
	}
end

--- Guesses a spec from the class and the role the group has assigned. WowUtils rejects an
--- import that is missing playerSpec, and specs cannot be read at all for anyone out of
--- inspect range, so a flagged guess beats leaving the field out.
function data.guessSpec(classToken, assignedRole)
	local byRole = guesses[classToken]
	if not byRole then
		return nil
	end

	local specId = byRole[data.assignedRoles[assignedRole] or "damage"] or byRole.damage
	local spec = specId and data.specs[specId]
	if not spec then
		return nil
	end

	return { name = spec.name, role = spec.role, guessed = true }
end
