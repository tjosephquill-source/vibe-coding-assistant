"""Test suite for the services module."""

from models import User
from services import UserService, OrderService


class TestUserService:
    """Tests for user-related operations."""

    def test_create_user(self):
        svc = UserService()
        user = svc.create_user("Alice", "alice@test.com")
        assert user.name == "Alice"

    def test_promote_user(self):
        svc = UserService()
        svc.create_user("Bob", "bob@test.com")
        result = svc.promote_to_admin("bob@test.com")
        assert result is True

