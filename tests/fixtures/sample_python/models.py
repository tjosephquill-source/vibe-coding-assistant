"""Models module — base classes, entities, and schemas."""


class BaseModel:
    """Abstract base class for all domain models."""

    def validate(self) -> bool:
        return True

    def to_dict(self) -> dict:
        return {}


class User(BaseModel):
    """A user in the system."""

    name: str
    email: str
    role: str = "member"

    def __init__(self, name: str, email: str):
        self.name = name
        self.email = email

    def get_display_name(self) -> str:
        return self.name

    def is_admin(self) -> bool:
        return self.role == "admin"


class Order(BaseModel):
    """An order placed by a user."""

    user: User
    items: list
    total: float = 0.0

    def __init__(self, user: User):
        self.user = user
        self.items = []
        self.total = 0.0

    def add_item(self, item: str, price: float) -> None:
        self.items.append(item)
        self.total += price

    def summary(self) -> str:
        return f"Order for {self.user.get_display_name()}: {len(self.items)} items, ${self.total}"


class OrderError(Exception):
    """Raised when an order operation fails."""
    pass

