from django.db import migrations


def add_code(apps, schema_editor):
    """Overdue visits are for foremen and administrators: the existing roles get the new permission."""
    Role = apps.get_model("core", "Role")
    for role in Role.objects.filter(name_en__in=["Administrator", "Foreman"]):
        if "visits.overdue" not in (role.permissions or []):
            role.permissions = list(role.permissions or []) + ["visits.overdue"]
            role.save(update_fields=["permissions"])


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0014_signatures"),
    ]

    operations = [
        migrations.RunPython(add_code, migrations.RunPython.noop),
    ]
