from django.contrib import admin
from .models import Contact, Newsletter, Settings

admin.site.register(Contact)
admin.site.register(Newsletter)
admin.site.register(Settings)
