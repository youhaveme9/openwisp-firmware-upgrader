# Generated by Django 3.1.1 on 2021-03-12 15:16

from django.db import migrations

from openwisp_firmware_upgrader.migrations import create_device_firmware_for_connections


def create_device_firmware_for_connections_helper(apps, schema_editor):
    app_label = "firmware_upgrader"
    create_device_firmware_for_connections(apps, schema_editor, app_label)


class Migration(migrations.Migration):
    dependencies = [
        ("sample_firmware_upgrader", "0002_default_permissions"),
    ]

    operations = [
        migrations.RunPython(
            create_device_firmware_for_connections_helper,
            reverse_code=migrations.RunPython.noop,
        ),
    ]
