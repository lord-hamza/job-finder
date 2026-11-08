from django.urls import path

from core import views

urlpatterns = [
    path("", views.home, name="home"),
    path("activity/", views.activity, name="activity"),
    path("resume/", views.upload_resume, name="upload_resume"),
    path("resume/<int:pk>/delete/", views.delete_resume, name="delete_resume"),
    path("profile/", views.save_profile, name="save_profile"),
    path("facts/", views.save_facts, name="save_facts"),
    path("rules/", views.save_rules, name="save_rules"),
    path("gmail/", views.connect_gmail, name="connect_gmail"),
    path("gemini/", views.save_gemini, name="save_gemini"),
    path("apply/", views.apply_jd, name="apply_jd"),
    path("crawl/", views.start_crawl, name="start_crawl"),
    path("applications/<int:pk>/", views.detail, name="detail"),
    path("applications/<int:pk>/send/", views.send, name="send"),
    path("applications/<int:pk>/retry/", views.retry, name="retry"),
    path("applications/<int:pk>/fill/", views.fill_form, name="fill_form"),
    path("applications/<int:pk>/applied/", views.mark_applied, name="mark_applied"),
]
