local addonName, ns = ...

--- Minimal JSON writer, shaped around the fixed layout of the WowUtils roster export.
--- Key order matters for a readable diff against the reference export, so the writer is
--- deliberately sequential instead of serialising Lua tables (whose key order is undefined).
local json = {}
ns.json = json

local escapes = {
	['"'] = '\\"',
	["\\"] = "\\\\",
	["\b"] = "\\b",
	["\f"] = "\\f",
	["\n"] = "\\n",
	["\r"] = "\\r",
	["\t"] = "\\t",
}

local function escapeCharacter(character)
	return escapes[character] or string.format("\\u%04x", character:byte())
end

--- Quotes a string. Bytes above 127 pass through untouched, JSON accepts raw UTF-8.
function json.quote(value)
	return '"' .. tostring(value):gsub('[%c"\\]', escapeCharacter) .. '"'
end

--- Encodes a scalar. Whole numbers are written without a decimal point.
function json.encode(value)
	local kind = type(value)
	if kind == "string" then
		return json.quote(value)
	elseif kind == "number" then
		return value % 1 == 0 and string.format("%d", value) or tostring(value)
	elseif kind == "boolean" then
		return value and "true" or "false"
	end
	return "null"
end

local function label(name)
	return name and (json.quote(name) .. ": ") or ""
end

--
-- Writer
--

local writer = {}
writer.__index = writer

--- Creates a pretty printing writer using two space indentation.
function json.writer()
	return setmetatable({ lines = {}, depth = 0 }, writer)
end

function writer:emit(text)
	self.lines[#self.lines + 1] = string.rep("  ", self.depth) .. text
end

--- Drops the trailing comma of the previous line, called whenever a container closes.
function writer:untrail()
	local last = #self.lines
	if last > 0 then
		self.lines[last] = self.lines[last]:gsub(",$", "")
	end
end

--- Writes `name: value`, or just `value` when inside an array.
function writer:field(name, value)
	self:emit(label(name) .. json.encode(value) .. ",")
end

--- Writes a field only when the value is present, used for the optional battletag.
function writer:optional(name, value)
	if value ~= nil and value ~= "" then
		self:field(name, value)
	end
end

function writer:openObject(name)
	self:emit(label(name) .. "{")
	self.depth = self.depth + 1
end

function writer:closeObject()
	self:untrail()
	self.depth = self.depth - 1
	self:emit("},")
end

function writer:openArray(name)
	self:emit(label(name) .. "[")
	self.depth = self.depth + 1
end

function writer:closeArray()
	self:untrail()
	self.depth = self.depth - 1
	self:emit("],")
end

--- Renders the document. Safe to call once every container has been closed.
function writer:build()
	self:untrail()
	return table.concat(self.lines, "\n")
end
