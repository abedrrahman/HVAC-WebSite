# Phone app connection

The website side lives in `core/views/api.py`. The phone app (github.com/abedrrahman/hvac-maintenance-app) is the other side.
All addresses below are under the website's address, which must be HTTPS when used from phones.

## 1. Sign in — `POST /api/login/`

Body (JSON): `{"username": "...", "password": "..."}`
Answer: `{"token": "...", "name": "...", "language": "en|ar"}`

- Keep the token on the phone and send it on every later request as the header `Authorization: Token <token>`.
- One phone per user: signing in again replaces the old token.
- 401 wrong password, 403 temporary password not changed yet, 429 too many failed tries (15 minutes).

## 2. Download site data — `GET /api/sync/`

Answer (JSON): `synced_at`, `clients`, `units`, `documents`, `visits`.

- `units[]`: `id, name_en, name_ar, client_id, category, subtype, manufacturer, model, serial_number, project, location, qr_token, url, document_ids`
- `documents[]`: `id, name_en, name_ar, type, language, url` — the url opens the file on the website (needs website sign-in).
- `visits[]`: the user's open visits: `id, number, unit_id, client_id, scheduled_date, type, overdue, status, assigned_to_me, contract`
  - `status` is `scheduled`, `rescheduled` or `in_progress`. `in_progress` means the office pressed Start Visit (or sent a whole contract) and chose a technician.
  - `assigned_to_me` is true when this user is that technician: these are the visits for the phone's "My visits" dashboard.
  - `contract` is `{id, number, title}` or null. Visits with the same contract id belong together and can be signed once for all units.
  - Order: visits sent to this user (`in_progress` and `assigned_to_me`) first, then the rest oldest first. The first visit listed for a unit is the one a report on that unit should close.
- `units[].report_type`: the phone's equipment type for the unit (`chiller`, `ahu`, `fcu`, `split`, `vrf`, `rtu`, `ccu`, `dehumidifier`, `cooling_tower`, `boiler` or `custom`), the same rule as the app's `machineTypeFor`.
- `report_forms`: the report forms changed on the website. Without an entry the phone uses its own built-in form.
  - `types`: `{ "<report_type>": { rows, materials, changed_at } }`, the default form of every unit of that type.
  - `units`: `{ "<unit id>": { report_type, rows, materials, setup, changed_at } }`, one unit's own form. It wins over its type's.
  - `rows`: the boxes in order, `[{ key, label_en, label_ar, hidden, custom }]`, or `null` (follow the type, or the built-in form).
    Standard boxes use the app's keys; a compressor, circuit or fan box has `#` for its number in the key and the names
    (`compressor_#_amps`, `Compressor # Amps`): replace `#` with 1, 2, 3 for each one the unit has. `hidden`: leave the box out.
    `custom`: a box added on the website or the phone (its key is `custom_...`). A standard box the list does not mention stays where the built-in form puts it.
  - `materials`: `[{ description, unit }]` the parts list starts with, or `null`.
  - `setup` (units only): any of `phase` (`1Ph`/`3Ph`), `compressors`, `circuits`, `supply_fans`, `extract_fans`, `return_fans` (numbers),
    `refrigerant`, `temp_unit` (`C`/`F`/`K`), `pressure_unit` (`bar`/`psi`/`kPa`/`MPa`). A missing key is left to the phone.
  - `changed_at`: when the form last changed. A new report uses the newest of the website's form and the unit's own form on the phone.
    A report already open keeps its boxes.
- The list of standard boxes per type is copied from the app into `core/report_form_catalog.json` by `node tools/export_form_catalog.mjs <app folder>`.
  Run it again whenever the app's built-in forms change.
- A user without "See all records" gets only the units of visits assigned to them.
- The whole list is sent every time; replace the phone's copy with it.

## Offline manuals — `GET /api/documents/<id>/file/`

The document's file, sent with the same token header. The app saves it on the phone the first time a manual is opened (or with "download all" on a unit) and opens the saved copy afterwards, with no signal. Each document in `/api/sync/` carries `file_name` and `size`; the size is part of the saved file's name, so a file replaced on the website is downloaded again. 404 when the user may not see the document.

## Alarm Lookup — `GET /api/alarms/` and `GET /api/alarms/ask/`

- `/api/alarms/?have=<version>`: the whole alarm list for use with no signal. Answer: `version`, `fields`, `alarms` (rows in the order of `fields`).
  When the phone's version is current, `alarms` is `null`.
- `/api/alarms/ask/?q=<question>&lang=ar|en&model=<optional model number>`: the website's assistant. Same answer as the website's Alarm Lookup page:
  `text`, `alarms[] {code, name, controllers, effect, causes, steps[] {label, text}, source}`, `passages[] {snippet, source}`.

## 3. Upload a finished report — `POST /api/reports/`

Multipart form:

- `report`: the report as JSON. Used fields: `id` (the app's report id, required), `unit_id` (from the download) or `serial_no`,
  `visit_id` (optional, an open visit from the download), `date`, `maintenance_type`, `technician`, `general_notes`,
  `required_maintenance`, `checklist_items[] {label_en, value}`, `attention_notes[] {unit_no, note}`, `materials[] {description, qty, unit}`
- `pdf`: the exported PDF (optional but expected)
- `photo`: site photos, repeatable, up to 30
- `signature`: the signatures as PNG or JPEG pictures, repeatable, up to 4. Name the files `signature_technician.png` and `signature_client.png`; the website prints them on the approved report.
- `created_at` (when the report was started) and `form`: the form the finished report ended with, `{ rows, materials, setup }` in the shapes of
  `report_forms` above (full `rows` list, including hidden standard boxes, with the report's own names and order). The website keeps it as the
  unit's form for its next report, unless the form was changed on the website after `created_at`: then the website's change wins.
  A report's readings are always kept, whatever happens to the form.
- For a contract signed once for all its units: send each unit's report separately with the same signature files, and put the same `contract_batch_id` (any text the phone makes up) in each report's JSON.

Answer: `{"visit_id", "number", "url", "already_uploaded"}` — 201 the first time, 200 when the same `id` is sent again.

- The report becomes a completed visit on the unit (the given open visit is completed, otherwise a new one is created).
- If the unit is not on the website (or not visible to this user), the report is still kept: the answer has `"pending": true`
  and the report waits under Maintenance Reports → Daily reports until someone presses "Add site and unit". The phone treats it as uploaded.
- Safe to retry: the same `id` never creates a second visit. Upload when the phone is online, and mark the report as uploaded only on 200/201.
- 401: sign in again. 400: bad data or file type. 403: the role cannot complete visits.
