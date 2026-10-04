# WowUtils Group Export

Exports your current party or raid as a JSON document that the WowUtils "Import Roster JSON" dialog accepts. The document is shown in a window with the text already selected, so copying it is a single Ctrl-C.

## Usage

`/wugexport`, `/groupexport` or `/wugx` opens the window. Paste the result into WowUtils under Import Roster JSON.

`/wugx rank <name>` changes the rank every member is exported with, "Raider" by default.

The window fills in immediately with what the client already knows, then keeps refreshing itself as more of the group comes into range. The status line says how many specialisations are still guesses. The document is only replaced when something actually changed, so a background refresh will not interrupt you mid copy.

## What ends up in the export

| Field                              | Source                                                                 |
| ---------------------------------- | ---------------------------------------------------------------------- |
| `metadata.exportedFrom`            | your guild name, or "<Name>'s group" when you are not in one           |
| `metadata.exportedBy`              | your battletag                                                         |
| `displayName`, `characters[].name` | the character name, accents intact                                     |
| `characters[].realm`               | the realm, with spaces restored for cross realm members                |
| `playerClass`, `playerSpec`        | always in English, so the export reads the same on any locale          |
| `rank`                             | "Raider" for everyone, or whatever `/wugx rank <name>` was set to      |
| `battletag`                        | yours, plus any group member on your Battle.net friends list           |
| `mainRole`                         | `tank`, `healer`, `melee` or `ranged`, derived from the specialisation |

## Specialisations

The game has no API that reports another player's specialisation, so the addon inspects the group one member at a time, which only works while they are visible and in inspect range. Join a group from the other side of the world and the client simply has nothing to report. Results are cached in saved variables for ten minutes, and the window retries every few seconds while it is open, so specs fill themselves in as people come into range.

WowUtils rejects an import that is missing `playerSpec`, so a spec that cannot be read is guessed from the class and the role the group assigned rather than left out:

- tanks are exact, every tanking class has exactly one tank spec
- healers are exact apart from Priest, where Discipline and Holy cannot be told apart
- damage dealers are the most common spec for the class, so these are the ones to check

Whenever anything had to be guessed the window shows a warning above the document, naming the members it could not read. Getting within inspect range and leaving the window open replaces the guesses with the real thing, and the warning clears itself once the export is complete.

## Installing

Copy the `WowUtilsGroupExport` folder into `World of Warcraft/_retail_/Interface/AddOns/`, then reload the client.

`scripts/package.sh` builds a distributable zip into `dist/`, named from the version in the TOC.

## License

All rights reserved, see [LICENSE](LICENSE). You may run it, you may not redistribute or publish modified versions of it.
