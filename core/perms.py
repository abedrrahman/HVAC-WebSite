"""Permission codes and the default roles. Roles store a list of these codes and are editable in the UI."""
PERMISSIONS = {
    "Equipment": ["equipment.view", "equipment.edit", "equipment.delete"],
    "Equipment categories & specifications": ["catalog.manage"],
    "Clients": ["clients.view", "clients.edit"],
    "Manufacturers & models": ["manufacturers.view", "manufacturers.edit"],
    "Technical documents": ["documents.view", "documents.upload", "documents.manage"],
    "Maintenance contracts": ["contracts.view", "contracts.edit"],
    "Maintenance schedules": ["schedules.view", "schedules.manage"],
    "Maintenance visits": ["visits.view", "visits.manage", "visits.update", "visits.attach", "visits.overdue"],
    "Maintenance reports from the phone": ["reports.review", "reports.approve"],
    "Reports": ["reports.view"],
    "Notifications": ["notifications.visits", "notifications.contracts"],
    "Administration": ["users.manage", "settings.manage"],
    "Data scope": ["scope.all"],
}

PERMISSION_LABELS = {
    "equipment.view": "View equipment", "equipment.edit": "Add and edit equipment",
    "equipment.delete": "Archive and delete equipment",
    "catalog.manage": "Manage categories, subtypes and technical fields",
    "clients.view": "View clients", "clients.edit": "Add, edit and archive clients",
    "manufacturers.view": "View manufacturers and models", "manufacturers.edit": "Add, edit and archive manufacturers and models",
    "documents.view": "View and download documents", "documents.upload": "Upload documents",
    "documents.manage": "Edit, assign, archive and delete any document",
    "contracts.view": "View contracts", "contracts.edit": "Create, edit, renew, suspend and terminate contracts",
    "schedules.view": "View maintenance schedules", "schedules.manage": "Create and edit maintenance schedules",
    "visits.view": "View visits", "visits.manage": "Create, assign, reschedule and cancel visits",
    "visits.update": "Update visit progress and complete visits", "visits.attach": "Upload visit reports and photographs",
    "visits.overdue": "See overdue visits and get a notification when a visit becomes overdue",
    "reports.review": "Foreman: read incoming phone reports, correct readings and send them to the engineer",
    "reports.approve": "Engineer: read, correct and approve phone reports, and send them to the client",
    "reports.view": "View and export reports",
    "notifications.visits": "Receive reminders for all visits", "notifications.contracts": "Receive contract expiry reminders",
    "users.manage": "Manage users and permissions", "settings.manage": "Manage system settings",
    "scope.all": "See all records (otherwise only equipment and visits assigned to the user)",
}

ALL_CODES = [c for group in PERMISSIONS.values() for c in group]

DEFAULT_ROLES = {
    ("Administrator", "مدير النظام"): ALL_CODES,
    ("Engineer", "مهندس"): [c for c in ALL_CODES if c not in ("users.manage", "reports.review")],  # an administrator's rights, except users and the foreman's step
    ("Foreman", "رئيس عمال"): [
        "equipment.view", "clients.view", "documents.view", "schedules.view", "visits.view", "visits.update", "visits.attach",
        "scope.all", "reports.review", "visits.overdue",
    ],
    ("Technician", "فني"): [
        "equipment.view", "documents.view", "schedules.view", "visits.view", "visits.update", "visits.attach",
    ],
    ("Viewer", "مطلع"): [
        "equipment.view", "clients.view", "manufacturers.view", "documents.view", "contracts.view", "schedules.view",
        "visits.view", "reports.view", "scope.all",
    ],
}
