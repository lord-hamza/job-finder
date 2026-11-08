import django.db.models.deletion
from django.db import migrations, models


def move_cvs(apps, schema_editor):
    """Each old CV becomes a Resume, and the first profile with a name becomes you."""
    Profile = apps.get_model("core", "Profile")
    Resume = apps.get_model("core", "Resume")
    Application = apps.get_model("core", "Application")
    profiles = list(Profile.objects.order_by("pk"))
    for profile in profiles:
        if profile.resume_text and profile.resume_bytes:
            cv = Resume.objects.create(name=profile.resume_name, data=profile.resume_bytes, text=profile.resume_text)
            Application.objects.filter(profile=profile).update(resume=cv)
    if profiles:
        keep = next((profile for profile in profiles if profile.full_name), profiles[0])
        keep.facts = keep.facts or next((profile.facts for profile in profiles if profile.facts), "")
        keep.save()
        Profile.objects.exclude(pk=keep.pk).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0003_two_cvs'),
    ]

    operations = [
        migrations.CreateModel(
            name='Resume',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=200)),
                ('data', models.BinaryField()),
                ('text', models.TextField()),
                ('created_at', models.DateTimeField(auto_now_add=True)),
            ],
            options={
                'ordering': ['created_at', 'id'],
            },
        ),
        migrations.AddField(
            model_name='application',
            name='resume',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='applications', to='core.resume'),
        ),
        migrations.AddField(
            model_name='profile',
            name='onsite_places',
            field=models.CharField(blank=True, default='Pakistan, Lahore, Islamabad, Karachi, Rawalpindi', max_length=300),
        ),
        migrations.AddField(
            model_name='profile',
            name='remote_countries',
            field=models.CharField(blank=True, default='United Kingdom, United States', max_length=300),
        ),
        migrations.RunPython(move_cvs, migrations.RunPython.noop),
        migrations.RemoveField(model_name='application', name='profile'),
        migrations.RemoveField(model_name='application', name='markets'),
        migrations.RemoveField(model_name='crawlrun', name='markets'),
        migrations.RemoveField(model_name='profile', name='market'),
        migrations.RemoveField(model_name='profile', name='selected'),
        migrations.RemoveField(model_name='profile', name='resume_name'),
        migrations.RemoveField(model_name='profile', name='resume_bytes'),
        migrations.RemoveField(model_name='profile', name='resume_text'),
    ]
