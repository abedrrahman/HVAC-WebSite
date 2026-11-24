# Prompt for the phone app: report forms from the website

Give this whole file to the AI working on the phone app (`D:\hvac-maintenance-app`, branch `web-sync`).

---

Read `AGENTS.md` in this repo first, and the "report_forms" parts of the website's `APP_SYNC.md` (sections 2 and 3, copied below).
Work on the `web-sync` branch. Expo SDK 56: check the Expo 56 docs before using an API. Run `npx tsc --noEmit` (0 errors) and the
existing tests before you finish. Nothing here may delete or change a report that is already open or finished.

## What the website now does

The office can change the report form (the boxes a new report starts with) on the website:

- a **default form per equipment type** (`chiller`, `ahu`, `fcu`, `split`, `vrf`, `rtu`, `ccu`, `dehumidifier`, `cooling_tower`, `boiler`), and
- **a unit's own form** for units with special features. A unit's own form wins over its type's form.

The office can rename boxes, change their order, switch standard boxes off, add new boxes, set the parts list a report starts with, and,
for one unit, its setup (phase, number of compressors, circuits and fans, refrigerant, temperature and pressure units).
"Back to default" on the website clears a form: the type goes back to the app's built-in form; a unit goes back to its type's form.

## 1. Read the forms at every sync

`GET /api/sync/` now has:

- `units[].report_type`: the unit's type (the same rule as `machineTypeFor`; use it instead of guessing from the category when present).
- `report_forms.types["<type>"] = { rows, materials, changed_at }`
- `report_forms.units["<unit id>"] = { report_type, rows, materials, setup, changed_at }`

`rows` is the full ordered list `[{ key, label_en, label_ar, hidden, custom }]` or `null` (follow the next level down).
In keys and names, `#` stands for a compressor, circuit or fan number: `compressor_#_amps_l1` / `Compressor # Amps L1` means
`compressor_1_amps_l1` / `Compressor 1 Amps L1`, `compressor_2_amps_l1` / `Compressor 2 Amps L1`, and so on, for every compressor the unit has.
`hidden: true` leaves the box out. `custom: true` is a box that is not in the built-in form (key `custom_xxxxxxxx`); it holds free text like
today's custom rows. A standard box the list does not mention (the app gained it after the form was saved) stays where the built-in form puts it.
`materials` is `[{ description, unit }]` or `null`. `setup` keys may be missing: a missing key is left to the phone.
`changed_at` is an ISO date-time.

Store these in SQLite (a new table, replaced at every sync like `visits`). Add a migration (next V number); do not change old tables in a way that loses data.

## 2. Build every NEW report from the newest form

When a new report is created for a unit (`createReport`):

1. Pick the form: the unit's form from the website if there is one, else the type's form from the website, else the built-in form.
2. Compare with the unit's own form on the phone (`machine_templates`, saved when a report is finished): **use whichever is newer**
   (website `changed_at` against the template's `updated_at`). The phone's template keeps working exactly as today when it is newer.
3. Build the rows: start from `buildChecklist(type, config)` with the unit's config (counts and phase from `setup` when given, else as today).
   Then apply `rows`: put the boxes in the list's order, use the list's names (with `#` replaced by the number), leave out `hidden` ones,
   and insert `custom` boxes at their place as custom rows (`is_custom = 1`, `field_key` = the custom key so it can be matched later).
   Rows of a compressor, circuit or fan the unit does not have are not created. Apply `materials` as the starting parts list, and
   `refrigerant`, `temp_unit`, `pressure_unit` from `setup` as the starting values.
4. Today a new report copies the previous report of the same unit (`carryOverFromReport`). Keep copying the client, site, model and serial,
   but when the website's form is newer than that previous report, the boxes come from the website's form, not from the previous report.

**Reports that are already open never change** when a new form arrives (`upgradeOpenReports` and `syncUnitRows` must not apply website forms
to them). Finished reports never change.

## 3. Send the form back with every finished report

In the report JSON sent to `POST /api/reports/` add:

- `created_at`: when the report was started (it is already in the report row; make sure it is sent).
- `form`: `{ rows, materials, setup }` describing the form the finished report ended with:
  - `rows`: every box of the report in its order, standard and custom, with its names, as `{ key, label_en, label_ar, hidden: false, custom }`,
    plus the standard boxes the technician removed from this report (`removed_rows`) as `hidden: true`.
    Write compressor, circuit and fan boxes once with `#` (send `compressor_#_amps` once, not once per compressor).
  - `materials`: the report's parts (description and unit).
  - `setup`: `phase`, `compressors`, `circuits`, `supply_fans`, `extract_fans`, `return_fans`, `refrigerant`, `temp_unit`, `pressure_unit`.

The website keeps this as the unit's form for the next report, **unless the form was changed on the website after the report was started**:
then the website's form wins and the report's form is ignored (the report's readings are always kept). Keep saving the phone's own
`machine_templates` at lock time as today.

## 4. Done means

- A new report on a unit whose type form was changed on the website shows the renamed, reordered, hidden and added boxes and the starting parts.
- A unit with its own form on the website gets that form, and its setup (counts, phase, refrigerant, units) as starting values.
- After "Back to default" on the website and a sync, new reports use the default again.
- An open report keeps its boxes after a sync that brings a changed form.
- A finished report's upload includes `created_at` and `form`.
- `npx tsc --noEmit` passes. Say what was not tested on a phone. Commit to `web-sync` and push.
