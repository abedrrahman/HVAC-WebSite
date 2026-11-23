# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

ASEE (Aladdin Sweiti Engineering Est.) office staff on desktop PCs: an administrator, foremen who check incoming phone reports, engineers who approve them, and read-only viewers. Field technicians work mainly in the companion phone app and use the web page only occasionally. Staff read English and Arabic; every page exists in both, and Arabic pages run right to left.

## Product Purpose

An internal tool for ASEE's HVAC maintenance business. It holds about 870 real client units for about 130 clients, with their manuals, alarm codes, maintenance contracts, scheduled visits and the maintenance reports technicians send from the phone. Success is the office always knowing what is due, what is late, which reports wait for approval, and having the right manual and history for any unit in a few clicks.

## Positioning

Internal only: not sold and not shown to clients. Approved reports leave the system as PDFs emailed to clients.

## Operating Context

- Office PCs, often several tabs open through the working day; the owner starts the page from a desktop file on one PC.
- Report flow: phone upload → foreman corrects and sends on → engineer approves → PDF emailed to the client.
- Units are found by their old ManWinWin code (for example AHU-0003), client, model or serial number.
- Data shape: category > subtype > model > unit; every unit belongs to a client; manufacturer is set on the model.

## Capabilities and Constraints

- Django server-rendered templates with one stylesheet (`static/app.css`) and one script (`static/app.js`); no front-end framework or build step.
- Every visible string needs an Arabic entry in `core/i18n.py`; layouts must work right to left.
- Numbers placed in CSS must be formatted as text (the Arabic locale writes decimal commas).
- Roles and permissions decide what each person sees; the same page renders differently per role.
- Runs on 127.0.0.1 on one PC today; will move to a rented server later.

## Brand Commitments

- Company name and logo: ASEE / Aladdin Sweiti Engineering Est. (`static/logo.png`).
- Brand colour: ASEE pink-red `#D1003C`, already used across the web page and the report PDFs.
- Bilingual English and Arabic throughout.

## Evidence on Hand

Real client data in the database (units, clients, contracts, visits, reports); manufacturer manuals; ASEE logo. No marketing copy, testimonials or public claims are needed or should be invented.

## Product Principles

1. Find it fast: any unit, manual or report within a few clicks.
2. What needs action comes first: late visits and reports waiting for approval are never hidden behind other content.
3. One look in both languages: Arabic is a first-class layout, not a mirror afterthought.
4. Real data, honestly shown: never decorative numbers or invented content.

## Accessibility & Inclusion

Long daily use on office monitors: readable text sizes, strong contrast, clear focus, full keyboard use, and correct right-to-left behaviour.
