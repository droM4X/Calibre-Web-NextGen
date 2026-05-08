# Calibre-Web Automated – fork of Calibre-Web
# Copyright (C) 2018-2026 Calibre-Web contributors
# Copyright (C) 2024-2026 Calibre-Web Automated contributors
# SPDX-License-Identifier: GPL-3.0-or-later
# See CONTRIBUTORS for full list of authors.

"""Regression tests for fork issue #95 (mirrors CWA #1269) — OAuth users
must be able to authenticate to OPDS / KOSync via per-user app passwords.

Pre-fix: `verify_password()` had three branches (LDAP bind, local password
hash check, LDAP auto-create). OAuth users have no usable local password,
so all three failed and `verify_password()` returned None on every Basic
auth attempt.

Post-fix: a new `UserAppPassword` table holds per-user labeled hashes;
`verify_password()` queries it before the existing branches. Match →
return user. No match → fall through to existing LDAP / local logic
unchanged (regression-pinned below).
"""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from werkzeug.security import generate_password_hash

from cps.ub import Base, User, UserAppPassword
from cps.usermanagement import _verify_app_password


@pytest.fixture
def in_memory_session():
    """Real SQLAlchemy session backed by SQLite in-memory — matches the
    repo's preferred pattern for query-correctness tests (see
    `tests/unit/test_kosync_book_id_keyed_lookup.py`)."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


@pytest.fixture
def oauth_user(in_memory_session):
    """An OAuth user with no local password — the actual symptom of #95."""
    u = User()
    u.name = "alice"
    u.email = "alice@example.com"
    u.password = ""  # OAuth users get '' per create_authenticated_user
    in_memory_session.add(u)
    in_memory_session.commit()
    in_memory_session.refresh(u)
    return u


@pytest.fixture
def issue_app_password(in_memory_session):
    """Helper to mint an app password for a user."""
    def _issue(user, label, cleartext, revoked=False):
        row = UserAppPassword(
            user_id=user.id,
            label=label,
            password_hash=generate_password_hash(cleartext),
            created_at=datetime.now(timezone.utc),
            revoked=revoked,
        )
        in_memory_session.add(row)
        in_memory_session.commit()
        in_memory_session.refresh(row)
        return row
    return _issue


@pytest.mark.unit
class TestVerifyAppPasswordHelper:
    """Pin the new `_verify_app_password` helper directly."""

    def test_match_returns_true_and_stamps_last_used(
            self, in_memory_session, oauth_user, issue_app_password):
        row = issue_app_password(oauth_user, "Kobo Forma", "correct horse battery staple")
        assert row.last_used_at is None

        with patch("cps.usermanagement.ub", MagicMock(
                session=in_memory_session, UserAppPassword=UserAppPassword)):
            ok = _verify_app_password(oauth_user, "correct horse battery staple")

        assert ok is True
        in_memory_session.refresh(row)
        assert row.last_used_at is not None, "last_used_at must be stamped on match"

    def test_no_match_returns_false_without_stamping(
            self, in_memory_session, oauth_user, issue_app_password):
        row = issue_app_password(oauth_user, "Kobo Forma", "correct horse battery staple")
        with patch("cps.usermanagement.ub", MagicMock(
                session=in_memory_session, UserAppPassword=UserAppPassword)):
            ok = _verify_app_password(oauth_user, "wrong-password")

        assert ok is False
        in_memory_session.refresh(row)
        assert row.last_used_at is None, "no-match must not stamp last_used_at"

    def test_revoked_password_is_ignored(
            self, in_memory_session, oauth_user, issue_app_password):
        issue_app_password(oauth_user, "Old Kobo", "old-secret", revoked=True)
        with patch("cps.usermanagement.ub", MagicMock(
                session=in_memory_session, UserAppPassword=UserAppPassword)):
            ok = _verify_app_password(oauth_user, "old-secret")
        assert ok is False, "revoked rows must not authenticate"

    def test_other_users_passwords_dont_match(
            self, in_memory_session, oauth_user, issue_app_password):
        # User #2 has a known password
        u2 = User()
        u2.name = "bob"
        u2.email = "bob@example.com"
        u2.password = ""
        in_memory_session.add(u2)
        in_memory_session.commit()
        in_memory_session.refresh(u2)

        issue_app_password(u2, "Bob's Kobo", "bobs-secret")

        with patch("cps.usermanagement.ub", MagicMock(
                session=in_memory_session, UserAppPassword=UserAppPassword)):
            # Alice tries Bob's password — must fail (filter scopes by user_id)
            ok = _verify_app_password(oauth_user, "bobs-secret")
        assert ok is False, "filter must scope by user_id; cross-user match is a privilege escalation"

    def test_multiple_app_passwords_any_match_authenticates(
            self, in_memory_session, oauth_user, issue_app_password):
        issue_app_password(oauth_user, "Kobo", "kobo-secret")
        issue_app_password(oauth_user, "iPad", "ipad-secret")
        issue_app_password(oauth_user, "KOReader Sage", "sage-secret")

        with patch("cps.usermanagement.ub", MagicMock(
                session=in_memory_session, UserAppPassword=UserAppPassword)):
            assert _verify_app_password(oauth_user, "ipad-secret") is True
            assert _verify_app_password(oauth_user, "sage-secret") is True
            assert _verify_app_password(oauth_user, "kobo-secret") is True

    def test_empty_password_returns_false_without_db_query(
            self, oauth_user):
        """Empty Basic-auth passwords are common (anonymous probes); short-
        circuit so we don't waste a DB query on every probe."""
        mock_session = MagicMock()
        mock_ub = MagicMock(session=mock_session, UserAppPassword=UserAppPassword)
        with patch("cps.usermanagement.ub", mock_ub):
            ok = _verify_app_password(oauth_user, "")
        assert ok is False
        mock_session.query.assert_not_called()

    def test_no_user_returns_false(self, in_memory_session):
        with patch("cps.usermanagement.ub", MagicMock(
                session=in_memory_session, UserAppPassword=UserAppPassword)):
            assert _verify_app_password(None, "anything") is False


@pytest.mark.unit
class TestVerifyPasswordIntegration:
    """End-to-end pins on the integration into `verify_password()` —
    proves OAuth users authenticate via their app password and that the
    fall-through to the existing LDAP / local branches is unchanged."""

    def test_oauth_user_authenticates_with_app_password(
            self, in_memory_session, oauth_user, issue_app_password):
        """The whole point of fork #95 — an OAuth user (empty local
        password) authenticates to /opds via a Basic-auth request that
        carries an app-password value."""
        from cps.usermanagement import verify_password

        issue_app_password(oauth_user, "iPhone OPDS", "device-token-123")

        # Mock the bits verify_password reaches for: ub.session lookup,
        # config flags, limiter, and request.remote_addr (only used in the
        # failure log line).
        mock_query = MagicMock()
        mock_query.filter.return_value.first.return_value = oauth_user
        mock_session = MagicMock()
        mock_session.query.return_value = mock_query

        # _verify_app_password uses ub.session directly with a different
        # query shape, so we keep the real session for that call.
        # Wire the ub mock to return oauth_user for the User lookup but
        # use the real session for the UserAppPassword query.
        real_session = in_memory_session

        def _query_side_effect(model):
            if model is User:
                return mock_query  # returns oauth_user via .filter().first()
            return real_session.query(model)
        mock_session.query.side_effect = _query_side_effect

        mock_ub = MagicMock(
            session=mock_session,
            User=User,
            UserAppPassword=UserAppPassword,
        )
        mock_config = MagicMock()
        mock_config.config_anonbrowse = 0
        mock_config.config_login_type = 0  # not LDAP
        mock_request = MagicMock(remote_addr="127.0.0.1")
        mock_limiter = MagicMock()
        mock_limiter.current_limits = []

        with patch("cps.usermanagement.ub", mock_ub), \
                patch("cps.usermanagement.config", mock_config), \
                patch("cps.usermanagement.request", mock_request), \
                patch("cps.usermanagement.limiter", mock_limiter):
            result = verify_password("alice", "device-token-123")

        assert result is oauth_user, (
            "OAuth user with a matching app password must authenticate — "
            "this is the user-visible fix for #95"
        )

    def test_oauth_user_with_wrong_app_password_still_rejected(
            self, in_memory_session, oauth_user, issue_app_password):
        from cps.usermanagement import verify_password

        issue_app_password(oauth_user, "iPhone", "right-token")

        mock_query = MagicMock()
        mock_query.filter.return_value.first.return_value = oauth_user
        mock_session = MagicMock()
        real_session = in_memory_session

        def _query_side_effect(model):
            if model is User:
                return mock_query
            return real_session.query(model)
        mock_session.query.side_effect = _query_side_effect

        mock_ub = MagicMock(
            session=mock_session, User=User, UserAppPassword=UserAppPassword)
        mock_config = MagicMock()
        mock_config.config_anonbrowse = 0
        mock_config.config_login_type = 0
        mock_request = MagicMock(remote_addr="127.0.0.1")
        mock_limiter = MagicMock()
        mock_limiter.current_limits = []
        # check_password_hash on '' returns False, which is correct for
        # the local-password fallback path.

        with patch("cps.usermanagement.ub", mock_ub), \
                patch("cps.usermanagement.config", mock_config), \
                patch("cps.usermanagement.request", mock_request), \
                patch("cps.usermanagement.limiter", mock_limiter):
            result = verify_password("alice", "wrong-token")

        assert result is None, (
            "wrong app-password value must NOT authenticate — would be a "
            "credential-bypass regression"
        )
