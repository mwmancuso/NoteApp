"""URL router for backend version 1."""

from django.conf.urls import patterns, include, url
from backend.v1_0_0 import validator

validator_list = patterns('',
    url(r'password', validator.PasswordValidatorView.as_view()),
)

urlpatterns = patterns('',
    url(r'validator/', include(validator_list))
)