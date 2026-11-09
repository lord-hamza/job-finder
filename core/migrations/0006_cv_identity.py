import django.db.models.deletion
from django.db import migrations, models


def give_each_cv_its_identity(apps, schema_editor):
    """Each CV gets the email, links and country found in it. Its name is the one you'd saved, since every email so
    far was signed with it, or the one in the CV if you hadn't saved one. No application changes."""
    Profile = apps.get_model("core", "Profile")
    Resume = apps.get_model("core", "Resume")
    cvs = list(Resume.objects.order_by("created_at", "id"))
    if not cvs:
        return
    from core.resume import contact_details  # only needed when there are CVs to read

    profile = Profile.objects.order_by("pk").first()
    signed_as = " ".join((profile.full_name if profile else "").split())
    for cv in cvs:
        try:
            found = contact_details(cv.name, bytes(cv.data), cv.text)
        except Exception:
            found = {}
        cv.full_name = (signed_as or found.get("full_name", ""))[:120]
        cv.email = found.get("email", "")[:254]
        cv.links = found.get("links", "")
        cv.country = found.get("country", "")[:60]
        cv.save(update_fields=["full_name", "email", "links", "country"])


def back_to_one_name(apps, schema_editor):
    """Undoing this: your name is the first CV's again."""
    Profile = apps.get_model("core", "Profile")
    Resume = apps.get_model("core", "Resume")
    profile = Profile.objects.order_by("pk").first()
    first = Resume.objects.order_by("created_at", "id").first()
    if profile and first:
        profile.full_name = first.full_name
        profile.save(update_fields=["full_name"])


class Migration(migrations.Migration):
    dependencies = [("core", "0005_cv_details")]

    operations = [
        migrations.AddField(model_name="resume", name="full_name", field=models.CharField(blank=True, max_length=120)),
        migrations.AddField(model_name="resume", name="email", field=models.CharField(blank=True, max_length=254)),
        migrations.AddField(model_name="resume", name="links", field=models.TextField(blank=True)),
        migrations.AddField(model_name="resume", name="country", field=models.CharField(blank=True, max_length=60)),
        migrations.AddField(model_name="resume", name="send_from", field=models.CharField(blank=True, max_length=254)),
        migrations.AddField(model_name="profile", name="cv_per_country", field=models.BooleanField(default=True)),
        migrations.AddField(
            model_name="application",
            name="parent",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="extras",
                to="core.application",
            ),
        ),
        migrations.AddField(model_name="application", name="open_to", field=models.CharField(blank=True, max_length=300)),
        migrations.AddField(model_name="application", name="sender", field=models.CharField(blank=True, max_length=254)),
        migrations.RunPython(give_each_cv_its_identity, back_to_one_name),
        migrations.RemoveField(model_name="profile", name="full_name"),
    ]
