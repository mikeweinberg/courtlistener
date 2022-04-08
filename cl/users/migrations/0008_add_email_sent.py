# Generated by Django 3.2.12 on 2022-04-07 22:17

from django.conf import settings
import django.contrib.postgres.fields
from django.db import migrations, models
import django.db.models.deletion
import uuid


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('users', '0007_alter_unique_constraints'),
    ]

    operations = [
        migrations.CreateModel(
            name='EmailSent',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('date_created', models.DateTimeField(auto_now_add=True, db_index=True, help_text='The moment when the item was created.')),
                ('date_modified', models.DateTimeField(auto_now=True, db_index=True, help_text='The last moment when the item was modified. A value in year 1750 indicates the value is unknown')),
                ('message_id', models.UUIDField(default=uuid.uuid4, editable=False, help_text='Unique message identifier')),
                ('from_email', models.CharField(help_text='From email address', max_length=300)),
                ('to', django.contrib.postgres.fields.ArrayField(base_field=models.CharField(max_length=254), blank=True, help_text='List of email recipients', size=None)),
                ('bcc', django.contrib.postgres.fields.ArrayField(base_field=models.CharField(max_length=254), blank=True, help_text='List of BCC emails addresses', size=None)),
                ('cc', django.contrib.postgres.fields.ArrayField(base_field=models.CharField(max_length=254), blank=True, help_text='List of CC emails addresses', size=None)),
                ('reply_to', django.contrib.postgres.fields.ArrayField(base_field=models.CharField(max_length=254), blank=True, help_text='List of Reply to emails addresses', size=None)),
                ('subject', models.TextField(blank=True, help_text='Subject')),
                ('plain_text', models.TextField(blank=True, help_text='Plain Text Message Body')),
                ('html_message', models.TextField(blank=True, help_text='HTML Message Body')),
                ('headers', models.JSONField(blank=True, help_text='Original email Headers', null=True)),
                ('user', models.ForeignKey(blank=True, help_text="The user that this message is related to in case of users change their email address we can send failed email to the user's new email address, this is optional in case we send email to anemail address that doesn't belong to a CL user.", null=True, on_delete=django.db.models.deletion.CASCADE, related_name='emails', to=settings.AUTH_USER_MODEL)),
            ],
        ),
        migrations.AddIndex(
            model_name='emailsent',
            index=models.Index(fields=['message_id'], name='users_email_message_f49e38_idx'),
        ),
    ]
