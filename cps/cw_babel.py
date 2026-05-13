# Calibre-Web Automated – fork of Calibre-Web
# Copyright (C) 2018-2026 Calibre-Web contributors
# Copyright (C) 2024-2026 Calibre-Web Automated contributors
# SPDX-License-Identifier: GPL-3.0-or-later
# See CONTRIBUTORS for full list of authors.

from babel import negotiate_locale
from flask_babel import Babel, Locale
from babel.core import UnknownLocaleError
from flask import request, has_request_context
from .cw_login import current_user

from . import logger

log = logger.create()

babel = Babel()


def _coerce_locale(raw, available):
    """Parse a raw locale string and return it if it's one we ship a
    translation for. Returns None on any failure — caller falls through."""
    if not raw:
        return None
    try:
        candidate = str(Locale.parse(raw.replace('-', '_')))
    except (UnknownLocaleError, ValueError) as e:
        log.debug('Could not parse locale "%s": %s', raw, e)
        return None
    if candidate in available:
        return candidate
    return None


def get_locale():
    # If no request context (e.g. background thread), fall back to English
    if not has_request_context():
        return 'en'

    available = get_available_translations()

    # Fork issue #160: per-request ?lang= override. droM4X's specific ask —
    # lets a user point any OPDS client at /opds?lang=hu and force Hungarian
    # even when the client (Readest, some Kobo readers) sends no
    # Accept-Language header. Validated against the locales we actually ship
    # so an unknown value falls through cleanly instead of returning a 500.
    lang_param = request.args.get('lang')
    coerced = _coerce_locale(lang_param, available)
    if coerced:
        return coerced

    # if a user is logged in, use the locale from the user settings
    if current_user is not None and hasattr(current_user, "locale"):
        # if the account is the guest account bypass the config lang settings
        if current_user.name != 'Guest':
            return current_user.locale

    preferred = list()
    if request.accept_languages:
        for x in request.accept_languages.values():
            # Skip wildcard '*' from Accept-Language headers (common in internal API requests)
            if x == '*':
                continue
            try:
                preferred.append(str(Locale.parse(x.replace('-', '_'))))
            except (UnknownLocaleError, ValueError) as e:
                log.debug('Could not parse locale "%s": %s', x, e)

    if preferred:
        negotiated = negotiate_locale(preferred, available)
        if negotiated:
            return negotiated

    # Fork issue #160 / #121 follow-up: anonymous OPDS clients commonly send
    # no Accept-Language at all (Readest, KOReader's built-in OPDS browser).
    # When that happens AND no per-request override is set, fall back to the
    # operator-configured OPDS default locale before the final 'en' fallback.
    # Scoped to /opds paths so we don't accidentally lock the web UI into a
    # non-English default for users who haven't configured anything.
    if request.path.startswith('/opds'):
        try:
            from . import config
            opds_default = getattr(config, 'config_opds_default_locale', '') or ''
        except Exception:
            opds_default = ''
        coerced = _coerce_locale(opds_default, available)
        if coerced:
            return coerced

    return negotiate_locale(preferred or ['en'], available)


def get_user_locale_language(user_language):
    return Locale.parse(user_language).get_language_name(get_locale())


def get_available_locale():
    # flask_babel.list_translations() already includes the default locale ('en')
    # whether or not a translation directory exists for it, so don't prepend
    # Locale('en') — that produced "English" twice in the language picker.
    # Sort by display name for a stable, alphabetic dropdown order.
    return sorted(babel.list_translations(), key=lambda x: x.display_name.lower())


def get_available_translations():
    return set(str(item) for item in get_available_locale())
