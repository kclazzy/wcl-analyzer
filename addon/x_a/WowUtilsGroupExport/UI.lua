local addonName, ns = ...

--- The export window, a scrollable read only edit box with its contents pre-selected so the
--- whole document is one Ctrl-C away.
local ui = {}
ns.ui = ui

local warningTop = -50
local scrollTop = -52

local frame, editBox, scrollFrame, statusText, warningText
local currentText = ""

--- The document starts below the warning when there is one, so the banner never overlaps it.
local function layout()
	if not scrollFrame then
		return
	end

	local offset = scrollTop
	if warningText:IsShown() then
		offset = warningTop - warningText:GetStringHeight() - 8
	end

	scrollFrame:SetPoint("TOPLEFT", 14, offset)
end

local function setTitle(target, title)
	local fontString = target.TitleText or (target.TitleContainer and target.TitleContainer.TitleText)
	if fontString then
		fontString:SetText(title)
	end
end

local function createFrame()
	frame = CreateFrame("Frame", "WowUtilsGroupExportFrame", UIParent, "BasicFrameTemplateWithInset")
	frame:SetSize(640, 520)
	frame:SetPoint("CENTER")
	frame:SetFrameStrata("FULLSCREEN_DIALOG")
	frame:SetToplevel(true)
	frame:SetMovable(true)
	frame:EnableMouse(true)
	frame:RegisterForDrag("LeftButton")
	frame:SetScript("OnDragStart", frame.StartMoving)
	frame:SetScript("OnDragStop", frame.StopMovingOrSizing)
	frame:SetScript("OnHide", function()
		if editBox then
			editBox:ClearFocus()
		end
	end)
	setTitle(frame, "WowUtils Group Export")
	tinsert(UISpecialFrames, "WowUtilsGroupExportFrame")

	local hint = frame:CreateFontString(nil, "ARTWORK", "GameFontHighlightSmall")
	hint:SetPoint("TOPLEFT", 14, -32)
	hint:SetPoint("TOPRIGHT", -14, -32)
	hint:SetJustifyH("LEFT")
	hint:SetText("Press Ctrl-C (Cmd-C on Mac) to copy, then paste into WowUtils, Import Roster JSON.")

	warningText = frame:CreateFontString(nil, "ARTWORK", "GameFontNormalSmall")
	warningText:SetPoint("TOPLEFT", 14, warningTop)
	warningText:SetPoint("TOPRIGHT", -14, warningTop)
	warningText:SetJustifyH("LEFT")
	warningText:SetJustifyV("TOP")
	warningText:SetWordWrap(true)
	warningText:SetSpacing(2)
	warningText:SetTextColor(1, 0.65, 0.13)
	warningText:Hide()

	scrollFrame = CreateFrame("ScrollFrame", "WowUtilsGroupExportScroll", frame, "UIPanelScrollFrameTemplate")
	scrollFrame:SetPoint("TOPLEFT", 14, scrollTop)
	scrollFrame:SetPoint("BOTTOMRIGHT", -34, 62)

	editBox = CreateFrame("EditBox", nil, scrollFrame)
	editBox:SetMultiLine(true)
	editBox:SetAutoFocus(false)
	editBox:SetMaxLetters(0)
	editBox:SetFontObject(ChatFontNormal)
	editBox:SetWidth(scrollFrame:GetWidth())
	editBox:SetScript("OnEscapePressed", function()
		frame:Hide()
	end)
	--- The box has to stay editable for copy to work, so any typing is simply undone.
	editBox:SetScript("OnTextChanged", function(self, isUserInput)
		if isUserInput then
			self:SetText(currentText)
			self:HighlightText()
		end
	end)
	scrollFrame:SetScrollChild(editBox)
	scrollFrame:SetScript("OnSizeChanged", function(_, width)
		editBox:SetWidth(width)
	end)

	statusText = frame:CreateFontString(nil, "ARTWORK", "GameFontDisableSmall")
	statusText:SetPoint("BOTTOMLEFT", 16, 22)
	statusText:SetJustifyH("LEFT")

	local close = CreateFrame("Button", nil, frame, "UIPanelButtonTemplate")
	close:SetSize(100, 22)
	close:SetPoint("BOTTOMRIGHT", -14, 16)
	close:SetText(CLOSE or "Close")
	close:SetScript("OnClick", function()
		frame:Hide()
	end)

	local selectAll = CreateFrame("Button", nil, frame, "UIPanelButtonTemplate")
	selectAll:SetSize(100, 22)
	selectAll:SetPoint("RIGHT", close, "LEFT", -6, 0)
	selectAll:SetText("Select all")
	selectAll:SetScript("OnClick", function()
		editBox:SetFocus()
		editBox:HighlightText()
	end)

	local refresh = CreateFrame("Button", nil, frame, "UIPanelButtonTemplate")
	refresh:SetSize(100, 22)
	refresh:SetPoint("RIGHT", selectAll, "LEFT", -6, 0)
	refresh:SetText("Refresh")
	refresh:SetScript("OnClick", function()
		ns.export()
	end)
end

--- Opens the window, creating it on first use.
function ui.show()
	if not frame then
		createFrame()
	end
	frame:Show()
	frame:Raise()
end

function ui.isShown()
	return frame ~= nil and frame:IsShown()
end

--- Replaces the document and selects it so it is ready to copy.
function ui.setText(value)
	if not frame then
		createFrame()
	end

	currentText = value or ""
	editBox:SetText(currentText)
	editBox:SetCursorPosition(0)
	editBox:SetFocus()
	editBox:HighlightText()
	scrollFrame:SetVerticalScroll(0)
end

function ui.setStatus(value)
	if statusText then
		statusText:SetText(value or "")
	end
end

--- Shows the incomplete export warning above the document, or clears it when every
--- specialisation was read off a real character.
function ui.setWarning(value)
	if not frame then
		createFrame()
	end

	if value and value ~= "" then
		warningText:SetText(value)
		warningText:Show()
	else
		warningText:SetText("")
		warningText:Hide()
	end

	layout()
end
