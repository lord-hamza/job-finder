from django.urls import path

from core import views

urlpatterns = [
    path("", views.home, name="home"),
    path("activity/", views.activity, name="activity"),
    path("resume/<int:pk>/delete/", views.delete_resume, name="delete_resume"),
    path("setup/", views.save_setup, name="save_setup"),
    path("gmail/", views.connect_gmail, name="connect_gmail"),
    path("gmail/remove/", views.disconnect_gmail, name="disconnect_gmail"),
    path("gemini/", views.save_gemini, name="save_gemini"),
    path("apply/", views.apply_jd, name="apply_jd"),
    path("crawl/", views.start_crawl, name="start_crawl"),
    path("applications/<int:pk>/", views.detail, name="detail"),
    path("applications/<int:pk>/send/", views.send, name="send"),
    path("applications/<int:pk>/retry/", views.retry, name="retry"),
    path("applications/<int:pk>/fill/", views.fill_form, name="fill_form"),
    path("applications/<int:pk>/applied/", views.mark_applied, name="mark_applied"),
]
