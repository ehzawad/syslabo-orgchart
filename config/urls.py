"""URL map.

Two routes, and no more. The chart is the product, so it sits at the root and
the printed sheet has a memorable address; everything that edits data goes
through ``django.contrib.admin``, which already has the permission model and
the login page ``LOGIN_URL`` points at.
"""

from django.contrib import admin
from django.contrib.staticfiles.urls import staticfiles_urlpatterns
from django.urls import path

from orgchart import views

urlpatterns = [
    path("", views.chart_view, name="chart"),
    path("admin/", admin.site.urls),
]

# Serving the stylesheets out of the app directory is a development-server
# convenience: this adds nothing when DEBUG is off, where ``collectstatic`` and
# a real static file server take over.
urlpatterns += staticfiles_urlpatterns()
