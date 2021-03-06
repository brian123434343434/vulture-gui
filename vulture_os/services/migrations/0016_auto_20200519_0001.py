# Generated by Django 2.1.3 on 2020-05-19 00:01

import django.core.validators
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('system', '0010_auto_20200518_2246'),
        ('services', '0015_auto_20200423_2131'),
    ]

    operations = [
        migrations.AddField(
            model_name='frontend',
            name='tenants_config',
            field=models.ForeignKey(default=1, help_text='Tenants config used for logs enrichment', null=True, on_delete=django.db.models.deletion.PROTECT, to='system.Tenants'),
        )
    ]
