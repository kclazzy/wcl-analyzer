local addonName, ns = ...

--- Name and realm normalisation. WowUtils builds character ids by lowercasing the character
--- name and realm and stripping everything that is not a letter or digit, so "Krag'jin"
--- becomes "kragjin" and "Tarren Mill" becomes "tarrenmill". Accented letters survive
--- lowercased, "Åntares" becomes "åntares", which needs a UTF-8 aware lower.
local text = {}
ns.text = text

--- Lowercases a single code point, covering Latin, Latin Extended-A, Greek and Cyrillic.
local function lowerCode(code)
	if code >= 65 and code <= 90 then
		return code + 32
	elseif code >= 0xC0 and code <= 0xDE and code ~= 0xD7 then
		return code + 32
	elseif code >= 0x100 and code <= 0x137 then
		return code % 2 == 0 and code + 1 or code
	elseif code >= 0x139 and code <= 0x148 then
		return code % 2 == 1 and code + 1 or code
	elseif code >= 0x14A and code <= 0x177 then
		return code % 2 == 0 and code + 1 or code
	elseif code == 0x178 then
		return 0xFF
	elseif code >= 0x179 and code <= 0x17E then
		return code % 2 == 1 and code + 1 or code
	elseif code >= 0x391 and code <= 0x3AB and code ~= 0x3A2 then
		return code + 32
	elseif code >= 0x400 and code <= 0x40F then
		return code + 80
	elseif code >= 0x410 and code <= 0x42F then
		return code + 32
	end
	return code
end

--- UTF-8 aware lower, used when the client does not expose string.utf8lower.
--- Only one and two byte sequences are folded, which covers every Western and Cyrillic
--- character name, longer sequences are copied through untouched.
local function lowerUtf8(value)
	local out = {}
	local index, length = 1, #value

	while index <= length do
		local byte = value:byte(index)
		if byte < 0x80 then
			out[#out + 1] = string.char(lowerCode(byte))
			index = index + 1
		elseif byte >= 0xC2 and byte <= 0xDF and index < length then
			local code = lowerCode((byte - 0xC0) * 0x40 + (value:byte(index + 1) - 0x80))
			out[#out + 1] = string.char(0xC0 + math.floor(code / 0x40), 0x80 + code % 0x40)
			index = index + 2
		else
			out[#out + 1] = value:sub(index, index)
			index = index + 1
		end
	end

	return table.concat(out)
end

--- Lowercases a name, preferring the client's own UTF-8 helper when it exists.
function text.lower(value)
	if not value then
		return ""
	end
	if string.utf8lower then
		return string.utf8lower(value)
	end
	return lowerUtf8(value)
end

--- Lowercases and strips ASCII punctuation and spaces, keeping accented letters intact.
function text.slug(value)
	return (text.lower(value):gsub("[^%w\128-\255]", ""))
end

--- Builds the "name-realm" id WowUtils uses to key characters.
function text.characterId(name, realm)
	return text.slug(name) .. "-" .. text.slug(realm)
end

--- The realm as it should be displayed. WoW hands out cross realm names without spaces
--- ("TarrenMill"), so a space is re-inserted at lower-to-upper boundaries. The id is
--- unaffected either way since slugging strips spaces again.
function text.realmName(realm)
	local localRealm = GetRealmName()
	if not realm or realm == "" or realm == localRealm or realm == GetNormalizedRealmName() then
		return localRealm
	end
	return (realm:gsub("(%l)(%u)", "%1 %2"))
end
